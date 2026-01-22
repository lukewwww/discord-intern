from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord

from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.classifier import MessageClassifier
from community_intern.adapters.discord.models import GatheredContext, MessageContext
from community_intern.knowledge_cache.utils import format_rfc3339
from community_intern.team_kb.models import Turn

if TYPE_CHECKING:
    from community_intern.team_kb.team_kb_manager import TeamKnowledgeManager

logger = logging.getLogger(__name__)


@dataclass
class ExtractedQA:
    turns: list[Turn]
    timestamp: str
    conversation_id: str
    message_ids: list[str]


def _to_utc_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class QACaptureHandler(ActionHandler):
    def __init__(
        self,
        *,
        manager: "TeamKnowledgeManager",
        classifier: MessageClassifier | None = None,
    ) -> None:
        self._manager = manager
        self._classifier = classifier

    def set_classifier(self, classifier: MessageClassifier) -> None:
        """Inject the classifier after initialization."""
        self._classifier = classifier

    async def handle(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> None:
        if self._classifier is None:
            logger.warning(
                "QACaptureHandler classifier not initialized. message_id=%s",
                str(message.id),
            )
            return

        result = self._extract_qa_pair(message, context, gathered_context)
        if result is None:
            logger.debug(
                "No Q&A pair extracted from team member reply. message_id=%s",
                str(message.id),
            )
            return

        try:
            await self._manager.capture_qa(
                turns=result.turns,
                timestamp=result.timestamp,
                conversation_id=result.conversation_id,
                message_ids=result.message_ids,
            )
            logger.info(
                "Captured Q&A pair from team member reply. message_id=%s turn_count=%d conversation_id=%s",
                str(message.id),
                len(result.turns),
                result.conversation_id,
            )
        except Exception:
            logger.exception("Failed to capture Q&A pair. message_id=%s", str(message.id))

    def _extract_qa_pair(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> ExtractedQA | None:
        turns: list[Turn] = []
        message_ids: list[str] = []
        conversation_id = ""

        if gathered_context.thread_history:
            turns, message_ids, conversation_id = self._extract_from_thread(
                message, gathered_context
            )
        else:
            turns, message_ids, conversation_id = self._extract_from_reply_chain(
                message, gathered_context
            )

        has_user = any(t.role == "user" for t in turns)
        has_team = any(t.role == "team" for t in turns)
        if not has_user or not has_team:
            return None

        timestamp = format_rfc3339(_to_utc_datetime(message.created_at))
        return ExtractedQA(
            turns=turns,
            timestamp=timestamp,
            conversation_id=conversation_id,
            message_ids=message_ids,
        )

    def _extract_from_thread(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
    ) -> tuple[list[Turn], list[str], str]:
        """Extract Q&A from thread history."""
        turns: list[Turn] = []
        message_ids: list[str] = []

        thread_id = str(message.channel.id)
        conversation_id = f"thread_{thread_id}"

        all_messages = list(gathered_context.thread_history)
        for msg in gathered_context.batch:
            if msg.id not in [m.id for m in all_messages]:
                all_messages.append(msg)

        all_messages.sort(key=lambda m: m.created_at)

        for msg in all_messages:
            if msg.author is None:
                continue

            author_type = self._classifier.classify_author(msg.author.id)
            if author_type == "community_user":
                role = "user"
            elif author_type == "bot":
                continue
            else:
                role = "team"
            text = (msg.content or "").strip()

            if text:
                turns.append(Turn(role=role, content=text))
                message_ids.append(str(msg.id))

        return turns, message_ids, conversation_id

    def _extract_from_reply_chain(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
    ) -> tuple[list[Turn], list[str], str]:
        """Extract Q&A from reply chain."""
        turns: list[Turn] = []
        message_ids: list[str] = []
        conversation_id = ""

        if gathered_context.reply_chain:
            first_group = gathered_context.reply_chain[0]
            if first_group.messages:
                root_msg_id = str(first_group.messages[0].id)
                conversation_id = f"reply_{root_msg_id}"

        for group in gathered_context.reply_chain:
            if group.author_type == "community_user":
                role = "user"
            elif group.author_type == "bot":
                continue
            else:
                role = "team"
            for msg in group.messages:
                text = (msg.content or "").strip()
                if text:
                    turns.append(Turn(role=role, content=text))
                    message_ids.append(str(msg.id))

        for msg in gathered_context.batch:
            text = (msg.content or "").strip()
            if text:
                if msg.author is not None and self._classifier is not None:
                    author_type = self._classifier.classify_author(msg.author.id)
                    if author_type == "bot":
                        continue
                    role = "team"
                else:
                    role = "team"
                turns.append(Turn(role=role, content=text))
                message_ids.append(str(msg.id))

        if not turns and gathered_context.reply_target_message is not None:
            target = gathered_context.reply_target_message
            target_text = (target.content or "").strip()
            if target_text:
                turns.append(Turn(role="user", content=target_text))
                message_ids.append(str(target.id))
                conversation_id = f"reply_{target.id}"

        return turns, message_ids, conversation_id
