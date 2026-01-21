from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

import aiohttp

from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.web_fetcher import WebFetcher
from community_intern.knowledge_cache.models import CacheRecord, CacheState, FetchStatus, SourceType, UrlMetadata
from community_intern.knowledge_cache.utils import format_rfc3339, hash_text, parse_rfc3339

logger = logging.getLogger(__name__)


class UrlLinksProvider:
    def __init__(self, *, config: KnowledgeBaseSettings) -> None:
        self._config = config
        self._urls: Dict[str, str] = {}
        self._download_semaphore = asyncio.Semaphore(max(1, int(self._config.url_download_concurrency)))

    async def discover(self, *, now: datetime) -> Dict[str, SourceType]:
        _ = now
        self._urls = {}
        links_file = Path(self._config.links_file_path)
        if not links_file.exists():
            return {}
        try:
            content = links_file.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("Failed to read links file. path=%s error=%s", links_file, e)
            return {}

        sources: Dict[str, SourceType] = {}
        for line in content.splitlines():
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            if url in sources:
                continue
            sources[url] = "url"
            self._urls[url] = url
        return sources

    async def init_record(self, *, source_id: str, now: datetime) -> CacheRecord | None:
        url = self._urls.get(source_id)
        if not url:
            return None

        async with WebFetcher(self._config) as fetcher:
            text = await self._fetch_url_text(fetcher, url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch URL content for new source. url=%s", url)
            return None

        content_hash = hash_text(text)
        return CacheRecord(
            source_type="url",
            content_hash=content_hash,
            summary_text="",
            last_indexed_at=format_rfc3339(now),
            summary_pending=True,
            url=UrlMetadata(
                url=url,
                last_fetched_at=format_rfc3339(now),
                etag=None,
                last_modified=None,
                fetch_status="success",
                next_check_at=format_rfc3339(now + timedelta(hours=self._config.url_refresh_min_interval_hours)),
            ),
        )

    async def refresh(self, *, cache: CacheState, now: datetime) -> bool:
        url_records: list[CacheRecord] = []
        for source_id, record in cache.sources.items():
            if record.source_type != "url" or not record.url:
                continue
            if self._is_eligible(record=record, now=now):
                url_records.append(record)

        if not url_records:
            return False

        changed_any = False
        async with WebFetcher(self._config) as fetcher:
            tasks = [
                asyncio.create_task(self._refresh_one(cache=cache, record=record, now=now, fetcher=fetcher))
                for record in url_records
            ]
            results = await asyncio.gather(*tasks)
        changed_any = any(results)
        return changed_any

    async def load_text(self, *, source_id: str) -> str | None:
        url = self._urls.get(source_id) or source_id
        fetcher = WebFetcher(self._config)
        return fetcher.get_cached_content(url)

    def _is_eligible(self, *, record: CacheRecord, now: datetime) -> bool:
        if not record.url:
            return False
        try:
            next_check = parse_rfc3339(record.url.next_check_at)
        except Exception:
            return True
        return next_check <= now

    async def _refresh_one(self, *, cache: CacheState, record: CacheRecord, now: datetime, fetcher: WebFetcher) -> bool:
        if not record.url:
            return False
        url_meta = record.url

        try:
            status, etag, last_modified = await self._conditional_request_limited(
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
            url_meta.last_fetched_at = format_rfc3339(now)
            url_meta.next_check_at = format_rfc3339(now + timedelta(hours=self._config.url_refresh_min_interval_hours))
            return True

        if status != 200:
            logger.warning("Unexpected URL refresh status. url=%s status=%s", url_meta.url, status)
            return self._mark_url_failure(record, "error", now)

        text = await self._fetch_url_text(fetcher, url_meta.url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch URL content during refresh. url=%s", url_meta.url)
            return self._mark_url_failure(record, "error", now)

        content_hash = hash_text(text)
        url_meta.etag = etag
        url_meta.last_modified = last_modified
        url_meta.fetch_status = "success"
        url_meta.last_fetched_at = format_rfc3339(now)
        url_meta.next_check_at = format_rfc3339(now + timedelta(hours=self._config.url_refresh_min_interval_hours))

        should_summarize = content_hash != record.content_hash or record.summary_pending or not record.summary_text.strip()
        record.content_hash = content_hash
        if should_summarize:
            record.summary_pending = True
        return True

    async def _fetch_url_text(self, fetcher: WebFetcher, url: str, *, force_refresh: bool) -> str:
        async with self._download_semaphore:
            return await fetcher.fetch(url, force_refresh=force_refresh)

    async def _conditional_request_limited(
        self,
        *,
        url: str,
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> Tuple[int, Optional[str], Optional[str]]:
        async with self._download_semaphore:
            return await self._conditional_request(url=url, etag=etag, last_modified=last_modified)

    async def _conditional_request(
        self,
        *,
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
        url_meta.next_check_at = format_rfc3339(now + timedelta(seconds=self._config.runtime_refresh_tick_seconds))
        return True

