# Community Intern

Community Intern is an AI and LLM powered Discord FAQ assistant that monitors selected channels, detects questions, and drafts source grounded answers using a managed knowledge base built from local files and online links.

## What it does

- Watches all readable Discord channels for question-like messages
- Uses an LLM to decide whether a message is answerable and in scope, and skips messages that are not
- Uses an LLM to select relevant sources from a managed knowledge base built from local files and web links
- Uses an LLM to draft an answer grounded only in the selected source content
- Creates a thread from the triggering message and replies inside that thread
- Supports follow up questions by replying again when a thread continues, using the full thread context

## Key features

- **AI-generated, source-grounded answers**: An LLM generates answers from your documentation sources and can include citations back to those sources.
- **Guided retrieval for reliable source selection**: Uses an LLM-guided selection flow (instead of embedding-only RAG) to pick the best sources for short or ambiguous questions.
- **Knowledge base from files and links**: Uses a local folder of text sources and can incorporate web pages referenced by links, including JavaScript-rendered pages that require a headless browser to fully load before content extraction.
- **Token-efficient web ingestion**: After fetching web pages, the HTML content is cleaned to keep only key content, reducing LLM token usage during indexing and answering.
- **Incremental updates and auto-refresh**: Automatically monitors and updates the knowledge base when local files or web sources change. Uses a persistent cache to process only changed content, avoiding redundant LLM summarization and network requests.
- **Bring your own LLM**: Choose which LLM provider and model to use via configuration.
- **Thread-first replies**: Answers live in message-backed threads rather than cluttering the channel.
- **Configurable scope**: Communities can tune what kinds of questions are considered answerable without changing code.

## Documentation

See [`./docs/architecture.md`](./docs//architecture.md) for architecture and module-level documentation, plus configuration guidance.

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
```

**Prompt configuration**

The AI module is configured under the `ai` section in `data/config/config.yaml`. The key prompt inputs are:

- `project_introduction`: A shared domain introduction that is appended to multiple prompt steps. Keep it accurate, concise, and written as a stable project specification. This text strongly influences what the bot considers in scope and how it explains concepts.
- `gating_prompt`: Decides whether the bot should reply at all.
- `selection_prompt`: Selects the most relevant sources from the knowledge base index.
- `answer_prompt`: Generates the final answer using only the selected source content.
- `verification_prompt`: Verifies the draft answer for clarity, safety, and grounding.
- `summarization_prompt`: Summarizes source text for the knowledge base index.

The runtime enforces output format requirements for gating, selection, and verification in code. Keep your prompts focused on task intent rather than JSON schemas. For full details, see [`./docs/module-ai-response.md`](./docs/module-ai-response.md).

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
