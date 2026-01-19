from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Type, TypeVar

from pydantic import BaseModel

from community_intern.core.models import AIResult, Conversation, RequestContext

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class MockAIClient:
    """
    A deterministic AI client for end-to-end adapter testing.

    For any input conversation, returns a fixed reply text.
    """

    reply_text: str = (
        "Mock AI response: thanks for your message. "
        "This is a fixed reply used to test the Discord adapter end-to-end."
    )

    @property
    def project_introduction(self) -> str:
        """Return empty project introduction for testing."""
        return ""

    async def generate_reply(self, conversation: Conversation, context: RequestContext) -> AIResult:
        return AIResult(
            should_reply=True,
            reply_text=self.reply_text,
            debug={
                "mock": True,
                "message_count": len(conversation.messages),
                "platform": context.platform,
            },
        )

    async def invoke_llm(
        self,
        *,
        system_prompt: str,
        user_content: str,
        response_model: Optional[Type[T]] = None,
    ) -> str | T:
        """Mock LLM invocation that returns dummy responses."""
        if response_model is not None:
            return response_model.model_construct()
        return "Mock LLM response"
