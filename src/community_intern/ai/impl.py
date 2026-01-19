import logging
import asyncio
from typing import Optional, Type, TypeVar

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_crynux import ChatCrynux
from pydantic import BaseModel

from community_intern.ai.interfaces import AIClient, AIConfig
from community_intern.core.models import AIResult, Conversation, RequestContext
from community_intern.kb.interfaces import KnowledgeBase
from community_intern.ai.graph import build_ai_graph, GraphState

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


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

        # Build and compile the graph for generate_reply
        self._app: Runnable = build_ai_graph(config)

        # Shared LLM instance for simple single-step calls
        self._llm = ChatCrynux(
            base_url=config.llm_base_url,
            api_key=config.llm_api_key,
            model=config.llm_model,
            vram_limit=config.vram_limit,
            temperature=0.0,
            request_timeout=config.llm_timeout_seconds,
            max_retries=config.max_retries,
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
        response_model: Optional[Type[T]] = None,
    ) -> str | T:
        """
        Invoke the LLM with the given prompts.

        Callers are responsible for appending project_introduction to the system prompt
        if needed. Use ai_client.project_introduction to get the configured value.

        Args:
            system_prompt: The system prompt to send to the LLM
            user_content: The user message content
            response_model: Optional Pydantic model for structured output

        Returns:
            If response_model is None: plain text response (str)
            If response_model is provided: validated instance of the model
        """
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]

        if response_model is not None:
            structured_llm = self._llm.with_structured_output(response_model)
            result = await asyncio.wait_for(
                structured_llm.ainvoke(messages),
                timeout=self._config.llm_timeout_seconds,
            )
            return result
        else:
            response = await asyncio.wait_for(
                self._llm.ainvoke(messages),
                timeout=self._config.llm_timeout_seconds,
            )
            return response.content.strip()
