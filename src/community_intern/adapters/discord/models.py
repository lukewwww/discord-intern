from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Sequence

import discord

AuthorType = Literal["community_user", "team_member", "bot"]
LocationType = Literal["channel", "thread"]


@dataclass(frozen=True, slots=True)
class MessageTarget:
    author_type: AuthorType
    author_id: str


@dataclass(frozen=True, slots=True)
class MessageContext:
    author_type: AuthorType
    location: LocationType
    reply_target: Optional[MessageTarget]
    thread_owner_type: Optional[AuthorType]


@dataclass(slots=True)
class MessageGroup:
    author_id: str
    author_type: AuthorType
    messages: list[discord.Message] = field(default_factory=list)


@dataclass(slots=True)
class GatheredContext:
    batch: list[discord.Message]
    thread_history: list[discord.Message]
    reply_chain: list[MessageGroup]
    reply_target_message: Optional[discord.Message]
