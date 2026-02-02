# Module Design: Team Knowledge Capture

## Purpose

This module captures knowledge from team member replies in Discord and organizes it into a searchable document library for future AI-assisted answering.

When a configured team member replies to another user's message via Discord reply or thread, the system extracts the Q&A context and stores it in a two-tier knowledge base structure.

---

## Functional Requirements

### F1: Knowledge Capture from Team Conversations

The module captures knowledge from team member replies in Discord and makes it available as a source for the Knowledge Base module (see `module-knowledge-base.md`):
- When a team member replies to a user's question, the Q&A exchange is captured
- The captured knowledge is organized into topic-indexed documents
- These documents serve as additional sources for the AI response module to answer future user questions
- The Knowledge Base module can select and load these documents just like other KB sources

### F2: Team Member Handling

Messages from configured team member Discord accounts receive special handling:
- Team member messages do not trigger the AI response workflow
- When a team member replies to a community user (via Discord reply or thread), the Q&A exchange is captured for the knowledge base
- Bot messages are not stored in the team knowledge base

### F3: Complete Conversation Capture

The system captures complete Q&A exchanges from Discord:
- Extract the user's question and the team member's answer
- Group consecutive messages from the same author into a single block
- Handle multi-turn conversations: user question → team answer → user follow-up → team follow-up answer are captured as a single Q&A pair
- Preserve original message text without summarization
- Summarize key information from images into text before Q&A extraction

**Thread handling**: When a team member posts in a thread, the capture includes the full thread message history plus the thread starter message from the parent channel. If any included message is a Discord reply, the capture also includes the referenced message chain and adjacent messages from the same author within the batching window.

**Conversation-level deduplication**: Each capture includes a `conversation_id` (thread ID or reply chain root message ID) and `message_ids` list. During regeneration, the system keeps only the most complete version of each conversation (the one with the most message IDs), avoiding duplicate processing of incremental captures.

### F4: Knowledge Organization and Maintenance

The captured knowledge is organized for efficient use:
- Q&A pairs are organized by topic to minimize context sent to the LLM during query answering
- An index file describes each topic for LLM-based source selection
- When new information supersedes older Q&A pairs, outdated content is removed to prevent contradictions
- A raw data archive preserves all original captures for audit and regeneration

---

## Technical Specification

### Two-Tier Storage Architecture

The system maintains two storage tiers:

Before any tier storage, the system MUST summarize key image information into text using the full conversation context and inject the summary into the Q&A text.

**Tier 1: Raw Data Archive**
- Permanent, append-only archive of all captured Q&A pairs
- Partitioned by time (weekly) for manageability
- Serves as source of truth; never modified after initial write

**Tier 2: Topic-Indexed Library**
- Q&A pairs organized by topic
- Maintains an index file for LLM-based source selection
- Can be fully regenerated from Tier 1 data

### LLM-Based Topic Classification

The capture workflow is event-driven: each team member reply event triggers immediate processing. Events that occur when the app is not running are simply ignored (no queuing), and no message-level deduplication is needed since each event is processed exactly once.

When a new Q&A pair is captured:
- The system sends the Q&A pair and current index to an LLM
- The LLM decides whether to add it to an existing topic file or create a new topic
- No embeddings or vector similarity are used; all classification is done by the LLM

### LLM-Based Topic Integration

When adding a Q&A pair to a topic file, the LLM decides how to integrate it:
- The system sends the existing topic file content and the new Q&A pair to an LLM
- The LLM decides whether to add the new pair or skip it (if the information is already covered)
- The LLM also identifies which existing pairs to remove (obsolete or superseded)
- Only Tier 2 (topic files) is affected; Tier 1 (raw archive) is never modified

### LLM Integration

The Team Knowledge module uses the shared LLM invoker component for LLM operations:

- Calls `LLMInvoker.invoke_llm` from `src/community_intern/llm/invoker.py` with a system prompt and Pydantic response model
- Prompts are configured in the `kb` section of the config file
- Uses `with_structured_output` for automatic JSON schema and validation
- Appends `project_introduction` from AI response config to all LLM calls
- LLM calls MUST use ChatCrynux with `kb.llm` when configured, otherwise they MUST use `ai_response.llm`
- Image summarization MUST use `invoke_llm` with base64 images provided by the Discord adapter
- Image downloads use `src/community_intern/llm/image_transport.py`
- Image adapters live in `src/community_intern/llm/image_adapters.py`
- Prompt composition uses `src/community_intern/llm/prompts.py`

### LLM Instances

- Team Knowledge MUST use its own ChatCrynux instance configured from `kb.llm` when set, or from `ai_response.llm` when `kb.llm` is null.

LLM responses are kept minimal to reduce token usage and improve reliability:

**Classification**:
- Pydantic model: `ClassificationResult`
- Returns: `skip` (bool), `topic_name` (str)
- `skip`: if true, the Q&A pair lacks sufficient information and should not be added to any topic file
- `topic_name`: the topic identifier (e.g., "node-startup-issues"); empty when skip is true
- The system determines if a topic is new by checking whether the file exists

**Integration**:
- Pydantic model: `IntegrationResult`
- Returns: `skip` (bool), `remove_ids` (list of str, can be empty)
- `skip`: if true, the new Q&A pair is not added (information already covered by existing pairs)
- `remove_ids`: IDs of existing pairs to remove (obsolete or superseded)

**Index Summarization**:
- After each topic file update, the system generates an index description via the summarization prompt
- This description is cached and written to `index-team.txt` for use in future classifications

### Regeneration

The indexed library can be rebuilt from scratch via CLI command:
- Reads all Q&A pairs from raw data archive
- Clears existing topic files and index
- Reprocesses all pairs through LLM classification and integration
- Useful after prompt changes, to clean up obsolete pairs, or to recover from corruption

### Q&A Capture Handler Interface

This module implements the `QACaptureHandler` interface from the Discord adapter's unified message routing architecture (see [`module-bot-integration.md`](./module-bot-integration.md) § Action Handlers).

**Trigger conditions** (determined by bot integration's ActionRouter):

| MessageContext | Routed to this handler |
|----------------|------------------------|
| `author_type=team_member`, `reply_target.author_type=community_user` | Yes |
| `author_type=team_member`, in thread with community user question | Yes |
| `author_type=team_member`, `reply_target.author_type=team_member` | No |
| `author_type=team_member`, no reply context | No |

**Received context** (gathered by bot integration after quiet window expires):

| Field | Usage |
|-------|-------|
| `batch` | Team member's consecutive messages within quiet window (merged into answer) |
| `thread_history` | Full thread history for multi-turn extraction |
| `reply_chain` | Full reply chain with consecutive message expansion |

The handler is only invoked after `discord.message_batch_wait_seconds` expires, ensuring the team member's complete response is captured even if sent across multiple messages.

### Q&A Pair Formation

This handler transforms the gathered context into Q&A pairs:

| Aspect | Behavior |
|--------|----------|
| Thread extraction | From `thread_history`: all messages in the thread, grouped by author into alternating Q/A turns |
| Reply chain extraction | From `reply_chain`: all consecutive user messages that form the original question |
| Answer extraction | The team member's `batch` messages |
| Multi-turn handling | When user asks → team answers → user follows up → team answers again, all messages form a single Q&A pair with alternating Q/A sections |

**Extraction priority**:
1. If `thread_history` is available (message is in a thread), use thread history to extract the full conversation
2. Otherwise, use `reply_chain` for direct reply scenarios
3. Always append the team member's `batch` messages as the final answer

**Example 1 - Thread**: Team member posts in a thread with 5 messages:
- `thread_history` contains all 5 messages (user and team interleaved)
- `batch` contains the team member's current message(s)
- Result: Q&A pair with full thread conversation, `conversation_id` = thread ID

**Example 2 - Direct reply**: User sends 3 messages as a question, team replies:
- `reply_chain` contains the 3-message group (the question)
- `batch` contains the team member's reply
- Result: Q&A pair with complete question and answer, `conversation_id` = root message ID

### Handler Implementation

```python
async def handle(
    message: discord.Message,
    context: MessageContext,
    gathered_context: GatheredContext
) -> None:
    # 1. Summarize image content using full conversation context
    #    The summary is keyed by message_id and image_index to preserve ordering and association.
    image_summary = summarize_images(context, gathered_context)

    # 2. Extract Q&A pair from gathered context and image summary
    #    The extractor injects the summary into the matching message by id.
    qa_pair = extract_qa_pair(context, gathered_context, image_summary)

    # 3. Store to raw archive (Tier 1)
    await append_to_raw_archive(qa_pair)

    # 4. Classify and integrate into topic file (Tier 2)
    await classify_and_integrate(qa_pair)
```

### Storage Layout

```
data/team-knowledge/
  raw/                            # Tier 1: Raw data archive (append-only)
    2026-W01.txt                  # ISO week format, plain text
    2026-W02.txt
    2026-W03.txt
    ...
  topics/                         # Tier 2: Topic-indexed documents
    node-startup-issues.txt       # Plain text, LLM-ready
    token-deposits.txt
    relay-account-setup.txt
    ...
  index-team.txt                  # Index for topic documents (loaded by KB module)
  index-team-cache.json           # Cache for incremental index updates (same schema as KB cache)
```

### Q&A Pair Format

**Raw files (Tier 1)** use plain text format for simple append-only storage:

```
--- QA ---
id: qa_20260201_031056.228000
timestamp: 2026-02-01T03:10:56.228000Z
conversation_id: thread_1458745245675032709
message_ids: msg_123, msg_124, msg_125
User: How do I start a Crynux node?
Team: You can start a node by running the Docker container with the following command...

--- QA ---
id: qa_20260201_031057.000000
timestamp: 2026-02-01T03:10:57.000000Z
conversation_id: thread_1458745245675032710
message_ids: msg_200, msg_201, msg_202, msg_203
User: My node shows GPU not detected, what should I do?
User: I'm using an RTX 3080
Team: First, make sure your NVIDIA drivers are up to date.
Team: Then check if Docker has GPU access by running nvidia-smi inside the container.
```

Raw file metadata fields:
- `conversation_id`: Thread ID (prefixed with `thread_`) or root message ID (prefixed with `reply_`) for deduplication
- `message_ids`: Comma-separated list of Discord message IDs included in this capture

### Timestamp and QA ID Specification

This module uses two related identifiers:

- `timestamp`: RFC 3339 timestamp in UTC, ending with `Z`.
  - Examples: `2026-02-01T03:10:56Z`, `2026-02-01T03:10:56.228000Z`
- `qa_id`: A stable identifier derived directly from `timestamp`.
  - Format: `qa_YYYYMMDD_HHMMSS` with an optional fractional seconds suffix.
  - Examples: `qa_20260201_031056`, `qa_20260201_031056.228000`

Derivation rules:

- Start from the timestamp string and remove separators:
  - Remove `-` and `:`
  - Replace `T` with `_`
  - Remove the trailing `Z`
- Preserve fractional seconds if present.

Notes:

- Raw archive files must store both `id:` and `timestamp:`. This avoids format drift and ensures state tracking is stable.
- Topic files must include both `id:` and `timestamp:`. The `id:` must match the derived `qa_id` for that `timestamp`.

**Topic files (Tier 2)** use plain text format optimized for direct LLM consumption and stable diffing:

```
--- QA ---
id: qa_20260201_031056.228000
timestamp: 2026-02-01T03:10:56.228000Z
User: How do I start a Crynux node?
Team: You can start a node by running the Docker container...

--- QA ---
id: qa_20260201_031057.000000
timestamp: 2026-02-01T03:10:57.000000Z
User: My node shows GPU not detected, what should I do?
Team: First, make sure your NVIDIA drivers are up to date.
User: I'm using an RTX 3080
Team: Then check if Docker has GPU access...
```

Format rules:
- Each Q&A block starts with `--- QA ---`
- Each block MUST include `id:` and `timestamp:` for stable reference and caching
- `id:` MUST be the `qa_id` derived from the `timestamp:` value using the rules above
- Each conversation turn starts with `User:` or `Team:`

### Index File Format

The index follows the existing knowledge base format:

```
team:node-startup-issues.txt
Common questions about starting Crynux nodes, including GPU detection failures,
Docker configuration, and network connectivity problems.

team:token-deposits.txt
Questions about depositing tokens into the Crynux Portal, relay account setup,
and cross-chain transfers between supported L2 networks.
```

Format rules:
- First line: `team:<topic_filename>` identifier
- Following lines: description of what topics/questions the file covers
- Blank line between entries
- File-level granularity (describes the file as a whole, not individual Q&A pairs)

### Index Cache

The team index cache (`index-team-cache.json`) stores persistent state for incremental updates of `index-team.txt`.

This cache uses the same schema as the Knowledge Base cache. See [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md).

The shared indexing component is specified in [`./module-knowledge-indexer.md`](./module-knowledge-indexer.md).

Team Knowledge indexing uses file sources only. Topic files under `topics/` are summarized into `index-team.txt` using `kb.team_summarization_prompt`.

### LLM Classification Prompt

The classification prompt receives:
- The current `index.txt` content
- The new Q&A pair to classify

Expected output (JSON):
- `skip`: boolean, true if the Q&A pair should not be indexed
- `topic_name`: the topic identifier (e.g., "node-startup-issues"); empty when skip is true

Example outputs:

Q&A pair with sufficient information:
```json
{
  "skip": false,
  "topic_name": "node-startup-issues"
}
```

Q&A pair lacking sufficient information:
```json
{
  "skip": true,
  "topic_name": ""
}
```

The system checks if `{topic_name}.txt` exists to determine whether this is a new topic.

**Skip behavior**: The LLM should set `skip: true` when the text conversation is not self-contained or cannot guide future answers. Common cases include:
- The question uses vague references like "this error" or "this issue" without describing what it is
- The answer cannot be understood without additional context not present in the text
- Greetings, casual chat, or off-topic exchanges without technical content

Skipped Q&A pairs remain in the raw archive for audit purposes but are not added to topic files.

**Fallback behavior**: If classification fails due to LLM errors, the Q&A pair is skipped (not added to any topic file). The raw archive preserves all captures regardless of classification outcome.

### LLM Integration Prompt

When updating an existing topic file, the integration prompt receives:
- The current topic file content (plain text, including Q&A block IDs)
- The new Q&A pair to add

Expected output (JSON):
- `skip`: boolean, true if the new Q&A should not be added
- `remove_ids`: array of QA pair IDs to remove (can be empty)

Example outputs:

New Q&A supersedes an older one:
```json
{
  "skip": false,
  "remove_ids": ["qa_20260115_143200"]
}
```

New Q&A adds new information:
```json
{
  "skip": false,
  "remove_ids": []
}
```

New Q&A is redundant (information already covered):
```json
{
  "skip": true,
  "remove_ids": []
}
```

### Regeneration Processing

During regeneration:
1. Load all Q&A pairs from raw files
2. **Deduplicate by conversation**: Group pairs by `conversation_id`, keep only the entry with the most `message_ids` (the most complete version)
3. Sort deduplicated pairs by timestamp (oldest first)
4. Process each pair sequentially through classification and integration
5. Later pairs can supersede earlier pairs on the same topic

The deduplication step ensures that incremental captures of the same conversation (e.g., Q1-A1, then Q1-A1-Q2-A2) are collapsed to the final complete version, avoiding redundant LLM processing.

---

## Configuration

Discord configuration is defined in [`module-bot-integration.md`](./module-bot-integration.md) § Runtime configuration:
- `discord.team_member_ids`: List of Discord user IDs for team members
- `discord.message_batch_wait_seconds`: Quiet window for message batching

This module adds:

```yaml
kb:
  # ... other kb config ...
  llm: null
  team_raw_dir: "data/team-knowledge/raw"
  team_topics_dir: "data/team-knowledge/topics"
  team_index_path: "data/team-knowledge/index-team.txt"
  team_index_cache_path: "data/team-knowledge/index-team-cache.json"
  qa_raw_last_processed_id: ""

  team_classification_prompt: |
    You are a topic classifier for a team knowledge base.
    Given the current index of topics and a new Q&A pair, decide whether to add it to an existing topic, create a new one, or skip it entirely.
    First, evaluate whether the Q&A pair contains enough useful information...
    ...

  team_integration_prompt: |
    You are integrating a new Q&A pair into an existing topic file.
    ...

  team_summarization_prompt: |
    Write a compact index description of this Q&A topic file.
    ...

  team_image_summary_prompt: |
    Summarize key information from images in the context of the team conversation.
    ...
```

### Raw Start Cursor Behavior

`qa_raw_last_processed_id` lets operators set a raw processing cursor.

Rules:
- The value must be a valid `qa_id` from the raw archive, for example `qa_20260201_031056.228000`
- On startup, the system compares this value with `state.json` and uses whichever is newer
- Items with `qa_id` less than or equal to the chosen cursor are ignored
- Items with `qa_id` greater than the chosen cursor are processed

### Processing State

The module persists a processing cursor in `state.json` as:

- `last_processed_qa_id`: The most recent `qa_id` that was successfully processed into topic files.

Incremental processing rules:

- On each tick, the raw archive is scanned for entries with `qa_id` greater than `last_processed_qa_id`.
- If `last_processed_qa_id` is empty, the system loads and processes all raw entries.
- If `last_processed_qa_id` is not a valid `qa_id` format, the application exits with an error.

---

## CLI Commands

### Regenerate Command

```bash
python -m community_intern init_team_kb
```

Workflow:
1. Read all Q&A pairs from `raw/` directory
2. Clear `topics/` directory and `index-team.txt`
3. Reprocess all pairs through LLM classification and integration
4. Rebuild complete indexed library

Use cases:
- After modifying classification/integration prompts
- To clean up obsolete pairs
- To recover from corrupted topic files

---

## Runtime Flows

### Capture Flow

```
Discord Message Event
      │
      ▼
┌─────────────────┐
│ Bot Integration │ Classification + Context Gathering + Routing
│ (see module-    │ (handled by Discord adapter)
│ bot-integration)│
└────────┬────────┘
         │ routes to QACaptureHandler
         ▼
┌─────────────────┐
│ Image Summary   │ Summarize image content with full conversation context
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Q&A Extraction  │ Transform gathered context into Q&A pair
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Raw Storage     │ Append to weekly raw file (Tier 1)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Classification  │ LLM determines topic
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Integration     │ LLM updates topic file (add new, remove obsolete)
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Index Update    │ Compute hash → compare with cache → LLM if changed
└─────────────────┘
```

### Regeneration Flow

```
CLI Command
      │
      ▼
┌─────────────────┐
│ Load Raw Data   │ Read all Q&A pairs, sort by timestamp
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Clear Tier 2    │ Remove topics/ and index.txt
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Process Each    │ For each Q&A: classify → integrate
│ (chronological) │ Later pairs can supersede earlier ones
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Complete        │ Indexed library rebuilt
└─────────────────┘
```

---

## Error Handling

| Error Type | Handling |
|------------|----------|
| LLM call failure | Log and skip indexing; Q&A pair remains in raw archive |
| File I/O failure | Log and propagate; no partial writes |
| Classification returns skip | Log and skip indexing; Q&A pair remains in raw archive |
| Empty topic_name without skip | Log warning and skip indexing |
| Malformed raw file entry | Log warning, skip entry during regeneration |
| Image summary failure | Log and abort capture before Tier 1 write |

---

## Observability

Logs:
- `qa_pair_captured`: New Q&A pair detected and stored
- `topic_classified`: LLM classification result
- `topic_file_updated`: Topic file modified
- `regeneration_started`, `regeneration_completed`: CLI regeneration lifecycle
- `llm_classification_failed`, `llm_integration_failed`: Error events

---

## Knowledge Base Integration

The topic-indexed library integrates with the main Knowledge Base module (see `module-knowledge-base.md`):

- The KB module loads `index-team.txt` alongside its main `index.txt`
- Both indices are combined and sent to the LLM for source selection
- Team topic identifiers are namespaced as `team:<topic_filename>` to avoid collisions with normal file sources
- When generating answers, team knowledge takes precedence over static documentation
- The KB module can load topic files from `topics/` just like other KB sources

### Content Formatting for LLM

When loading topic files for answer generation, the KB module loads the topic file as plain text and passes it through unchanged:

```
--- QA ---
id: qa_20260116_091500
timestamp: 2026-01-16T09:15:00Z
User: My node shows GPU not detected, what should I do?
Team: First, make sure your NVIDIA drivers are up to date.
```

This format:
- Preserves the multi-turn conversation order
- Is easy for the LLM to understand
- Includes stable Q&A IDs for remove-by-id operations
- Includes timestamps for recency and cache invalidation

Module boundaries:
- This module owns all files under `team-knowledge/`, `index-team.txt`, and `index-team-cache.json`
- The KB module only reads these files, never writes them
- Both modules use shared indexing utilities for consistency (see [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md) § Shared utilities)

---

## Dependencies

- Discord adapter ([`module-bot-integration.md`](./module-bot-integration.md)): This module implements `QACaptureHandler`; the adapter provides message classification, context gathering, and routing
- Shared LLM utilities: `src/community_intern/llm/invoker.py`, `src/community_intern/llm/image_adapters.py`, `src/community_intern/llm/image_transport.py`, `src/community_intern/llm/prompts.py`
- Shared cache modules:
  - `src/community_intern/knowledge_cache/utils.py`
  - `src/community_intern/knowledge_cache/io.py`
  - `src/community_intern/knowledge_cache/models.py`
- Knowledge Base module: Loads team knowledge (read-only)
