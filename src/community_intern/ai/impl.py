import logging
import asyncio
from typing import Optional

import aiohttp
from langchain_core.runnables import Runnable

from community_intern.ai.interfaces import AIClient, AIConfig
from community_intern.core.models import AIResult, Conversation, RequestContext
from community_intern.kb.interfaces import KnowledgeBase
from community_intern.ai.graph import build_ai_graph, GraphState

logger = logging.getLogger(__name__)

def _append_selected_links(reply_text: str, *, selected_source_ids: list[str]) -> str:
    links = []
    for source_id in selected_source_ids:
        if source_id.startswith(("http://", "https://")):
            links.append(source_id)

    if not links:
        return reply_text

    lines = [reply_text.rstrip(), "", "Links:"]
    for link in links:
        lines.append(f"- {link}")
    return "\n".join(lines).strip()

class AIClientImpl(AIClient):
    def __init__(self, config: AIConfig, kb: Optional[KnowledgeBase] = None):
        self._config = config
        self._kb = kb

        # Build and compile the graph once at startup
        self._app: Runnable = build_ai_graph(config)

    def set_kb(self, kb: KnowledgeBase) -> None:
        """
        Inject KnowledgeBase after initialization if needed to resolve circular dependencies.
        """
        self._kb = kb

    async def generate_reply(self, conversation: Conversation, context: RequestContext) -> AIResult:
        if not self._kb:
            logger.warning("Knowledge base is not configured, skipping AI reply generation.")
            return AIResult(should_reply=False, reply_text=None)

        initial_state: GraphState = {
            "conversation": conversation,
            "context": context,
            "config": self._config,
            "kb": self._kb,
            "user_question": "",
            "kb_index_text": "",
            "selected_source_ids": [],
            "loaded_sources": [],
            "draft_answer": "",
            "verification": None,
            "should_reply": False,
            "final_reply_text": None
        }

        try:
            # Reusing the compiled self._app is thread-safe and supports concurrency
            final_state = await asyncio.wait_for(
                self._app.ainvoke(initial_state),
                timeout=self._config.graph_timeout_seconds
            )

            reply_text = final_state.get("final_reply_text")
            if reply_text:
                reply_text = _append_selected_links(
                    reply_text,
                    selected_source_ids=list(final_state.get("selected_source_ids", [])),
                )

            return AIResult(
                should_reply=final_state.get("should_reply", False),
                reply_text=reply_text,
                debug={
                    "verification": final_state.get("verification")
                }
            )
        except asyncio.TimeoutError:
            logger.warning("AI graph timed out while generating a reply.")
            return AIResult(should_reply=False, reply_text=None)
        except Exception:
            logger.exception("AI reply generation failed.")
            return AIResult(should_reply=False, reply_text=None)

    async def summarize_for_kb_index(
        self,
        *,
        source_id: str,
        text: str,
    ) -> str:
        """
        Summarize text for the Knowledge Base index using the LLM.

        This is implemented as a direct LLM call rather than a graph
        because it is a single-step transformation without complex control flow.
        """
        if not text.strip():
            return ""

        url = f"{self._config.llm_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._config.llm_api_key}",
            "Content-Type": "application/json",
        }

        messages = []
        if self._config.project_introduction.strip():
            messages.append(
                {
                    "role": "system",
                    "content": f"Project introduction:\n{self._config.project_introduction.strip()}",
                }
            )
        if self._config.summarization_prompt:
            messages.append({"role": "system", "content": self._config.summarization_prompt})
        messages.append({"role": "user", "content": text})

        payload = {
            "model": self._config.llm_model,
            "messages": messages,
            "temperature": 0.0,
        }

        async def _make_request():
            last_error = None
            for attempt in range(self._config.max_retries + 1):
                try:
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            url,
                            headers=headers,
                            json=payload,
                            timeout=self._config.llm_timeout_seconds
                        ) as resp:
                            if resp.status == 200:
                                data = await resp.json()
                                content = data["choices"][0]["message"]["content"]
                                return content.strip()

                            response_text = await resp.text()
                            # Retry on rate limits (429) or server errors (5xx)
                            if resp.status == 429 or 500 <= resp.status < 600:
                                logger.warning(
                                    "LLM summarization request failed and will be retried. source_id=%s status=%s attempt=%s",
                                    source_id,
                                    resp.status,
                                    attempt + 1,
                                )
                                last_error = RuntimeError(f"HTTP {resp.status}: {response_text}")
                            else:
                                # Do not retry on other client errors (400, 401, 403, etc.)
                                logger.error(
                                    "LLM summarization request failed with a non-retryable status. source_id=%s status=%s",
                                    source_id,
                                    resp.status,
                                )
                                raise RuntimeError(f"HTTP {resp.status}: {response_text}")

                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    logger.warning(
                        "LLM summarization request encountered a network error and will be retried. source_id=%s error=%s attempt=%s",
                        source_id,
                        str(e),
                        attempt + 1,
                    )
                    last_error = e

                # Exponential backoff if we are going to retry
                if attempt < self._config.max_retries:
                    await asyncio.sleep(0.5 * (2 ** attempt))

            if last_error:
                raise last_error
            raise RuntimeError("Max retries exceeded")

        try:
            logger.info("Starting LLM summarization for source_id=%s", source_id)
            start_time = asyncio.get_running_loop().time()
            result = await _make_request()
            duration = asyncio.get_running_loop().time() - start_time
            logger.info("LLM summarization completed. source_id=%s duration=%.2fs", source_id, duration)
            return result
        except Exception:
            logger.exception("Failed to summarize source for knowledge base index. source_id=%s", source_id)
            raise
