"""
ops_actions.py — Operations review actions for DisputeAgent.

Implements Rule 6:
- approve(dispute_id): Idempotently approves a pending dispute, calls PaySwift with
  idempotency key 'ops-approve-<dispute_id>', and updates matching Settlement rows to 'paid'.
- reject(dispute_id): Rejects a pending dispute and updates matching Settlement rows to
  'rejected' so the items can be claimed again.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from app.db.session import SessionLocal
from app.db.models import OpsDispute, Settlement
from app.agent.services import call_payswift_payout

logger = logging.getLogger(__name__)

IST_OFFSET = timezone(timedelta(hours=5, minutes=30))


def approve(dispute_id: str) -> Dict[str, Any]:
    """Approve a pending Ops dispute.

    Idempotent: if already approved, returns success without double-paying.
    Uses PaySwift idempotency key 'ops-approve-<dispute_id>'.
    Sets matching Settlement rows to 'paid'.
    """
    db = SessionLocal()
    try:
        dispute = db.query(OpsDispute).filter(OpsDispute.dispute_id == dispute_id).first()
        if not dispute:
            return {"status": "error", "message": f"Dispute {dispute_id} not found."}

        # Idempotency check: double call pays once
        if dispute.status == "APPROVED":
            return {
                "status": "already_approved",
                "dispute_id": dispute_id,
                "amount": dispute.amount,
                "message": f"Dispute {dispute_id} was already approved."
            }

        if dispute.status != "PENDING":
            return {
                "status": "error",
                "message": f"Dispute {dispute_id} cannot be approved from status {dispute.status}."
            }

        amount = int(round(dispute.amount or 0))
        idempotency_key = f"ops-approve-{dispute_id}"

        # Call PaySwift
        resp = call_payswift_payout(
            rider_id=dispute.rider_id,
            amount=amount,
            reference=f"Ops approval for {dispute_id}",
            idempotency_key=idempotency_key,
        )
        payout_id = resp.get("payout_id", f"PS-{idempotency_key}")

        today_ist = datetime.now(IST_OFFSET).strftime("%Y-%m-%d")

        dispute.status = "APPROVED"

        # Update matching settlement rows to 'paid'
        matching_settlements = db.query(Settlement).filter(
            Settlement.ref == dispute_id
        ).all()
        for s in matching_settlements:
            s.status = "paid"
            s.paid_on = today_ist

        db.commit()
        return {
            "status": "approved",
            "dispute_id": dispute_id,
            "payout_id": payout_id,
            "amount": amount,
            "message": f"Dispute {dispute_id} approved for ₹{amount} (Payout ID: {payout_id})."
        }
    except Exception as e:
        logger.error(f"Error approving dispute {dispute_id}: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        db.close()


def reject(dispute_id: str, reason: str = "") -> Dict[str, Any]:
    """Reject a pending Ops dispute.

    Sets matching Settlement rows to 'rejected' so they may be claimed again.
    """
    db = SessionLocal()
    try:
        dispute = db.query(OpsDispute).filter(OpsDispute.dispute_id == dispute_id).first()
        if not dispute:
            return {"status": "error", "message": f"Dispute {dispute_id} not found."}

        dispute.status = "REJECTED"

        # Set matching settlement rows to 'rejected' so items can be re-claimed
        matching_settlements = db.query(Settlement).filter(
            Settlement.ref == dispute_id
        ).all()
        for s in matching_settlements:
            s.status = "rejected"

        db.commit()
        return {
            "status": "rejected",
            "dispute_id": dispute_id,
            "message": f"Dispute {dispute_id} rejected."
        }
    except Exception as e:
        logger.error(f"Error rejecting dispute {dispute_id}: {e}", exc_info=True)
        return {"status": "error", "message": str(e)}
    finally:
        db.close()
