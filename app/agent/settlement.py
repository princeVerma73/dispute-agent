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

import hashlib
import logging
from datetime import datetime, timezone
from typing import List, Dict, Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.db.session import SessionLocal
from app.db.models import Settlement, OpsDispute
from app.agent.services import call_payswift_payout
from app.agent.calculator import Finding

logger = logging.getLogger(__name__)


def compute_idempotency_key(keys: List[str]) -> str:
    """Generate deterministic PaySwift idempotency key:
    'settle-' + sha1 of sorted keys joined by '|' (first 24 hex chars).
    """
    sorted_keys = sorted(keys)
    joined = "|".join(sorted_keys)
    h = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:24]
    return f"settle-{h}"


def execute_settlement(
    rider_id: str,
    findings: List[Finding],
    today_ist: str
) -> Dict[str, Any]:
    """Execute settlement for a list of findings for a specific rider.

    Args:
        rider_id: Canonical rider ID (e.g. 'R007').
        findings: List of Finding objects from calculator.
        today_ist: The IST date of the incoming message (YYYY-MM-DD).

    Returns:
        Dict with keys:
            'result': 'PAID' | 'QUEUED' | 'NOTHING_OWED' | 'FAILED' | 'ALREADY_HANDLED'
            'amount': total rupees
            'ref': payout_id or dispute_id
            'message': human/LLM-readable plain text summary
    """
    if not findings:
        return {
            "result": "NOTHING_OWED",
            "amount": 0,
            "ref": None,
            "message": "No discrepancies found to settle. Result: NOTHING_OWED."
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

        now_iso = datetime.now(timezone.utc).isoformat()

        # Check existing settlements for these keys
        existing_rows = db.query(Settlement).filter(
            Settlement.key.in_([f.key for f in unique_findings])
        ).all()
        existing_by_key = {r.key: r for r in existing_rows}

        for f in unique_findings:
            row = existing_by_key.get(f.key)
            if row:
                if row.status in ("reserved", "paid", "queued"):
                    return {
                        "result": "ALREADY_HANDLED",
                        "amount": 0,
                        "ref": row.ref,
                        "message": f"Finding {f.key} is already being handled (status: {row.status})."
                    }
                elif row.status in ("rejected", "failed"):
                    # Re-claim rejected or failed items
                    row.status = "reserved"
                    row.amount = f.amount
                    row.channel = None
                    row.ref = None
                    row.paid_on = None
                    row.created_at = now_iso
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

        # Rule 3: Reserve rows first (commit) so concurrent requests cannot double pay
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return {
                "result": "ALREADY_HANDLED",
                "amount": 0,
                "ref": None,
                "message": "Dispute items are already being handled."
            }

        # Rule 4: Finance check in database
        # total <= 200 AND no autopay settlement with paid_on == today (IST)
        has_autopay_today = db.query(Settlement).filter(
            Settlement.rider_id == rider_id,
            Settlement.channel == "autopay",
            Settlement.status == "paid",
            Settlement.paid_on == today_ist
        ).first() is not None

        can_autopay = (total_amount <= 200) and (not has_autopay_today)

        if can_autopay:
            # Rule 5: Deterministic idempotency key
            idempotency_key = compute_idempotency_key([f.key for f in unique_findings])
            keys_summary = ", ".join(sorted(f.key for f in unique_findings))
            ref_text = f"Dispute settlement for {rider_id}: {keys_summary}"

            try:
                resp = call_payswift_payout(
                    rider_id=rider_id,
                    amount=total_amount,
                    reference=ref_text,
                    idempotency_key=idempotency_key
                )
                payout_id = resp.get("payout_id", f"PS-{idempotency_key[7:19]}")

                # Mark items 'paid'
                for f in unique_findings:
                    s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                    if s_row:
                        s_row.status = "paid"
                        s_row.channel = "autopay"
                        s_row.ref = payout_id
                        s_row.paid_on = today_ist
                db.commit()

                return {
                    "result": "PAID",
                    "amount": total_amount,
                    "ref": payout_id,
                    "message": f"Result: PAID. Amount ₹{total_amount} paid via PaySwift (Payout ID: {payout_id}) for {keys_summary}."
                }
            except Exception as e:
                # Rule 5: PaySwift failed -> mark items 'failed', create ops escalation, allow retry
                logger.error(f"PaySwift payout failed for rider {rider_id}: {e}", exc_info=True)
                for f in unique_findings:
                    s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                    if s_row:
                        s_row.status = "failed"
                        s_row.channel = "autopay"

                count = db.query(OpsDispute).count() + 1
                op_id = f"op-{count:03d}"
                esc = OpsDispute(
                    dispute_id=op_id,
                    rider_id=rider_id,
                    type="escalation",
                    amount=None,
                    reason=f"PaySwift payout failed for {keys_summary}: {str(e)}",
                    status="PENDING",
                    created_at=now_iso,
                )
                db.add(esc)
                db.commit()

                return {
                    "result": "FAILED",
                    "amount": total_amount,
                    "ref": op_id,
                    "message": f"Result: FAILED. PaySwift payout failed ({str(e)}). Escalated to Ops with ID {op_id}. Items can be retried."
                }
        else:
            # Rule 4: Queue for Ops approval
            count = db.query(OpsDispute).count() + 1
            op_id = f"op-{count:03d}"
            keys_summary = ", ".join(sorted(f.key for f in unique_findings))

            reason = (
                f"Amount ₹{total_amount} exceeds ₹200 auto-pay limit ({keys_summary})"
                if total_amount > 200
                else f"Second auto-pay on {today_ist} for rider {rider_id}; requires Ops approval ({keys_summary})"
            )

            dispute = OpsDispute(
                dispute_id=op_id,
                rider_id=rider_id,
                type="approval",
                amount=float(total_amount),
                reason=reason,
                status="PENDING",
                created_at=now_iso,
            )
            db.add(dispute)

            for f in unique_findings:
                s_row = db.query(Settlement).filter(Settlement.key == f.key).first()
                if s_row:
                    s_row.status = "queued"
                    s_row.channel = "ops"
                    s_row.ref = op_id

            db.commit()

            return {
                "result": "QUEUED",
                "amount": total_amount,
                "ref": op_id,
                "message": f"Result: QUEUED. Amount ₹{total_amount} queued for Ops approval (Dispute ID: {op_id}). Reason: {reason}."
            }
    finally:
        db.close()
