from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import discord
from discord.ext import commands

from community_intern.adapters.discord.action_router import ActionRouter
from community_intern.adapters.discord.ai_response_handler import AIResponseHandler
from community_intern.adapters.discord.classifier import MessageClassifier
from community_intern.adapters.discord.context_gatherer import ContextGatherer
from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.models import GatheredContext
from community_intern.ai.interfaces import AIClient
from community_intern.config.models import DiscordSettings

logger = logging.getLogger(__name__)


@dataclass
class _PendingBatch:
    messages: list[discord.Message]
    task: asyncio.Task[None] | None
    generation: int


class MessageRouterCog(commands.Cog):
    """
    Routes Discord events through the 3-layer architecture:
    1. Message Classification
    2. Context Gathering
    3. Action Routing

    This implements the behavior specified in docs/module-bot-integration.md.
    """

    def __init__(
        self,
        *,
        bot: commands.Bot,
        ai_client: AIClient,
        settings: DiscordSettings,
        dry_run: bool,
        qa_capture_handler: Optional[ActionHandler] = None,
    ) -> None:
        self._bot = bot
        self._ai_client = ai_client
        self._settings = settings
        self._dry_run = dry_run
        self._qa_capture_handler = qa_capture_handler

        self._classifier: Optional[MessageClassifier] = None
        self._context_gatherer: Optional[ContextGatherer] = None
        self._action_router: Optional[ActionRouter] = None

        self._pending_batches: dict[tuple[str, str, str], _PendingBatch] = {}

    @property
    def ai_client(self) -> AIClient:
        return self._ai_client

    def set_qa_capture_handler(self, handler: ActionHandler) -> None:
        self._qa_capture_handler = handler
        if self._action_router is not None and self._bot.user is not None:
            self._action_router = self._build_action_router(self._bot.user.id)

    def _initialize_components(self, bot_user_id: int) -> None:
        team_member_ids = frozenset(self._settings.team_member_ids)

        self._classifier = MessageClassifier(
            bot_user_id=bot_user_id,
            team_member_ids=self._settings.team_member_ids,
        )

        self._context_gatherer = ContextGatherer(
            classifier=self._classifier,
            batch_wait_seconds=self._settings.message_batch_wait_seconds,
        )

        self._action_router = self._build_action_router(bot_user_id)

        if self._qa_capture_handler is not None:
            if hasattr(self._qa_capture_handler, "set_classifier"):
                self._qa_capture_handler.set_classifier(self._classifier)

    def _build_action_router(self, bot_user_id: int) -> ActionRouter:
        team_member_ids = frozenset(self._settings.team_member_ids)

        ai_handler = AIResponseHandler(
            ai_client=self._ai_client,
            bot_user_id=bot_user_id,
            team_member_ids=team_member_ids,
            dry_run=self._dry_run,
        )

        return ActionRouter(
            ai_handler=ai_handler,
            qa_capture_handler=self._qa_capture_handler,
            bot_user_id=bot_user_id,
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author is not None and message.author.bot:
            return

        content = (message.content or "").strip()
        if not content:
            return

        bot_user = self._bot.user
        if bot_user is None:
            logger.warning("Discord bot user is not available. message_id=%s", getattr(message, "id", None))
            return

        if self._classifier is None:
            self._initialize_components(bot_user.id)

        assert self._classifier is not None

        context = await self._classifier.classify(message)

        if context.author_type == "bot":
            return

        if message.guild is None:
            return

        channel_id = getattr(message.channel, "id", None)
        if channel_id is None:
            return
        if message.author is None:
            return

        key = (str(message.guild.id), str(channel_id), str(message.author.id))
        self._enqueue_batch(message=message, key=key)

        await self._bot.process_commands(message)

    def _enqueue_batch(self, *, message: discord.Message, key: tuple[str, str, str]) -> None:
        pending = self._pending_batches.get(key)
        if pending is None:
            pending = _PendingBatch(messages=[], task=None, generation=0)
            self._pending_batches[key] = pending

        prior_count = len(pending.messages)
        pending.messages.append(message)
        pending.generation += 1
        generation = pending.generation

        wait_action = "start" if prior_count == 0 else "reset"
        if pending.task is not None and not pending.task.done():
            pending.task.cancel()
        pending.task = asyncio.create_task(self._flush_batch_after_wait(key=key, generation=generation))
        logger.debug(
            "Waiting to batch Discord messages. platform=discord guild_id=%s channel_id=%s author_id=%s message_id=%s batch_size=%s wait_seconds=%s action=%s generation=%s",
            key[0],
            key[1],
            key[2],
            str(message.id),
            len(pending.messages),
            self._settings.message_batch_wait_seconds,
            wait_action,
            generation,
        )

    async def _flush_batch_after_wait(self, *, key: tuple[str, str, str], generation: int) -> None:
        try:
            await asyncio.sleep(self._settings.message_batch_wait_seconds)
        except asyncio.CancelledError:
            return

        pending = self._pending_batches.get(key)
        if pending is None:
            return
        if pending.generation != generation:
            return

        messages = pending.messages
        if not messages:
            del self._pending_batches[key]
            return

        logger.debug(
            "Processing batched Discord messages. platform=discord guild_id=%s channel_id=%s author_id=%s batch_size=%s last_message_id=%s generation=%s",
            key[0],
            key[1],
            key[2],
            len(messages),
            str(messages[-1].id),
            generation,
        )
        del self._pending_batches[key]

        try:
            await self._process_batch(messages=messages)
        except Exception:
            logger.exception("Failed to process batched Discord messages. guild_id=%s channel_id=%s author_id=%s", *key)

    async def _process_batch(self, *, messages: list[discord.Message]) -> None:
        messages = [m for m in messages if (m.content or "").strip()]
        if not messages:
            return

        last_message = messages[-1]

        if self._classifier is None or self._context_gatherer is None or self._action_router is None:
            logger.warning("Router components not initialized.")
            return

        context = await self._classifier.classify(last_message)

        gathered_context = await self._context_gatherer.gather(
            batch=messages,
            message=last_message,
        )

        await self._action_router.route(last_message, context, gathered_context)
