from __future__ import annotations

from typing import Optional, Dict

from community_intern.core.models import Message, Conversation


def format_attachment_placeholder(filename: Optional[str], is_image: bool = False) -> str:
    """
    Formats a placeholder string for a file attachment or image.
    
    Args:
        filename: The name of the file, if available.
        is_image: Whether the attachment is an image.
        
    Returns:
        A string like "Attachment: file.txt" or "Image: photo.png".
    """
    name = (filename or "").strip()
    label = "Image" if is_image else "Attachment"
    if name:
        return f"{label}: {name}"
    return f"{label}: file uploaded"


def format_message_as_text(msg: Message) -> list[str]:
    """
    Formats a message into a list of text lines, including attachment and image placeholders.
    """
    text_lines: list[str] = []
    raw_text = (msg.text or "").strip()
    if raw_text:
        text_lines.append(raw_text)
    
    if msg.attachments:
        for attachment in msg.attachments:
            text_lines.append(
                format_attachment_placeholder(
                    attachment.filename, is_image=attachment.is_image
                )
            )
            
    # If the message has no text and no file attachments, but has images,
    # we add placeholders for the images so they are represented in text.
    if not text_lines and msg.images:
        for image in msg.images:
            text_lines.append(
                format_attachment_placeholder(image.filename, is_image=True)
            )
            
    return text_lines


def format_conversation_as_text(
    conversation: Conversation,
    role_map: Optional[Dict[str, str]] = None
) -> str:
    """
    Formats a conversation into a single string.
    
    Args:
        conversation: The conversation to format.
        role_map: Optional mapping from message role (e.g. 'user') to display name (e.g. 'User').
                  Defaults to {"user": "User", "assistant": "You", "system": "System"}.
                  
    Returns:
        A string representation of the conversation.
    """
    if role_map is None:
        role_map = {
            "user": "User",
            "assistant": "You",
            "system": "System"
        }
    
    lines: list[str] = []
    for msg in conversation.messages:
        text_lines = format_message_as_text(msg)
        if not text_lines:
            continue
        text = "\n".join(text_lines)
        role = role_map.get(msg.role, msg.role.capitalize())
        lines.append(f"{role}: {text}")
    
    return "\n".join(lines).strip()
