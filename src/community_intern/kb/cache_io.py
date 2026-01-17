from __future__ import annotations

import json
from pathlib import Path
from typing import Dict

from community_intern.kb.cache_models import (
    CacheRecord,
    CacheState,
    FileMetadata,
    SchemaVersion,
    UrlMetadata,
)
from community_intern.kb.cache_utils import format_rfc3339, utc_now


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
        content_hash=payload["content_hash"],
        summary_text=payload["summary_text"],
        last_indexed_at=payload["last_indexed_at"],
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
