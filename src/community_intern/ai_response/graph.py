import logging
from functools import partial
from typing import Any, Dict, List, Optional, TYPE_CHECKING, TypedDict

from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.runnables import Runnable
from langchain_crynux import ChatCrynux
from langgraph.graph import StateGraph, END
from pydantic import BaseModel, Field

from community_intern.ai_response.interfaces import AIConfig
from community_intern.core.models import Conversation, RequestContext, AIResult
from community_intern.kb.interfaces import KnowledgeBase, SourceContent

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from langchain_openai import ChatOpenAI

# --- Prompt composition ---

def _compose_system_prompt(*, base_prompt: str, project_introduction: str) -> str:
    parts: List[str] = []
    if base_prompt.strip():
        parts.append(base_prompt.strip())
    if project_introduction.strip():
        parts.append(f"Project introduction:\n{project_introduction.strip()}")
    return "\n\n".join(parts).strip()


# --- Graph State ---

class GraphState(TypedDict):
    conversation: Conversation
    context: RequestContext
    config: AIConfig
    kb: KnowledgeBase

    user_question: str

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

async def node_gating(state: GraphState, *, llm: "ChatOpenAI") -> Dict[str, Any]:
    config = state["config"]
    conversation = state["conversation"]

    last_msg = conversation.messages[-1].text if conversation.messages else ""

    structured_llm = llm.with_structured_output(
        LLMGateDecision,
        method=config.structured_output_method,
    )

    messages = [
        SystemMessage(
            content=_compose_system_prompt(
                base_prompt=config.gating_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        HumanMessage(content=f"User input: {last_msg}")
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


async def node_selection(state: GraphState, *, llm: "ChatOpenAI") -> Dict[str, Any]:
    config = state["config"]
    kb = state["kb"]
    query = state["user_question"]

    try:
        kb_index_text = await kb.load_index_text()
    except Exception:
        logger.exception("Failed to load knowledge base index.")
        return {"selected_source_ids": [], "should_reply": False}

    structured_llm = llm.with_structured_output(
        LLMSelectionResult,
        method=config.structured_output_method,
    )

    messages = [
        SystemMessage(
            content=_compose_system_prompt(
                base_prompt=config.selection_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        HumanMessage(content=f"Index:\n{kb_index_text}\n\nQuery: {query}")
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


async def node_generation(state: GraphState, *, llm: "ChatOpenAI") -> Dict[str, Any]:
    config = state["config"]
    loaded = state["loaded_sources"]
    query = state["user_question"]

    sources_text = "\n\n".join([f"Source: {s.source_id}\nContent:\n{s.text}" for s in loaded])

    structured_llm = llm.with_structured_output(
        LLMGenerationResult,
        method=config.structured_output_method,
    )

    messages = [
        SystemMessage(
            content=_compose_system_prompt(
                base_prompt=config.answer_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        HumanMessage(content=f"Context:\n{sources_text}\n\nQuestion: {query}")
    ]

    try:
        result: LLMGenerationResult = await structured_llm.ainvoke(messages)
        answer = (result.answer or "").strip()
        if not answer:
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


async def node_verification(state: GraphState, *, llm: "ChatOpenAI") -> Dict[str, Any]:
    config = state["config"]
    draft = state["draft_answer"]
    loaded = state["loaded_sources"]

    sources_text = "\n\n".join([f"Source: {s.source_id}\nContent:\n{s.text}" for s in loaded])

    structured_llm = llm.with_structured_output(
        LLMVerificationResult,
        method=config.structured_output_method,
    )

    messages = [
        SystemMessage(
            content=_compose_system_prompt(
                base_prompt=config.verification_prompt,
                project_introduction=config.project_introduction,
            )
        ),
        HumanMessage(content=f"Context:\n{sources_text}\n\nDraft Answer: {draft}")
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


def build_ai_graph(config: AIConfig) -> Runnable:
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
    workflow.add_node("gating", partial(node_gating, llm=llm))
    workflow.add_node("selection", partial(node_selection, llm=llm))
    workflow.add_node("loading", node_loading)
    workflow.add_node("generation", partial(node_generation, llm=llm))
    workflow.add_node("verification", partial(node_verification, llm=llm))

    workflow.set_entry_point("gating")

    def check_gating(state: GraphState) -> str:
        if state.get("should_reply", False):
            return "selection"
        return END

    def check_selection(state: GraphState) -> str:
        if state.get("should_reply", False) and state.get("selected_source_ids"):
            return "loading"
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
