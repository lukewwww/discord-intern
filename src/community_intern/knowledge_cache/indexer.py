from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Protocol

from community_intern.ai.interfaces import AIClient, LLMTextResult
from community_intern.knowledge_cache.io import atomic_write_json, build_index_entries, encode_cache, read_cache_file, write_index_file
from community_intern.knowledge_cache.models import CacheRecord, CacheState, SchemaVersion, SourceType
from community_intern.knowledge_cache.utils import format_rfc3339, utc_now

logger = logging.getLogger(__name__)


def _compose_system_prompt(base_prompt: str, project_introduction: str) -> str:
    parts = []
    if base_prompt.strip():
        parts.append(base_prompt.strip())
    if project_introduction.strip():
        parts.append(f"Project introduction:\n{project_introduction.strip()}")
    return "\n\n".join(parts).strip()


class SourceProvider(Protocol):
    async def discover(self, *, now: datetime) -> Dict[str, SourceType]:
        ...

    async def init_record(self, *, source_id: str, now: datetime) -> CacheRecord | None:
        ...

    async def refresh(self, *, cache: CacheState, now: datetime) -> bool:
        ...

    async def load_text(self, *, source_id: str) -> str | None:
        ...


class KnowledgeIndexer:
    def __init__(
        self,
        *,
        cache_path: str,
        index_path: str,
        index_prefix: str,
        summarization_prompt: str,
        summarization_concurrency: int,
        ai_client: AIClient,
        providers: Iterable[SourceProvider],
        source_type_order: list[SourceType],
    ) -> None:
        self._cache_path = Path(cache_path)
        self._index_path = Path(index_path)
        self._index_prefix = index_prefix
        self._summarization_prompt = summarization_prompt
        self._ai_client = ai_client
        self._providers = list(providers)
        self._source_type_order = source_type_order

        self._lock = asyncio.Lock()
        self._summary_semaphore = asyncio.Semaphore(max(1, int(summarization_concurrency)))

    async def run_once(self) -> None:
        async with self._lock:
            await self._run_once_locked()

    async def notify_changed(self, source_id: str) -> None:
        _ = source_id
        await self.run_once()

    async def _run_once_locked(self) -> None:
        now = utc_now()
        cache = read_cache_file(self._cache_path)
        if cache.schema_version != SchemaVersion:
            cache = CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(now), sources={})

        discovered, owner = await self._discover_sources(now=now)

        changed = False
        changed |= await self._reconcile(cache=cache, now=now, discovered=discovered, owner=owner)

        for provider in self._providers:
            try:
                provider_changed = await provider.refresh(cache=cache, now=now)
            except Exception:
                logger.exception("Indexer provider refresh failed.")
                provider_changed = False
            if provider_changed:
                changed = True

        if changed:
            self._persist(cache=cache, now=now)

        await self._summarize_pending(cache=cache, now=now, owner=owner)

    async def _discover_sources(self, *, now: datetime) -> tuple[Dict[str, SourceType], Dict[str, SourceProvider]]:
        combined: Dict[str, SourceType] = {}
        owner: Dict[str, SourceProvider] = {}
        for provider in self._providers:
            mapping = await provider.discover(now=now)
            for source_id, source_type in mapping.items():
                if source_id in combined:
                    raise ValueError(f"Duplicate source_id discovered: {source_id}")
                combined[source_id] = source_type
                owner[source_id] = provider
        return combined, owner

    async def _reconcile(
        self,
        *,
        cache: CacheState,
        now: datetime,
        discovered: Dict[str, SourceType],
        owner: Dict[str, SourceProvider],
    ) -> bool:
        changed = False

        for source_id in list(cache.sources.keys()):
            if source_id not in discovered:
                cache.sources.pop(source_id, None)
                changed = True

        for source_id, source_type in discovered.items():
            record = cache.sources.get(source_id)
            if record is None or record.source_type != source_type:
                provider = owner.get(source_id)
                if provider is None:
                    continue
                initialized = await provider.init_record(source_id=source_id, now=now)
                if initialized is None:
                    continue
                cache.sources[source_id] = initialized
                changed = True

        if changed:
            cache.generated_at = format_rfc3339(now)
        return changed

    async def _summarize_pending(
        self,
        *,
        cache: CacheState,
        now: datetime,
        owner: Dict[str, SourceProvider],
    ) -> None:
        tasks = []
        for source_id, record in cache.sources.items():
            if not record.summary_pending:
                continue
            provider = owner.get(source_id)
            if provider is None:
                continue
            tasks.append(
                asyncio.create_task(
                    self._summarize_one(cache=cache, record=record, source_id=source_id, provider=provider, now=now)
                )
            )
        if tasks:
            await asyncio.gather(*tasks)

    async def _summarize_one(
        self,
        *,
        cache: CacheState,
        record: CacheRecord,
        source_id: str,
        provider: SourceProvider,
        now: datetime,
    ) -> None:
        async with self._summary_semaphore:
            try:
                text = await provider.load_text(source_id=source_id)
                if not text or not text.strip():
                    return
                system_prompt = _compose_system_prompt(
                    self._summarization_prompt,
                    self._ai_client.project_introduction,
                )
                result = await self._ai_client.invoke_llm(
                    system_prompt=system_prompt,
                    user_content=text,
                    response_model=LLMTextResult,
                )
                summary = result.text.strip()
            except Exception:
                logger.exception("Indexer summarization failed. source_id=%s", source_id)
                return

        current = cache.sources.get(source_id)
        if current is not record or not current.summary_pending:
            return
        current.summary_text = summary
        current.last_indexed_at = format_rfc3339(now)
        current.summary_pending = False
        self._persist(cache=cache, now=now)

    def _persist(self, *, cache: CacheState, now: datetime) -> None:
        cache.generated_at = format_rfc3339(now)
        atomic_write_json(self._cache_path, encode_cache(cache))
        entries = build_index_entries(
            cache,
            source_types=self._source_type_order,
            prefix=self._index_prefix,
        )
        write_index_file(self._index_path, entries)
