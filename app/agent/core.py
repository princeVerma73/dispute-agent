"""core.py — DisputeAgent LangGraph ReAct orchestration.

Dispatches all dispute resolution through a per-request compiled ReAct agent
with request-scoped verified tools, dynamic prompts, and fallback chains.
"""

from __future__ import annotations

import os
import logging
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, List

from langgraph.prebuilt import create_react_agent

from app.agent.prompts import build_system_prompt
from app.agent.tools import RequestContext, build_tools
from app.agent.calculator import normalize_rider_id, DISPUTE_WINDOW_DAYS
from app.agent.services import record_trace
from app.db.session import SessionLocal
from app.db.models import Rider, ConversationMessage, OpsDispute

logger = logging.getLogger(__name__)


def _flatten_content(content: Any) -> str:
    """Flatten list-type or block message content (e.g. from Gemini or multi-part) to plain string."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                if "text" in item:
                    parts.append(str(item["text"]).strip())
                elif "content" in item:
                    parts.append(str(item["content"]).strip())
                else:
                    parts.append(str(item).strip())
            elif hasattr(item, "text"):
                parts.append(str(getattr(item, "text")).strip())
            else:
                parts.append(str(item).strip())
        return " ".join(p for p in parts if p).strip()
    return str(content).strip()


@lru_cache(maxsize=1)
def get_llm():
    """Lazily instantiate the LLM chain from env LLM_ORDER with fallbacks.

    Skips any model whose provider API key is missing.
    Raises RuntimeError if no models can be configured.
    """
    default_order = "groq:openai/gpt-oss-120b,groq:openai/gpt-oss-20b,gemini:gemini-2.5-flash"
    llm_order = os.environ.get("LLM_ORDER", default_order)
    models = []

    for item in llm_order.split(","):
        item = item.strip()
        if not item or ":" not in item:
            continue
        provider, name = item.split(":", 1)
        provider = provider.strip().lower()
        name = name.strip()

        if provider == "groq" and os.environ.get("GROQ_API_KEY"):
            try:
                from langchain_groq import ChatGroq
                models.append(ChatGroq(api_key=os.environ["GROQ_API_KEY"], model_name=name, temperature=0, max_retries=1))
                logger.info(f"Initialized Groq LLM: {name}")
            except Exception as e:
                logger.warning(f"Could not load Groq model {name}: {e}")
        elif provider == "gemini" and os.environ.get("GOOGLE_API_KEY"):
            try:
                from langchain_google_genai import ChatGoogleGenerativeAI
                models.append(ChatGoogleGenerativeAI(model=name, google_api_key=os.environ["GOOGLE_API_KEY"], temperature=0, max_retries=1))
                logger.info(f"Initialized Gemini LLM: {name}")
            except Exception as e:
                logger.warning(f"Could not load Gemini model {name}: {e}")

    if not models:
        raise RuntimeError("No LLM configured: set GROQ_API_KEY and/or GOOGLE_API_KEY for providers in LLM_ORDER.")

    return models[0].with_fallbacks(models[1:]) if len(models) > 1 else models[0]


def get_recent_history(rider_id: str, current_message_id: str = "", limit: int = 4) -> list:
    """Fetches recent conversation turns from the database, excluding current message_id."""
    db = SessionLocal()
    history = []
    try:
        query = db.query(ConversationMessage).filter(
            ConversationMessage.rider_id == rider_id,
            ConversationMessage.sender == "rider"
        )
        if current_message_id:
            query = query.filter(ConversationMessage.message_id != current_message_id)
        msgs = query.order_by(ConversationMessage.id.desc()).limit(limit).all()

        msgs.reverse()
        for m in msgs:
            history.append(("human", m.text))
            if m.reply_text:
                history.append(("ai", m.reply_text))
    except Exception as e:
        logger.warning(f"Could not load conversation history for {rider_id}: {e}")
    finally:
        db.close()
    return history


def process_dispute_message(
    rider_id: str,
    message: str,
    message_id: str = "",
    received_at: str = "",
    llm: Any = None,
) -> str:
    """Primary handler: processes a dispute message through the LangGraph ReAct agent.

    Validates rider_id first; constructs RequestContext, builds request-scoped tools
    and system prompt, invokes agent with recursion_limit=14, retries on failure,
    and escalates to Ops if the agent fails twice.
    """
    # 1. Validate rider ID BEFORE constructing RequestContext
    clean_rider_id = None
    try:
        clean_rider_id = normalize_rider_id(rider_id)
    except Exception:
        clean_rider_id = None

    rider_found = False
    if clean_rider_id:
        db = SessionLocal()
        try:
            r = db.query(Rider).filter(Rider.rider_id == clean_rider_id).first()
            if r:
                rider_found = True
        finally:
            db.close()

    if not clean_rider_id or not rider_found:
        # Invalid format or unknown rider: polite fixed reply + ops escalation + NO LLM call
        fallback_rider_id = clean_rider_id or str(rider_id)
        db = SessionLocal()
        try:
            count = db.query(OpsDispute).count() + 1
            op_id = f"op-{count:03d}"
            now_iso = datetime.now(timezone.utc).isoformat()
            escalation = OpsDispute(
                dispute_id=op_id,
                rider_id=fallback_rider_id,
                trip_id=None,
                type="escalation",
                amount=None,
                reason=f"Invalid or unknown rider ID: {rider_id}",
                status="PENDING",
                created_at=now_iso,
            )
            db.add(escalation)
            db.commit()
        except Exception as e:
            logger.error(f"Error logging ops escalation for bad rider {rider_id}: {e}")
        finally:
            db.close()

        record_trace(fallback_rider_id, "decision", "invalid_rider", {"raw_id": rider_id}, "Escalated to Ops")
        return "Aapka rider account verify nahi ho paya. Humne yeh case manual review ke liye Operations team ko forward kar diya hai."

    # 2. Construct verified RequestContext and tools
    ctx = RequestContext(clean_rider_id, received_at=received_at, message_id=message_id)
    tools = build_tools(ctx)

    # 3. Dynamic system prompt based on rider and IST dispute window
    today_d = date.fromisoformat(ctx.today_ist)
    window_start_d = today_d - timedelta(days=DISPUTE_WINDOW_DAYS - 1)
    prompt = build_system_prompt(clean_rider_id, ctx.today_ist, window_start_d.isoformat())

    # 4. Resolve active LLM (parameter takes precedence for tests)
    active_llm = llm if llm is not None else get_llm()

    # 5. Compile request-scoped LangGraph ReAct agent
    agent_executor = create_react_agent(
        model=active_llm,
        tools=tools,
        prompt=prompt
    )

    # 6. Build messages with multi-turn history excluding current message_id
    history = get_recent_history(clean_rider_id, current_message_id=message_id, limit=4)
    messages: List[Any] = []
    for role, text in history:
        messages.append((role, text))
    messages.append(("human", message))

    record_trace(clean_rider_id, "message_in", "user_message", {"message_id": message_id}, message)

    # 7. Execute agent loop (with recursion_limit=14, up to 2 attempts)
    last_err = None
    for attempt in range(2):
        try:
            output = agent_executor.invoke({"messages": messages}, config={"recursion_limit": 14})
            raw_reply = output["messages"][-1].content
            reply = _flatten_content(raw_reply)
            record_trace(clean_rider_id, "reply", "agent_reply", {"attempt": attempt + 1}, reply)
            return reply
        except Exception as e:
            last_err = e
            logger.warning(f"Agent attempt {attempt + 1} failed for {clean_rider_id}: {e}", exc_info=True)

    # 8. Agent failed twice: create ops escalation and return polite fallback
    db = SessionLocal()
    try:
        count = db.query(OpsDispute).count() + 1
        op_id = f"op-{count:03d}"
        now_iso = datetime.now(timezone.utc).isoformat()
        escalation = OpsDispute(
            dispute_id=op_id,
            rider_id=clean_rider_id,
            trip_id=None,
            type="escalation",
            amount=None,
            reason=f"Agent failed after 2 attempts: {str(last_err)}",
            status="PENDING",
            created_at=now_iso,
        )
        db.add(escalation)
        db.commit()
    except Exception as e:
        logger.error(f"Error logging failed agent ops escalation: {e}")
    finally:
        db.close()

    fallback_reply = (
        "Aapka message receive ho gaya hai. System issue ki wajah se humne aapka case "
        "manual review ke liye Operations team ko bhej diya hai. Jald hi aapko update milega."
    )
    record_trace(clean_rider_id, "reply", "agent_fallback", {"error": str(last_err)}, fallback_reply)
    return fallback_reply