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
from app.agent.tools import RequestContext, build_tools
from app.agent.settlement import create_escalation, new_dispute_id
from app.main import receive_message, MessageRequest


def _get_tool(tools, name):
    return next(t for t in tools if t.name == name)


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


# ===========================================================================
# Test g: Escalations unique IDs, deduplication, concurrency, and retry
# ===========================================================================
def test_escalations_unique_ids_and_deduplication():
    db = SessionLocal()
    _seed_rider(db, "R001")

    # 1. Back-to-back distinct escalations get distinct IDs
    esc1 = create_escalation(db, "R001", reason="Distinct reason 1")
    esc2 = create_escalation(db, "R001", reason="Distinct reason 2")
    assert esc1.dispute_id != esc2.dispute_id
    assert esc1.dispute_id.startswith("op-")
    assert esc2.dispute_id.startswith("op-")
    assert len(esc1.dispute_id) == 11

    # 2. Identical open escalation is not duplicated
    esc1_dup = create_escalation(db, "R001", reason="Distinct reason 1")
    assert esc1_dup.dispute_id == esc1.dispute_id

    # Verify rows in DB: exactly 2 distinct rows
    rows = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001").all()
    assert len(rows) == 2
    db.close()


def test_four_concurrent_escalations_all_get_distinct_ids():
    db = SessionLocal()
    _seed_rider(db, "R001")
    db.close()

    created_ids = []
    lock = threading.Lock()

    def worker(i):
        w_db = SessionLocal()
        try:
            esc = create_escalation(w_db, "R001", reason=f"Concurrent escalation {i}")
            with lock:
                created_ids.append(esc.dispute_id)
        finally:
            w_db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(created_ids) == 4
    assert len(set(created_ids)) == 4  # all 4 distinct!

    db = SessionLocal()
    rows = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001").all()
    assert len(rows) == 4
    for r in rows:
        assert r.dispute_id.startswith("op-")
    db.close()


def test_escalation_retry_on_transient_failure(monkeypatch):
    db = SessionLocal()
    _seed_rider(db, "R001")

    real_commit = db.commit
    fail_once = True

    def flaky_commit():
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise Exception("Transient DB commit error")
        return real_commit()

    monkeypatch.setattr(db, "commit", flaky_commit)
    esc = create_escalation(db, "R001", reason="Flaky commit reason")
    assert esc is not None
    assert esc.dispute_id.startswith("op-")
    db.close()


# ===========================================================================
# Test h: Forced OpsDispute insert failure in settle() marks Settlement rows
#         as 'failed', and settle_owed can be called again and succeeds.
# ===========================================================================
def test_ops_dispute_insert_failure_marks_settlement_failed_and_retryable(monkeypatch, payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # T001: 5 km at 1.0x surge = 43 owed
    # T002: 5 km at 1.0x surge = 43 owed
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    _seed_trip(db, "T002", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    # 1. First settle T001: auto-paid via PaySwift (<=200)
    res1 = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res1
    assert len(payswift_server.payouts) == 1

    # 2. Settle T002: second payment on the same day -> triggers Ops approval path.
    # Force OpsDispute creation to fail once.
    from sqlalchemy.orm import Session
    real_add = Session.add

    fail_ops_dispute = True

    def flaky_add(self, instance, *args, **kwargs):
        if isinstance(instance, OpsDispute) and instance.type == "approval" and fail_ops_dispute:
            raise RuntimeError("Forced OpsDispute approval insert error")
        return real_add(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", flaky_add)

    res_fail = settle_tool.invoke({"trip_ids": "T002"})
    assert "FAILED" in res_fail

    # Assert Settlement row for T002 ended as 'failed' (never left reserved!)
    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T002").first()
    assert s_row is not None
    assert s_row.status == "failed"
    db.close()

    # 3. Disable the simulated failure and retry settle_owed for T002:
    fail_ops_dispute = False
    res_retry = settle_tool.invoke({"trip_ids": "T002"})
    assert "QUEUED" in res_retry

    # Assert Settlement row is now 'queued'
    db = SessionLocal()
    s_row_after = db.query(Settlement).filter(Settlement.key == "trip:T002").first()
    assert s_row_after is not None
    assert s_row_after.status == "queued"
    db.close()


# ===========================================================================
# Test i: A FAILED result's text contains no exception message
# ===========================================================================
def test_failed_result_text_contains_no_exception_message(monkeypatch, payswift_server):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    # 1. PaySwift failure path
    payswift_server.inject_failures(10)
    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T001"})
    assert "FAILED" in res
    assert "The payment could not be completed; the case was sent to the ops team." in res
    assert "503" not in res
    assert "HTTP" not in res
    assert "Exception" not in res
    assert "Traceback" not in res

    # 2. OpsDispute insert failure path
    payswift_server.reset()
    # First settle T001 to use up the daily autopay
    res_paid = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res_paid

    # Seed T002 for second payment (triggers Ops approval)
    db = SessionLocal()
    _seed_trip(db, "T002", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    from sqlalchemy.orm import Session
    real_add = Session.add

    def failing_add(self, instance, *args, **kwargs):
        if isinstance(instance, OpsDispute) and instance.type == "approval":
            raise RuntimeError("SecretInternalDatabaseException: disk full or unique key violation")
        return real_add(self, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", failing_add)

    res_fail = settle_tool.invoke({"trip_ids": "T002"})
    assert "FAILED" in res_fail
    assert "The payment could not be completed; the case was sent to the ops team." in res_fail
    assert "SecretInternalDatabaseException" not in res_fail
    assert "disk full" not in res_fail
    assert "RuntimeError" not in res_fail


# ===========================================================================
# Test j: Multi-day findings in one settle_owed call:
#         Day 1 (15) PAID, Day 2 (10) QUEUED
# ===========================================================================
def test_multiday_findings_first_paid_second_queued(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # Day 1: 2026-09-20 -> completed 2.0 km (fare 25), paid 10 -> owed 15
    _seed_trip(db, "T101", "R001", date="2026-09-20", distance_raw=2.0)
    _seed_line(db, "L101", "R001", "T101", 10.0)

    # Day 2: 2026-09-21 -> completed 2.0 km (fare 25), paid 15 -> owed 10
    _seed_trip(db, "T102", "R001", date="2026-09-21", distance_raw=2.0)
    _seed_line(db, "L102", "R001", "T102", 15.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T101 T102"})
    assert "2026-09-20: PAID Rs 15" in res
    assert "2026-09-21: QUEUED Rs 10" in res

    # Exactly 1 payout made to PaySwift for Day 1
    assert len(payswift_server.payouts) == 1
    assert payswift_server.payouts[0]["amount"] == 15

    # Exactly 1 OpsDispute created for Day 2
    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    assert ops is not None
    assert int(round(ops.amount)) == 10
    db.close()


# ===========================================================================
# Test k: Two trips on one day totalling 176 -> exactly one payout of 176
# ===========================================================================
def test_two_trips_same_day_totalling_176_one_payout(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # T201: 12 km -> (25 + 10*6) = 85 owed
    # T202: 13 km -> (25 + 11*6) = 91 owed
    # 85 + 91 = 176
    _seed_trip(db, "T201", "R001", date="2026-09-20", distance_raw=12.0)
    _seed_trip(db, "T202", "R001", date="2026-09-20", distance_raw=13.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T201 T202"})
    assert "2026-09-20: PAID Rs 176" in res

    # Exactly 1 payout of 176
    assert len(payswift_server.payouts) == 1
    assert payswift_server.payouts[0]["amount"] == 176


# ===========================================================================
# Test l: One day over 200 and another under 200 in one call
#         Over-200 day is QUEUED, under-200 day is PAID
# ===========================================================================
def test_one_day_over_200_and_another_under_200_in_one_call(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # Day 1 (2026-09-19): 50 km -> (25 + 48*6) = 313 owed (> 200) -> QUEUED
    _seed_trip(db, "T301", "R001", date="2026-09-19", distance_raw=50.0)
    # Day 2 (2026-09-20): 5 km -> (25 + 3*6) = 43 owed (<= 200) -> PAID
    _seed_trip(db, "T302", "R001", date="2026-09-20", distance_raw=5.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T301 T302"})
    assert "2026-09-19: QUEUED Rs 313" in res
    assert "2026-09-20: PAID Rs 43" in res

    # Exactly 1 payout of 43
    assert len(payswift_server.payouts) == 1
    assert payswift_server.payouts[0]["amount"] == 43

    # Exactly 1 OpsDispute of 313
    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    assert ops is not None
    assert int(round(ops.amount)) == 313
    db.close()


# ===========================================================================
# Test m: Dev reset isolation & idempotency salt
#         a. Finding settled before and after reset produces different keys
#         b. Finding settled twice with no reset produces same key (1 payout)
#         c. Calling /reset twice gives two different salts
# ===========================================================================
def test_reset_isolation_and_idempotency_salt(payswift_server, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app.agent.settlement import compute_idempotency_key, set_idempotency_salt, get_idempotency_salt

    client = TestClient(app)

    # Ensure /reset is enabled
    monkeypatch.setenv("ALLOW_RESET", "1")

    # c. Calling /reset twice gives two different salts
    res1 = client.post("/reset")
    assert res1.status_code == 200
    salt1 = res1.json()["salt"]
    assert salt1

    res2 = client.post("/reset")
    assert res2.status_code == 200
    salt2 = res2.json()["salt"]
    assert salt2
    assert salt1 != salt2

    # a. The same finding settled before and after a reset produces two different keys
    key_before = compute_idempotency_key(["trip:T001"])
    client.post("/reset")
    key_after = compute_idempotency_key(["trip:T001"])
    assert key_before != key_after

    # b. The same finding settled twice with no reset produces the same key and PaySwift is called once
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res_first = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res_first
    assert len(payswift_server.payouts) == 1

    # Second call without reset
    res_second = settle_tool.invoke({"trip_ids": "T001"})
    assert "ALREADY_HANDLED" in res_second or "already being handled" in res_second
    assert len(payswift_server.payouts) == 1
    # Reset in-memory salt back to empty string for subsequent tests
    set_idempotency_salt("")
