import logging
import asyncio
from typing import Optional, Type, TypeVar

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_crynux import ChatCrynux
from pydantic import BaseModel

from community_intern.ai_response.interfaces import AIClient, AIConfig
from community_intern.core.models import AIResult, Conversation, RequestContext
from community_intern.kb.interfaces import KnowledgeBase
from community_intern.ai_response.graph import build_ai_graph, GraphState

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def _append_selected_links(reply_text: str, *, selected_source_ids: list[str]) -> str:
    links = []
    for source_id in selected_source_ids:
        raw = source_id.strip()
        if raw.startswith(("http://", "https://")):
            links.append(raw)
            continue
        if raw.startswith("kb:"):
            inner = raw[len("kb:") :].strip()
            if inner.startswith(("http://", "https://")):
                links.append(inner)

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

        # Build and compile the graph for generate_reply
        self._app: Runnable = build_ai_graph(config)

        # Shared LLM instance for simple single-step calls
        llm_config = config.llm
        self._llm = ChatCrynux(
            base_url=llm_config.base_url,
            api_key=llm_config.api_key,
            model=llm_config.model,
        # Only pass vram_limit if it is not None
        **({"vram_limit": llm_config.vram_limit} if llm_config.vram_limit is not None else {}),
            temperature=0.0,
            request_timeout=llm_config.timeout_seconds,
            max_retries=llm_config.max_retries,
        )

    @property
    def project_introduction(self) -> str:
        """Return the project introduction prompt from configuration."""
        return self._config.project_introduction

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

    async def invoke_llm(
        self,
        *,
        system_prompt: str,
        user_content: str,
        response_model: Type[T],
    ) -> T:
        """
        Invoke the LLM with the given prompts.

        Callers are responsible for appending project_introduction to the system prompt
        if needed. Use ai_client.project_introduction to get the configured value.

        Args:
            system_prompt: The system prompt to send to the LLM
            user_content: The user message content
            response_model: Pydantic model for structured output

        Returns:
            Validated instance of the model.
        """
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        structured_llm = self._llm.with_structured_output(
            response_model,
            method=self._config.llm.structured_output_method,
        )
        result = await asyncio.wait_for(
            structured_llm.ainvoke(messages),
            timeout=self._config.llm.timeout_seconds,
        )
        if result is None:
            raise RuntimeError("LLM returned null structured output.")
        try:
            return response_model.model_validate(result)
        except Exception as exc:
            raise RuntimeError(
                f"LLM returned unexpected structured output. expected={response_model.__name__} got={type(result).__name__}"
            ) from exc
