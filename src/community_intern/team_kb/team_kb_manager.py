from __future__ import annotations

import asyncio
import logging
from typing import List

from pydantic import BaseModel, Field

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.knowledge_cache.indexer import KnowledgeIndexer
from community_intern.knowledge_cache.providers.file_folder import FileFolderProvider
from community_intern.team_kb.models import QAPair, Turn
from community_intern.team_kb.raw_archive import RawArchive
from community_intern.team_kb.topic_storage import TopicStorage, format_topic_block

logger = logging.getLogger(__name__)

TEAM_SOURCE_ID_PREFIX = "team:"


# --- Pydantic Models for Structured LLM Output ---


class ClassificationResult(BaseModel):
    skip: bool = Field(
        default=False,
        description="If true, the Q&A pair lacks sufficient information and should not be added to any topic",
    )
    topic_name: str = Field(
        default="",
        description="Topic identifier (e.g., 'node-startup-issues'). Empty when skip is true.",
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
        self._topic_indexer = KnowledgeIndexer(
            cache_path=config.team_index_cache_path,
            index_path=config.team_index_path,
            index_prefix=TEAM_SOURCE_ID_PREFIX,
            summarization_prompt=config.team_summarization_prompt,
            summarization_concurrency=config.summarization_concurrency,
            ai_client=ai_client,
            providers=[FileFolderProvider(sources_dir=config.team_topics_dir)],
            source_type_order=["file"],
        )

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

    def _strip_team_prefix_from_index_text(self, index_text: str) -> str:
        """
        Team index on disk uses namespaced identifiers (team:<topic_filename>).

        For topic classification prompts, we present identifiers without the prefix to keep
        topic naming stable (topic_name remains "<slug>" or "<slug>.txt").
        """
        text = index_text.strip()
        if not text:
            return ""

        chunks = text.split("\n\n")
        rewritten = []
        for chunk in chunks:
            lines = chunk.split("\n")
            if not lines:
                continue
            first = lines[0].strip()
            if first.startswith(TEAM_SOURCE_ID_PREFIX):
                lines[0] = first[len(TEAM_SOURCE_ID_PREFIX) :].strip()
            rewritten.append("\n".join(lines).strip())

        return "\n\n".join([c for c in rewritten if c.strip()]).strip()

    async def _classify_and_integrate(self, qa_pair: QAPair) -> None:
        index_text = self._strip_team_prefix_from_index_text(self._topic_storage.load_index_text())

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
        except Exception:
            logger.exception(
                "LLM classification failed, skipping integration. qa_id=%s", qa_pair.id
            )
            return

        if result.skip:
            logger.info(
                "Skipped QA pair during classification (insufficient information). qa_id=%s",
                qa_pair.id,
            )
            return

        topic_name = result.topic_name
        if not topic_name:
            logger.warning(
                "Classification returned empty topic_name without skip=true. qa_id=%s",
                qa_pair.id,
            )
            return

        logger.debug(
            "Topic classification result. qa_id=%s topic_name=%s",
            qa_pair.id,
            topic_name,
        )

        raw = topic_name.strip()
        if raw.endswith(".json"):
            raw = raw[: -len(".json")]
        filename = raw if raw.endswith(".txt") else f"{raw}.txt"
        topic_exists = self._topic_storage.topic_exists(filename)

        if topic_exists:
            await self._integrate_into_topic(qa_pair, filename)
        else:
            await self._create_new_topic(qa_pair, filename)

    async def _create_new_topic(self, qa_pair: QAPair, filename: str) -> None:
        self._topic_storage.create_topic(filename, qa_pair)
        await self._topic_indexer.notify_changed(filename)

        logger.info("Created new topic. filename=%s", filename)

    async def _integrate_into_topic(self, qa_pair: QAPair, filename: str) -> None:
        existing_text = self._topic_storage.load_topic_as_text(filename).strip()
        new_block = format_topic_block(qa_pair).strip()

        user_content = f"Existing topic file content:\n{existing_text}\n\n---\n\nNew Q&A pair to add:\n{new_block}"

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

        await self._topic_indexer.notify_changed(filename)

        logger.info(
            "Integrated QA into topic. filename=%s qa_id=%s removed_count=%d",
            filename,
            qa_pair.id,
            len(remove_ids),
        )

    async def regenerate(self) -> None:
        logger.info("Starting team knowledge base regeneration.")

        async with self._lock:
            self._topic_storage.clear_all()
            await self._topic_indexer.run_once()

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
