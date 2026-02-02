from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import List

from pydantic import BaseModel, Field

from community_intern.llm import LLMInvoker
from community_intern.llm.prompts import compose_system_prompt
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.knowledge_cache.io import atomic_write_text
from community_intern.knowledge_cache.indexer import KnowledgeIndexer
from community_intern.knowledge_cache.providers.file_folder import FileFolderProvider
from community_intern.team_kb.models import QAPair, Turn, TeamKBState
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
    topic_name: str | None = Field(
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


class TeamKnowledgeManager:
    def __init__(
        self,
        *,
        config: KnowledgeBaseSettings,
        llm_invoker: LLMInvoker,
    ) -> None:
        self._config = config
        self._llm_invoker = llm_invoker
        self._lock = asyncio.Lock()

        self._raw_archive = RawArchive(config.team_raw_dir)
        self._topic_storage = TopicStorage(config.team_topics_dir, config.team_index_path)
        self._topic_indexer = KnowledgeIndexer(
            cache_path=config.team_index_cache_path,
            index_path=config.team_index_path,
            index_prefix=TEAM_SOURCE_ID_PREFIX,
            summarization_prompt=config.team_summarization_prompt,
            summarization_concurrency=config.summarization_concurrency,
            llm_invoker=llm_invoker,
            providers=[FileFolderProvider(sources_dir=config.team_topics_dir)],
            source_type_order=["file"],
        )

        self._state_path = Path(config.team_state_path)

    @property
    def config(self) -> KnowledgeBaseSettings:
        return self._config

    @property
    def llm_invoker(self) -> LLMInvoker:
        return self._llm_invoker

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

        # Trigger pending item processing (will acquire lock internally)
        # This ensures that even if this item fails, it's queued for retry,
        # and if previous items were pending, they get processed in order.
        await self.process_pending_items()

        logger.info(
            "Q&A pair captured. qa_id=%s turn_count=%d conversation_id=%s",
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
            if turn.role == "user":
                prefix = "User:"
            elif turn.role == "bot":
                prefix = "Bot:"
            else:
                prefix = "Team:"
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

        system_prompt = compose_system_prompt(
            self._config.team_classification_prompt,
            self._llm_invoker.project_introduction,
        )
        result: ClassificationResult = await self._llm_invoker.invoke_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            response_model=ClassificationResult,
        )

        if result.skip:
            logger.info(
                "Skipped QA pair during classification (insufficient information). qa_id=%s",
                qa_pair.id,
            )
            return

        topic_name = result.topic_name
        if not topic_name:
            logger.warning(
                "Classification returned empty topic_name without skip=true. qa_id=%s. Treating as skip.",
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

        logger.info("Updated team knowledge index for new topic file. filename=%s", filename)

    async def _integrate_into_topic(self, qa_pair: QAPair, filename: str) -> None:
        existing_text = self._topic_storage.load_topic_as_text(filename).strip()
        new_block = format_topic_block(qa_pair).strip()

        user_content = f"Existing topic file content:\n{existing_text}\n\n---\n\nNew Q&A pair to add:\n{new_block}"

        system_prompt = compose_system_prompt(
            self._config.team_integration_prompt,
            self._llm_invoker.project_introduction,
        )
        result: IntegrationResult = await self._llm_invoker.invoke_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            response_model=IntegrationResult,
        )
        skip = result.skip
        remove_ids = result.remove_ids

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

            # Reset state for regeneration
            self._save_state(TeamKBState())

            for i, qa_pair in enumerate(all_pairs):
                try:
                    await self._classify_and_integrate(qa_pair)
                    # Update state during regeneration too
                    self._save_state(TeamKBState(
                        last_processed_qa_id=qa_pair.id
                    ))
                    logger.debug(
                        "Reprocessed QA pair %d/%d. qa_id=%s",
                        i + 1,
                        len(all_pairs),
                        qa_pair.id,
                    )
                except Exception:
                    logger.exception("Failed to reprocess QA pair. qa_id=%s", qa_pair.id)
                    # For regeneration, we might want to continue even if one fails
                    # But if we update state, we might skip failed ones if we run regenerate again?
                    # No, regenerate clears everything, so it starts fresh.

        logger.info("Team knowledge base regeneration completed. processed=%d", len(all_pairs))

    def _load_state(self) -> TeamKBState:
        state = TeamKBState()
        if self._state_path.exists():
            try:
                state = TeamKBState.model_validate_json(self._state_path.read_text(encoding="utf-8"))
            except Exception:
                raise RuntimeError("Failed to load team KB state. state.json is invalid.")

        # Apply manual raw processing cursor from config if present.
        # This value behaves like a persisted cursor:
        # - Items with qa_id <= cursor are ignored
        # - Items with qa_id > cursor are processed
        config_cursor_id = self._config.qa_raw_last_processed_id.strip()
        if config_cursor_id:
            self._raw_archive._parse_qa_id_datetime(config_cursor_id)
            if config_cursor_id > state.last_processed_qa_id:
                logger.info(
                    "Using configured qa_raw_last_processed_id as override. stored=%s config_id=%s",
                    state.last_processed_qa_id,
                    config_cursor_id,
                )
                state.last_processed_qa_id = config_cursor_id

        return state

    def _save_state(self, state: TeamKBState) -> None:
        try:
            atomic_write_text(self._state_path, state.model_dump_json(indent=2))
        except Exception:
            logger.error("Failed to save team KB state.", exc_info=True)

    async def process_pending_items(self) -> None:
        async with self._lock:
            # 1. Process pending raw archive items (Tier 1 -> Tier 2)
            state = self._load_state()
            pending = self._raw_archive.load_since(state.last_processed_qa_id)

            if pending:
                logger.info("Processing %d pending team QA pairs.", len(pending))
                for qa_pair in pending:
                    try:
                        await self._classify_and_integrate(qa_pair)
                        # Update state after success
                        state.last_processed_qa_id = qa_pair.id
                        self._save_state(state)
                    except Exception:
                        logger.exception(
                            "Failed to process pending QA pair. qa_id=%s. Stopping queue processing.",
                            qa_pair.id
                        )
                        break

            # 2. Process pending index summaries (Tier 2 -> Index)
            # This handles cases where index summarization failed previously,
            # ensuring self-healing for the team knowledge index.
            try:
                await self._topic_indexer.run_once()
            except Exception:
                logger.exception("Failed to run team topic indexer during pending items processing.")
