from __future__ import annotations

from typing import Protocol

import discord

from community_intern.adapters.discord.models import GatheredContext, MessageContext


class ActionHandler(Protocol):
    async def handle(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> None:
        ...
