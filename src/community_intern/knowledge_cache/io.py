from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, Iterable, Sequence

from community_intern.knowledge_cache.models import (
    CacheRecord,
    CacheState,
    FileMetadata,
    SchemaVersion,
    SourceType,
    UrlMetadata,
)
from community_intern.knowledge_cache.utils import format_rfc3339, utc_now

logger = logging.getLogger(__name__)


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


def atomic_write_text(path: Path, text: str) -> None:
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
        content_hash=payload.get("content_hash", ""),
        summary_text=payload.get("summary_text", ""),
        last_indexed_at=payload.get("last_indexed_at", ""),
        summary_pending=bool(payload.get("summary_pending", False)),
        file=file_value,
        url=url_value,
    )


def encode_cache(cache: CacheState) -> dict:
    return {
        "schema_version": cache.schema_version,
        "generated_at": cache.generated_at,
        "sources": {source_id: _encode_record(record) for source_id, record in cache.sources.items()},
    }


def decode_cache(payload: dict) -> CacheState:
    sources_payload = payload.get("sources", {})
    sources: Dict[str, CacheRecord] = {}
    for source_id, record_payload in sources_payload.items():
        sources[source_id] = _decode_record(record_payload)
    return CacheState(
        schema_version=int(payload.get("schema_version", SchemaVersion)),
        generated_at=payload.get("generated_at", format_rfc3339(utc_now())),
        sources=sources,
    )


def read_cache_file(path: Path) -> CacheState:
    if not path.exists():
        return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        cache = decode_cache(payload)
        if cache.schema_version != SchemaVersion:
            logger.warning(
                "Cache schema version mismatch, starting fresh. path=%s expected=%s actual=%s",
                path,
                SchemaVersion,
                cache.schema_version,
            )
            return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})
        return cache
    except Exception:
        logger.exception("Failed to read cache file, starting fresh. path=%s", path)
        return CacheState(schema_version=SchemaVersion, generated_at=format_rfc3339(utc_now()), sources={})


def build_index_entries(cache: CacheState, *, source_types: Sequence[SourceType], prefix: str) -> list[str]:
    entries: list[str] = []
    for source_type in source_types:
        group: list[tuple[str, str]] = []
        for source_id, record in cache.sources.items():
            if record.source_type != source_type:
                continue
            summary = record.summary_text.strip()
            if not summary:
                continue
            group.append((source_id, summary))
        for source_id, summary in sorted(group, key=lambda item: item[0]):
            identifier = f"{prefix}{source_id}".strip()
            entries.append(f"{identifier}\n{summary}".strip())
    return entries


def write_index_file(path: Path, entries: Iterable[str]) -> None:
    content = "\n\n".join([e for e in entries if e.strip()])
    atomic_write_text(path, content)

