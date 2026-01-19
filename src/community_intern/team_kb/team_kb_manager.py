from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.cache_io import atomic_write_json
from community_intern.kb.cache_models import CacheRecord, CacheState, SchemaVersion
from community_intern.kb.cache_utils import format_rfc3339, utc_now
from community_intern.team_kb.models import QAPair, Turn
from community_intern.team_kb.raw_archive import RawArchive
from community_intern.team_kb.topic_storage import TopicStorage, qa_pair_to_dict

logger = logging.getLogger(__name__)


# --- Pydantic Models for Structured LLM Output ---


class ClassificationResult(BaseModel):
    topic_name: str = Field(
        description="Topic identifier (e.g., 'node-startup-issues')"
    )


class IntegrationResult(BaseModel):
    skip: bool = Field(
        default=False,
        description="If true, the new Q&A pair is not added (information already covered)",
    )
    remove_ids: List[str] = Field(
        default_factory=list,
        description="IDs of obsolete Q&A pairs to remove",
    )


def _compose_system_prompt(base_prompt: str, project_introduction: str) -> str:
    """Compose system prompt by appending project introduction if available."""
    parts = []
    if base_prompt.strip():
        parts.append(base_prompt.strip())
    if project_introduction.strip():
        parts.append(f"Project introduction:\n{project_introduction.strip()}")
    return "\n\n".join(parts).strip()


class TeamKnowledgeManager:
    def __init__(
        self,
        *,
        config: KnowledgeBaseSettings,
        ai_client: AIClient,
    ) -> None:
        self._config = config
        self._ai_client = ai_client
        self._lock = asyncio.Lock()

        self._raw_archive = RawArchive(config.team_raw_dir)
        self._topic_storage = TopicStorage(config.team_topics_dir, config.team_index_path)
        self._cache_path = Path(config.team_index_cache_path)

    async def capture_qa(
        self,
        *,
        turns: list[Turn],
        timestamp: str,
        conversation_id: str = "",
        message_ids: list[str] | None = None,
    ) -> None:
        qa_id = self._generate_qa_id(timestamp)
        qa_pair = QAPair(
            id=qa_id,
            timestamp=timestamp,
            turns=turns,
            conversation_id=conversation_id,
            message_ids=message_ids or [],
        )

        async with self._lock:
            await self._raw_archive.append(qa_pair)

            await self._classify_and_integrate(qa_pair)

            logger.info(
                "Q&A pair captured and indexed. qa_id=%s turn_count=%d conversation_id=%s",
                qa_id,
                len(turns),
                conversation_id,
            )

    def _generate_qa_id(self, timestamp: str) -> str:
        clean = timestamp.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
        return f"qa_{clean}"

    def _format_qa_pair_for_llm(self, qa_pair: QAPair) -> str:
        lines = []
        for turn in qa_pair.turns:
            prefix = "User:" if turn.role == "user" else "Team:"
            lines.append(f"{prefix} {turn.content}")
        return "\n".join(lines)

    async def _classify_and_integrate(self, qa_pair: QAPair) -> None:
        index_text = self._topic_storage.load_index_text()

        qa_text = self._format_qa_pair_for_llm(qa_pair)
        user_content = f"Current index:\n{index_text}\n\n---\n\nNew Q&A pair:\n{qa_text}"

        try:
            system_prompt = _compose_system_prompt(
                self._config.team_classification_prompt,
                self._ai_client.project_introduction,
            )
            result: ClassificationResult = await self._ai_client.invoke_llm(
                system_prompt=system_prompt,
                user_content=user_content,
                response_model=ClassificationResult,
            )
            topic_name = result.topic_name
        except Exception:
            logger.exception("LLM classification failed. qa_id=%s", qa_pair.id)
            topic_name = "general-qa"

        logger.debug(
            "Topic classification result. qa_id=%s topic_name=%s",
            qa_pair.id,
            topic_name,
        )

        filename = topic_name if topic_name.endswith(".json") else f"{topic_name}.json"
        topic_exists = self._topic_storage.topic_exists(filename)

        if topic_exists:
            await self._integrate_into_topic(qa_pair, filename)
        else:
            await self._create_new_topic(qa_pair, filename)

    async def _create_new_topic(self, qa_pair: QAPair, filename: str) -> None:
        self._topic_storage.create_topic(filename, qa_pair)
        await self._update_index_for_topic(filename)

        logger.info("Created new topic. filename=%s", filename)

    async def _integrate_into_topic(self, qa_pair: QAPair, filename: str) -> None:
        existing_pairs = self._topic_storage.load_topic(filename)
        existing_content = [qa_pair_to_dict(qa) for qa in existing_pairs]
        new_qa_dict = qa_pair_to_dict(qa_pair)

        user_content = (
            f"Existing topic file content:\n{json.dumps(existing_content, indent=2)}\n\n"
            f"---\n\nNew Q&A pair to add:\n{json.dumps(new_qa_dict, indent=2)}"
        )

        try:
            system_prompt = _compose_system_prompt(
                self._config.team_integration_prompt,
                self._ai_client.project_introduction,
            )
            result: IntegrationResult = await self._ai_client.invoke_llm(
                system_prompt=system_prompt,
                user_content=user_content,
                response_model=IntegrationResult,
            )
            skip = result.skip
            remove_ids = result.remove_ids
        except Exception:
            logger.exception("LLM integration failed. qa_id=%s topic=%s", qa_pair.id, filename)
            skip = False
            remove_ids = []

        logger.debug(
            "Topic integration result. qa_id=%s topic=%s skip=%s remove_ids=%s",
            qa_pair.id,
            filename,
            skip,
            remove_ids,
        )

        if skip:
            logger.info(
                "Skipped QA pair (redundant). filename=%s qa_id=%s",
                filename,
                qa_pair.id,
            )
            return

        self._topic_storage.add_to_topic(filename, qa_pair, remove_ids)

        await self._update_index_for_topic(filename)

        logger.info(
            "Integrated QA into topic. filename=%s qa_id=%s removed_count=%d",
            filename,
            qa_pair.id,
            len(remove_ids),
        )

    async def _update_index_for_topic(self, filename: str) -> None:
        cache = self._load_cache()
        cached_record = cache.sources.get(filename)

        current_hash = self._topic_storage.get_topic_hash(filename) or ""

        if cached_record and cached_record.content_hash == current_hash:
            return

        if cached_record:
            description = cached_record.summary_text
        else:
            pairs = self._topic_storage.load_topic(filename)
            topics = set()
            for qa in pairs:
                for turn in qa.turns:
                    if turn.role == "user":
                        topics.add(turn.content[:50])
            description = f"Q&A about: {', '.join(list(topics)[:3])}"

            try:
                topic_text = self._format_topic_for_summarization(pairs)
                system_prompt = _compose_system_prompt(
                    self._config.team_summarization_prompt,
                    self._ai_client.project_introduction,
                )
                description = await self._ai_client.invoke_llm(
                    system_prompt=system_prompt,
                    user_content=topic_text,
                )
            except Exception:
                logger.warning("Failed to summarize topic for index. filename=%s", filename)

        cache.sources[filename] = CacheRecord(
            source_type="team_topic",
            content_hash=current_hash,
            summary_text=description,
            last_indexed_at=format_rfc3339(utc_now()),
            summary_pending=False,
        )

        self._save_cache(cache)
        self._rebuild_index_from_cache(cache)

    def _format_topic_for_summarization(self, pairs: list[QAPair]) -> str:
        lines = []
        for qa in pairs:
            for turn in qa.turns:
                prefix = "Q:" if turn.role == "user" else "A:"
                lines.append(f"{prefix} {turn.content}")
            lines.append("")
        return "\n".join(lines)

    def _rebuild_index_from_cache(self, cache: CacheState) -> None:
        entries: list[tuple[str, str]] = []
        for source_id, record in sorted(cache.sources.items()):
            if record.summary_text.strip():
                entries.append((source_id, record.summary_text))
        self._topic_storage.save_index(entries)

    def _load_cache(self) -> CacheState:
        if not self._cache_path.exists():
            return CacheState(
                schema_version=SchemaVersion,
                generated_at=format_rfc3339(utc_now()),
                sources={},
            )

        try:
            content = self._cache_path.read_text(encoding="utf-8")
            data = json.loads(content)
            if data.get("schema_version") != SchemaVersion:
                logger.warning("Cache schema version mismatch, starting fresh.")
                return CacheState(
                    schema_version=SchemaVersion,
                    generated_at=format_rfc3339(utc_now()),
                    sources={},
                )

            sources = {}
            for source_id, record_data in data.get("sources", {}).items():
                sources[source_id] = CacheRecord(
                    source_type=record_data.get("source_type", "team_topic"),
                    content_hash=record_data.get("content_hash", ""),
                    summary_text=record_data.get("summary_text", ""),
                    last_indexed_at=record_data.get("last_indexed_at", ""),
                    summary_pending=record_data.get("summary_pending", False),
                )

            return CacheState(
                schema_version=data.get("schema_version", SchemaVersion),
                generated_at=data.get("generated_at", format_rfc3339(utc_now())),
                sources=sources,
            )
        except (OSError, json.JSONDecodeError):
            logger.exception("Failed to load team KB cache.")
            return CacheState(
                schema_version=SchemaVersion,
                generated_at=format_rfc3339(utc_now()),
                sources={},
            )

    def _save_cache(self, cache: CacheState) -> None:
        cache.generated_at = format_rfc3339(utc_now())

        sources_data = {}
        for source_id, record in cache.sources.items():
            sources_data[source_id] = {
                "source_type": record.source_type,
                "content_hash": record.content_hash,
                "summary_text": record.summary_text,
                "last_indexed_at": record.last_indexed_at,
                "summary_pending": record.summary_pending,
            }

        data = {
            "schema_version": cache.schema_version,
            "generated_at": cache.generated_at,
            "sources": sources_data,
        }

        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._cache_path, data)

    async def regenerate(self) -> None:
        logger.info("Starting team knowledge base regeneration.")

        async with self._lock:
            self._topic_storage.clear_all()

            empty_cache = CacheState(
                schema_version=SchemaVersion,
                generated_at=format_rfc3339(utc_now()),
                sources={},
            )
            self._save_cache(empty_cache)

            all_pairs = self._raw_archive.load_all()
            logger.info("Loaded %d QA pairs from raw archive.", len(all_pairs))

            for i, qa_pair in enumerate(all_pairs):
                try:
                    await self._classify_and_integrate(qa_pair)
                    logger.debug(
                        "Reprocessed QA pair %d/%d. qa_id=%s",
                        i + 1,
                        len(all_pairs),
                        qa_pair.id,
                    )
                except Exception:
                    logger.exception("Failed to reprocess QA pair. qa_id=%s", qa_pair.id)

        logger.info("Team knowledge base regeneration completed. processed=%d", len(all_pairs))
