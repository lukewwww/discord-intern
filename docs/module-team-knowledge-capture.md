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
- These documents serve as additional sources for the AI to answer future user questions
- The Knowledge Base module can select and load these documents just like other KB sources

### F2: Team Member Handling

Messages from configured team member Discord accounts receive special handling:
- Team member messages do not trigger the AI reply workflow
- When a team member replies to a community user (via Discord reply or thread), the Q&A exchange is captured for the knowledge base

### F3: Complete Conversation Capture

The system captures complete Q&A exchanges from Discord:
- Extract the user's question and the team member's answer
- Group consecutive messages from the same author into a single block
- Handle multi-turn conversations: user question → team answer → user follow-up → team follow-up answer are captured as a single Q&A pair
- Preserve original message content without summarization

**Thread handling**: When a team member posts in a thread, the entire thread history is captured as a single Q&A pair. If messages within the thread have reply references to other messages, those referenced messages are also included.

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

The Team Knowledge module uses the AI module's `invoke_llm` interface for LLM operations (see `module-ai-response.md`):

- Calls `AIClient.invoke_llm` with a system prompt and Pydantic response model
- Prompts are configured in the `kb` section of the config file
- Uses `with_structured_output` for automatic JSON schema and validation
- Appends `project_introduction` from AI config to all LLM calls

LLM responses are kept minimal to reduce token usage and improve reliability:

**Classification**:
- Pydantic model: `ClassificationResult`
- Returns: `topic_name` (str)
- The topic identifier (e.g., "node-startup-issues")
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
    # 1. Extract Q&A pair from gathered context
    qa_pair = extract_qa_pair(context, gathered_context)

    # 2. Store to raw archive (Tier 1)
    await append_to_raw_archive(qa_pair)

    # 3. Classify and integrate into topic file (Tier 2)
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
    node-startup-issues.json      # JSON format for easy QA pair manipulation
    token-deposits.json
    relay-account-setup.json
    ...
  index-team.txt                  # Index for topic documents (loaded by KB module)
  index-team-cache.json           # Cache for incremental index updates (same schema as KB cache)
```

### Q&A Pair Format

**Raw files (Tier 1)** use plain text format for simple append-only storage:

```
--- QA ---
timestamp: 2026-01-15T14:32:00Z
conversation_id: thread_1458745245675032709
message_ids: msg_123, msg_124, msg_125
Q: How do I start a Crynux node?
A: You can start a node by running the Docker container with the following command...

--- QA ---
timestamp: 2026-01-16T09:15:00Z
conversation_id: thread_1458745245675032710
message_ids: msg_200, msg_201, msg_202, msg_203
Q: My node shows GPU not detected, what should I do?
Q: I'm using an RTX 3080
A: First, make sure your NVIDIA drivers are up to date.
A: Then check if Docker has GPU access by running nvidia-smi inside the container.
```

Raw file metadata fields:
- `conversation_id`: Thread ID (prefixed with `thread_`) or root message ID (prefixed with `reply_`) for deduplication
- `message_ids`: Comma-separated list of Discord message IDs included in this capture

**Topic files (Tier 2)** use JSON format for easy QA pair identification and manipulation:

```json
[
  {
    "id": "qa_20260115_143200",
    "timestamp": "2026-01-15T14:32:00Z",
    "turns": [
      {"role": "user", "content": "How do I start a Crynux node?"},
      {"role": "team", "content": "You can start a node by running the Docker container..."}
    ]
  },
  {
    "id": "qa_20260116_091500",
    "timestamp": "2026-01-16T09:15:00Z",
    "turns": [
      {"role": "user", "content": "My node shows GPU not detected, what should I do?"},
      {"role": "team", "content": "First, make sure your NVIDIA drivers are up to date."},
      {"role": "user", "content": "I'm using an RTX 3080"},
      {"role": "team", "content": "Then check if Docker has GPU access..."}
    ]
  }
]
```

JSON format advantages:
- Each QA pair has a unique ID for precise reference
- LLM can specify which pairs to remove by ID
- Easy to parse and validate
- `turns` array preserves the original order of multi-turn conversations

### Index File Format

The index follows the existing knowledge base format:

```
node-startup-issues.txt
Common questions about starting Crynux nodes, including GPU detection failures,
Docker configuration, and network connectivity problems.

token-deposits.txt
Questions about depositing tokens into the Crynux Portal, relay account setup,
and cross-chain transfers between supported L2 networks.
```

Format rules:
- First line: topic file name (identifier)
- Following lines: description of what topics/questions the file covers
- Blank line between entries
- File-level granularity (describes the file as a whole, not individual Q&A pairs)

### Index Cache

The `index-team-cache.json` file tracks topic file states to avoid unnecessary LLM calls when regenerating index entries. This cache uses the shared schema and utilities defined in [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md).

Differences from main KB cache:
- `source_type` is always `"team_topic"` (no file metadata or URL-specific fields)
- No `summary_pending` flag needed (simpler single-phase processing)

Cache behavior:
- After updating a topic file, compute its content hash using `hash_text`
- If the hash matches the cached value, reuse the cached `summary_text`
- If the hash differs, call LLM to generate a new summary and update cache

### LLM Classification Prompt

The classification prompt receives:
- The current `index.txt` content
- The new Q&A pair to classify

Expected output (JSON):
- `topic_name`: the topic identifier (e.g., "node-startup-issues")

Example output:
```json
{
  "topic_name": "node-startup-issues"
}
```

The system checks if `{topic_name}.json` exists to determine whether this is a new topic.

Fallback behavior: If uncertain, create a new topic file.

### LLM Integration Prompt

When updating an existing topic file, the integration prompt receives:
- The current topic file content (JSON array of existing Q&A pairs with IDs)
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
  team_raw_dir: "data/team-knowledge/raw"
  team_topics_dir: "data/team-knowledge/topics"
  team_index_path: "data/team-knowledge/index-team.txt"
  team_index_cache_path: "data/team-knowledge/index-team-cache.json"

  team_classification_prompt: |
    You are a topic classifier for a team knowledge base.
    Given the current index of topics and a new Q&A pair, decide whether to add it to an existing topic or create a new one.
    ...

  team_integration_prompt: |
    You are integrating a new Q&A pair into an existing topic file.
    ...

  team_summarization_prompt: |
    Write a compact index description of this Q&A topic file.
    ...
```

---

## CLI Commands

### Regenerate Command

```bash
python -m community_intern regenerate-team-kb
```

Workflow:
1. Read all Q&A pairs from `raw/` directory
2. Clear `topics/` directory and `index.txt`
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
| LLM call failure | Log, retry with backoff; on max retries, store to raw but skip indexing |
| File I/O failure | Log and propagate; no partial writes |
| Classification ambiguity | Default to creating new topic file |
| Malformed raw file entry | Log warning, skip entry during regeneration |

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
- When generating answers, team knowledge takes precedence over static documentation
- The KB module can load topic files from `topics/` just like other KB sources

### Content Formatting for LLM

When loading topic files for answer generation, the KB module formats the JSON into readable conversation text:

```
--- 2026-01-16T09:15:00Z ---
User: My node shows GPU not detected, what should I do?
Team: First, make sure your NVIDIA drivers are up to date.
User: I'm using an RTX 3080
Team: Then check if Docker has GPU access...

--- 2026-01-17T10:30:00Z ---
User: How do I start a Crynux node?
Team: You can start a node by running the Docker container...
```

This format:
- Preserves the multi-turn conversation order
- Is easy for the LLM to understand
- Removes JSON syntax overhead
- Groups each Q&A exchange with its timestamp

Module boundaries:
- This module owns all files under `team-knowledge/`, `index-team.txt`, and `index-team-cache.json`
- The KB module only reads these files, never writes them
- Both modules share cache utilities (`cache_utils`) for consistency (see [`./module-knowledge-base-cache.md`](./module-knowledge-base-cache.md) § Shared utilities)

---

## Dependencies

- Discord adapter ([`module-bot-integration.md`](./module-bot-integration.md)): This module implements `QACaptureHandler`; the adapter provides message classification, context gathering, and routing
- AI module ([`module-ai-response.md`](./module-ai-response.md)): Provides LLM calls for classification, integration, and index summarization via a shared `ChatCrynux` instance.
- Shared cache modules:
  - `cache_utils`: Timestamp utilities.
  - `cache_io`: Cache I/O, index utilities.
  - `cache_models`.
- Knowledge Base module: Loads team knowledge (read-only)
