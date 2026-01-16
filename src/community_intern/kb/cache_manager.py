from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Literal, Optional, Tuple

import aiohttp

from community_intern.ai.interfaces import AIClient
from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.web_fetcher import WebFetcher

logger = logging.getLogger(__name__)

SchemaVersion = 1
FetchStatus = Literal["success", "not_modified", "timeout", "error"]


@dataclass(slots=True)
class FileMetadata:
    rel_path: str
    size_bytes: int
    mtime_ns: int


@dataclass(slots=True)
class UrlMetadata:
    url: str
    last_fetched_at: str
    etag: Optional[str]
    last_modified: Optional[str]
    fetch_status: FetchStatus
    next_check_at: str


@dataclass(slots=True)
class CacheRecord:
    source_type: Literal["file", "url"]
    content_hash: str
    summary_text: str
    last_indexed_at: str
    summary_pending: bool = False
    file: Optional[FileMetadata] = None
    url: Optional[UrlMetadata] = None


@dataclass(slots=True)
class CacheState:
    schema_version: int
    generated_at: str
    sources: Dict[str, CacheRecord]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_rfc3339(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_rfc3339(value: str) -> datetime:
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def _normalize_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in normalized.split("\n")]
    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _hash_text(text: str) -> str:
    normalized = _normalize_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    tmp_path.replace(path)


def _encode_record(record: CacheRecord) -> dict:
    payload = {
        "source_type": record.source_type,
        "content_hash": record.content_hash,
        "summary_text": record.summary_text,
        "last_indexed_at": record.last_indexed_at,
        "summary_pending": record.summary_pending,
    }
    if record.file:
        payload["file"] = {
            "rel_path": record.file.rel_path,
            "size_bytes": record.file.size_bytes,
            "mtime_ns": record.file.mtime_ns,
        }
    if record.url:
        payload["url"] = {
            "url": record.url.url,
            "last_fetched_at": record.url.last_fetched_at,
            "etag": record.url.etag,
            "last_modified": record.url.last_modified,
            "fetch_status": record.url.fetch_status,
            "next_check_at": record.url.next_check_at,
        }
    return payload


def _decode_record(payload: dict) -> CacheRecord:
    file_meta = payload.get("file")
    url_meta = payload.get("url")
    file_value = None
    url_value = None
    if file_meta:
        file_value = FileMetadata(
            rel_path=file_meta["rel_path"],
            size_bytes=int(file_meta["size_bytes"]),
            mtime_ns=int(file_meta["mtime_ns"]),
        )
    if url_meta:
        url_value = UrlMetadata(
            url=url_meta["url"],
            last_fetched_at=url_meta["last_fetched_at"],
            etag=url_meta.get("etag"),
            last_modified=url_meta.get("last_modified"),
            fetch_status=url_meta["fetch_status"],
            next_check_at=url_meta["next_check_at"],
        )
    return CacheRecord(
        source_type=payload["source_type"],
        content_hash=payload["content_hash"],
        summary_text=payload["summary_text"],
        last_indexed_at=payload["last_indexed_at"],
        summary_pending=bool(payload.get("summary_pending", False)),
        file=file_value,
        url=url_value,
    )


def _encode_cache(cache: CacheState) -> dict:
    return {
        "schema_version": cache.schema_version,
        "generated_at": cache.generated_at,
        "sources": {source_id: _encode_record(record) for source_id, record in cache.sources.items()},
    }


def _decode_cache(payload: dict) -> CacheState:
    sources_payload = payload.get("sources", {})
    sources: Dict[str, CacheRecord] = {}
    for source_id, record_payload in sources_payload.items():
        sources[source_id] = _decode_record(record_payload)
    return CacheState(
        schema_version=int(payload.get("schema_version", SchemaVersion)),
        generated_at=payload.get("generated_at", _format_rfc3339(_utc_now())),
        sources=sources,
    )


class KnowledgeBaseCacheManager:
    def __init__(self, config: KnowledgeBaseSettings, ai_client: AIClient, lock: asyncio.Lock):
        self._config = config
        self._ai_client = ai_client
        self._lock = lock
        self._runtime_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

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
        now = _utc_now()
        if full_scan:
            await self._process_full_scan(cache, now)

    def _load_cache(self) -> CacheState:
        cache_path = Path(self._config.index_cache_path)
        if not cache_path.exists():
            return CacheState(schema_version=SchemaVersion, generated_at=_format_rfc3339(_utc_now()), sources={})
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            cache = _decode_cache(payload)
            if cache.schema_version != SchemaVersion:
                logger.warning(
                    "Knowledge base cache schema mismatch. expected=%s actual=%s",
                    SchemaVersion,
                    cache.schema_version,
                )
                return CacheState(schema_version=SchemaVersion, generated_at=_format_rfc3339(_utc_now()), sources={})
            return cache
        except Exception:
            logger.exception("Failed to load knowledge base cache file. path=%s", cache_path)
            return CacheState(schema_version=SchemaVersion, generated_at=_format_rfc3339(_utc_now()), sources={})

    def _write_cache(self, cache: CacheState) -> None:
        cache_path = Path(self._config.index_cache_path)
        _atomic_write_json(cache_path, _encode_cache(cache))

    def _write_index(self, entries: Iterable[str]) -> None:
        index_path = Path(self._config.index_path)
        entries_list = [entry for entry in entries if entry.strip()]
        content = "\n\n".join(entries_list)
        _atomic_write_text(index_path, content)
        logger.info("Knowledge base index written. path=%s entries=%d", index_path, len(entries_list))

    def _persist_cache_and_index(self, cache: CacheState, now: datetime) -> None:
        cache.generated_at = _format_rfc3339(now)
        self._write_cache(cache)
        index_entries = self._build_index_entries(cache)
        self._write_index(index_entries)

    def _discover_file_sources(self) -> Dict[str, Path]:
        sources_dir = Path(self._config.sources_dir)
        if not sources_dir.exists():
            logger.warning("Knowledge base sources directory is missing. path=%s", sources_dir)
            return {}
        file_sources: Dict[str, Path] = {}
        for file_path in sources_dir.rglob("*"):
            if file_path.is_file() and not file_path.name.startswith("."):
                try:
                    rel_path = file_path.relative_to(sources_dir).as_posix()
                    file_sources[rel_path] = file_path
                except ValueError:
                    continue
        return file_sources

    def _discover_url_sources(self) -> Dict[str, str]:
        links_file = Path(self._config.links_file_path)
        url_sources: Dict[str, str] = {}
        if not links_file.exists():
            return url_sources
        try:
            content = links_file.read_text(encoding="utf-8")
            for line in content.splitlines():
                url = line.strip()
                if url and not url.startswith("#"):
                    url_sources[url] = url
            return url_sources
        except Exception as e:
            logger.warning("Failed to read knowledge base links file. path=%s error=%s", links_file, e)
            return url_sources

    async def _process_full_scan(self, cache: CacheState, now: datetime) -> None:
        file_sources = self._discover_file_sources()
        url_sources = self._discover_url_sources()

        current_ids = set(file_sources.keys()) | set(url_sources.keys())
        for source_id in list(cache.sources.keys()):
            if source_id not in current_ids:
                cache.sources.pop(source_id, None)
                self._persist_cache_and_index(cache, now)

        for rel_path, file_path in sorted(file_sources.items()):
            changed = await self._process_file_source(
                cache=cache,
                rel_path=rel_path,
                file_path=file_path,
                now=now,
            )
            if changed:
                self._persist_cache_and_index(cache, now)

        async with WebFetcher(self._config) as fetcher:
            for url in sorted(url_sources.keys()):
                record = cache.sources.get(url)
                if record is None:
                    changed = await self._create_url_source(
                        cache=cache,
                        url=url,
                        now=now,
                        fetcher=fetcher,
                    )
                    if changed:
                        self._persist_cache_and_index(cache, now)
        await self._refresh_urls(cache=cache, now=now)

    async def _process_file_source(
        self,
        cache: CacheState,
        rel_path: str,
        file_path: Path,
        now: datetime,
    ) -> bool:
        try:
            stat = file_path.stat()
        except OSError as e:
            logger.warning("Failed to stat knowledge base file. path=%s error=%s", file_path, e)
            return False

        record = cache.sources.get(rel_path)
        if record is None:
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
                return False
            except OSError as e:
                logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
                return False

            content_hash = _hash_text(text)
            try:
                summary = await self._ai_client.summarize_for_kb_index(source_id=rel_path, text=text)
            except Exception:
                logger.exception("Failed to summarize knowledge base file source. path=%s", file_path)
                cache.sources[rel_path] = CacheRecord(
                    source_type="file",
                    content_hash=content_hash,
                    summary_text="",
                    last_indexed_at=_format_rfc3339(now),
                    summary_pending=True,
                    file=FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
                )
                return True
            cache.sources[rel_path] = CacheRecord(
                source_type="file",
                content_hash=content_hash,
                summary_text=summary,
                last_indexed_at=_format_rfc3339(now),
                summary_pending=False,
                file=FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns),
            )
            return True

        if record.source_type != "file":
            logger.warning("Cache record type mismatch for file source. source_id=%s", rel_path)
            cache.sources.pop(rel_path, None)
            return True

        file_meta = record.file
        if not file_meta:
            file_meta = FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if file_meta.size_bytes == stat.st_size and file_meta.mtime_ns == stat.st_mtime_ns:
            if not record.summary_pending:
                return False
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
                return False
            except OSError as e:
                logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
                return False
            content_hash = _hash_text(text)
            try:
                summary = await self._ai_client.summarize_for_kb_index(source_id=rel_path, text=text)
            except Exception:
                logger.exception("Failed to summarize knowledge base file source. path=%s", file_path)
                return False
            record.summary_text = summary
            record.content_hash = content_hash
            record.last_indexed_at = _format_rfc3339(now)
            record.summary_pending = False
            return True

        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Skipping non-UTF8 knowledge base file. path=%s", file_path)
            return False
        except OSError as e:
            logger.warning("Failed to read knowledge base file. path=%s error=%s", file_path, e)
            return False

        content_hash = _hash_text(text)
        record.file = FileMetadata(rel_path=rel_path, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
        if content_hash != record.content_hash or record.summary_pending:
            try:
                summary = await self._ai_client.summarize_for_kb_index(source_id=rel_path, text=text)
            except Exception:
                logger.exception("Failed to summarize knowledge base file source. path=%s", file_path)
                record.content_hash = content_hash
                record.summary_pending = True
                return True
            record.summary_text = summary
            record.content_hash = content_hash
            record.last_indexed_at = _format_rfc3339(now)
            record.summary_pending = False
        return True

    async def _create_url_source(
        self,
        cache: CacheState,
        url: str,
        now: datetime,
        fetcher: WebFetcher,
    ) -> bool:
        text = await fetcher.fetch(url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch knowledge base URL source content. url=%s", url)
            return False
        content_hash = _hash_text(text)

        # Save intermediate state: download success, summary pending.
        # This ensures that if the process exits during LLM summarization,
        # we don't need to re-download the content next time.
        record = CacheRecord(
            source_type="url",
            content_hash=content_hash,
            summary_text="",
            last_indexed_at=_format_rfc3339(now),
            summary_pending=True,
            url=UrlMetadata(
                url=url,
                last_fetched_at=_format_rfc3339(now),
                etag=None,
                last_modified=None,
                fetch_status="success",
                next_check_at=_format_rfc3339(now + timedelta(seconds=self._config.url_refresh_min_interval_seconds)),
            ),
        )
        cache.sources[url] = record
        self._persist_cache_and_index(cache, now)

        try:
            summary = await self._ai_client.summarize_for_kb_index(source_id=url, text=text)
            record.summary_text = summary
            record.summary_pending = False
        except Exception:
            logger.exception("Failed to summarize knowledge base URL source. url=%s", url)
            # Record remains in pending state

        return True

    async def _refresh_urls(self, cache: CacheState, now: datetime) -> bool:
        url_records: list[Tuple[str, CacheRecord, datetime, bool]] = []
        for source_id, record in cache.sources.items():
            if record.source_type != "url" or not record.url:
                continue
            if self._is_url_eligible(record, now):
                try:
                    next_check = _parse_rfc3339(record.url.next_check_at)
                except Exception:
                    next_check = now
                url_records.append((source_id, record, next_check, True))
            elif record.summary_pending:
                try:
                    next_check = _parse_rfc3339(record.url.next_check_at)
                except Exception:
                    next_check = now
                url_records.append((source_id, record, next_check, False))

        url_records.sort(key=lambda item: item[2])

        changed = False
        async with WebFetcher(self._config) as fetcher:
            for source_id, record, _, should_check in url_records:
                if not should_check:
                    refreshed = await self._summarize_cached_only(record=record, now=now, fetcher=fetcher)
                else:
                    refreshed = await self._refresh_single_url(cache=cache, record=record, now=now, fetcher=fetcher)
                if refreshed:
                    self._persist_cache_and_index(cache, now)
                    changed = True
        return changed

    def _is_url_eligible(self, record: CacheRecord, now: datetime) -> bool:
        if not record.url:
            return False
        try:
            next_check = _parse_rfc3339(record.url.next_check_at)
        except Exception:
            return True
        if next_check <= now:
            return True
        return False

    async def _refresh_single_url(self, cache: CacheState, record: CacheRecord, now: datetime, fetcher: WebFetcher) -> bool:
        if not record.url:
            return False
        url_meta = record.url
        try:
            status, etag, last_modified = await self._conditional_request(
                url=url_meta.url,
                etag=url_meta.etag,
                last_modified=url_meta.last_modified,
            )
        except asyncio.TimeoutError:
            return self._mark_url_failure(record, "timeout", now)
        except aiohttp.ClientError as e:
            logger.warning("URL refresh request failed. url=%s error=%s", url_meta.url, e)
            return self._mark_url_failure(record, "error", now)
        except Exception:
            logger.exception("Unexpected URL refresh error. url=%s", url_meta.url)
            return self._mark_url_failure(record, "error", now)

        if status == 304:
            url_meta.fetch_status = "not_modified"
            url_meta.last_fetched_at = _format_rfc3339(now)
            url_meta.next_check_at = _format_rfc3339(
                now + timedelta(seconds=self._config.url_refresh_min_interval_seconds)
            )
            if record.summary_pending:
                cached_text = fetcher.get_cached_content(url_meta.url)
                if cached_text:
                    try:
                        summary = await self._ai_client.summarize_for_kb_index(
                            source_id=url_meta.url,
                            text=cached_text,
                        )
                    except Exception:
                        logger.exception("Failed to summarize cached URL content. url=%s", url_meta.url)
                    else:
                        record.summary_text = summary
                        record.content_hash = _hash_text(cached_text)
                        record.last_indexed_at = _format_rfc3339(now)
                        record.summary_pending = False
            return True

        if status != 200:
            logger.warning("Unexpected URL refresh status. url=%s status=%s", url_meta.url, status)
            return self._mark_url_failure(record, "error", now)

        text = await fetcher.fetch(url_meta.url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch knowledge base URL source content. url=%s", url_meta.url)
            return self._mark_url_failure(record, "error", now)

        content_hash = _hash_text(text)

        # Update metadata for successful download
        url_meta.etag = etag
        url_meta.last_modified = last_modified
        url_meta.fetch_status = "success"
        url_meta.last_fetched_at = _format_rfc3339(now)
        url_meta.next_check_at = _format_rfc3339(
            now + timedelta(seconds=self._config.url_refresh_min_interval_seconds)
        )

        should_summarize = content_hash != record.content_hash or record.summary_pending or not record.summary_text.strip()
        if should_summarize:
            record.content_hash = content_hash
            record.summary_pending = True
            # Save intermediate state: new content downloaded, summary pending.
            self._persist_cache_and_index(cache, now)

            try:
                summary = await self._ai_client.summarize_for_kb_index(source_id=url_meta.url, text=text)
                record.summary_text = summary
                record.summary_pending = False
            except Exception:
                logger.exception("Failed to summarize knowledge base URL source. url=%s", url_meta.url)
                # Retry sooner on summarization failure?
                # The original logic set next_check_at to runtime_refresh_tick_seconds here.
                url_meta.next_check_at = _format_rfc3339(
                    now + timedelta(seconds=self._config.runtime_refresh_tick_seconds)
                )

        return True

    async def _summarize_cached_only(self, record: CacheRecord, now: datetime, fetcher: WebFetcher) -> bool:
        if not record.url or not record.summary_pending:
            return False
        cached_text = fetcher.get_cached_content(record.url.url)
        if not cached_text:
            return False
        try:
            summary = await self._ai_client.summarize_for_kb_index(
                source_id=record.url.url,
                text=cached_text,
            )
        except Exception:
            logger.exception("Failed to summarize cached URL content. url=%s", record.url.url)
            return False
        record.summary_text = summary
        record.content_hash = _hash_text(cached_text)
        record.last_indexed_at = _format_rfc3339(now)
        record.summary_pending = False
        return True

    async def _conditional_request(
        self,
        url: str,
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> Tuple[int, Optional[str], Optional[str]]:
        headers = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        timeout = aiohttp.ClientTimeout(total=self._config.web_fetch_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as response:
                status = response.status
                response_etag = response.headers.get("ETag")
                response_last_modified = response.headers.get("Last-Modified")
                response.release()
                return status, response_etag, response_last_modified

    def _mark_url_failure(self, record: CacheRecord, status: FetchStatus, now: datetime) -> bool:
        if not record.url:
            return False
        url_meta = record.url
        url_meta.fetch_status = status
        url_meta.next_check_at = _format_rfc3339(now + timedelta(seconds=self._config.runtime_refresh_tick_seconds))
        return True

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
