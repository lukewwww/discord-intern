from __future__ import annotations

import logging
from typing import Optional, Sequence

import discord

from community_intern.adapters.discord.models import (
    AuthorType,
    LocationType,
    MessageContext,
    MessageTarget,
)

logger = logging.getLogger(__name__)


class MessageClassifier:
    def __init__(self, *, bot_user_id: int, team_member_ids: Sequence[str]) -> None:
        self._bot_user_id = bot_user_id
        self._team_member_ids = frozenset(team_member_ids)

    def classify_author(self, author_id: int) -> AuthorType:
        if author_id == self._bot_user_id:
            return "bot"
        if str(author_id) in self._team_member_ids:
            return "team_member"
        return "community_user"

    async def classify(
        self,
        message: discord.Message,
        *,
        resolve_reference: bool = True,
    ) -> MessageContext:
        author_type = self.classify_author(message.author.id)

        location: LocationType
        thread_owner_type: Optional[AuthorType] = None

        if isinstance(message.channel, discord.Thread):
            location = "thread"
            if message.channel.owner_id is not None:
                thread_owner_type = self.classify_author(message.channel.owner_id)
        else:
            location = "channel"

        reply_target: Optional[MessageTarget] = None
        if resolve_reference and message.reference is not None and message.reference.message_id is not None:
            reply_target = await self._resolve_reply_target(message)

        return MessageContext(
            author_type=author_type,
            location=location,
            reply_target=reply_target,
            thread_owner_type=thread_owner_type,
        )

    async def _resolve_reply_target(self, message: discord.Message) -> Optional[MessageTarget]:
        reference = message.reference
        if reference is None or reference.message_id is None:
            return None

        if isinstance(reference.resolved, discord.Message):
            ref_msg = reference.resolved
        else:
            try:
                ref_msg = await message.channel.fetch_message(reference.message_id)
            except discord.NotFound:
                logger.warning(
                    "Referenced Discord message was not found. guild_id=%s channel_id=%s message_id=%s reference_id=%s",
                    str(message.guild.id) if message.guild else None,
                    str(getattr(message.channel, "id", None)),
                    str(message.id),
                    str(reference.message_id),
                )
                return None
            except discord.DiscordException:
                logger.exception(
                    "Failed to fetch referenced Discord message. guild_id=%s channel_id=%s message_id=%s reference_id=%s",
                    str(message.guild.id) if message.guild else None,
                    str(getattr(message.channel, "id", None)),
                    str(message.id),
                    str(reference.message_id),
                )
                return None

        if ref_msg.author is None:
            return None

        return MessageTarget(
            author_type=self.classify_author(ref_msg.author.id),
            author_id=str(ref_msg.author.id),
        )
