from __future__ import annotations

import logging
from datetime import timedelta
from typing import Optional

import discord

from community_intern.adapters.discord.classifier import MessageClassifier
from community_intern.adapters.discord.models import GatheredContext, MessageGroup

logger = logging.getLogger(__name__)


class ContextGatherer:
    def __init__(
        self,
        *,
        classifier: MessageClassifier,
        grouping_window_seconds: float,
        max_reply_chain_depth: int = 10,
    ) -> None:
        self._classifier = classifier
        self._grouping_window_seconds = grouping_window_seconds
        self._max_reply_chain_depth = max_reply_chain_depth

    async def gather(
        self,
        *,
        batch: list[discord.Message],
        message: discord.Message,
    ) -> GatheredContext:
        thread_history: list[discord.Message] = []
        if isinstance(message.channel, discord.Thread):
            thread_history = await self._fetch_thread_history(message.channel)

        reply_chain: list[MessageGroup] = []
        reply_target_message: Optional[discord.Message] = None

        if message.reference is not None and message.reference.message_id is not None:
            reply_target_message, reply_chain = await self._resolve_reply_chain(message)

        return GatheredContext(
            batch=batch,
            thread_history=thread_history,
            reply_chain=reply_chain,
            reply_target_message=reply_target_message,
        )

    async def _fetch_thread_history(self, thread: discord.Thread) -> list[discord.Message]:
        try:
            history = [m async for m in thread.history(limit=None, oldest_first=True)]
        except discord.DiscordException:
            logger.exception(
                "Failed to load Discord thread history. thread_id=%s",
                str(thread.id),
            )
            return []

        extra: list[discord.Message] = []
        extra.extend(await self._fetch_thread_starter_context(thread))
        extra.extend(await self._fetch_reply_reference_context(history))

        by_id: dict[int, discord.Message] = {m.id: m for m in history}
        for msg in extra:
            by_id.setdefault(msg.id, msg)

        merged = list(by_id.values())
        merged.sort(key=lambda m: m.created_at)
        return merged

    async def _fetch_thread_starter_context(self, thread: discord.Thread) -> list[discord.Message]:
        starter_id = getattr(thread, "message_id", None)
        parent = getattr(thread, "parent", None)
        if starter_id is None or parent is None:
            return []

        try:
            starter = await parent.fetch_message(starter_id)
        except discord.NotFound:
            logger.warning(
                "Thread starter message not found. thread_id=%s starter_id=%s",
                str(thread.id),
                str(starter_id),
            )
            return []
        except discord.Forbidden:
            logger.warning(
                "Forbidden from fetching thread starter message. thread_id=%s starter_id=%s",
                str(thread.id),
                str(starter_id),
            )
            return []
        except discord.DiscordException:
            logger.exception(
                "Failed to fetch thread starter message. thread_id=%s starter_id=%s",
                str(thread.id),
                str(starter_id),
            )
            return []

        starter_group = await self._expand_consecutive_messages(starter)
        collected: list[discord.Message] = list(starter_group.messages)

        if starter.reference is not None and starter.reference.message_id is not None:
            _, chain = await self._resolve_reply_chain(starter)
            for group in chain:
                collected.extend(group.messages)

        return collected

    async def _fetch_reply_reference_context(
        self,
        messages: list[discord.Message],
    ) -> list[discord.Message]:
        extra: list[discord.Message] = []
        processed: set[int] = set()

        for msg in messages:
            if msg.id in processed:
                continue
            processed.add(msg.id)

            reference = msg.reference
            if reference is None or reference.message_id is None:
                continue

            _, chain = await self._resolve_reply_chain(msg)
            for group in chain:
                extra.extend(group.messages)

        return extra

    async def _resolve_reply_chain(
        self,
        message: discord.Message,
    ) -> tuple[Optional[discord.Message], list[MessageGroup]]:
        chain: list[MessageGroup] = []
        current_msg = message
        depth = 0

        while depth < self._max_reply_chain_depth:
            reference = current_msg.reference
            if reference is None or reference.message_id is None:
                break

            ref_msg = await self._fetch_referenced_message(current_msg, reference)
            if ref_msg is None:
                break

            if depth == 0:
                reply_target_message = ref_msg
            else:
                reply_target_message = None

            group = await self._expand_consecutive_messages(ref_msg)
            chain.insert(0, group)

            current_msg = group.messages[0]
            depth += 1

        if depth == 0:
            return None, []

        chain.reverse()
        chain.reverse()

        return reply_target_message if depth > 0 else None, chain

    async def _fetch_referenced_message(
        self,
        message: discord.Message,
        reference: discord.MessageReference,
    ) -> Optional[discord.Message]:
        if isinstance(reference.resolved, discord.Message):
            return reference.resolved

        if reference.message_id is None:
            return None

        try:
            target_channel = message.channel

            ref_channel_id = getattr(reference, "channel_id", None)
            current_channel_id = getattr(message.channel, "id", None)
            if ref_channel_id is not None and current_channel_id is not None and ref_channel_id != current_channel_id:
                target_channel = None
                if message.guild is not None:
                    target_channel = message.guild.get_channel(ref_channel_id)
                    if target_channel is None:
                        try:
                            target_channel = await message.guild.fetch_channel(ref_channel_id)
                        except discord.Forbidden:
                            logger.warning(
                                "Forbidden from fetching referenced channel. message_id=%s reference_channel_id=%s",
                                str(message.id),
                                str(ref_channel_id),
                            )
                            return None
                        except discord.DiscordException:
                            logger.exception(
                                "Failed to fetch referenced channel. message_id=%s reference_channel_id=%s",
                                str(message.id),
                                str(ref_channel_id),
                            )
                            return None

                if target_channel is None:
                    logger.warning(
                        "Referenced channel not available. message_id=%s reference_channel_id=%s",
                        str(message.id),
                        str(ref_channel_id),
                    )
                    return None

            if not hasattr(target_channel, "fetch_message"):
                logger.warning(
                    "Referenced channel cannot fetch messages. message_id=%s reference_id=%s",
                    str(message.id),
                    str(reference.message_id),
                )
                return None

            return await target_channel.fetch_message(reference.message_id)
        except discord.NotFound:
            logger.warning(
                "Referenced message not found. message_id=%s reference_id=%s",
                str(message.id),
                str(reference.message_id),
            )
            return None
        except discord.Forbidden:
            logger.warning(
                "Forbidden from fetching referenced message. message_id=%s reference_id=%s",
                str(message.id),
                str(reference.message_id),
            )
            return None
        except discord.DiscordException:
            logger.exception(
                "Failed to fetch referenced message. message_id=%s reference_id=%s",
                str(message.id),
                str(reference.message_id),
            )
            return None

    async def _expand_consecutive_messages(
        self,
        ref_msg: discord.Message,
    ) -> MessageGroup:
        if ref_msg.author is None:
            author_type = self._classifier.classify_author(0)
            return MessageGroup(
                author_id="unknown",
                author_type=author_type,
                messages=[ref_msg],
            )

        author_id = ref_msg.author.id
        author_type = self._classifier.classify_author(author_id)
        messages: list[discord.Message] = [ref_msg]

        quiet_window = timedelta(seconds=self._grouping_window_seconds)

        before_msgs = await self._fetch_messages_before(ref_msg, author_id, quiet_window)
        messages = before_msgs + messages

        after_msgs = await self._fetch_messages_after(ref_msg, author_id, quiet_window)
        messages = messages + after_msgs

        return MessageGroup(
            author_id=str(author_id),
            author_type=author_type,
            messages=messages,
        )

    async def _fetch_messages_before(
        self,
        ref_msg: discord.Message,
        author_id: int,
        quiet_window: timedelta,
    ) -> list[discord.Message]:
        before_msgs: list[discord.Message] = []
        try:
            async for msg in ref_msg.channel.history(
                limit=20,
                before=ref_msg,
                oldest_first=False,
            ):
                if msg.author is None or msg.author.id != author_id:
                    break
                time_diff = ref_msg.created_at - msg.created_at
                if time_diff > quiet_window:
                    break
                before_msgs.insert(0, msg)
        except discord.DiscordException:
            logger.exception("Failed to fetch messages before reference.")
        return before_msgs

    async def _fetch_messages_after(
        self,
        ref_msg: discord.Message,
        author_id: int,
        quiet_window: timedelta,
    ) -> list[discord.Message]:
        after_msgs: list[discord.Message] = []
        try:
            async for msg in ref_msg.channel.history(
                limit=20,
                after=ref_msg,
                oldest_first=True,
            ):
                if msg.author is None or msg.author.id != author_id:
                    break
                time_diff = msg.created_at - ref_msg.created_at
                if time_diff > quiet_window:
                    break
                after_msgs.append(msg)
        except discord.DiscordException:
            logger.exception("Failed to fetch messages after reference.")
        return after_msgs
