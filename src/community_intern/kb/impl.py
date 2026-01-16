from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Sequence

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.interfaces import IndexEntry, SourceContent
from community_intern.kb.cache_manager import KnowledgeBaseCacheManager
from community_intern.kb.web_fetcher import WebFetcher

logger = logging.getLogger(__name__)


class FileSystemKnowledgeBase:
    def __init__(self, config: KnowledgeBaseSettings, ai_client: AIClient):
        self.config = config
        self.ai_client = ai_client
        self._index_lock = asyncio.Lock()
        self._cache_manager = KnowledgeBaseCacheManager(config=config, ai_client=ai_client, lock=self._index_lock)

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
        """Load the startup-produced index artifact as plain text."""
        index_path = Path(self.config.index_path)
        if not index_path.exists():
            return ""
        return index_path.read_text(encoding="utf-8")

    async def load_index_entries(self) -> Sequence[IndexEntry]:
        """Load the startup-produced index artifact as structured entries."""
        text = await self.load_index_text()
        entries = []
        if not text:
            return entries

        # Split by double newlines to separate entries
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
        await self._cache_manager.build_index_incremental()
        logger.info("Knowledge base index build completed.")

    def start_runtime_refresh(self) -> None:
        self._cache_manager.start_runtime_refresh()

    async def stop_runtime_refresh(self) -> None:
        await self._cache_manager.stop_runtime_refresh()

    async def load_source_content(self, *, source_id: str) -> SourceContent:
        """Load full source content for a file path or URL identifier."""
        sources_dir = Path(self.config.sources_dir)

        # Check if it's a URL
        if source_id.startswith(("http://", "https://")):
             # Reuse WebFetcher logic (it handles caching)
             # Note: For single fetch, this will start/stop browser if not cached, which is heavy but safe.
             async with WebFetcher(self.config) as fetcher:
                 text = await fetcher.fetch(source_id)
                 if not text.strip():
                     raise RuntimeError(f"Failed to load URL source content: {source_id}")
                 return SourceContent(source_id=source_id, text=text)

        try:
            raw_path = Path(source_id.strip())
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
                normalized_id = self._normalize_file_source_id(source_id=source_id, sources_dir=sources_dir)
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
