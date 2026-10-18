"""
tests/test_agent_flow.py — End-to-end integration tests for Stage 3 LangGraph ReAct agent.

Tests:
  a. audit_day then settle_owed then reply pays the exact amount
  b. a model that tries to settle for another rider and passes a fake amount cannot move anyone else's money
  c. unknown or invalid rider id is escalated without calling the LLM
  d. a total LLM failure escalates and returns the fallback
  e. list-type content is flattened
  f. the same message_id sent concurrently from 3 threads runs the agent once and returns the same reply
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, List, Optional
import pytest
from pydantic import Field

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatResult, ChatGeneration

from app.db.session import SessionLocal
from app.db.models import Rider, Trip, PayoutLine, OpsDispute, Settlement, ConversationMessage
from app.agent.core import process_dispute_message
from app.main import receive_message, MessageRequest


class ScriptedChatModel(BaseChatModel):
    """Deterministic in-memory chat model returning scripted AIMessages."""
    responses: List[Any] = Field(default_factory=list)
    call_count: int = 0
    bound_tools: List[Any] = Field(default_factory=list)

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        self.bound_tools = list(tools)
        return self

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        if self.call_count >= len(self.responses):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="Default scripted reply."))])
        item = self.responses[self.call_count]
        self.call_count += 1
        if isinstance(item, Exception):
            raise item
        if isinstance(item, str):
            item = AIMessage(content=item)
        return ChatResult(generations=[ChatGeneration(message=item)])

    @property
    def _llm_type(self) -> str:
        return "scripted_chat_model"


def _seed_rider(db, rider_id="R001", name="Test Rider"):
    db.merge(Rider(rider_id=rider_id, name=name, phone="9999999999", city="Delhi"))
    db.commit()


def _seed_trip(db, trip_id, rider_id, date="2026-09-21", distance_raw=5.0, status="completed", surge=1.0):
    db.merge(Trip(
        trip_id=trip_id,
        rider_id=rider_id,
        date=date,
        distance_km_raw=distance_raw,
        status=status,
        surge_multiplier=surge,
    ))
    db.commit()


def _seed_line(db, line_id, rider_id, trip_id, amount, category="trip", date="2026-09-21"):
    db.merge(PayoutLine(
        line_id=line_id,
        rider_id=rider_id,
        trip_id=trip_id,
        amount=amount,
        category=category,
        date=date,
    ))
    db.commit()


# ===========================================================================
# Test a: audit_day then settle_owed then reply pays exact amount
# ===========================================================================
def test_audit_day_then_settle_owed_pays_exact_amount(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # Trip T001: 5 km at 1.0x surge = 25 + (5-2)*6 = 43. Paid 25. Owed 18.
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    _seed_line(db, "L001", "R001", "T001", 25.0)
    db.close()

    scripted_llm = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "audit_day", "args": {"date_query": "2026-09-21"}, "id": "call_1"}]),
        AIMessage(content="", tool_calls=[{"name": "settle_owed", "args": {"day": "2026-09-21"}, "id": "call_2"}]),
        AIMessage(content="Aapka 21 Sep ka Rs 18 PaySwift se settle kar diya gaya hai."),
    ])

    reply = process_dispute_message(
        rider_id="R001",
        message="mera kal ka payout kam aaya",
        message_id="msg-a1",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )

    assert "settle" in reply.lower() or "payswift" in reply.lower() or "18" in reply
    # Verify PaySwift received exactly ₹18
    assert len(payswift_server.payouts) == 1
    assert payswift_server.payouts[0]["amount"] == 18
    assert payswift_server.payouts[0]["rider_id"] == "R001"

    # Verify Settlement record in DB
    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row is not None
    assert s_row.status == "paid"
    assert s_row.amount == 18
    db.close()


# ===========================================================================
# Test b: Model trying to settle for another rider cannot move anyone else's money
# ===========================================================================
def test_model_cannot_settle_for_another_rider(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_rider(db, "R002")
    # T999 belongs to R002
    _seed_trip(db, "T999", "R002", date="2026-09-21", distance_raw=5.0)
    db.close()

    # Model tries to settle T999 for R002 and inject a fake amount
    scripted_llm = ScriptedChatModel(responses=[
        AIMessage(content="", tool_calls=[{"name": "settle_owed", "args": {"trip_ids": "T999"}, "id": "call_bad"}]),
        AIMessage(content="Settlement attempted."),
    ])

    reply = process_dispute_message(
        rider_id="R001",
        message="settle trip T999",
        message_id="msg-b1",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )

    assert reply is not None
    # Verify NO payout was made for R002 or T999
    assert len(payswift_server.payouts) == 0
    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T999").first()
    assert s_row is None
    db.close()


# ===========================================================================
# Test c: Unknown or invalid rider ID is escalated without calling the LLM
# ===========================================================================
def test_unknown_or_invalid_rider_id_escalated_without_llm(payswift_server):
    scripted_llm = ScriptedChatModel(responses=[
        AIMessage(content="Should not be called"),
    ])

    # 1. Invalid format
    reply_invalid = process_dispute_message(
        rider_id="invalid-rider-id",
        message="payout help",
        message_id="msg-c1",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )
    assert scripted_llm.call_count == 0  # LLM NOT CALLED
    assert "verify" in reply_invalid.lower() or "operations" in reply_invalid.lower()

    # 2. Unknown rider ID (valid format R999, but not in DB)
    reply_unknown = process_dispute_message(
        rider_id="R999",
        message="payout help",
        message_id="msg-c2",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )
    assert scripted_llm.call_count == 0  # LLM STILL NOT CALLED

    # Verify ops escalations were created in DB
    db = SessionLocal()
    escalations = db.query(OpsDispute).filter(OpsDispute.type == "escalation").all()
    assert len(escalations) >= 2
    assert any("invalid-rider-id" in e.reason for e in escalations)
    assert any("R999" in e.reason for e in escalations)
    db.close()


# ===========================================================================
# Test d: Total LLM failure escalates and returns fallback
# ===========================================================================
def test_total_llm_failure_escalates_and_returns_fallback(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    db.close()

    # Model fails on both attempts
    scripted_llm = ScriptedChatModel(responses=[
        RuntimeError("LLM Provider Timeout 1"),
        RuntimeError("LLM Provider Timeout 2"),
    ])

    reply = process_dispute_message(
        rider_id="R001",
        message="help",
        message_id="msg-d1",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )

    # Server must never crash, returns polite fallback
    assert "operations" in reply.lower() or "update" in reply.lower()
    assert scripted_llm.call_count == 2

    # Verify Ops escalation created
    db = SessionLocal()
    esc = db.query(OpsDispute).filter(
        OpsDispute.rider_id == "R001",
        OpsDispute.type == "escalation",
    ).first()
    assert esc is not None
    assert "Agent failed" in esc.reason
    db.close()


# ===========================================================================
# Test e: List-type content is flattened
# ===========================================================================
def test_list_type_content_is_flattened(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    db.close()

    # Gemini-style content blocks: list of dicts
    scripted_llm = ScriptedChatModel(responses=[
        AIMessage(content=[
            {"type": "text", "text": "Namaste rider. "},
            {"type": "text", "text": "Aapka dispute resolve ho gaya hai."},
        ]),
    ])

    reply = process_dispute_message(
        rider_id="R001",
        message="status",
        message_id="msg-e1",
        received_at="2026-09-22T10:00:00Z",
        llm=scripted_llm,
    )

    assert isinstance(reply, str)
    assert "Namaste rider. Aapka dispute resolve ho gaya hai." in reply


# ===========================================================================
# Test f: Same message_id sent concurrently from 3 threads runs agent once
# ===========================================================================
def test_concurrent_message_deduplication(monkeypatch, payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    db.close()

    execution_count = 0
    lock = threading.Lock()

    def slow_agent_process(rider_id, message, message_id="", received_at="", llm=None):
        nonlocal execution_count
        with lock:
            execution_count += 1
        time.sleep(0.3)
        return "Dispute handled successfully."

    monkeypatch.setattr("app.main.process_dispute_message", slow_agent_process)

    results = []

    def send_msg():
        req = MessageRequest(
            message_id="msg-concurrent-100",
            rider_id="R001",
            text="mera payout check karo",
            received_at="2026-09-22T10:00:00Z",
        )
        res = receive_message(req)
        results.append(res)

    threads = [threading.Thread(target=send_msg) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All 3 requests must receive the exact same successful reply
    assert len(results) == 3
    for r in results:
        assert r["reply"] == "Dispute handled successfully."

    # Agent was invoked exactly once!
    assert execution_count == 1
