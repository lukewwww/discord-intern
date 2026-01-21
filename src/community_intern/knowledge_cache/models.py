from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Literal, Optional

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


SourceType = Literal["file", "url"]


@dataclass(slots=True)
class CacheRecord:
    source_type: SourceType
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

