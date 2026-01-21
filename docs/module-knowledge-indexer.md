# Module Design: Knowledge Indexer

## Purpose

The Knowledge Indexer maintains index artifacts for a set of knowledge sources using incremental rules.

It produces:

- `index.txt` as a compact list of source identifiers and descriptions
- `index-cache.json` as persistent state for incremental updates

The indexer is shared code used by the Knowledge Base indexing flow and the Team Knowledge indexing flow.

## Responsibilities

- Discover the current source set through providers
- Reconcile cache state with the discovered source set
- Refresh eligible sources that require polling such as URLs
- Summarize sources marked as pending using the AI client
- Write `index-cache.json` and `index.txt` deterministically

## Index artifacts

### Index file

The index file is UTF-8 plain text.

Each entry is:

- One identifier line
- One or more description lines

Entries are separated by a blank line.

Identifier lines are prefixed by the caller:

- Knowledge Base index uses `kb:` plus the source id
- Team Knowledge index uses `team:` plus the source id

### Cache file

The cache file is UTF-8 JSON.

It stores:

- `schema_version`
- `generated_at`
- `sources` as a mapping keyed by source id

Each source record stores:

- `source_type` as `file` or `url`
- `content_hash`
- `summary_text`
- `last_indexed_at`
- `summary_pending`

File records additionally store:

- `file.rel_path`
- `file.size_bytes`
- `file.mtime_ns`

URL records additionally store:

- `url.url`
- `url.last_fetched_at`
- `url.etag`
- `url.last_modified`
- `url.fetch_status`
- `url.next_check_at`

## Provider contract

The indexer delegates source specific behavior to providers.

Each provider owns a disjoint set of source ids and implements:

- `discover` returns a mapping of source id to source type
- `init_record` returns an initialized cache record for a source id
- `refresh` updates cache records for sources that require polling and returns whether any state changed
- `load_text` returns the current text content for a source id

## Processing pipeline

One indexer run performs these phases in order:

- Discover sources through all providers
- Reconcile cache additions deletions and type mismatches using provider `init_record`
- Refresh through providers
- Summarize all records where `summary_pending` is true
- Persist cache and write the index

The pipeline supports a periodic run and a manual trigger.

- Periodic run calls `run_once`
- Manual trigger calls `notify_changed` and runs the same pipeline

## Concurrency and locking

Each indexer instance serializes executions with a single writer lock.

Summarization is concurrency limited by a semaphore.

## Provider implementations

### File folder provider

`FileFolderProvider` discovers sources by scanning a configured folder.

It uses file metadata as a fast path to skip reading unchanged files.

### URL links provider

`UrlLinksProvider` discovers sources by reading a links file.

It refreshes eligible URLs using conditional requests and stores extracted content in the web cache directory.

For new URL sources, `init_record` fetches content and persists it to the web cache before returning the record.

## Module wiring

### Knowledge Base indexing flow

The Knowledge Base uses:

- `FileFolderProvider` for local files
- `UrlLinksProvider` for URLs

It writes `index.txt` and `index-cache.json` under the Knowledge Base paths.

### Team Knowledge indexing flow

Team Knowledge uses:

- `FileFolderProvider` for topic files

It writes `index-team.txt` and `index-team-cache.json` under the team knowledge paths.
