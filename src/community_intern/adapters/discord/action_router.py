from __future__ import annotations

import logging
from typing import Literal, Optional

import discord

from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.models import GatheredContext, MessageContext

logger = logging.getLogger(__name__)

RoutingDecision = Literal["ai_response", "qa_capture", "ignored"]


class ActionRouter:
    def __init__(
        self,
        *,
        ai_handler: ActionHandler,
        qa_capture_handler: Optional[ActionHandler] = None,
        bot_user_id: int,
    ) -> None:
        self._ai_handler = ai_handler
        self._qa_capture_handler = qa_capture_handler
        self._bot_user_id = bot_user_id

    def determine_routing(self, context: MessageContext) -> RoutingDecision:
        if context.author_type == "bot":
            return "ignored"

        if context.author_type == "community_user":
            return self._route_community_user(context)

        if context.author_type == "team_member":
            return self._route_team_member(context)

        return "ignored"

    def _route_community_user(self, context: MessageContext) -> RoutingDecision:
        if context.location == "channel":
            if context.reply_target is None:
                return "ai_response"
            if context.reply_target.author_id == str(self._bot_user_id):
                return "ai_response"
            return "ignored"

        if context.location == "thread":
            if context.thread_owner_type == "bot":
                return "ai_response"
            return "ignored"

        return "ignored"

    def _route_team_member(self, context: MessageContext) -> RoutingDecision:
        if context.reply_target is not None:
            if context.reply_target.author_type == "community_user":
                return "qa_capture"
            return "ignored"

        if context.location == "thread":
            return "qa_capture"

        return "ignored"

    async def route(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> RoutingDecision:
        routing = self.determine_routing(context)

        log_context = self._build_log_context(message, context)

        if routing == "ignored":
            logger.debug("Message ignored by router. %s", log_context)
            return routing

        if routing == "ai_response":
            logger.debug("Routing to AI handler. %s", log_context)
            await self._ai_handler.handle(message, context, gathered_context)
            return routing

        if routing == "qa_capture":
            if self._qa_capture_handler is None:
                logger.debug("QA capture handler not configured, ignoring. %s", log_context)
                return "ignored"
            logger.debug("Routing to QA capture handler. %s", log_context)
            await self._qa_capture_handler.handle(message, context, gathered_context)
            return routing

        return routing

    def _build_log_context(self, message: discord.Message, context: MessageContext) -> str:
        guild_id = str(message.guild.id) if message.guild else None
        channel_id = str(getattr(message.channel, "id", None))
        return (
            f"author_type={context.author_type} "
            f"location={context.location} "
            f"guild_id={guild_id} "
            f"channel_id={channel_id} "
            f"message_id={message.id}"
        )
