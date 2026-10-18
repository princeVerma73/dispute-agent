"""
tests/test_stage2.py — Comprehensive tests for Stage 2 tools and money movement.

Hermetic testing (SQLite, FakePaySwift, synthetic data):
 a. exact autopay amount equals the finding
 b. >200 goes to ops, not PaySwift
 c. second payment the same IST day goes to ops
 d. the same finding is never paid twice
 e. outside the window is not paid
 f. the window moves with received_at
 g. another rider's trip is blocked (inspect and settle)
 h. settle_owed has no amount parameter
 i. PaySwift outage -> failed + escalated + retryable
 j. 6 concurrent threads -> paid exactly once
 k. ops reject makes the item claimable again
 l. ops approve is idempotent (double call pays once)
"""

import inspect
import threading
from datetime import datetime, timezone
import pytest

from app.db.session import SessionLocal
from app.db.models import Rider, Trip, PayoutLine, OpsDispute, Settlement
from app.agent.tools import RequestContext, build_tools
from app.agent.ops_actions import approve, reject


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


def _get_tool(tools, name):
    return next(t for t in tools if t.name == name)


# ===========================================================================
# Test a: Exact autopay amount equals the finding
# ===========================================================================
def test_exact_autopay_amount_equals_finding(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # 5 km at 1.0x surge = 25 + (5-2)*6 = 43. Paid 25. Owed 18.
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    _seed_line(db, "L001", "R001", "T001", 25.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res
    assert "Rs 18" in res

    # Verify PaySwift received exactly ₹18
    assert len(payswift_server.payouts) == 1
    assert payswift_server.payouts[0]["amount"] == 18
    assert payswift_server.payouts[0]["rider_id"] == "R001"

    # Verify Settlement record
    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row is not None
    assert s_row.status == "paid"
    assert s_row.amount == 18
    assert s_row.channel == "autopay"
    assert s_row.paid_on == "2026-09-22"
    db.close()


# ===========================================================================
# Test b: >200 goes to ops, not PaySwift
# ===========================================================================
def test_greater_than_200_goes_to_ops_not_payswift(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # 50 km at 1.0x surge = 25 + (50-2)*6 = 313. Zero paid. Owed 313 (> 200).
    _seed_trip(db, "T002", "R001", date="2026-09-21", distance_raw=50.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T002"})
    assert "QUEUED" in res
    assert "Rs 313" in res

    # PaySwift must NOT have received any payout
    assert len(payswift_server.payouts) == 0

    # OpsDispute must have approval row
    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    assert ops is not None
    assert ops.amount == 313.0
    assert ops.status == "PENDING"

    # Settlement must be queued
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T002").first()
    assert s_row is not None
    assert s_row.status == "queued"
    assert s_row.channel == "ops"
    assert s_row.ref == ops.dispute_id
    db.close()


# ===========================================================================
# Test c: Second payment the same IST day goes to ops
# ===========================================================================
def test_second_payment_same_ist_day_goes_to_ops(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # T001 owed 40
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=4.0)
    _seed_line(db, "L001", "R001", "T001", 0.0)
    # T002 owed 40
    _seed_trip(db, "T002", "R001", date="2026-09-21", distance_raw=4.0)
    _seed_line(db, "L002", "R001", "T002", 0.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    # First settlement today -> auto-paid via PaySwift
    res1 = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res1
    assert len(payswift_server.payouts) == 1

    # Second settlement on the same IST day -> goes to Ops approval
    res2 = settle_tool.invoke({"trip_ids": "T002"})
    assert "QUEUED" in res2
    assert len(payswift_server.payouts) == 1  # No second PaySwift call

    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    assert ops is not None
    assert ops.status == "PENDING"
    db.close()


# ===========================================================================
# Test d: The same finding is never paid twice
# ===========================================================================
def test_same_finding_never_paid_twice(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    _seed_line(db, "L001", "R001", "T001", 25.0)  # owed 18
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res1 = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res1
    assert len(payswift_server.payouts) == 1

    # Try settling again
    res2 = settle_tool.invoke({"trip_ids": "T001"})
    assert "already being handled" in res2.lower() or "noting_owed" in res2.lower() or "already" in res2.lower()
    assert len(payswift_server.payouts) == 1  # Still exactly 1 payout


# ===========================================================================
# Test e: Outside the window is not paid
# ===========================================================================
def test_outside_window_not_paid(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # Date 2026-09-15 is 8 calendar days before 2026-09-22 (boundary is 2026-09-16)
    _seed_trip(db, "T_OLD", "R001", date="2026-09-15", distance_raw=5.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T_OLD"})
    assert "NOTHING_OWED" in res
    assert len(payswift_server.payouts) == 0


# ===========================================================================
# Test f: The window moves with received_at
# ===========================================================================
def test_window_moves_with_received_at(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # Trip on 2026-09-15
    _seed_trip(db, "T015", "R001", date="2026-09-15", distance_raw=5.0)
    db.close()

    # Message received on 22 Sep -> 15 Sep is expired
    ctx_late = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools_late = build_tools(ctx_late)
    res_late = _get_tool(tools_late, "settle_owed").invoke({"trip_ids": "T015"})
    assert "NOTHING_OWED" in res_late
    assert len(payswift_server.payouts) == 0

    # Message received on 21 Sep -> 15 Sep is day 7 (within window [2026-09-15, 2026-09-21])
    ctx_ontime = RequestContext("R001", "2026-09-21T10:00:00Z")
    tools_ontime = build_tools(ctx_ontime)
    res_ontime = _get_tool(tools_ontime, "settle_owed").invoke({"trip_ids": "T015"})
    assert "PAID" in res_ontime
    assert len(payswift_server.payouts) == 1


# ===========================================================================
# Test g: Another rider's trip is blocked (inspect and settle)
# ===========================================================================
def test_another_riders_trip_blocked(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_rider(db, "R002")
    # T999 belongs to R002
    _seed_trip(db, "T999", "R002", date="2026-09-21", distance_raw=5.0)
    db.close()

    # R001 tries to inspect T999
    ctx_r1 = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools_r1 = build_tools(ctx_r1)

    inspect_tool = _get_tool(tools_r1, "inspect_trip")
    insp_res = inspect_tool.invoke({"trip_id": "T999"})
    assert "not found on your account" in insp_res.lower()
    assert "R002" not in insp_res  # Must not leak existence for R002

    # R001 tries to settle T999
    settle_tool = _get_tool(tools_r1, "settle_owed")
    settle_res = settle_tool.invoke({"trip_ids": "T999"})
    assert "NOTHING_OWED" in settle_res
    assert len(payswift_server.payouts) == 0


# ===========================================================================
# Test h: settle_owed has no amount parameter
# ===========================================================================
def test_settle_owed_has_no_amount_parameter():
    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    sig = inspect.signature(settle_tool.func)
    assert "amount" not in sig.parameters, "settle_owed must NOT take an amount parameter"
    assert "day" in sig.parameters
    assert "trip_ids" in sig.parameters
    assert "rider_id" not in sig.parameters, "tools must NOT take rider_id parameter"

    # Verify no tool in build_tools accepts an amount parameter
    for t in tools:
        t_sig = inspect.signature(t.func)
        assert "amount" not in t_sig.parameters, f"Tool {t.name} must not accept an amount parameter"


# ===========================================================================
# Test i: PaySwift outage -> failed + escalated + retryable
# ===========================================================================
def test_payswift_outage_failed_escalated_retryable(payswift_server, monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)

    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    # Inject persistent 503 failures to simulate PaySwift outage
    payswift_server.inject_failures(10)

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T001"})
    assert "FAILED" in res

    db = SessionLocal()
    # Check settlement row is marked failed
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row is not None
    assert s_row.status == "failed"

    # Check ops escalation created
    esc = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "escalation").first()
    assert esc is not None
    assert esc.status == "PENDING"
    db.close()

    # Clear outage and retry settlement
    payswift_server.reset()
    res_retry = settle_tool.invoke({"trip_ids": "T001"})
    assert "PAID" in res_retry

    db = SessionLocal()
    s_row_after = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row_after.status == "paid"
    db.close()


# ===========================================================================
# Test j: 6 concurrent threads -> paid exactly once
# ===========================================================================
def test_six_concurrent_threads_paid_exactly_once(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=5.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    results = []

    def worker():
        tools = build_tools(ctx)
        settle = _get_tool(tools, "settle_owed")
        r = settle.invoke({"trip_ids": "T001"})
        results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 1 thread succeeds in paying
    paid_results = [r for r in results if "PAID" in r]
    assert len(paid_results) == 1, f"Expected 1 PAID, got {len(paid_results)} in {results}"

    # Exactly 1 payout made to PaySwift
    assert len(payswift_server.payouts) == 1

    db = SessionLocal()
    s_rows = db.query(Settlement).filter(Settlement.key == "trip:T001").all()
    assert len(s_rows) == 1
    assert s_rows[0].status == "paid"
    db.close()


# ===========================================================================
# Test k: Ops reject makes the item claimable again
# ===========================================================================
def test_ops_reject_makes_item_claimable_again(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    # 50 km unpaid = 313 (> 200) -> queues for ops
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=50.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    settle_tool = _get_tool(tools, "settle_owed")

    res = settle_tool.invoke({"trip_ids": "T001"})
    assert "QUEUED" in res

    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    dispute_id = ops.dispute_id
    db.close()

    # Ops rejects dispute
    rej_res = reject(dispute_id)
    assert rej_res["status"] == "rejected"

    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row.status == "rejected"
    db.close()

    # Claim again
    res2 = settle_tool.invoke({"trip_ids": "T001"})
    assert "QUEUED" in res2

    db = SessionLocal()
    ops2 = db.query(OpsDispute).filter(
        OpsDispute.rider_id == "R001",
        OpsDispute.type == "approval",
        OpsDispute.status == "PENDING"
    ).first()
    assert ops2 is not None
    assert ops2.dispute_id != dispute_id
    db.close()


# ===========================================================================
# Test l: Ops approve is idempotent (double call pays once)
# ===========================================================================
def test_ops_approve_is_idempotent_double_call_pays_once(payswift_server):
    db = SessionLocal()
    _seed_rider(db, "R001")
    _seed_trip(db, "T001", "R001", date="2026-09-21", distance_raw=50.0)
    db.close()

    ctx = RequestContext("R001", "2026-09-22T10:00:00Z")
    tools = build_tools(ctx)
    _get_tool(tools, "settle_owed").invoke({"trip_ids": "T001"})

    db = SessionLocal()
    ops = db.query(OpsDispute).filter(OpsDispute.rider_id == "R001", OpsDispute.type == "approval").first()
    dispute_id = ops.dispute_id
    db.close()

    # First approve call
    app_res1 = approve(dispute_id)
    assert app_res1["status"] == "approved"
    assert len(payswift_server.payouts) == 1

    # Second approve call -> idempotent, no double pay
    app_res2 = approve(dispute_id)
    assert app_res2["status"] == "already_approved"
    assert len(payswift_server.payouts) == 1

    db = SessionLocal()
    s_row = db.query(Settlement).filter(Settlement.key == "trip:T001").first()
    assert s_row.status == "paid"
    db.close()
