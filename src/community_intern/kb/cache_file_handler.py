from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from community_intern.kb.cache_models import CacheRecord, CacheState, FileMetadata

logger = logging.getLogger(__name__)


class CacheFileHandler:
    def __init__(
        self,
        *,
        persist_cache_and_index_async: Callable[[CacheState, datetime], Awaitable[None]],
        hash_text: Callable[[str], str],
        format_rfc3339: Callable[[datetime], str],
    ) -> None:
        self._persist_cache_and_index_async = persist_cache_and_index_async
        self._hash_text = hash_text
        self._format_rfc3339 = format_rfc3339

    async def process_file_source(
        self,
        *,
        cache: CacheState,
        rel_path: str,
        file_path: Path,
        now: datetime,
    ) -> None:
        try:
            stat = file_path.stat()
        except OSError as e:
            logger.warning("Failed to stat knowledge base file. path=%s error=%s", file_path, e)
            return

        record = cache.sources.get(rel_path)
        if record is None:
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
                return
            except OSError as e:
                logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
                return

            content_hash = self._hash_text(text)
            cache.sources[rel_path] = CacheRecord(
                source_type="file",
                content_hash=content_hash,
                summary_text="",
                last_indexed_at=self._format_rfc3339(now),
                summary_pending=True,
                file=FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
            )
            await self._persist_cache_and_index_async(cache, now)
            return

        if record.source_type != "file":
            logger.warning("Cache record type mismatch for file source. source_id=%s", rel_path)
            cache.sources.pop(rel_path, None)
            await self._persist_cache_and_index_async(cache, now)
            return

        file_meta = record.file
        if not file_meta:
            file_meta = FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if file_meta.size_bytes == stat.st_size and file_meta.mtime_ns == stat.st_mtime_ns:
            return

        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
            return
        except OSError as e:
            logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
            return

        content_hash = self._hash_text(text)
        record.file = FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if content_hash != record.content_hash or record.summary_pending:
            record.content_hash = content_hash
            record.summary_pending = True
            await self._persist_cache_and_index_async(cache, now)
            return
        await self._persist_cache_and_index_async(cache, now)
