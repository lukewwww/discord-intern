"""Team Knowledge Capture module for capturing Q&A from team member replies."""

from __future__ import annotations

from typing import TYPE_CHECKING

from community_intern.team_kb.models import QAPair, TopicEntry, Turn

if TYPE_CHECKING:
    from community_intern.team_kb.capture_handler import QACaptureHandler
    from community_intern.team_kb.team_kb_manager import TeamKnowledgeManager

__all__ = [
    "QACaptureHandler",
    "QAPair",
    "TeamKnowledgeManager",
    "TopicEntry",
    "Turn",
]


def __getattr__(name: str):
    if name == "QACaptureHandler":
        from community_intern.team_kb.capture_handler import QACaptureHandler as _QACaptureHandler

        return _QACaptureHandler
    if name == "TeamKnowledgeManager":
        from community_intern.team_kb.team_kb_manager import TeamKnowledgeManager as _TeamKnowledgeManager

        return _TeamKnowledgeManager
    raise AttributeError(name)
