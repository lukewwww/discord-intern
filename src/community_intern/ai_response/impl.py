import logging
import asyncio
from typing import Optional, Sequence
from langchain_core.runnables import Runnable
from community_intern.ai_response.config import AIConfig
from community_intern.llm.image_adapters import ContentPart, ImagePart, TextPart, get_image_adapter
from community_intern.core.models import AIResult, AttachmentInput, Conversation, ImageInput, Message, RequestContext
from community_intern.kb.interfaces import KnowledgeBase
from community_intern.ai_response.graph import build_ai_graph, GraphState

logger = logging.getLogger(__name__)

def _append_selected_links(reply_text: str, *, selected_source_ids: list[str]) -> str:
    links = []
    for source_id in selected_source_ids:
        raw = source_id.strip()
        if raw.startswith(("http://", "https://")):
            links.append(raw)
            continue
        if raw.startswith("kb:"):
            inner = raw[len("kb:") :].strip()
            if inner.startswith(("http://", "https://")):
                links.append(inner)

    if not links:
        return reply_text

    lines = [reply_text.rstrip(), "", "Links:"]
    for link in links:
        lines.append(f"- {link}")
    return "\n".join(lines).strip()


class AIResponseService:
    def __init__(self, config: AIConfig, kb: Optional[KnowledgeBase] = None):
        self._config = config
        self._kb = kb

        # Build and compile the graph for generate_reply
        self._image_adapter = get_image_adapter(config.llm_image_adapter)
        self._app: Runnable = build_ai_graph(config, image_adapter=self._image_adapter)

    def set_kb(self, kb: KnowledgeBase) -> None:
        """
        Inject KnowledgeBase after initialization if needed to resolve circular dependencies.
        """
        self._kb = kb

    async def generate_reply(self, conversation: Conversation, context: RequestContext) -> AIResult:
        if not self._kb:
            logger.warning("Knowledge base is not configured, skipping AI reply generation.")
            return AIResult(should_reply=False, reply_text=None)

        user_parts: list[ContentPart] = []
        if self._config.llm_enable_image:
            try:
                user_parts = _build_user_parts(conversation)
            except Exception:
                logger.exception(
                    "Image base64 payload missing; skipping AI reply. platform=%s message_id=%s",
                    context.platform,
                    context.message_id,
                )
                return AIResult(
                    should_reply=False,
                    reply_text=None,
                    debug={"error": "image_base64_missing"},
                )

        initial_state: GraphState = {
            "conversation": conversation,
            "context": context,
            "config": self._config,
            "kb": self._kb,
            "user_question": "",
            "user_parts": user_parts,
            "kb_index_text": "",
            "selected_source_ids": [],
            "loaded_sources": [],
            "draft_answer": "",
            "verification": None,
            "should_reply": False,
            "final_reply_text": None
        }

        try:
            final_state = await asyncio.wait_for(
                self._app.ainvoke(initial_state),
                timeout=self._config.graph_timeout_seconds
            )

            reply_text = final_state.get("final_reply_text")
            if reply_text:
                reply_text = _append_selected_links(
                    reply_text,
                    selected_source_ids=list(final_state.get("selected_source_ids", [])),
                )

            return AIResult(
                should_reply=final_state.get("should_reply", False),
                reply_text=reply_text,
                debug={
                    "verification": final_state.get("verification")
                }
            )
        except asyncio.TimeoutError:
            logger.warning("AI graph timed out while generating a reply.")
            return AIResult(should_reply=False, reply_text=None)
        except Exception:
            logger.exception("AI reply generation failed.")
            return AIResult(should_reply=False, reply_text=None)

def _build_user_parts(conversation: Conversation) -> list[ContentPart]:
    parts: list[ContentPart] = []
    for msg in conversation.messages:
        if msg.role != "user":
            continue
        text_lines = format_message_as_text(msg)
        if text_lines:
            parts.append(TextPart(type="text", text=f"User: {'\\n'.join(text_lines)}"))
        if msg.images:
            base64_images = build_base64_images(msg.images)
            parts.extend([ImagePart(type="image", image=img) for img in base64_images])
    return parts






