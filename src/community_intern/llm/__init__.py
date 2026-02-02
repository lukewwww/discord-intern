"""Shared LLM helpers and settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from community_intern.llm.invoker import LLMInvoker
    from community_intern.llm.models import LLMTextResult
    from community_intern.llm.settings import LLMSettings

__all__ = ["LLMInvoker", "LLMSettings", "LLMTextResult"]


def __getattr__(name: str):
    if name == "LLMInvoker":
        from community_intern.llm.invoker import LLMInvoker as _LLMInvoker

        return _LLMInvoker
    if name == "LLMTextResult":
        from community_intern.llm.models import LLMTextResult as _LLMTextResult

        return _LLMTextResult
    if name == "LLMSettings":
        from community_intern.llm.settings import LLMSettings as _LLMSettings

        return _LLMSettings
    raise AttributeError(name)
