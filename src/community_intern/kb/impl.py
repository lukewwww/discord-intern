from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Optional, Sequence

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.interfaces import IndexEntry, SourceContent
from community_intern.kb.web_fetcher import WebFetcher
from community_intern.knowledge_cache.indexer import KnowledgeIndexer
from community_intern.knowledge_cache.providers.file_folder import FileFolderProvider
from community_intern.knowledge_cache.providers.url_links import UrlLinksProvider
from community_intern.team_kb.topic_storage import TopicStorage

logger = logging.getLogger(__name__)

KB_SOURCE_ID_PREFIX = "kb:"
TEAM_SOURCE_ID_PREFIX = "team:"

class FileSystemKnowledgeBase:
    def __init__(self, config: KnowledgeBaseSettings, ai_client: AIClient):
        self.config = config
        self.ai_client = ai_client
        self._topic_storage = TopicStorage(config.team_topics_dir, config.team_index_path)
        self._indexer = KnowledgeIndexer(
            cache_path=config.index_cache_path,
            index_path=config.index_path,
            index_prefix=KB_SOURCE_ID_PREFIX,
            summarization_prompt=config.summarization_prompt,
            summarization_concurrency=config.summarization_concurrency,
            ai_client=ai_client,
            providers=[
                FileFolderProvider(sources_dir=config.sources_dir),
                UrlLinksProvider(config=config),
            ],
            source_type_order=["file", "url"],
        )
        self._runtime_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

    def _extract_team_topic_filename(self, source_id: str) -> str:
        raw = source_id.strip()
        if not raw.startswith(TEAM_SOURCE_ID_PREFIX):
            raise ValueError(f"Not a team topic source id: {source_id}")
        filename = raw[len(TEAM_SOURCE_ID_PREFIX) :].strip()
        if not filename:
            raise ValueError(f"Empty team topic filename in source id: {source_id}")
        return filename

    def _normalize_file_source_id(self, *, source_id: str, sources_dir: Path) -> str:
        """
        Normalize file source IDs to be relative to sources_dir.

        The KB index stores file source IDs as paths relative to sources_dir.
        The LLM may sometimes return a path that includes the sources_dir prefix.
        """
        raw = source_id.strip()
        normalized = raw.replace("\\", "/").lstrip("/")
        sources_dir_norm = sources_dir.as_posix().rstrip("/")
        if sources_dir_norm and normalized.startswith(sources_dir_norm + "/"):
            normalized = normalized[len(sources_dir_norm) + 1 :]
        return normalized

    async def load_index_text(self) -> str:
        """Load the startup-produced index artifact as plain text.

        Combines main index and team index if both exist.
        """
        index_path = Path(self.config.index_path)
        team_index_path = Path(self.config.team_index_path)

        parts = []

        if index_path.exists():
            main_text = index_path.read_text(encoding="utf-8").strip()
            if main_text:
                parts.append(main_text)

        if team_index_path.exists():
            team_text = team_index_path.read_text(encoding="utf-8").strip()
            if team_text:
                parts.append(team_text)

        return "\n\n".join(parts)

    async def load_index_entries(self) -> Sequence[IndexEntry]:
        """Load the startup-produced index artifact as structured entries.

        Loads entries from both main index and team index.
        """
        entries = []

        for index_path_str in [self.config.index_path, self.config.team_index_path]:
            index_path = Path(index_path_str)
            if not index_path.exists():
                continue

            text = index_path.read_text(encoding="utf-8")
            if not text.strip():
                continue

            chunks = text.strip().split("\n\n")
            for chunk in chunks:
                lines = chunk.strip().split("\n")
                if not lines:
                    continue
                source_id = lines[0].strip()
                description = "\n".join(lines[1:]).strip()
                entries.append(IndexEntry(source_id=source_id, description=description))

        return entries

    async def build_index(self) -> None:
        """Build the startup index artifact on disk."""
        logger.info("Starting knowledge base index build.")
        await self._indexer.run_once()
        logger.info("Knowledge base index build completed.")

    def start_runtime_refresh(self) -> None:
        if self._runtime_task and not self._runtime_task.done():
            return
        self._stop_event.clear()
        self._runtime_task = asyncio.create_task(self._runtime_loop())

    async def stop_runtime_refresh(self) -> None:
        if not self._runtime_task:
            return
        self._stop_event.set()
        await self._runtime_task
        self._runtime_task = None

    async def _runtime_loop(self) -> None:
        while not self._stop_event.is_set():
            started = time.monotonic()
            try:
                await self._indexer.run_once()
            except Exception:
                logger.exception("Knowledge base runtime refresh tick failed.")
            elapsed = time.monotonic() - started
            sleep_seconds = max(0.0, self.config.runtime_refresh_tick_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                continue

    async def load_source_content(self, *, source_id: str) -> SourceContent:
        """Load full source content for a file path or URL identifier."""
        sources_dir = Path(self.config.sources_dir)
        team_topics_dir = Path(self.config.team_topics_dir)
        raw = source_id.strip()

        if raw.startswith(TEAM_SOURCE_ID_PREFIX):
            filename = self._extract_team_topic_filename(raw)
            team_topic_path = team_topics_dir / filename
            if not team_topic_path.exists() or not team_topic_path.is_file():
                raise FileNotFoundError(f"Team topic source not found: {source_id}")
            text = self._topic_storage.load_topic_as_text(filename)
            if not text.strip():
                raise ValueError(f"Team topic source is empty: {source_id}")
            return SourceContent(source_id=source_id, text=text)

        if not raw.startswith(KB_SOURCE_ID_PREFIX):
            raise ValueError(
                f"Invalid source_id (missing prefix). Expected '{KB_SOURCE_ID_PREFIX}' or '{TEAM_SOURCE_ID_PREFIX}'. source_id={source_id}"
            )

        raw = raw[len(KB_SOURCE_ID_PREFIX) :].strip()

        # Check if it's a URL
        if raw.startswith(("http://", "https://")):
            async with WebFetcher(self.config) as fetcher:
                text = await fetcher.fetch(raw)
                if not text.strip():
                    raise RuntimeError(f"Failed to load URL source content: {raw}")
                return SourceContent(source_id=source_id, text=text)

        try:
            raw_path = Path(raw)
            sources_dir_resolved = sources_dir.resolve()

            if raw_path.is_absolute():
                resolved = raw_path.resolve()
                try:
                    rel = resolved.relative_to(sources_dir_resolved)
                except ValueError:
                    logger.warning(
                        "KB file source is outside sources directory. source_id=%s path=%s sources_dir=%s",
                        source_id,
                        resolved,
                        sources_dir_resolved,
                    )
                    raise ValueError(f"File source is outside sources_dir: {source_id}")
                file_path = sources_dir_resolved / rel
            else:
                normalized_id = self._normalize_file_source_id(source_id=raw, sources_dir=sources_dir)
                file_path = sources_dir / Path(normalized_id)

            if not file_path.exists() or not file_path.is_file():
                raise FileNotFoundError(f"KB file source not found: {source_id}")

            text = file_path.read_text(encoding="utf-8")
            if not text.strip():
                raise ValueError(f"KB file source is empty: {source_id}")
            return SourceContent(source_id=source_id, text=text)
        except UnicodeDecodeError as e:
            logger.warning("Failed to decode KB file source. source_id=%s error=%s", source_id, e)
            raise
        except OSError as e:
            logger.warning("OS error while reading KB file source. source_id=%s error=%s", source_id, e)
            raise
