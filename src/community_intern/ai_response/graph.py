import logging
from functools import partial
from typing import Any, Dict, List, Optional, Sequence, TYPE_CHECKING, TypedDict

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_crynux import ChatCrynux
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from community_intern.ai_response.config import AIConfig
from community_intern.llm.image_adapters import ContentPart, ImagePart, LLMImageAdapter, TextPart
from community_intern.core.models import AttachmentInput, Conversation, ImageInput, Message, RequestContext, AIResult
from community_intern.kb.interfaces import KnowledgeBase, SourceContent
from community_intern.llm.prompts import compose_system_prompt
from community_intern.core.formatters import format_message_as_text, format_conversation_as_text

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

# --- Graph State ---

class GraphState(TypedDict):
    conversation: Conversation
    context: RequestContext
    config: AIConfig
    kb: KnowledgeBase

    user_question: str
    user_parts: List[ContentPart]

    kb_index_text: str
    selected_source_ids: List[str]
    loaded_sources: List[SourceContent]

    draft_answer: str

    verification: Optional[bool]

    should_reply: bool
    final_reply_text: Optional[str]

# --- Pydantic Models for Structured Output ---

class LLMGateDecision(BaseModel):
    should_reply: bool = Field(description="Whether the bot should reply to the user input")

class LLMSelectionResult(BaseModel):
    selected_source_ids: List[str] = Field(description="List of source IDs relevant to the query")

class LLMGenerationResult(BaseModel):
    answer: Optional[str] = Field(description="The generated answer text, or null if the question cannot be answered from the provided context")

class LLMVerificationResult(BaseModel):
    is_good_enough: bool = Field(description="Whether the answer is safe and accurate enough to post")

# --- Nodes ---

def _build_user_message(
    *,
    text: str,
    parts: List[ContentPart],
    adapter: LLMImageAdapter,
    enable_images: bool,
) -> HumanMessage:
    if enable_images and parts:
        content = adapter.build_user_content(
            parts=[TextPart(type="text", text=text), *parts],
        )
    else:
        content = text
    return HumanMessage(content=content)






async def node_gating(
    state: GraphState, *, llm: "ChatOpenAI", image_adapter: LLMImageAdapter
) -> Dict[str, Any]:
    config = state["config"]
    conversation = state["conversation"]
    parts = state.get("user_parts", [])

    last_msg = format_message_as_text(conversation.messages[-1]) if conversation.messages else ""
    if last_msg:
        last_msg = "\n".join(last_msg)
    else:
        last_msg = ""
    if not last_msg and parts:
        last_msg = "User provided images without additional text."

    structured_llm = llm.with_structured_output(
        LLMGateDecision,
        method=config.llm.structured_output_method,
    )

    history_text = format_conversation_as_text(conversation)
    history_block = f"Conversation history:\n{history_text}\n\n" if history_text else ""
    messages = [
        SystemMessage(
            content=compose_system_prompt(
                base_prompt=config.gating_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        _build_user_message(
            text=f"{history_block}User input: {last_msg}",
            parts=parts,
            adapter=image_adapter,
            enable_images=config.llm_enable_image,
        ),
    ]

    try:
        decision: LLMGateDecision = await structured_llm.ainvoke(messages)

        return {
            "user_question": last_msg,
            "should_reply": decision.should_reply,
        }
    except Exception:
        logger.exception("AI gating step failed.")
        return {
            "should_reply": False
        }


async def node_selection(
    state: GraphState, *, llm: "ChatOpenAI", image_adapter: LLMImageAdapter
) -> Dict[str, Any]:
    config = state["config"]
    kb = state["kb"]
    query = state["user_question"]
    conversation = state["conversation"]
    parts = state.get("user_parts", [])
    if not query and parts:
        query = "User provided images without additional text."

    try:
        kb_index_text = await kb.load_index_text()
    except Exception:
        logger.exception("Failed to load knowledge base index.")
        return {"selected_source_ids": [], "should_reply": False}

    structured_llm = llm.with_structured_output(
        LLMSelectionResult,
        method=config.llm.structured_output_method,
    )

    history_text = format_conversation_as_text(conversation)
    history_block = f"Conversation history:\n{history_text}\n\n" if history_text else ""
    # Append max_sources instruction to the base prompt
    base_prompt = config.selection_prompt
    if config.max_sources > 0:
        base_prompt = f"{base_prompt.strip()}\n\nSelect at most {config.max_sources} sources."

    messages = [
        SystemMessage(
            content=compose_system_prompt(
                base_prompt=base_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        _build_user_message(
            text=f"{history_block}Index:\n{kb_index_text}\n\nQuery: {query}",
            parts=parts,
            adapter=image_adapter,
            enable_images=config.llm_enable_image,
        ),
    ]

    try:
        result: LLMSelectionResult = await structured_llm.ainvoke(messages)
        selected_ids = result.selected_source_ids[:config.max_sources]

        if not selected_ids:
            return {"selected_source_ids": [], "should_reply": False}

        return {"selected_source_ids": selected_ids, "kb_index_text": kb_index_text}
    except Exception:
        logger.exception("AI knowledge base source selection failed.")
        return {"selected_source_ids": [], "should_reply": False}


async def node_loading(state: GraphState) -> Dict[str, Any]:
    kb = state["kb"]
    selected_ids = state["selected_source_ids"]

    loaded = []
    for source_id in selected_ids:
        content = await kb.load_source_content(source_id=source_id)
        if not content.text.strip():
            raise RuntimeError(f"Loaded source has empty content: {source_id}")
        loaded.append(content)

    if not loaded:
        return {"loaded_sources": [], "should_reply": False}

    return {"loaded_sources": loaded}


async def node_generation(
    state: GraphState, *, llm: "ChatOpenAI", image_adapter: LLMImageAdapter
) -> Dict[str, Any]:
    config = state["config"]
    loaded = state["loaded_sources"]
    query = state["user_question"]
    conversation = state["conversation"]
    parts = state.get("user_parts", [])
    if not query and parts:
        query = "User provided images without additional text."

    sources_text = "\n\n".join([f"Source: {s.source_id}\nContent:\n{s.text}" for s in loaded])

    structured_llm = llm.with_structured_output(
        LLMGenerationResult,
        method=config.llm.structured_output_method,
    )

    history_text = format_conversation_as_text(conversation)
    history_block = f"Conversation history:\n{history_text}\n\n" if history_text else ""
    messages = [
        SystemMessage(
            content=compose_system_prompt(
                base_prompt=config.answer_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        _build_user_message(
            text=f"{history_block}Context:\n{sources_text}\n\nQuestion: {query}",
            parts=parts,
            adapter=image_adapter,
            enable_images=config.llm_enable_image,
        ),
    ]

    try:
        result: LLMGenerationResult = await structured_llm.ainvoke(messages)
        answer = (result.answer or "").strip()
        # Fix: Some models return the literal string "null" or "Null" when instructed to return null.
        # We treat this as an empty answer.
        if not answer or answer.lower() == "null":
            return {"draft_answer": "", "should_reply": False}
        if not config.enable_verification:
            return {
                "draft_answer": answer,
                "verification": None,
                "should_reply": True,
                "final_reply_text": answer,
            }
        return {"draft_answer": answer}
    except Exception:
        logger.exception("AI answer generation failed.")
        return {"should_reply": False}


async def node_verification(
    state: GraphState, *, llm: "ChatOpenAI", image_adapter: LLMImageAdapter
) -> Dict[str, Any]:
    config = state["config"]
    draft = state["draft_answer"]
    loaded = state["loaded_sources"]
    conversation = state["conversation"]
    parts = state.get("user_parts", [])

    sources_text = "\n\n".join([f"Source: {s.source_id}\nContent:\n{s.text}" for s in loaded])

    structured_llm = llm.with_structured_output(
        LLMVerificationResult,
        method=config.llm.structured_output_method,
    )

    history_text = format_conversation_as_text(conversation)
    history_block = f"Conversation history:\n{history_text}\n\n" if history_text else ""
    messages = [
        SystemMessage(
            content=compose_system_prompt(
                base_prompt=config.verification_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        _build_user_message(
            text=f"{history_block}Context:\n{sources_text}\n\nDraft Answer: {draft}",
            parts=parts,
            adapter=image_adapter,
            enable_images=config.llm_enable_image,
        ),
    ]

    try:
        result: LLMVerificationResult = await structured_llm.ainvoke(messages)

        is_good_enough = result.is_good_enough
        if is_good_enough:
            return {
                "verification": True,
                "should_reply": True,
                "final_reply_text": draft
            }
        else:
            return {
                "verification": False,
                "should_reply": False
            }
    except Exception:
        logger.exception("AI answer verification failed.")
        return {"should_reply": False}


def build_ai_graph(config: AIConfig, *, image_adapter: LLMImageAdapter) -> Runnable:
    """
    Builds and compiles the AI LangGraph application.
    This should be called once at startup.
    """

    # Initialize LLM once
    llm_config = config.llm
    llm = ChatCrynux(
        base_url=llm_config.base_url,
        api_key=llm_config.api_key,
        model=llm_config.model,
        # Only pass vram_limit if it is not None
        **({"vram_limit": llm_config.vram_limit} if llm_config.vram_limit is not None else {}),
        temperature=0.0,
        request_timeout=llm_config.timeout_seconds,
        max_retries=llm_config.max_retries,
    )

    workflow = StateGraph(GraphState)

    # Inject LLM into nodes using partial application
    workflow.add_node("gating", partial(node_gating, llm=llm, image_adapter=image_adapter))
    workflow.add_node("selection", partial(node_selection, llm=llm, image_adapter=image_adapter))
    workflow.add_node("loading", node_loading)
    workflow.add_node("generation", partial(node_generation, llm=llm, image_adapter=image_adapter))
    workflow.add_node("verification", partial(node_verification, llm=llm, image_adapter=image_adapter))

    workflow.set_entry_point("gating")

    def check_gating(state: GraphState) -> str:
        if state.get("should_reply", False):
            return "selection"
        return END

    def check_selection(state: GraphState) -> str:
        if not state.get("should_reply", False):
            return END
        if state.get("selected_source_ids"):
            return "loading"
        if state.get("user_parts"):
            return "generation"
        return END

    def check_loading(state: GraphState) -> str:
        if state.get("should_reply", False) and state.get("loaded_sources"):
            return "generation"
        return END

    def check_generation(state: GraphState) -> str:
        if state.get("should_reply", False) and state.get("draft_answer"):
            if state["config"].enable_verification:
                return "verification"
            return END
        return END

    def check_verification(state: GraphState) -> str:
        if state.get("should_reply", False):
            return END
        return END

    workflow.add_conditional_edges("gating", check_gating)
    workflow.add_conditional_edges("selection", check_selection)
    workflow.add_conditional_edges("loading", check_loading)
    workflow.add_conditional_edges("generation", check_generation)
    workflow.add_conditional_edges("verification", check_verification)

    return workflow.compile()
