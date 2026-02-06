# Module Design: AI Response

## Purpose

The AI Response module is responsible for generating safe answers to user questions using a Knowledge Base. It owns the reply workflow and the LLM usage for that workflow.

## Responsibilities

- **Answer Generation**: Orchestrate a multi-step workflow to decide if a question is answerable, select sources, load content, generate an answer, and verify it.
- **Safety & Verification**: Ensure answers are grounded in provided sources and safe to post.
- **Resource Management**: Efficiently manage LLM API calls for the reply workflow.

## Public Interfaces

The AI response module exposes the reply workflow through its AI response service.

### Generate Reply

The main entry point for the bot's conversational capabilities.

- **Input**: `Conversation`, `RequestContext`
- **Output**: `AIResult` (contains `should_reply`, `reply_text`)
- **Implementation**: **Graph-based Orchestration** using LangGraph.


## Implementation Architecture

### Graph-based Orchestration (For `generate_reply`)

The complex "Generate Reply" workflow is modeled as a Directed Acyclic Graph (DAG) using `langgraph`.

#### Retrieval Strategy

The system does not use a traditional RAG pipeline where the user query is embedded and used to perform semantic search over the knowledge base.

Instead, retrieval is a two-stage, index-driven workflow:

1. The Knowledge Base provides a plain-text index where each entry contains a `source_id` and a short description of the source.
2. The LLM reads this index and selects the most relevant `source_id` values for the user question.
3. The system loads the full content for those selected sources and passes them as context to the answer-generation step.

This design makes the retrieval decision explicit and reviewable, and it avoids brittle query-to-embedding behavior for short or ambiguous questions.

Source ID rules:
- For knowledge base sources, `source_id` is namespaced as `kb:<identifier>`.
  - For file sources: `kb:<rel_path>` where `<rel_path>` is a path relative to `kb.sources_dir`.
  - For web sources: `kb:<url>` where `<url>` is the full URL.
- For team topic sources, `source_id` is namespaced as `team:<topic_filename>` (for example `team:get-test-tokens.txt`).

#### High-level Node Graph

Nodes represent distinct processing steps:

1.  **Question Gating**: Decides if the input is a question and if it is answerable.
2.  **Source Selection**: Uses the KB index to pick relevant files.
3.  **Content Loading**: Fetches full text for selected files.
4.  **Answer Generation**: Synthesizes an answer.
5.  **Answer Verification**: Optional final check for quality and safety.

```mermaid
flowchart LR
  In[Input: Conversation + Context] --> Gate[1. Gating]
  Gate -->|not answerable| OutNo[Return should_reply=false]
  Gate -->|answerable| Select[2. Source selection via KB index]
  Select -->|no sources and no images| OutNo
  Select -->|no sources and images| Gen
  Select --> Load[3. Load selected source content]
  Load --> Gen[4. Generation]
  Gen -->|if enabled| Ver[5. Verification]
  Gen -->|if disabled| OutYes[Return should_reply=true]
  Ver -->|approved| OutYes[Return should_reply=true]
  Ver -->|rejected| OutNo
```

#### Graph Reuse and Concurrency

The Graph structure is the high-level container for the workflow logic.

- **Write Once, Run Many**: The `StateGraph` is defined and compiled into a `CompiledStateGraph` (Runnable) **once** at application startup.
- **Thread-Safety**: The compiled graph is immutable and thread-safe. A single instance handles all concurrent requests.
- **Stateless Execution**: While the graph *manages* state during a single request, it does not persist it. Each request starts with a fresh state. Checkpointing is explicitly disabled.

#### Detailed Node Designs

##### Node 1: Question gating
- **Goal**: Decide whether the input is answerable.
- **Inputs**: Conversation history, `gating_prompt`.
- **Output**: `should_reply`.
- **Behavior**: Fast fail if the user input is chit-chat or off-topic.
  - When image input is enabled, each user message can include images and the order is preserved.
  - Bot replies are included in the history and labeled as `You:` for the LLM.

##### Node 2: Source selection
- **Goal**: Select relevant file paths from the KB index.
- **Inputs**: User question, `kb_index_text`, `selection_prompt`.
- **Output**: List of `selected_source_ids`.
- **Behavior**: The LLM analyzes the index (which contains file summaries) to pick the best matches.

##### Node 3: Load selected source content
- **Goal**: Retrieve full text for the chosen IDs.
- **Inputs**: `selected_source_ids`.
- **Output**: List of `{source_id, text}`.
- **Behavior**: Calls the Knowledge Base module. If loading fails for all sources, the flow stops.

##### Node 4: Answer generation
- **Goal**: Produce a concise answer.
- **Inputs**: User question, loaded source text, `answer_prompt`.
- **Output**: `draft_answer`.
- **Constraints**: Must use only provided context. Must not hallucinate.
  - When image input is enabled, the user message includes images in the configured adapter format.

##### Node 5: Answer verification
- **Goal**: Final safety check.
- **Inputs**: Draft answer, source context, `verification_prompt`.
- **Output**: `verification` (boolean).
- **Behavior**: Acts as a "supervisor" to reject low-quality or unsafe answers. This step is skipped unless verification is enabled in configuration.

## Link Inclusion

When the selected sources include URL identifiers, the final reply text includes a short "Links" section with those URLs. This makes it easier for users to jump directly to the primary references without requiring citation formatting in the answer text.

## LLM Integration

The module uses `langchain-crynux` (`ChatCrynux`, ChatOpenAI-compatible) for all reply workflow interactions:

- **Graph workflow**: A `ChatCrynux` instance is created at graph build time and injected into graph nodes

AI response operations MUST use ChatCrynux configured from `ai_response.llm`.

Shared LLM utilities are defined in `src/community_intern/llm/` and include image adapters plus prompt composition helpers that are reused across modules.

## Shared Data Models

See `src/community_intern/core/models.py`. The module relies on:
- `Conversation`: Platform-agnostic chat history.
- `AIResult`: The standardized output contract.

## Configuration

The AI response module is configured under the `ai_response` section in `config.yaml`.

### Shared Keys (Connection & Resilience)
The `ai_response.llm` object defines:
- `base_url`: Base URL for the LLM API.
- `api_key`: API key for the LLM.
- `model`: Model name to use.
- `vram_limit`: Minimum GPU VRAM required for the inference run in GB.
- `structured_output_method`: Structured output mode (`json_schema` or `function_calling`).
- `timeout_seconds`: Timeout per individual LLM call (network timeout).
- `max_retries`: Maximum retry attempts for transient failures.

The AI response module MUST use `ai_response.llm` for `generate_reply`. Knowledge Base LLM overrides are configured under `kb.llm` and MUST NOT change AI response behavior.

### Graph-Specific Keys (`generate_reply`)
- **Workflow Timeout**: `graph_timeout_seconds` (End-to-end timeout for the entire graph execution).
- **Project Introduction**: `project_introduction` (shared domain introduction appended to multiple prompt steps).
- **Verification Toggle**: `enable_verification` (When `true`, run the verification step after generation. When `false`, return the generated answer as final without verification. Default: `false`.)
- **Prompts**: `gating_prompt`, `selection_prompt`, `answer_prompt`, `verification_prompt`.
- **Limits**: `max_sources`, `max_answer_chars`.

### Image Handling Keys
- **Image Enable Switch**: `llm_enable_image` MUST gate all image handling behavior.
- **Image Adapter**: `llm_image_adapter` MUST select the adapter for request formatting.
- **Download Timeout**: `image_download_timeout_seconds` MUST bound each image download.
- **Download Retries**: `image_download_max_retries` MUST control retry attempts for image fetch.

Image adapter implementations live in `src/community_intern/llm/image_adapters.py`. Image downloads are handled by `src/community_intern/llm/image_transport.py` in the integration layer before the AI response graph runs.

## Error Handling

- **Timeouts**: Strict timeouts apply to the overall request and individual LLM calls.
- **Fail-Safe**: If any step in the graph fails (e.g., API error, validation error), the module returns `should_reply=false` rather than crashing.
- **Image Failures**: If any required image download fails, the module MUST return `should_reply=false` without answering.
- **Logging**: Detailed logs capture the decision path (gating -> selection -> generation) for debugging.

## Prompt Assembly Rules

The configuration provides task-focused prompt content only. The runtime assembles the final system prompts by appending shared and fixed requirements in code:

- For the graph workflow (`generate_reply`): `project_introduction` is appended to the system prompt for gating, source selection, answer generation, and verification.
- All LLM calls MUST use JSON-only structured outputs. Output format requirements are enforced in code and are not configurable.
 - The user message to each LLM step includes the full conversation history with `You:` labels for bot replies.

The shared prompt composition helper lives in `src/community_intern/llm/prompts.py` and ensures consistent assembly across modules.

## Observability

- **Logs**: Latency, decision outcomes (e.g., `should_reply`), and token usage.
- **Metrics**:
  - `ai_requests_total`: Counters for success/skip/error.
  - `ai_gate_total`: Track how often the bot decides to answer.
 - **Batch boundary debug logs**:
   - `discord.user_batch_wait`: Emitted when the adapter schedules or resets the quiet-window timer after a user message in a channel.
   - `discord.user_batch_process_start`: Emitted when the adapter starts processing a completed user message batch and is about to call `generate_reply`.
