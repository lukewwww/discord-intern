from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Awaitable, Callable, Optional, Tuple

import aiohttp

from community_intern.config.models import KnowledgeBaseSettings
from community_intern.kb.cache_models import CacheRecord, CacheState, FetchStatus, UrlMetadata
from community_intern.kb.web_fetcher import WebFetcher

logger = logging.getLogger(__name__)


class CacheUrlHandler:
    def __init__(
        self,
        *,
        config: KnowledgeBaseSettings,
        download_semaphore: asyncio.Semaphore,
        persist_cache_and_index_async: Callable[[CacheState, datetime], Awaitable[None]],
        hash_text: Callable[[str], str],
        format_rfc3339: Callable[[datetime], str],
        parse_rfc3339: Callable[[str], datetime],
    ) -> None:
        self._config = config
        self._download_semaphore = download_semaphore
        self._persist_cache_and_index_async = persist_cache_and_index_async
        self._hash_text = hash_text
        self._format_rfc3339 = format_rfc3339
        self._parse_rfc3339 = parse_rfc3339

    async def fetch_url_text(self, fetcher: WebFetcher, url: str, *, force_refresh: bool) -> str:
        async with self._download_semaphore:
            return await fetcher.fetch(url, force_refresh=force_refresh)

    async def conditional_request_limited(
        self,
        *,
        url: str,
        etag: Optional[str],
        last_modified: Optional[str],
    ) -> Tuple[int, Optional[str], Optional[str]]:
        async with self._download_semaphore:
            return await self.conditional_request(url=url, etag=etag, last_modified=last_modified)

    async def create_url_source(
        self,
        *,
        cache: CacheState,
        url: str,
        now: datetime,
        fetcher: WebFetcher,
    ) -> bool:
        text = await self.fetch_url_text(fetcher, url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch knowledge base URL source content. url=%s", url)
            return False
        content_hash = self._hash_text(text)

        record = CacheRecord(
            source_type="url",
            content_hash=content_hash,
            summary_text="",
            last_indexed_at=self._format_rfc3339(now),
            summary_pending=True,
            url=UrlMetadata(
                url=url,
                last_fetched_at=self._format_rfc3339(now),
                etag=None,
                last_modified=None,
                fetch_status="success",
                next_check_at=self._format_rfc3339(
                    now + timedelta(hours=self._config.url_refresh_min_interval_hours)
                ),
            ),
        )
        cache.sources[url] = record
        await self._persist_cache_and_index_async(cache, now)
        return True

    async def refresh_urls(self, cache: CacheState, now: datetime) -> bool:
        url_records: list[CacheRecord] = []
        for source_id, record in cache.sources.items():
            if record.source_type != "url" or not record.url:
                continue
            if self.is_url_eligible(record, now):
                url_records.append(record)

        if not url_records:
            return False

        async with WebFetcher(self._config) as fetcher:
            tasks = [
                asyncio.create_task(self.refresh_single_url(cache=cache, record=record, now=now, fetcher=fetcher))
                for record in url_records
            ]
            results = await asyncio.gather(*tasks)
        return any(results)

    def is_url_eligible(self, record: CacheRecord, now: datetime) -> bool:
        if not record.url:
            return False
        try:
            next_check = self._parse_rfc3339(record.url.next_check_at)
        except Exception:
            return True
        if next_check <= now:
            return True
        return False

    async def refresh_single_url(
        self,
        *,
        cache: CacheState,
        record: CacheRecord,
        now: datetime,
        fetcher: WebFetcher,
    ) -> bool:
        if not record.url:
            return False
        url_meta = record.url
        try:
            status, etag, last_modified = await self.conditional_request_limited(
                url=url_meta.url,
                etag=url_meta.etag,
                last_modified=url_meta.last_modified,
            )
        except asyncio.TimeoutError:
            if self.mark_url_failure(record, "timeout", now):
                await self._persist_cache_and_index_async(cache, now)
                return True
            return False
        except aiohttp.ClientError as e:
            logger.warning("URL refresh request failed. url=%s error=%s", url_meta.url, e)
            if self.mark_url_failure(record, "error", now):
                await self._persist_cache_and_index_async(cache, now)
                return True
            return False
        except Exception:
            logger.exception("Unexpected URL refresh error. url=%s", url_meta.url)
            if self.mark_url_failure(record, "error", now):
                await self._persist_cache_and_index_async(cache, now)
                return True
            return False

        if status == 304:
            url_meta.fetch_status = "not_modified"
            url_meta.last_fetched_at = self._format_rfc3339(now)
            url_meta.next_check_at = self._format_rfc3339(
                now + timedelta(hours=self._config.url_refresh_min_interval_hours)
            )
            await self._persist_cache_and_index_async(cache, now)
            return True

        if status != 200:
            logger.warning("Unexpected URL refresh status. url=%s status=%s", url_meta.url, status)
            if self.mark_url_failure(record, "error", now):
                await self._persist_cache_and_index_async(cache, now)
                return True
            return False

        text = await self.fetch_url_text(fetcher, url_meta.url, force_refresh=True)
        if not text:
            logger.warning("Failed to fetch knowledge base URL source content. url=%s", url_meta.url)
            if self.mark_url_failure(record, "error", now):
                await self._persist_cache_and_index_async(cache, now)
                return True
            return False

        content_hash = self._hash_text(text)

        url_meta.etag = etag
        url_meta.last_modified = last_modified
        url_meta.fetch_status = "success"
        url_meta.last_fetched_at = self._format_rfc3339(now)
        url_meta.next_check_at = self._format_rfc3339(
            now + timedelta(hours=self._config.url_refresh_min_interval_hours)
        )

        should_summarize = content_hash != record.content_hash or record.summary_pending or not record.summary_text.strip()
        if should_summarize:
            record.content_hash = content_hash
            record.summary_pending = True
        else:
            record.content_hash = content_hash
        await self._persist_cache_and_index_async(cache, now)
        return True

    async def conditional_request(
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

    def mark_url_failure(self, record: CacheRecord, status: FetchStatus, now: datetime) -> bool:
        if not record.url:
            return False
        url_meta = record.url
        url_meta.fetch_status = status
        url_meta.next_check_at = self._format_rfc3339(
            now + timedelta(seconds=self._config.runtime_refresh_tick_seconds)
        )
        return True
