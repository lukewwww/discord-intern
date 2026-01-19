# Community Intern

Community Intern is an AI and LLM powered Discord FAQ assistant that monitors selected channels, detects questions, and drafts source grounded answers using a managed knowledge base built from local files and online links.

It also learns from your team by automatically capturing Q&A conversations when team members reply to community questions, growing the knowledge base over time.

## What it does

- Watches all readable Discord channels for question-like messages
- Uses an LLM to decide whether a message is answerable and in scope, and skips messages that are not
- Uses an LLM to select relevant sources from a managed knowledge base built from local files and web links
- Uses an LLM to draft an answer grounded only in the selected source content
- Creates a thread from the triggering message and replies inside that thread
- Supports follow up questions by replying again when a thread continues, using the full thread context
- Captures Q&A conversations from team member replies and automatically grows the knowledge base over time

## Key features

- **AI-generated, source-grounded answers**: An LLM generates answers from your documentation sources and can include citations back to those sources.
- **Guided retrieval for reliable source selection**: Uses an LLM-guided selection flow (instead of embedding-only RAG) to pick the best sources for short or ambiguous questions.
- **Knowledge base from files and links**: Uses a local folder of text sources and can incorporate web pages referenced by links, including JavaScript-rendered pages that require a headless browser to fully load before content extraction.
- **Token-efficient web ingestion**: After fetching web pages, the HTML content is cleaned to keep only key content, reducing LLM token usage during indexing and answering.
- **Incremental updates and auto-refresh**: Automatically monitors and updates the knowledge base when local files or web sources change. Uses a persistent cache to process only changed content, avoiding redundant LLM summarization and network requests.
- **Bring your own LLM**: Choose which LLM provider and model to use via configuration.
- **Thread-first replies**: Answers live in message-backed threads rather than cluttering the channel.
- **Configurable scope**: Communities can tune what kinds of questions are considered answerable without changing code.

### Team Knowledge Capture

The bot learns from your team. When a configured team member replies to a community question in Discord, the system automatically captures the Q&A exchange and adds it to the knowledge base. Over time, the bot builds a growing library of real-world answers from your team's expertise.

Key capabilities:

- **Automatic capture**: When a team member replies to a user question (via Discord reply or thread), the complete Q&A exchange is captured without any manual tagging or commands.
- **Multi-message handling**: Supports natural conversation flow where users ask questions across multiple messages and team members respond with detailed multi-message answers. Consecutive messages from the same author are automatically grouped.
- **Multi-turn conversations**: Captures complete conversations including follow-up questions and answers as a single coherent Q&A pair.
- **LLM-organized topic library**: Captured knowledge is automatically classified into topic-indexed documents using an LLM. New Q&A pairs are intelligently integrated with existing knowledge, and outdated information is automatically superseded when newer answers replace old ones.
- **Two-tier storage**: A raw archive preserves all original captures for audit and regeneration, while the topic-indexed library provides efficient retrieval for answering questions.
- **Knowledge base integration**: Team knowledge is seamlessly combined with static documentation when answering questions, with team knowledge taking precedence for real-world, community-tested answers.

## Documentation

See [`./docs/architecture.md`](./docs/architecture.md) for architecture and module-level documentation, plus configuration guidance.

For details on how team knowledge capture works, see [`./docs/module-team-knowledge-capture.md`](./docs/module-team-knowledge-capture.md).

## Run with Docker

Please follow [`./build/README.md`](./build/README.md) to start using Docker images.

## Start from Source Code

### 1) Create a Discord bot and enable message content intent

- Create an application + bot in the Discord Developer Portal.
- Enable **Message Content Intent** for the bot (required to read message text).
- Invite/install the bot to your server **without** requesting **View Channels** (and without **Administrator**). The bot should start with no channel visibility by default.
- After installation, Discord will create a role for the bot (for this project: **Community Intern**).
- To allow the bot to operate in a specific channel, grant the **Community Intern** role channel permissions:
  - **View Channel**
  - **Read Message History**
  - **Create Public Threads**
  - **Send Messages in Threads**

### 2) Install dependencies

```bash
$ python -m venv venv
$ ./venv/bin/activate
(venv) $ pip install -r requirements.txt
(venv) $ pip install .
```

### 3) Configure the application

**a) Create `data/config/config.yaml`**

Start from [`./examples/config.yaml`](./examples/config.yaml) and copy it to `data/config/config.yaml`.

```yaml
# Any OpenAI-compatible chat completion API could be used
ai:
  llm_base_url: "https://bridge.crynux-as.xyz/v1/llm"
  llm_model: "Qwen/Qwen2.5-7B-Instruct"
  vram_limit: 24  # Required for Crynux when using larger models: minimum GPU VRAM (GB) for inference
```

**Prompt configuration**

Prompts are configured in two sections of `data/config/config.yaml`:

The `ai` section configures the Q&A workflow:

- `project_introduction`: A shared domain introduction appended to multiple prompt steps. This text strongly influences what the bot considers in scope.
- `gating_prompt`: Decides whether the bot should reply at all.
- `selection_prompt`: Selects the most relevant sources from the knowledge base index.
- `answer_prompt`: Generates the final answer using only the selected source content.
- `verification_prompt`: Verifies the draft answer for clarity, safety, and grounding.

The `kb` section configures knowledge base indexing:

- `summarization_prompt`: Summarizes source text for the knowledge base index.
- `team_classification_prompt`: Classifies captured Q&A pairs into topics.
- `team_integration_prompt`: Integrates new Q&A pairs into existing topic files, removing obsolete entries.
- `team_summarization_prompt`: Summarizes topic files for the team knowledge index.

**Team member configuration (optional)**

To enable Team Knowledge Capture, add your team members' Discord user IDs to `config.yaml`:

```yaml
discord:
  team_member_ids:
    - "123456789012345678"
    - "234567890123456789"
```

To get a Discord user ID, enable Developer Mode in Discord (User Settings → App Settings → Advanced → Developer Mode), then right-click on a user and select "Copy User ID".

When configured, team member replies to community questions are automatically captured and added to the knowledge base. Team member messages do not trigger the AI reply workflow. See [`./docs/module-team-knowledge-capture.md`](./docs/module-team-knowledge-capture.md) for details.

**b) Create `.env` for secrets**

Create a `.env` file at `data/.env` to store sensitive keys.

```bash
APP__DISCORD__TOKEN=your_discord_bot_token
APP__AI__LLM_API_KEY=your_llm_api_key
```

Notes:

- Environment variables in `.env` override values in `config.yaml` using the `APP__` prefix (e.g., `APP__DISCORD__TOKEN` overrides `discord.token`).

### 4) Setup Knowledge Base Sources

Add your documentation to the knowledge base so the bot can answer questions.

- **Local Files**: Place text files (Markdown, .txt, etc.) in the `data/knowledge-base/sources/` directory.
- **Web Links**: List URLs in `data/knowledge-base/links.txt` (one URL per line). The bot will fetch and index the content of these pages.

### 5) Initialize Knowledge Base

Before running the bot, initialize the knowledge base index. This will scan your sources folder and fetch any web links.

```bash
(venv) $ python -m community_intern init_kb
```

### 6) Run the bot

```bash
(venv) $ python -m community_intern run
```

## LangSmith tracing

LangSmith tracing is supported for the LangGraph based Q&A workflow. The Knowledge Base indexing is not traced.

### 1) Create a LangSmith project and API key

- Create a LangSmith account and project.
- Create a LangSmith API key.

### 2) Configure environment variables

Add these to your `.env` file:

```bash
LANGSMITH_TRACING=true
LANGSMITH_API_KEY=your_langsmith_api_key
LANGSMITH_PROJECT=community-intern
```

### 3) Run the bot

Start the bot as usual. Traces should appear in your LangSmith project.
