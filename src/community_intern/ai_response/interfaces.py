from __future__ import annotations

from typing import Literal, Optional, Type, TypeVar

from pydantic import BaseModel, ConfigDict

from community_intern.core.models import AIResult, Conversation, RequestContext

T = TypeVar("T", bound=BaseModel)


class LLMTextResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


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
        response_model: Type[T],
    ) -> T:
        """
        Invoke the LLM with the given prompts.

        Callers are responsible for appending project_introduction to the system prompt
        if needed. Use ai_client.project_introduction to get the configured value.

        Args:
            system_prompt: The system prompt to send to the LLM
            user_content: The user message content
            response_model: Pydantic model for structured output. The response is validated
                            and returned as an instance of this model.

        Returns:
            Validated instance of the model.
        """
        raise NotImplementedError



class LLMSettings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str
    api_key: str
    model: str
    vram_limit: Optional[int] = None
    structured_output_method: Literal["json_schema", "function_calling"] = "function_calling"
    timeout_seconds: float
    max_retries: int


class AIConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    # LLM Settings
    llm: LLMSettings

    # Timeouts and retries
    graph_timeout_seconds: float

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
