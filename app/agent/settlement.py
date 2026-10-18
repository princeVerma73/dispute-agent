"""
settlement.py — Financial settlement engine for DisputeAgent.

Implements Rules 3, 4, and 5:
- Finding deduplication and unique reservation via Settlement table.
- Deterministic PaySwift idempotency key: 'settle-' + sha1(sorted_keys)[:24].
- Database-backed once-per-rider-per-day auto-pay check (no in-memory state).
- Auto-pay for total <= 200 AND first auto-pay of the IST day; otherwise queues for Ops approval.
- Postgres advisory transaction lock on 'settle:<rider_id>' (gracefully no-op on SQLite).
- Handles PaySwift failures: marks items 'failed', creates Ops escalation, enables retry.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
import logging
from uuid import uuid4
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.db.models import Settlement, OpsDispute, Trip
from app.agent.services import call_payswift_payout, record_trace
from app.agent.calculator import Finding

logger = logging.getLogger(__name__)


def new_dispute_id() -> str:
    """Generate a unique dispute ID formatted as 'op-' + 8 hex chars."""
    return "op-" + uuid4().hex[:8]


def create_escalation(db, rider_id: str, reason: str, trip_id: Optional[str] = None) -> OpsDispute:
    """Inserts an OpsDispute (type 'escalation') and does not duplicate an identical open escalation.

    If creating an escalation fails, logs the error and retries once with a fresh id before giving up.
    """
    clean_trip_id = trip_id.strip().upper() if trip_id else None

    trip_filter = OpsDispute.trip_id.is_(None) if clean_trip_id is None else (OpsDispute.trip_id == clean_trip_id)
    existing = db.query(OpsDispute).filter(
        OpsDispute.rider_id == rider_id,
        trip_filter,
        OpsDispute.reason == reason,
        OpsDispute.type == "escalation",
        OpsDispute.status == "PENDING"
    ).first()
    if existing:
        return existing

    now_iso = datetime.now(timezone.utc).isoformat()
    last_err = None
    for attempt in range(2):
        op_id = new_dispute_id()
        dispute = OpsDispute(
            dispute_id=op_id,
            rider_id=rider_id,
            trip_id=clean_trip_id,
            type="escalation",
            amount=None,
            reason=reason,
            status="PENDING",
            created_at=now_iso,
        )
        try:
            db.add(dispute)
            db.commit()
            return dispute
        except Exception as e:
            db.rollback()
            last_err = e
            logger.error(f"Attempt {attempt + 1} to create escalation failed for {rider_id}: {e}", exc_info=True)
            if attempt == 0:
                continue

    raise RuntimeError(f"Failed to create escalation for {rider_id} after 2 attempts: {last_err}")


IDEMPOTENCY_SALT: str = ""


def get_idempotency_salt() -> str:
    """Return the current in-memory idempotency salt."""
    return IDEMPOTENCY_SALT


def set_idempotency_salt(new_salt: Optional[str] = None) -> str:
    """Set a new in-memory idempotency salt (uuid4 hex by default) and return it."""
    global IDEMPOTENCY_SALT
    if new_salt is None:
        new_salt = uuid4().hex
    IDEMPOTENCY_SALT = new_salt
    return IDEMPOTENCY_SALT


def compute_idempotency_key(keys: List[str]) -> str:
    """Generate deterministic PaySwift idempotency key:
    'settle-' + sha1 of sorted keys joined by '|' (plus IDEMPOTENCY_SALT if set).
    """
    sorted_keys = sorted(keys)
    joined = "|".join(sorted_keys)
    if IDEMPOTENCY_SALT:
        joined = f"{joined}:{IDEMPOTENCY_SALT}"
    h = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:24]
    return f"settle-{h}"


def _get_finding_day(f: Finding, db: Any, default_day: str) -> str:
    """Resolve the calendar day associated with a finding."""
    if getattr(f, "date", None):
        return str(f.date)
    if f.key.startswith("incentive:"):
        parts = f.key.split(":")
        if len(parts) > 1 and parts[1]:
            return parts[1]
    tid = f.trip_id or (f.key.split(":", 1)[1] if f.key.startswith(("trip:", "penalty:")) else None)
    if tid:
        trip = db.query(Trip).filter(Trip.trip_id == tid).first()
        if trip and trip.date:
            return str(trip.date)
    return default_day


def format_finding_item(f: Finding) -> str:
    """Format a Finding into a human-readable item descriptor.

    Examples:
        - trip T926334 underpaid
        - trip T123456 unpaid
        - duplicate penalty on trip T123456
        - daily incentive
    """
    if f.kind == "incentive":
        return "daily incentive"
    tid = f.trip_id or (f.key.split(":", 1)[1] if ":" in f.key and not f.key.startswith("incentive:") else "")
    if f.kind == "duplicate_penalty":
        return f"duplicate penalty on trip {tid}"
    elif f.kind in ("underpaid", "unpaid"):
        return f"trip {tid} {f.kind}"
    return f"trip {tid} {f.kind}" if tid else f.kind


def settle(
    rider_id: str,
    findings: List[Finding],
    today_ist: str
) -> Dict[str, Any]:
    """Execute settlement for a list of findings for a specific rider.

    Groups findings by day and treats each day as a separate dispute processed in
    chronological order. Each day gets its own autopay-vs-ops decision, its own PaySwift
    call with an idempotency key built from that day's finding keys, and its own OpsDispute
    if queued.

    Args:
        rider_id: Canonical rider ID (e.g. 'R007').
        findings: List of Finding objects from calculator.
        today_ist: The IST date of the incoming message (YYYY-MM-DD).

    Returns:
        Dict with keys:
            'result': 'PAID' | 'QUEUED' | 'NOTHING_OWED' | 'FAILED' | 'ALREADY_HANDLED' | 'MIXED'
            'amount': total rupees settled/queued
            'details': list of per-day result dicts
            'message': human/LLM-readable plain text summary (one line per day joined by '; ')
    """
    if not findings:
        return {
            "result": "NOTHING_OWED",
            "amount": 0,
            "ref": None,
            "message": "No eligible discrepancies found within the dispute window. Result: NOTHING_OWED."
        }

    # Dedup findings by key
    unique_findings_map = {f.key: f for f in findings}
    unique_findings = list(unique_findings_map.values())
    total_amount = sum(f.amount for f in unique_findings)

    if total_amount <= 0:
        return {
            "result": "NOTHING_OWED",
            "amount": 0,
            "ref": None,
            "message": "Total owed is zero. Result: NOTHING_OWED."
        }

    db = SessionLocal()
    try:
        # Rule 3: On Postgres, take pg_advisory_xact_lock(hashtext('settle:'||rider_id))
        dialect_name = db.bind.dialect.name if db.bind else ""
        if dialect_name == "postgresql":
            db.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:lock_key))"),
                {"lock_key": f"settle:{rider_id}"}
            )

        # Group findings by day
        findings_by_day: Dict[str, List[Finding]] = defaultdict(list)
        for f in unique_findings:
            day = _get_finding_day(f, db, today_ist)
            findings_by_day[day].append(f)

        sorted_days = sorted(findings_by_day.keys())
        day_messages: List[str] = []
        day_results: List[Dict[str, Any]] = []

        now_iso = datetime.now(timezone.utc).isoformat()

        for day in sorted_days:
            day_f_list = findings_by_day[day]
            day_total = sum(f.amount for f in day_f_list)
            if day_total <= 0:
                day_messages.append(f"{day}: NOTHING_OWED")
                continue

            # 1. Check existing settlements for this day's finding keys
            existing_rows = db.query(Settlement).filter(
                Settlement.key.in_([f.key for f in day_f_list])
            ).all()
            existing_by_key = {r.key: r for r in existing_rows}

            actionable_findings: List[Finding] = []
            for f in day_f_list:
                row = existing_by_key.get(f.key)
                if row:
                    if row.status in ("reserved", "paid", "queued"):
                        pass
                    elif row.status in ("rejected", "failed"):
                        row.status = "reserved"
                        row.amount = f.amount
                        row.channel = None
                        row.ref = None
                        row.paid_on = None
                        row.created_at = now_iso
                        actionable_findings.append(f)
                else:
                    new_row = Settlement(
                        key=f.key,
                        rider_id=rider_id,
                        kind=f.kind,
                        amount=f.amount,
                        channel=None,
                        status="reserved",
                        ref=None,
                        paid_on=None,
                        created_at=now_iso,
                    )
                    db.add(new_row)
                    actionable_findings.append(f)

            if not actionable_findings:
                day_messages.append(f"{day}: ALREADY_HANDLED (already being handled)")
                day_results.append({"day": day, "result": "ALREADY_HANDLED", "amount": 0, "ref": None})
                continue

            # Reserve rows first (commit) so concurrent requests cannot double pay
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                day_messages.append(f"{day}: ALREADY_HANDLED (already being handled)")
                day_results.append({"day": day, "result": "ALREADY_HANDLED", "amount": 0, "ref": None})
                continue

            items_summary = ", ".join(format_finding_item(f) for f in actionable_findings)

            # Rule 4: Autopay decision: day total <= 200 AND no autopay settlement with paid_on == today (IST)
            has_autopay_today = db.query(Settlement).filter(
                Settlement.rider_id == rider_id,
                Settlement.channel == "autopay",
                Settlement.status == "paid",
                Settlement.paid_on == today_ist
            ).first() is not None

            can_autopay = (day_total <= 200) and (not has_autopay_today)

            if can_autopay:
                idempotency_key = compute_idempotency_key([f.key for f in actionable_findings])
                keys_summary = ", ".join(sorted(f.key for f in actionable_findings))
                ref_text = f"Dispute settlement for {rider_id}: {keys_summary}"

                try:
                    resp = call_payswift_payout(
                        rider_id=rider_id,
                        amount=day_total,
                        reference=ref_text,
                        idempotency_key=idempotency_key
                    )
                    payout_id = resp.get("payout_id", f"PS-{idempotency_key[7:19]}")

                    for f in actionable_findings:
                        s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                        if s_row:
                            s_row.status = "paid"
                            s_row.channel = "autopay"
                            s_row.ref = payout_id
                            s_row.paid_on = today_ist
                    db.commit()

                    day_messages.append(f"{day}: PAID Rs {day_total} ({items_summary})")
                    day_results.append({"day": day, "result": "PAID", "amount": day_total, "ref": payout_id})
                except Exception as e:
                    logger.error(f"PaySwift payout failed for rider {rider_id} on {day}: {e}", exc_info=True)
                    record_trace(rider_id, "error", "payswift_payout_failed", {"day": day, "keys": keys_summary, "amount": day_total}, str(e))
                    for f in actionable_findings:
                        s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                        if s_row:
                            s_row.status = "failed"
                            s_row.channel = "autopay"
                    db.commit()

                    esc = create_escalation(
                        db,
                        rider_id=rider_id,
                        reason=f"PaySwift payout failed for {keys_summary}: {str(e)}",
                        trip_id=None
                    )
                    op_id = esc.dispute_id
                    day_messages.append(f"{day}: FAILED. The payment could not be completed; the case was sent to the ops team.")
                    day_results.append({"day": day, "result": "FAILED", "amount": day_total, "ref": op_id})
            else:
                # Queue for Ops approval
                op_id = new_dispute_id()
                keys_summary = ", ".join(sorted(f.key for f in actionable_findings))

                reason = (
                    f"Amount ₹{day_total} exceeds ₹200 auto-pay limit ({keys_summary})"
                    if day_total > 200
                    else f"Second auto-pay on {today_ist} for rider {rider_id}; requires Ops approval ({keys_summary})"
                )

                try:
                    dispute = OpsDispute(
                        dispute_id=op_id,
                        rider_id=rider_id,
                        type="approval",
                        amount=float(day_total),
                        reason=reason,
                        status="PENDING",
                        created_at=now_iso,
                    )
                    db.add(dispute)

                    for f in actionable_findings:
                        s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                        if s_row:
                            s_row.status = "queued"
                            s_row.channel = "ops"
                            s_row.ref = op_id

                    db.commit()

                    day_messages.append(f"{day}: QUEUED Rs {day_total} ({items_summary})")
                    day_results.append({"day": day, "result": "QUEUED", "amount": day_total, "ref": op_id})
                except Exception as e:
                    db.rollback()
                    logger.error(f"Failed to queue Ops approval dispute for {rider_id} on {day}: {e}", exc_info=True)
                    record_trace(rider_id, "error", "ops_approval_queue_failed", {"day": day, "keys": keys_summary, "amount": day_total}, str(e))
                    for f in actionable_findings:
                        s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                        if s_row:
                            s_row.status = "failed"
                            s_row.channel = "ops"
                    try:
                        db.commit()
                    except Exception:
                        db.rollback()

                    esc_id = None
                    try:
                        esc = create_escalation(
                            db,
                            rider_id=rider_id,
                            reason=f"Ops approval queue creation failed for {keys_summary}: {str(e)}",
                            trip_id=None
                        )
                        esc_id = esc.dispute_id
                    except Exception as esc_err:
                        logger.error(f"Failed to create fallback escalation: {esc_err}")

                    day_messages.append(f"{day}: FAILED. The payment could not be completed; the case was sent to the ops team.")
                    day_results.append({"day": day, "result": "FAILED", "amount": day_total, "ref": esc_id})

        if any(r["result"] == "FAILED" for r in day_results):
            overall_result = "FAILED"
        elif all(r["result"] == "PAID" for r in day_results):
            overall_result = "PAID"
        elif all(r["result"] == "QUEUED" for r in day_results):
            overall_result = "QUEUED"
        elif all(r["result"] == "ALREADY_HANDLED" for r in day_results):
            overall_result = "ALREADY_HANDLED"
        else:
            overall_result = "MIXED"

        total_settled_amount = sum(r["amount"] for r in day_results if r["result"] in ("PAID", "QUEUED"))
        return {
            "result": overall_result,
            "amount": total_settled_amount,
            "details": day_results,
            "message": "; ".join(day_messages)
        }
    finally:
        db.close()


execute_settlement = settle
