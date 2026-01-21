## Module Design: Knowledge Base Cache and Incremental Updates

### Purpose

This document specifies the incremental update mechanism used to keep `index.txt` and its supporting cache state up to date without re-summarizing unchanged sources.

The shared indexing component is specified in [`./module-knowledge-indexer.md`](./module-knowledge-indexer.md).

The mechanism applies to:

- Local file sources discovered under `kb.sources_dir`
- Web URL sources listed in `kb.links_file_path`

### Design goals

- The Knowledge Base MUST avoid calling the AI summarization method when a source's content has not changed.
- The Knowledge Base MUST avoid re-fetching all URLs on every incremental update run.
- The Knowledge Base MUST produce a stable and human-readable `index.txt`.
- The Knowledge Base MUST keep the same processing rules across `init_kb`, application startup sync, and runtime refresh.
- The Knowledge Base MUST run URL downloads and LLM summarization as independent phases.
- The Knowledge Base MUST enforce separate concurrency limits for URL downloads and LLM summarization.

### Core approach

The Knowledge Base MUST implement a single update workflow that keeps `index.txt` current.

The update workflow MUST be triggered by the following entrypoints:

- `init_kb`: the update workflow MUST run once and the process MUST exit after completion.
- Application startup sync: the update workflow MUST run once to ensure the cache and `index.txt` are up to date and MUST NOT block bot startup.
- Runtime refresh for long-running processes: the update workflow MUST run periodically every `kb.runtime_refresh_tick_seconds` seconds.

The shared indexing component is specified in [`./module-knowledge-indexer.md`](./module-knowledge-indexer.md).

Cache record fields and persisted file formats are specified in this document under Persisted state and files.

### Runtime configuration

All configuration is loaded from `config.yaml` with environment-variable overrides as specified in `docs/configuration.md`.

This mechanism reads these keys under the `kb` section:

- `kb.index_cache_path`
- `kb.url_download_concurrency`
- `kb.summarization_concurrency`
- `kb.url_refresh_min_interval_hours`
- `kb.runtime_refresh_tick_seconds`

## End-to-end workflow

### Update workflow

The Knowledge Base update workflow is implemented by the shared Knowledge Indexer using these providers:

- `FileFolderProvider` for local file sources under `kb.sources_dir`
- `UrlLinksProvider` for URL sources listed in `kb.links_file_path`

Each run performs a full scan and applies incremental rules based on the persisted cache file at `kb.index_cache_path`.

### File source rules

For file sources:

- File sources are discovered by scanning `kb.sources_dir`.
- File changes are detected using `size_bytes` and `mtime_ns` as a fast path.
- When file metadata changes, the system reads the file as UTF-8 text and computes `content_hash`.
- When `content_hash` changes, the system sets `summary_pending = true` so the summarization phase generates a new `summary_text`.

### URL source rules

For URL sources:

- URL sources are discovered by reading `kb.links_file_path` line by line, trimming whitespace, ignoring empty lines, and deduplicating by exact match.
- The URL provider treats a URL as eligible for refresh when `next_check_at` is less than or equal to the current time.
- For eligible URLs, the provider uses conditional HTTP requests when possible:
  - If `etag` is present, send `If-None-Match`.
  - If `last_modified` is present, send `If-Modified-Since`.
- When the server responds with HTTP 304:
  - Update `fetch_status` to `not_modified`.
  - Update `last_fetched_at`.
  - Update `next_check_at`.
- When the server responds with HTTP 200:
  - Fetch content using the web fetching mechanism defined in `docs/module-knowledge-base.md`.
  - Persist extracted content to the web cache directory.
  - Compute `content_hash` for the extracted content.
  - When `content_hash` changes, set `summary_pending = true` so the summarization phase generates a new `summary_text`.
  - Update `etag`, `last_modified`, `fetch_status`, `last_fetched_at`, and `next_check_at`.
- When an eligible URL check fails due to timeout or error:
  - Set `fetch_status` to `timeout` or `error`.
  - Set `next_check_at` based on `kb.runtime_refresh_tick_seconds`.

## Index generation

After any per-source change:

- The Knowledge Base MUST generate `index.txt` by rewriting the full file contents.
- The Knowledge Base MUST use cached `summary_text` for unchanged sources.
- The Knowledge Base MUST NOT call the AI during index generation.
- The Knowledge Base MUST order entries deterministically:
  - File sources first, then URL sources
  - Within each group, sort by `source_id` ascending

Each entry in `index.txt` MUST follow the format defined in `docs/module-knowledge-base.md`.

## Persisted state and files

### Persisted cache state

The Knowledge Base MUST persist cache state as a UTF-8 JSON file at `kb.index_cache_path`.

The cache JSON MUST contain:

- `schema_version`: integer
- `generated_at`: RFC 3339 timestamp in UTC
- `sources`: object keyed by `source_id`

Each `sources[source_id]` record MUST contain:

- `source_type`: `"file"` or `"url"`
- `content_hash`: SHA-256 hex digest of the normalized source content
- `summary_text`: the description text used to generate the `index.txt` entry for this source, excluding the identifier line
- `last_indexed_at`: RFC 3339 timestamp in UTC
- `summary_pending`: boolean flag indicating whether summarization is required but not yet completed

File records MUST additionally contain a `file` object with:

- `rel_path`: the file path relative to `kb.sources_dir`
- `size_bytes`: integer
- `mtime_ns`: integer

URL records MUST additionally contain a `url` object with:

- `url`: full URL string
- `last_fetched_at`: RFC 3339 timestamp in UTC
- `etag`: nullable string
- `last_modified`: nullable string
- `fetch_status`: `"success"`, `"not_modified"`, `"timeout"`, or `"error"`
- `next_check_at`: RFC 3339 timestamp in UTC

### Source identifiers

- For file sources, `source_id` MUST be the file path relative to `kb.sources_dir`.
- For URL sources, `source_id` MUST be the full URL.

When generating `index.txt`, the Knowledge Base MUST namespace the identifier line as `kb:<source_id>`.

If a file is renamed or moved within `kb.sources_dir`, its relative path changes and the Knowledge Base MUST treat it as a deletion of the old `source_id` and an addition of a new `source_id`.

### URL content cache files

The Knowledge Base MUST persist URL source content to an on-disk cache used by this module for cached-only summarization.

- The cache directory MUST be `kb.web_fetch_cache_dir`.
- The cache filename MUST be the SHA-256 hex digest of the full URL string encoded as UTF-8.
- Each cache file MUST be a UTF-8 encoded text file.

### Content hashing and normalization

The Knowledge Base MUST compute `content_hash` from normalized UTF-8 text.

- For file sources, the Knowledge Base MUST decode file bytes as UTF-8 text.
- For URL sources, the Knowledge Base MUST hash the extracted `<body>` text produced by the web fetching mechanism.

Before hashing, the Knowledge Base MUST normalize the text as follows:

- Convert line endings to `\n`.
- Remove trailing whitespace on each line.
- Remove leading and trailing blank lines.

### Atomic writes and locking

- The Knowledge Base MUST write the cache file atomically by writing to a temporary file and then renaming it to `kb.index_cache_path`.
- When updates are persisted, the persistence operation MUST update both `kb.index_cache_path` and `kb.index_path` to reflect the same cache state.

## Shared utilities

The following utilities are implemented as shared code and reused by both the Knowledge Base and Team Knowledge indexing flows.

### Timestamp utilities (`src/community_intern/knowledge_cache/utils.py`)

| Function | Description |
|----------|-------------|
| `utc_now() -> datetime` | Return current UTC datetime |
| `format_rfc3339(dt) -> str` | Format datetime to RFC 3339 string with `Z` suffix |
| `parse_rfc3339(value) -> datetime` | Parse RFC 3339 string to datetime |

### Content utilities (`src/community_intern/knowledge_cache/utils.py`)

| Function | Description |
|----------|-------------|
| `normalize_text(text) -> str` | Convert line endings to `\n`, trim trailing whitespace per line, remove leading/trailing blank lines |
| `hash_text(text) -> str` | Normalize text, then compute SHA-256 hex digest |

### Cache I/O (`src/community_intern/knowledge_cache/io.py`)

| Function | Description |
|----------|-------------|
| `atomic_write_json(path, payload)` | Write JSON dict to temp file, then rename to target path |
| `atomic_write_text(path, text)` | Write text to temp file, then rename to target path |
| `encode_cache(cache) -> dict` | Serialize `CacheState` to JSON-serializable dict |
| `decode_cache(payload) -> CacheState` | Deserialize dict to typed `CacheState` object |
| `read_cache_file(path) -> CacheState` | Read cache JSON, handle missing file, validate schema version |

`read_cache_file` behavior:
- If file does not exist, return empty `CacheState`
- If `schema_version` does not match current version, log warning and return empty `CacheState`
- Parse JSON and return typed `CacheState` object

### Index utilities (`src/community_intern/knowledge_cache/io.py`)

| Function | Description |
|----------|-------------|
| `build_index_entries(cache, source_types, prefix) -> list[str]` | Build sorted index entry strings from cache for specified source types and an identifier prefix |
| `write_index_file(path, entries)` | Join entries with blank lines and write atomically |

`build_index_entries` behavior:
- Filter records by `source_type` from the provided list
- Skip records with empty `summary_text`
- Sort entries by `source_id` ascending within each source type group
- Return list of formatted entries: `"{prefix}{source_id}\n{summary_text}"`

`write_index_file` behavior:
- Join entries with double newlines
- Write atomically using `atomic_write_text`

### Cache schema

Both the KB cache (`index-cache.json`) and Team Knowledge cache (`index-team-cache.json`) use the same JSON schema:

```json
{
  "schema_version": 1,
  "generated_at": "RFC 3339 timestamp",
  "sources": {
    "source_id": {
      "source_type": "file|url",
      "content_hash": "SHA-256 hex",
      "summary_text": "index entry description",
      "last_indexed_at": "RFC 3339 timestamp"
    }
  }
}
```

### Source type fields

| `source_type` | Additional fields | Notes |
|---------------|-------------------|-------|
| `file` | `file.rel_path`, `file.size_bytes`, `file.mtime_ns`, `summary_pending` | File metadata for change detection |
| `url` | `url.url`, `url.last_fetched_at`, `url.etag`, `url.last_modified`, `url.fetch_status`, `url.next_check_at`, `summary_pending` | URL fetch metadata |

The Team Knowledge Capture module uses file sources for topic files and does not use URL sources. See [`./module-team-knowledge-capture.md`](./module-team-knowledge-capture.md).

## Example artifacts

- An example cache file is provided at `examples/index-cache.json`.
