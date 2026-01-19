from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.models import GatheredContext, MessageContext
from community_intern.kb.cache_utils import format_rfc3339
from community_intern.team_kb.models import Turn

if TYPE_CHECKING:
    from community_intern.team_kb.team_kb_manager import TeamKnowledgeManager

logger = logging.getLogger(__name__)


def _to_utc_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class QACaptureHandler(ActionHandler):
    def __init__(self, *, manager: "TeamKnowledgeManager") -> None:
        self._manager = manager

    async def handle(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> None:
        result = self._extract_qa_pair(message, context, gathered_context)
        if result is None:
            logger.debug(
                "No Q&A pair extracted from team member reply. message_id=%s",
                str(message.id),
            )
            return

        turns, timestamp = result

        try:
            await self._manager.capture_qa(
                turns=turns,
                timestamp=timestamp,
            )
            logger.info(
                "Captured Q&A pair from team member reply. message_id=%s turn_count=%d",
                str(message.id),
                len(turns),
            )
        except Exception:
            logger.exception("Failed to capture Q&A pair. message_id=%s", str(message.id))

    def _extract_qa_pair(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> tuple[list[Turn], str] | None:
        turns: list[Turn] = []

        for group in gathered_context.reply_chain:
            role = "user" if group.author_type == "community_user" else "team"
            for msg in group.messages:
                text = (msg.content or "").strip()
                if text:
                    turns.append(Turn(role=role, content=text))

        for msg in gathered_context.batch:
            text = (msg.content or "").strip()
            if text:
                turns.append(Turn(role="team", content=text))

        if not turns and gathered_context.reply_target_message is not None:
            target = gathered_context.reply_target_message
            target_text = (target.content or "").strip()
            if target_text:
                turns.append(Turn(role="user", content=target_text))

        has_user = any(t.role == "user" for t in turns)
        has_team = any(t.role == "team" for t in turns)
        if not has_user or not has_team:
            return None

        timestamp = format_rfc3339(_to_utc_datetime(message.created_at))
        return turns, timestamp
