from __future__ import annotations

from typing import Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict

from community_intern.core.models import AIResult, Conversation, RequestContext

T = TypeVar("T", bound=BaseModel)


class AIClient:
    @property
    def project_introduction(self) -> str:
        """Return the project introduction prompt from configuration."""
        raise NotImplementedError

    async def generate_reply(self, conversation: Conversation, context: RequestContext) -> AIResult:
        """Return a single normalized decision + optional reply."""
        raise NotImplementedError

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
            response_model: Optional Pydantic model for structured output. If provided,
                           the response is validated and returned as an instance of this model.

        Returns:
            If response_model is None: plain text response (str)
            If response_model is provided: validated instance of the model
        """
        raise NotImplementedError



class AIConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # LLM Settings
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    vram_limit: int

    # Timeouts and retries
    graph_timeout_seconds: float
    llm_timeout_seconds: float
    max_retries: int

    # Workflow policy
    enable_verification: bool = False

    # Prompts and policy
    project_introduction: str = ""
    gating_prompt: str
    selection_prompt: str
    answer_prompt: str
    verification_prompt: str

    # Retrieval policy
    max_sources: int
    max_snippets: int
    max_snippet_chars: int
    min_snippet_score: float

    # Output policy
    max_answer_chars: int
