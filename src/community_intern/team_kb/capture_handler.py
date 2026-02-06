from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import discord
from pydantic import BaseModel, Field

from community_intern.adapters.discord.handlers import ActionHandler
from community_intern.adapters.discord.classifier import MessageClassifier
from community_intern.adapters.discord.models import GatheredContext, MessageContext
from community_intern.adapters.discord.utils import (
    extract_image_inputs,
    download_image_inputs,
    is_image_attachment,
)
from community_intern.core.formatters import format_attachment_placeholder
from community_intern.llm.prompts import compose_system_prompt
from community_intern.core.models import ImageInput
from community_intern.knowledge_cache.utils import format_rfc3339
from community_intern.team_kb.models import Turn

if TYPE_CHECKING:
    from community_intern.team_kb.team_kb_manager import TeamKnowledgeManager

logger = logging.getLogger(__name__)


class ImageSummaryItem(BaseModel):
    message_id: str = Field(description="Discord message ID tied to the image input")
    image_index: int = Field(description="1-based index of the image within the message")
    summary: str = Field(description="Summary of the key information from the image")


class ImageSummaryResult(BaseModel):
    summaries: list[ImageSummaryItem] = Field(default_factory=list)


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
        llm_enable_image: bool,
        image_download_timeout_seconds: float,
        image_download_max_retries: int,
        classifier: MessageClassifier | None = None,
    ) -> None:
        self._manager = manager
        self._classifier = classifier
        self._llm_enable_image = llm_enable_image
        self._image_download_timeout_seconds = image_download_timeout_seconds
        self._image_download_max_retries = image_download_max_retries

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

        context_messages = self._collect_context_messages(message, gathered_context)
        try:
            image_summaries = await self._summarize_images(context_messages)
        except Exception:
            logger.exception(
                "Image summarization failed; aborting capture. message_id=%s",
                str(message.id),
            )
            return

        result = self._extract_qa_pair(message, context, gathered_context, image_summaries)
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
        image_summaries: dict[str, list[tuple[int, str]]],
    ) -> ExtractedQA | None:
        turns: list[Turn] = []
        message_ids: list[str] = []
        conversation_id = ""

        if gathered_context.thread_history:
            turns, message_ids, conversation_id = self._extract_from_thread(
                message, gathered_context, image_summaries
            )
        else:
            turns, message_ids, conversation_id = self._extract_from_reply_chain(
                message, gathered_context, image_summaries
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
        image_summaries: dict[str, list[tuple[int, str]]],
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
                role = "bot"
            else:
                role = "team"
            summaries = image_summaries.get(str(msg.id), [])
            text = _build_message_text_with_summaries(msg, summaries=summaries)
            if text:
                turns.append(Turn(role=role, content=text))
                message_ids.append(str(msg.id))

        return turns, message_ids, conversation_id

    def _extract_from_reply_chain(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
        image_summaries: dict[str, list[tuple[int, str]]],
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
                role = "bot"
            else:
                role = "team"
            for msg in group.messages:
                summaries = image_summaries.get(str(msg.id), [])
                text = _build_message_text_with_summaries(msg, summaries=summaries)
                if text:
                    turns.append(Turn(role=role, content=text))
                    message_ids.append(str(msg.id))

        for msg in gathered_context.batch:
            summaries = image_summaries.get(str(msg.id), [])
            text = _build_message_text_with_summaries(msg, summaries=summaries)
            if text:
                if msg.author is not None and self._classifier is not None:
                    author_type = self._classifier.classify_author(msg.author.id)
                    role = "bot" if author_type == "bot" else "team"
                else:
                    role = "team"
                turns.append(Turn(role=role, content=text))
                message_ids.append(str(msg.id))

        if not turns and gathered_context.reply_target_message is not None:
            target = gathered_context.reply_target_message
            summaries = image_summaries.get(str(target.id), [])
            target_text = _build_message_text_with_summaries(target, summaries=summaries)
            if target_text:
                turns.append(Turn(role="user", content=target_text))
                message_ids.append(str(target.id))
                conversation_id = f"reply_{target.id}"

        return turns, message_ids, conversation_id

    def _collect_context_messages(
        self,
        message: discord.Message,
        gathered_context: GatheredContext,
    ) -> list[discord.Message]:
        seen: set[int] = set()
        messages: list[discord.Message] = []
        if gathered_context.thread_history:
            all_messages = list(gathered_context.thread_history)
            for msg in gathered_context.batch:
                if msg.id not in [m.id for m in all_messages]:
                    all_messages.append(msg)
            all_messages.sort(key=lambda m: m.created_at)
            return all_messages

        for group in gathered_context.reply_chain:
            for msg in group.messages:
                if msg.id in seen:
                    continue
                seen.add(msg.id)
                messages.append(msg)

        for msg in gathered_context.batch:
            if msg.id in seen:
                continue
            seen.add(msg.id)
            messages.append(msg)

        target = gathered_context.reply_target_message
        if target is not None and target.id not in seen:
            messages.append(target)

        messages.sort(key=lambda m: m.created_at)
        return messages

    async def _summarize_images(self, messages: list[discord.Message]) -> dict[str, list[tuple[int, str]]]:
        image_items: list[tuple[str, int, ImageInput]] = []
        for msg in messages:
            inputs = extract_image_inputs(msg)
            if not inputs:
                continue
            msg_id = str(msg.id)
            try:
                images_with_payload = await download_image_inputs(
                    inputs,
                    timeout_seconds=self._image_download_timeout_seconds,
                    max_retries=self._image_download_max_retries,
                )
            except Exception:
                logger.exception("Failed to download image attachments. message_id=%s", msg_id)
                raise
            for idx, image in enumerate(images_with_payload, start=1):
                image_items.append((msg_id, idx, image))

        if not image_items:
            return {}
        if not self._llm_enable_image:
            raise RuntimeError("Image input is disabled by configuration.")

        context_text = _format_conversation_context(messages, classifier=self._classifier)
        mapping_lines = []
        for item_idx, (msg_id, image_index, image) in enumerate(image_items, start=1):
            filename = image.filename or "unknown"
            mapping_lines.append(
                f"Image {item_idx}: message_id={msg_id} image_index={image_index} filename={filename}"
            )

        user_content = (
            "Conversation context:\n"
            f"{context_text}\n\n"
            "Image mapping:\n"
            f"{chr(10).join(mapping_lines)}\n\n"
            "Summarize the key information from each image in the context of the conversation. "
            "Return one summary per message_id and image_index."
        )
        system_prompt = compose_system_prompt(
            self._manager.config.team_image_summary_prompt,
            self._manager.llm_invoker.project_introduction,
        )
        result: ImageSummaryResult = await self._manager.llm_invoker.invoke_llm(
            system_prompt=system_prompt,
            user_content=user_content,
            images=[item[2] for item in image_items],
            response_model=ImageSummaryResult,
        )
        summaries: dict[str, list[tuple[int, str]]] = {}
        for item in result.summaries:
            summary = item.summary.strip()
            if summary:
                summaries.setdefault(item.message_id, []).append((item.image_index, summary))
        return summaries


def _format_conversation_context(
    messages: list[discord.Message],
    *,
    classifier: MessageClassifier,
) -> str:
    lines = []
    for msg in messages:
        if msg.author is None:
            continue
        author_type = classifier.classify_author(msg.author.id)
        if author_type == "community_user":
            role = "User"
        elif author_type == "bot":
            role = "You"
        else:
            role = "Team"
        content_lines: list[str] = []
        raw_content = (msg.content or "").strip()
        if raw_content:
            content_lines.append(raw_content)
        content_lines.extend(_build_non_image_attachment_placeholders(msg))
        if not content_lines and any(is_image_attachment(att) for att in msg.attachments):
            content_lines.append("Image-only message.")
        if content_lines:
            lines.append(f"{role}: {'\\n'.join(content_lines)}")
    return "\n".join(lines).strip()


def _build_message_text_with_summaries(
    message: discord.Message,
    *,
    summaries: list[tuple[int, str]],
) -> str:
    lines: list[str] = []
    raw_text = (message.content or "").strip()
    if raw_text:
        lines.append(raw_text)
    summary_map = {idx: summary for idx, summary in summaries}
    attachment_lines: list[str] = []
    image_index = 0
    if message.attachments:
        for attachment in message.attachments:
            if is_image_attachment(attachment):
                image_index += 1
                summary = summary_map.get(image_index, "").strip()
                if summary:
                    attachment_lines.append(f"Image summary ({image_index}): {summary}")
            else:
                attachment_lines.append(
                    format_attachment_placeholder(attachment.filename, is_image=False)
                )
    elif summaries:
        for idx, summary in sorted(summaries, key=lambda item: item[0]):
            summary_text = summary.strip()
            if summary_text:
                attachment_lines.append(f"Image summary ({idx}): {summary_text}")
    lines.extend(attachment_lines)
    return "\n".join(lines).strip()


def _build_non_image_attachment_placeholders(message: discord.Message) -> list[str]:
    placeholders: list[str] = []
    for attachment in message.attachments:
        if is_image_attachment(attachment):
            continue
        placeholders.append(
            format_attachment_placeholder(attachment.filename, is_image=False)
        )
    return placeholders


