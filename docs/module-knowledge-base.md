# Module Design: Knowledge Base

## Purpose

The Knowledge Base module manages documentation sources and team knowledge. It builds indices describing each source and provides helpers so the AI module can select and load only the most relevant content for a given query.

The KB module is designed for fast source selection and bounded content loading.

## Responsibilities

- Ingest sources from:
  - A folder of local text files
  - HTTP/HTTPS links referenced inside those files
- Load team-captured Q&A topic files (read-only)
- On each startup:
  - Analyze and summarize sources
  - Produce small index artifacts that can be searched quickly
- Provide retrieval helpers to the AI module:
  - Provide the full index text for the AI module to send to the LLM for source selection
  - Load full content for a selected file path or URL identifier
- Enforce safety and performance constraints:
  - Strict timeouts for web fetches
  - Caching for web content
  - Size bounds for loaded content

## Terminology

- **Source**: a file path or URL.
- **Index**: a compact, searchable description per source.

## Runtime configuration

All configuration is loaded from `config.yaml` with environment-variable overrides as specified in [`./configuration.md`](./configuration.md).

The Knowledge Base reads these keys under the `kb` section:

- `kb.sources_dir`
- `kb.index_path`
- `kb.links_file_path`
- `kb.web_fetch_timeout_seconds`
- `kb.web_fetch_cache_dir`
- `kb.url_download_concurrency`
- `kb.summarization_concurrency`
- `kb.summarization_prompt`
- `kb.max_source_bytes`
- `kb.team_topics_dir`
- `kb.team_index_path`
- `kb.team_index_cache_path`

## Cache and incremental updates

The Knowledge Base uses a persistent cache metadata file to support incremental updates of `index.txt` and to reduce unnecessary AI summarization and URL fetching.

The cache schema and the full incremental update requirements are specified in [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md).

The shared indexing component is specified in [`./module-knowledge-indexer.md`](./module-knowledge-indexer.md).

## Index artifact format

The index is intended to be small and fast to read at runtime. It MUST be a **UTF-8 text file**. The AI module may send the full index text to the LLM for source selection, so the format should prioritize readability and stable diffs.

The Knowledge Base maintains two index files:
- `kb.index_path` (`index.txt`): Index for static documentation sources (files and URLs)
- `kb.team_index_path` (`index-team.txt`): Index for team-captured Q&A pairs

Both indices follow the same format. Each entry MUST be:

- A single line containing the source identifier:
  - For files: `kb:<rel_path>` where `<rel_path>` is the file path relative to the knowledge base folder
  - For web sources: `kb:<url>` where `<url>` is the full URL
  - For team topics: `team:<topic_filename>` (for example `team:get-test-tokens.txt`)
- Followed by one or more lines of free-text description for source selection

Entries MUST be separated by at least one blank line.

See the example index file: `examples/kb_index.txt`.

Notes:
- The identifier line must be stable across runs for citation stability.
- Keep descriptions short and focused on when the source is relevant.

### Source identifier namespaces

To avoid collisions between different source types and to keep loading logic unambiguous, both index artifacts use explicit namespaces:

- Knowledge base sources: `kb:...`
- Team topic sources: `team:...`

## Public interfaces

The AI module should not know about filesystem scanning or HTTP caching details. It should call a small retrieval API.

See `src/community_intern/kb/interfaces.py` `KnowledgeBase`.

## Ingestion

### File scanning

- Scan `kb.sources_dir` for text files.
- Read `kb.links_file_path` to obtain a list of URL sources (one URL per line).
- Note: The `links.txt` file itself is NOT summarized; only the content of the URLs it lists is processed.

### URL fetching

- Fetch with strict timeout `kb.web_fetch_timeout_seconds`.
- Use a headless browser to wait for dynamic content (`networkidle` event) and capture the full DOM state.
- Extract content from the `<body>` tag.
- Cache responses in memory or on disk using a hash of the URL as the cache key and file name.
- Enforce max download size `kb.max_source_bytes` and reject larger responses.
- URL downloads MUST run independently from LLM summarization within the update workflow.
- URL download concurrency MUST be limited by `kb.url_download_concurrency`.

### Team knowledge

The KB module reads team knowledge but does not generate or modify it:

- Load the team index from `kb.team_index_path` (read-only)
- Load topic files from `kb.team_topics_dir` when selected by the LLM (read-only)
- Team knowledge capture, indexing, and caching are handled by a separate module. See [`./module-team-knowledge-capture.md`](./module-team-knowledge-capture.md).
- Both the KB module and the Team Knowledge Capture module use shared indexing utilities to ensure consistency. See [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md).

### Index generation

For each source:
- Use an LLM to produce a short description focused on what the source covers and when it is relevant.
- The LLM summarization MUST be performed via the AI module's `invoke_llm` interface. See [`./module-ai-response.md`](./module-ai-response.md).
- The summarization prompt is configured via `kb.summarization_prompt`.
- LLM summarization MUST run as an independent phase and MUST be limited by `kb.summarization_concurrency`.

The index generation step should be deterministic as much as possible to avoid noisy diffs.

Note: The team knowledge index (`index-team.txt`) is generated by the team knowledge capture workflow, not by this ingestion process.

## Retrieval

The Knowledge Base does not decide which sources are relevant.

At runtime, the AI module:

- Loads both index artifacts (`index.txt` and `index-team.txt`) as plain text from the Knowledge Base and receives a combined index by concatenating them.
- Sends the combined index text and the user query to the LLM to select a relevant list of source identifiers.
- Requests full content for those selected identifiers from the Knowledge Base.

When generating answers, if information from team knowledge conflicts with static documentation, the answer prompt instructs the LLM to prefer team sources as they reflect the most recent information from team members.

## Citation design

The KB module must maintain `source_id` continuity:

- The AI module cites sources using the identifier line from the index.
- Optionally, each snippet may later include location metadata:
  - file line ranges
  - URL fragment identifiers

For now, the identifier plus an optional quoted excerpt is sufficient.

## Error handling

- If index is missing or invalid:
  - Fail startup or rebuild index automatically.
- If a web source fetch fails:
  - Log and continue; do not block answering if other sources are available.
- If no selected sources can be loaded:
  - Return empty source content; the AI module decides whether to reply.

## Observability

Logs:

- Index build:
  - `sources_total`, `file_sources_total`, `url_sources_total`, `duration_ms`
  - per-source failures with `source_id` and reason
- Retrieval:
  - `query`, `selected_sources`, `loaded_sources`, `duration_ms`
  - cache hits/misses for URL fetches

Metrics:

- `kb_index_build_total{result=success|error}`
- `kb_web_fetch_total{result=success|timeout|error|cache_hit}`
- `kb_retrieval_selected_sources_histogram`
- `kb_retrieval_loaded_sources_histogram`

## Test plan

- Unit tests:
  - Link reading from links file
  - Index read/write and format validation
- Integration tests:
  - Build index from a sample folder with a mix of files and URLs
  - Loading selected source content returns stable identifiers
