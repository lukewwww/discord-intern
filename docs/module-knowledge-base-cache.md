## Module Design: Knowledge Base Cache and Incremental Updates

### Purpose

This document specifies the incremental update mechanism used by the Knowledge Base module to keep `index.txt` and its supporting cache state up to date without re-summarizing unchanged sources.

The mechanism applies to:

- Local file sources discovered under `kb.sources_dir`
- Web URL sources listed in `kb.links_file_path`

### Design goals

- The Knowledge Base MUST avoid calling the AI summarization method when a source's content has not changed.
- The Knowledge Base MUST avoid re-fetching all URLs on every incremental update run.
- The Knowledge Base MUST produce a stable and human-readable `index.txt`.
- The Knowledge Base MUST keep the same processing rules across `init_kb`, application startup sync, and runtime refresh.

### Core approach

The Knowledge Base MUST implement a single update workflow that keeps `index.txt` current.

The update workflow MUST be triggered by the following entrypoints:

- `init_kb`: the update workflow MUST run once and the process MUST exit after completion.
- Application startup sync: the update workflow MUST run once to ensure the cache and `index.txt` are up to date and MUST NOT block bot startup.
- Runtime refresh for long-running processes: the update workflow MUST run periodically every `kb.runtime_refresh_tick_seconds` seconds.

Only one update workflow execution MUST run at a time. Executions triggered by different entrypoints MUST NOT overlap.

Each update workflow execution MUST apply incremental processing rules:

- The Knowledge Base MUST discover the current source set from `kb.sources_dir` and `kb.links_file_path` and remove cached sources that no longer exist.
- For unchanged sources, the Knowledge Base MUST reuse cached `summary_text` and MUST NOT call the AI summarization method.
- For URL sources, the Knowledge Base MUST refresh only URLs that are eligible based on `next_check_at` and MUST use conditional requests when possible.

Cache record fields and persisted file formats are specified in this document under Persisted state and files.

### Runtime configuration

All configuration is loaded from `config.yaml` with environment-variable overrides as specified in `docs/configuration.md`.

This mechanism reads these keys under the `kb` section:

- `kb.index_cache_path`
- `kb.url_refresh_min_interval_seconds`
- `kb.runtime_refresh_tick_seconds`

## End-to-end workflow

### Update workflow

Each update workflow execution MUST perform the following steps while holding a single-writer lock:

- Load the current cache state from `kb.index_cache_path` if it exists; otherwise initialize an empty cache state.
- Discover the current source set from `kb.sources_dir` and `kb.links_file_path`.
- Apply deletions for sources removed from the source set.
- Process file sources using the rules in "File source processing".
- Process URL sources using the rules in "URL source processing".
- After any per-source change, persist the updated cache state and regenerate the index file at `kb.index_path`.

### Source discovery

The Knowledge Base MUST compute the current set of sources:

- File sources: all supported text files under `kb.sources_dir`
- URL sources: all non-empty lines in `kb.links_file_path` after trimming leading and trailing whitespace

For URL sources:

- The Knowledge Base MUST treat each trimmed line as a URL string.
- The Knowledge Base MUST ignore empty lines after trimming.
- The Knowledge Base MUST deduplicate URLs by exact string match after trimming.

### Deletions

If a `source_id` exists in the cache but is not present in the current source set, the Knowledge Base MUST:

- Remove the record from the cache
- Exclude the source from the index file at `kb.index_path`

### File source processing

To efficiently handle local file updates, the system uses a multi-stage check. First, it compares the file's metadata (size and modification time) against the cache. If they match, the file is skipped to avoid unnecessary I/O. If they differ, the system reads the file and computes a content hash. The AI summarization is only triggered if this content hash has changed or if a previous summarization is marked as pending. In case of a summarization failure, the system updates the stored hash but flags the entry as pending, ensuring the summary generation is retried in the next run even if the file content remains the same.

For each discovered file source, the Knowledge Base MUST apply these rules:

- If the file source is new:
  - Load the file content as UTF-8 text.
  - Compute `content_hash`.
  - Attempt AI summarization to produce `summary_text`.
  - On success, store the cache record with `summary_pending = false`.
  - On failure, store the cache record with `summary_text = ""` and `summary_pending = true`.
- If the file source exists in the cache:
  - If both `size_bytes` and `mtime_ns` match the cached values, the file MUST be treated as unchanged and the AI MUST NOT be called.
    - If `summary_pending` is true, the Knowledge Base MUST attempt summarization using the current file content.
      - On success, the Knowledge Base MUST set `summary_text`, `content_hash`, `last_indexed_at`, and `summary_pending = false`.
      - On failure, the Knowledge Base MUST leave the cache record unchanged.
  - Otherwise, the Knowledge Base MUST read the file content and compute `content_hash`.
    - The Knowledge Base MUST update the cached file metadata fields `size_bytes` and `mtime_ns` to the current values.
    - If `content_hash` differs from the cached value or `summary_pending` is true:
      - The AI module summarization method MUST be called and `summary_text` MUST be updated on success.
      - If summarization fails, `summary_pending` MUST be set to true, `content_hash` MUST be updated to the new value, and the previous `summary_text` MUST remain unchanged.
      - If summarization succeeds, the Knowledge Base MUST set `summary_text`, `content_hash`, `last_indexed_at`, and `summary_pending = false`.
    - If `content_hash` is unchanged and `summary_pending` is false, the AI MUST NOT be called and the Knowledge Base MUST only update the cached file metadata fields `size_bytes` and `mtime_ns`.

### URL source processing

To efficiently handle URL updates, the system performs periodic checks based on a configured interval. When a check is due, it optimizes bandwidth by using conditional HTTP requests (ETag/Last-Modified). If the server reports the content hasn't changed (304 Not Modified), the cached summary is preserved. If the content has changed (200 OK), the system downloads the new content, computes a hash, and only triggers AI summarization if the content is effectively different or if a previous summary is pending.

### New URL source

If the URL source is new (i.e., its `source_id` is not present in the loaded cache):

- The Knowledge Base MUST fetch URL source content.
- The Knowledge Base MUST write the fetched URL content to the URL content cache file for that URL.
- The Knowledge Base MUST set `fetch_status = "success"` and `last_fetched_at = now`.
- The Knowledge Base MUST compute `content_hash`.
- The Knowledge Base MUST immediately store the cache record with `summary_pending = true` and `summary_text = ""`. This ensures that if the process exits before or during summarization, the downloaded content is preserved and can be summarized later without re-fetching.
- The Knowledge Base MUST attempt AI summarization to produce `summary_text`.
  - On success, the Knowledge Base MUST set `summary_text` and `summary_pending = false` and persist the cache record.
  - On failure, the Knowledge Base MUST leave `summary_pending = true` and the cache record as is.
- Each URL record MUST store `next_check_at` and the Knowledge Base MUST set `next_check_at = now + kb.url_refresh_min_interval_seconds`.

### Existing URL source

If the URL source exists in the cache, the Knowledge Base MUST NOT fetch URL content during URL discovery. Instead, the Knowledge Base MUST decide whether to refresh.

The Knowledge Base MUST treat a URL as eligible for refresh if `next_check_at` is less than or equal to the current time.

For eligible URLs, the Knowledge Base MUST first issue a conditional request to obtain HTTP status and updated validator headers (`ETag`, `Last-Modified`).

- If `etag` is present, the Knowledge Base MUST send an `If-None-Match` request header.
- If `last_modified` is present, the Knowledge Base MUST send an `If-Modified-Since` request header.

If the server responds with HTTP 304:

- The Knowledge Base MUST treat the URL as unchanged for this refresh.
- The Knowledge Base MUST set `fetch_status = "not_modified"` and update `last_fetched_at`.
- The Knowledge Base MUST NOT fetch URL content for this refresh.
- The Knowledge Base MUST set `next_check_at = now + kb.url_refresh_min_interval_seconds`.
- If `summary_pending` is true, the Knowledge Base MUST attempt summarization using cached content without issuing a network request.
  - On success, the Knowledge Base MUST set `summary_text`, `content_hash`, `last_indexed_at`, and `summary_pending = false`.
  - On failure or if cached content is missing, the Knowledge Base MUST leave the cache record unchanged.

If the server responds with HTTP 200:

- The Knowledge Base MUST fetch content using the web fetching mechanism defined in `docs/module-knowledge-base.md` and write it to the URL content cache file for that URL.
- The Knowledge Base MUST compute `content_hash` for the extracted content.
- If the content is new or summarization is required, the Knowledge Base MUST call the AI module summarization method.
  - Summarization is required when `content_hash` differs from the cached value, `summary_pending` is true, or `summary_text` is empty after trimming whitespace.
  - Before calling the AI, if content has changed, the Knowledge Base MUST immediately update the cache record with the new `content_hash`, `summary_pending = true`, and updated fetch metadata (`etag`, `last_modified`, `fetch_status = "success"`, `last_fetched_at`), and persist the cache. This ensures the new content is acknowledged even if summarization fails.
  - On success, the Knowledge Base MUST set `summary_text`, `content_hash`, `last_indexed_at`, and `summary_pending = false` and persist the cache.
  - On failure, the Knowledge Base MUST leave `summary_pending = true`, MUST keep the previous `summary_text` (or empty string if new), and MUST set `next_check_at = now + kb.runtime_refresh_tick_seconds`.
- The Knowledge Base MUST update `etag` and `last_modified` when present, set `fetch_status = "success"`, update `last_fetched_at`, and set `next_check_at = now + kb.url_refresh_min_interval_seconds`.

If an eligible URL check fails due to timeout, error, or unexpected HTTP status:

- The Knowledge Base MUST set `fetch_status` to `"timeout"` or `"error"`.
- The Knowledge Base MUST set `next_check_at = now + kb.runtime_refresh_tick_seconds`.
- The Knowledge Base MUST NOT modify `content_hash` or `summary_text`.

When `summary_pending` is true for a URL and the URL is not eligible for refresh:

- The Knowledge Base MUST attempt summarization using cached content without issuing conditional requests.
- On success, the Knowledge Base MUST set `summary_text`, `content_hash`, `last_indexed_at`, and `summary_pending = false`.
- On failure or if cached content is missing, the Knowledge Base MUST leave the cache record unchanged.

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

The `source_id` string MUST be used as the identifier line in `index.txt`.

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
- The Knowledge Base MUST ensure that only one writer updates `kb.index_path` and `kb.index_cache_path` at a time.
- When a source record is added, updated, or removed, the Knowledge Base MUST persist updates immediately for that single source change. This ensures that failures (e.g., URL download errors or LLM summarization issues) for one source do not cause the entire batch of updates to be lost.
- Each persistence operation MUST update both `kb.index_cache_path` and `kb.index_path` to reflect the same cache state.

## Example artifacts

- An example cache file is provided at `examples/index-cache.json`.
