from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, Optional, Tuple

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.cache_io import (
    atomic_write_json,
    atomic_write_text,
    decode_cache,
    encode_cache,
)
from community_intern.kb.cache_file_handler import CacheFileHandler
from community_intern.kb.cache_models import CacheRecord, CacheState, SchemaVersion
from community_intern.kb.cache_sources import discover_file_sources, discover_url_sources
from community_intern.kb.cache_url_handler import CacheUrlHandler
from community_intern.kb.cache_utils import format_rfc3339, hash_text, parse_rfc3339, utc_now
from community_intern.kb.web_fetcher import WebFetcher

logger = logging.getLogger(__name__)


def _compose_system_prompt(base_prompt: str, project_introduction: str) -> str:
    """Compose system prompt by appending project introduction if available."""
    parts = []
    if base_prompt.strip():
        parts.append(base_prompt.strip())
    if project_introduction.strip():
        parts.append(f"Project introduction:\n{project_introduction.strip()}")
    return "\n\n".join(parts).strip()



class KnowledgeBaseCacheManager:
    def __init__(self, config: KnowledgeBaseSettings, ai_client: AIClient, lock: asyncio.Lock):
        self._config = config
        self._ai_client = ai_client
        self._lock = lock
        self._runtime_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._persist_lock = asyncio.Lock()
        self._download_semaphore = asyncio.Semaphore(max(1, int(self._config.url_download_concurrency)))
        self._summary_semaphore = asyncio.Semaphore(max(1, int(self._config.summarization_concurrency)))
        self._file_handler = CacheFileHandler(
            persist_cache_and_index_async=self._persist_cache_and_index_async,
            hash_text=hash_text,
            format_rfc3339=format_rfc3339,
        )
        self._url_handler = CacheUrlHandler(
            config=self._config,
            download_semaphore=self._download_semaphore,
            persist_cache_and_index_async=self._persist_cache_and_index_async,
            hash_text=hash_text,
            format_rfc3339=format_rfc3339,
            parse_rfc3339=parse_rfc3339,
        )

    async def build_index_incremental(self) -> None:
        async with self._lock:
            await self._run_tick(full_scan=True)

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
                await self._runtime_tick()
            except Exception:
                logger.exception("Knowledge base runtime refresh tick failed.")
            elapsed = time.monotonic() - started
            sleep_seconds = max(0.0, self._config.runtime_refresh_tick_seconds - elapsed)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_seconds)
            except asyncio.TimeoutError:
                continue

    async def _runtime_tick(self) -> None:
        async with self._lock:
            await self._run_tick(full_scan=True)

    async def _run_tick(self, *, full_scan: bool) -> None:
        cache = self._load_cache()
        now = utc_now()
        if full_scan:
            await self._process_full_scan(cache, now)

    def _load_cache(self) -> CacheState:
        cache_path = Path(self._config.index_cache_path)
        if not cache_path.exists():
            return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = decode_cache(payload)
            if cache.schema_version != SchemaVersion:
                logger.warning(
                    "Knowledge base cache schema mismatch. expected=%s actual=%s",
                    SchemaVersion,
                    cache.schema_version,
                )
                return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})
            return cache
        except Exception:
            logger.exception("Failed to load knowledge base cache file. path=%s", cache_path)
            return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})

    def _write_cache(self, cache: CacheState) -> None:
        cache_path = Path(self._config.index_cache_path)
        atomic_write_json(cache_path, encode_cache(cache))

    def _write_index(self, entries: Iterable[str]) -> None:
        index_path = Path(self._config.index_path)
        entries_list = [entry for entry in entries if entry.strip()]
        content = "\n\n".join(entries_list)
        atomic_write_text(index_path, content)
        logger.info("Knowledge base index written. path=%s entries=%d", index_path, len(entries_list))

    def _persist_cache_and_index(self, cache: CacheState, now: datetime) -> None:
        cache.generated_at = format_rfc3339(now)
        self._write_cache(cache)
        index_entries = self._build_index_entries(cache)
        self._write_index(index_entries)

    async def _persist_cache_and_index_async(self, cache: CacheState, now: datetime) -> None:
        async with self._persist_lock:
            self._persist_cache_and_index(cache, now)

    async def _summarize_pending_sources(
        self,
        *,
        cache: CacheState,
        file_sources: Dict[str, Path],
        now: datetime,
    ) -> None:
        tasks = []
        fetcher = WebFetcher(self._config)
        for source_id, record in cache.sources.items():
            if not record.summary_pending:
                continue
            if record.source_type == "file" and record.file:
                file_path = file_sources.get(record.file.rel_path)
                if not file_path:
                    continue
                try:
                    text = file_path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
                    continue
                except OSError as e:
                    logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
                    continue
                content_hash = hash_text(text)
                tasks.append(
                    asyncio.create_task(
                        self._summarize_source(
                            cache=cache,
                            record=record,
                            source_id=source_id,
                            text=text,
                            content_hash=content_hash,
                            now=now,
                        )
                    )
                )
            elif record.source_type == "url" and record.url:
                cached_text = fetcher.get_cached_content(record.url.url)
                if not cached_text:
                    continue
                content_hash = hash_text(cached_text)
                tasks.append(
                    asyncio.create_task(
                        self._summarize_source(
                            cache=cache,
                            record=record,
                            source_id=source_id,
                            text=cached_text,
                            content_hash=content_hash,
                            now=now,
                        )
                    )
                )
        if tasks:
            await asyncio.gather(*tasks)

    async def _summarize_source(
        self,
        *,
        cache: CacheState,
        record: CacheRecord,
        source_id: str,
        text: str,
        content_hash: str,
        now: datetime,
    ) -> None:
        async with self._summary_semaphore:
            try:
                system_prompt = _compose_system_prompt(
                    self._config.summarization_prompt,
                    self._ai_client.project_introduction,
                )
                summary = await self._ai_client.invoke_llm(
                    system_prompt=system_prompt,
                    user_content=text,
                )
            except Exception:
                logger.exception("Failed to summarize knowledge base source. source_id=%s", source_id)
                return
        async with self._persist_lock:
            current = cache.sources.get(source_id)
            if current is not record or not current.summary_pending:
                return
            current.summary_text = summary
            current.content_hash = content_hash
            current.last_indexed_at = format_rfc3339(now)
            current.summary_pending = False
            self._persist_cache_and_index(cache, now)

    async def _process_full_scan(self, cache: CacheState, now: datetime) -> None:
        file_sources = discover_file_sources(self._config)
        url_sources = discover_url_sources(self._config)

        current_ids = set(file_sources.keys()) | set(url_sources.keys())
        for source_id in list(cache.sources.keys()):
            if source_id not in current_ids:
                cache.sources.pop(source_id, None)
                await self._persist_cache_and_index_async(cache, now)

        for rel_path, file_path in sorted(file_sources.items()):
            await self._file_handler.process_file_source(
                cache=cache,
                rel_path=rel_path,
                file_path=file_path,
                now=now,
            )

        async with WebFetcher(self._config) as fetcher:
            tasks = []
            for url in sorted(url_sources.keys()):
                record = cache.sources.get(url)
                if record is None:
                    tasks.append(
                        asyncio.create_task(
                            self._url_handler.create_url_source(
                                cache=cache,
                                url=url,
                                now=now,
                                fetcher=fetcher,
                            )
                        )
                    )
            if tasks:
                await asyncio.gather(*tasks)
        await self._url_handler.refresh_urls(cache=cache, now=now)
        await self._summarize_pending_sources(cache=cache, file_sources=file_sources, now=now)

    def _build_index_entries(self, cache: CacheState) -> Iterable[str]:
        file_entries = []
        url_entries = []
        for source_id, record in cache.sources.items():
            summary = record.summary_text.strip()
            if not summary:
                continue
            if record.source_type == "file":
                file_entries.append((source_id, summary))
            elif record.source_type == "url":
                url_entries.append((source_id, summary))

        entries = []
        for source_id, summary in sorted(file_entries, key=lambda item: item[0]):
            entries.append(f"{source_id}\n{summary}".strip())
        for source_id, summary in sorted(url_entries, key=lambda item: item[0]):
            entries.append(f"{source_id}\n{summary}".strip())
        return entries
