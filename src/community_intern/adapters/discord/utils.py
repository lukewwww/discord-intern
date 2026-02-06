from __future__ import annotations

from typing import Set
import discord
from community_intern.core.models import ImageInput, AttachmentInput
from community_intern.llm.image_transport import download_images_as_base64

IMAGE_EXTENSIONS: Set[str] = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".avif",
}


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = attachment.content_type
    if content_type:
        return content_type.startswith("image/")
    if attachment.filename:
        lower = attachment.filename.lower()
        for ext in IMAGE_EXTENSIONS:
            if lower.endswith(ext):
                return True
    return False


def extract_image_inputs(message: discord.Message) -> list[ImageInput]:
    images: list[ImageInput] = []
    for attachment in message.attachments:
        if not is_image_attachment(attachment):
            continue
        images.append(
            ImageInput(
                url=attachment.url,
                mime_type=attachment.content_type,
                filename=attachment.filename,
                size_bytes=attachment.size,
                source="discord",
            )
        )
    return images


def extract_attachment_inputs(
    message: discord.Message,
    *,
    include_images: bool,
) -> list[AttachmentInput]:
    attachments: list[AttachmentInput] = []
    for attachment in message.attachments:
        is_image = is_image_attachment(attachment)
        if is_image and not include_images:
            continue
        attachments.append(
            AttachmentInput(
                url=attachment.url,
                mime_type=attachment.content_type,
                filename=attachment.filename,
                size_bytes=attachment.size,
                source="discord",
                is_image=is_image,
            )
        )
    return attachments


async def download_image_inputs(
    images: list[ImageInput],
    *,
    timeout_seconds: float,
    max_retries: int,
) -> list[ImageInput]:
    if not images:
        return []
    base64_images = await download_images_as_base64(
        images,
        timeout_seconds=timeout_seconds,
        max_retries=max_retries,
    )
    by_url = {img.source_url: img for img in base64_images}
    enriched: list[ImageInput] = []
    for image in images:
        payload = by_url.get(image.url)
        if payload is None:
            continue
        enriched.append(
            ImageInput(
                url=image.url,
                mime_type=payload.mime_type or image.mime_type,
                filename=image.filename,
                size_bytes=image.size_bytes,
                source=image.source,
                base64_data=payload.base64_data,
            )
        )
    return enriched
