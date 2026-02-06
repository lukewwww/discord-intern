from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Iterable, TypeVar

import aiohttp
import discord

from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.models import GatheredContext, MessageContext
from community_intern.adapters.discord.utils import (
    download_image_inputs,
    extract_attachment_inputs,
    extract_image_inputs,
)
from community_intern.ai_response import AIResponseService
from community_intern.core.models import AttachmentInput, Conversation, ImageInput, Message, RequestContext

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _to_utc_datetime(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def _normalize_messages(
    messages: Iterable[discord.Message],
    *,
    bot_user_id: int,
    team_member_ids: frozenset[str],
    llm_enable_image: bool,
    image_download_timeout_seconds: float,
    image_download_max_retries: int,
) -> list[Message]:
    out: list[Message] = []
    for m in messages:
        text = (m.content or "").strip()
        images: list[ImageInput] = []
        attachments: list[AttachmentInput] = []
        if llm_enable_image:
            images = await download_image_inputs(
                extract_image_inputs(m),
                timeout_seconds=image_download_timeout_seconds,
                max_retries=image_download_max_retries,
            )
        attachments = extract_attachment_inputs(m, include_images=llm_enable_image)
        if not text and not images and not attachments:
            continue
        if m.author is None:
            role = "user"
        elif m.author.id == bot_user_id:
            role = "assistant"
        elif str(m.author.id) in team_member_ids:
            role = "assistant"
        else:
            role = "user"
        out.append(
            Message(
                role=role,
                text=text,
                timestamp=_to_utc_datetime(m.created_at),
                author_id=str(m.author.id) if m.author is not None else None,
                images=images or None,
                attachments=attachments or None,
            )
        )
    return out


def _thread_name_from_message(text: str) -> str:
    base = text.strip().replace("\n", " ")
    if not base:
        return "FAQ Answer"
    base = base[:80]
    return f"FAQ: {base}"


_RETRYABLE_DISCORD_HTTP_ERRORS: tuple[type[BaseException], ...] = (
    aiohttp.ClientConnectorError,
    aiohttp.ClientOSError,
    aiohttp.ServerDisconnectedError,
    asyncio.TimeoutError,
    ConnectionResetError,
)


async def _retry_async(
    operation: str,
    *,
    attempts: int,
    base_delay_seconds: float,
    make_call: Callable[[], Awaitable[T]],
    log_context: str,
) -> T:
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await make_call()
        except _RETRYABLE_DISCORD_HTTP_ERRORS as exc:
            last_error = exc
            if attempt >= attempts:
                break
            delay_seconds = base_delay_seconds * (2 ** (attempt - 1))
            logger.warning(
                "Retrying Discord HTTP request. operation=%s attempt=%s/%s delay_seconds=%s %s error=%s",
                operation,
                attempt,
                attempts,
                delay_seconds,
                log_context,
                type(exc).__name__,
            )
            await asyncio.sleep(delay_seconds)

    assert last_error is not None
    raise last_error


class AIResponseHandler(ActionHandler):
    def __init__(
        self,
        *,
        ai_client: AIResponseService,
        bot_user_id: int,
        team_member_ids: frozenset[str],
        dry_run: bool,
        llm_enable_image: bool,
        image_download_timeout_seconds: float,
        image_download_max_retries: int,
    ) -> None:
        self._ai_client = ai_client
        self._bot_user_id = bot_user_id
        self._team_member_ids = team_member_ids
        self._dry_run = dry_run
        self._llm_enable_image = llm_enable_image
        self._image_download_timeout_seconds = image_download_timeout_seconds
        self._image_download_max_retries = image_download_max_retries

    async def handle(
        self,
        message: discord.Message,
        context: MessageContext,
        gathered_context: GatheredContext,
    ) -> None:
        if context.location == "thread":
            await self._handle_thread(message, gathered_context)
        else:
            await self._handle_channel(message, gathered_context)

    async def _handle_channel(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
    ) -> None:
        messages = gathered_context.batch
        messages = [
            m
            for m in messages
            if _message_has_text_or_attachments(m, llm_enable_image=self._llm_enable_image)
        ]
        if not messages:
            return

        last_message = messages[-1]
        if last_message.guild is None:
            return

        channel_id = getattr(last_message.channel, "id", None)
        if channel_id is None:
            return

        try:
            conversation_messages = await _normalize_messages(
                messages,
                bot_user_id=self._bot_user_id,
                team_member_ids=self._team_member_ids,
                llm_enable_image=self._llm_enable_image,
                image_download_timeout_seconds=self._image_download_timeout_seconds,
                image_download_max_retries=self._image_download_max_retries,
            )
        except Exception:
            logger.exception(
                "Failed to download image attachments for AI response. message_id=%s",
                str(last_message.id),
            )
            return
        if not conversation_messages:
            return

        conversation = Conversation(messages=tuple(conversation_messages))
        request_context = RequestContext(
            platform="discord",
            guild_id=str(last_message.guild.id),
            channel_id=str(channel_id),
            thread_id=None,
            message_id=str(last_message.id),
        )

        started = time.perf_counter()
        try:
            result = await self._ai_client.generate_reply(conversation=conversation, context=request_context)
        except Exception:
            logger.exception(
                "AI request failed. platform=discord guild_id=%s channel_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.message_id,
            )
            return
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "AI request completed. platform=discord routing=channel_message guild_id=%s channel_id=%s message_id=%s latency_ms=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.message_id,
                elapsed_ms,
            )

        if not result.should_reply or not result.reply_text:
            return

        if self._dry_run:
            logger.info(
                "Dry run enabled, skipping Discord reply. platform=discord guild_id=%s channel_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.message_id,
            )
            return

        await self._create_thread_and_reply(last_message, result.reply_text, request_context)

    async def _handle_thread(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
    ) -> None:
        if not isinstance(message.channel, discord.Thread):
            return

        thread = message.channel
        history = gathered_context.thread_history if gathered_context.thread_history else []

        is_eligible = any(m.author is not None and m.author.id == self._bot_user_id for m in history)
        if not is_eligible:
            return

        try:
            normalized = await _normalize_messages(
                history,
                bot_user_id=self._bot_user_id,
                team_member_ids=self._team_member_ids,
                llm_enable_image=self._llm_enable_image,
                image_download_timeout_seconds=self._image_download_timeout_seconds,
                image_download_max_retries=self._image_download_max_retries,
            )
        except Exception:
            logger.exception(
                "Failed to download image attachments for AI response. message_id=%s",
                str(message.id),
            )
            return
        if not normalized:
            return

        channel_id = str(thread.parent_id) if thread.parent_id is not None else str(thread.id)
        request_context = RequestContext(
            platform="discord",
            guild_id=str(thread.guild.id) if thread.guild is not None else None,
            channel_id=channel_id,
            thread_id=str(thread.id),
            message_id=str(message.id),
        )

        conversation = Conversation(messages=normalized)

        started = time.perf_counter()
        try:
            result = await self._ai_client.generate_reply(conversation=conversation, context=request_context)
        except Exception:
            logger.exception(
                "AI request failed. platform=discord guild_id=%s channel_id=%s thread_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.thread_id,
                request_context.message_id,
            )
            return
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "AI request completed. platform=discord routing=thread_update guild_id=%s channel_id=%s thread_id=%s message_id=%s latency_ms=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.thread_id,
                request_context.message_id,
                elapsed_ms,
            )

        if not result.should_reply or not result.reply_text:
            return

        if self._dry_run:
            logger.info(
                "Dry run enabled, skipping Discord reply. platform=discord guild_id=%s channel_id=%s thread_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.thread_id,
                request_context.message_id,
            )
            return

        await self._post_thread_reply(thread, result.reply_text, request_context)

    async def _create_thread_and_reply(
        self,
        message: discord.Message,
        reply_text: str,
        request_context: RequestContext,
    ) -> None:
        thread_name = _thread_name_from_message(message.content or "")
        log_context = (
            f"platform=discord guild_id={request_context.guild_id} "
            f"channel_id={request_context.channel_id} message_id={request_context.message_id}"
        )
        try:
            thread = await _retry_async(
                "create_thread",
                attempts=3,
                base_delay_seconds=0.5,
                make_call=lambda: message.create_thread(name=thread_name),
                log_context=log_context,
            )
        except _RETRYABLE_DISCORD_HTTP_ERRORS:
            logger.exception("Giving up on Discord thread creation after retries. %s", log_context)
            return
        except discord.DiscordException:
            logger.exception(
                "Failed to create Discord thread. platform=discord guild_id=%s channel_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                request_context.message_id,
            )
            return

        try:
            await _retry_async(
                "post_message",
                attempts=3,
                base_delay_seconds=0.5,
                make_call=lambda: thread.send(reply_text),
                log_context=f"{log_context} thread_id={thread.id}",
            )
        except _RETRYABLE_DISCORD_HTTP_ERRORS:
            logger.exception(
                "Giving up on posting to Discord thread after retries. %s thread_id=%s",
                log_context,
                str(thread.id),
            )
            return
        except discord.DiscordException:
            logger.exception(
                "Failed to post message to Discord thread. platform=discord guild_id=%s channel_id=%s thread_id=%s message_id=%s",
                request_context.guild_id,
                request_context.channel_id,
                str(thread.id),
                request_context.message_id,
            )
            return

        logger.info(
            "Posted reply to Discord thread. platform=discord routing=channel_message guild_id=%s channel_id=%s thread_id=%s message_id=%s",
            request_context.guild_id,
            request_context.channel_id,
            str(thread.id),
            request_context.message_id,
        )

    async def _post_thread_reply(
        self,
        thread: discord.Thread,
        reply_text: str,
        request_context: RequestContext,
    ) -> None:
        log_context = (
            f"platform=discord guild_id={request_context.guild_id} channel_id={request_context.channel_id} "
            f"thread_id={request_context.thread_id} message_id={request_context.message_id}"
        )
        try:
            await _retry_async(
                "post_message",
                attempts=3,
                base_delay_seconds=0.5,
                make_call=lambda: thread.send(reply_text),
                log_context=log_context,
            )
        except _RETRYABLE_DISCORD_HTTP_ERRORS:
            logger.exception(
                "Giving up on posting to Discord thread after retries. %s",
                log_context,
            )
            return
        except discord.DiscordException:
            logger.exception(
                "Failed to post message to Discord thread. %s",
                log_context,
            )
            return

        logger.info(
            "Posted reply to Discord thread. platform=discord routing=thread_update guild_id=%s channel_id=%s thread_id=%s message_id=%s",
            request_context.guild_id,
            request_context.channel_id,
            request_context.thread_id,
            request_context.message_id,
        )


def _message_has_text_or_attachments(
    message: discord.Message,
    *,
    llm_enable_image: bool,
) -> bool:
    if (message.content or "").strip():
        return True
    attachments = extract_attachment_inputs(message, include_images=llm_enable_image)
    return bool(attachments)
