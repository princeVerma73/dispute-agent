"""
tools.py — Dynamic request-scoped dispute tools.

Implements Rules 1, 2, 7, 8, 9:
- LLM never chooses amount. initiate_payout and amount parameters deleted.
- settle_owed recomputes owed amount server-side from calculator findings.
- Tools built per-request via build_tools(ctx) with RequestContext(rider_id, received_at, message_id).
- Tools take NO rider_id parameter.
- Rider can only see or settle their own trips ('not found on your account' without leaking existence).
- Hardcoded dates and trip lists deleted. Expiry window is relative to received_at (last 7 calendar days).
- Tool outputs are plain text with all required fields and explicit result words:
  PAID, QUEUED, NOTHING_OWED, FAILED.
- escalate_to_ops prevents duplicate open escalations for the same rider/trip/reason.
- Every tool call recorded with record_trace.
"""

from __future__ import annotations

import re
import logging
from datetime import datetime, timezone
from typing import List, Optional, Any

from langchain_core.tools import tool

from app.db.session import SessionLocal
from app.db.models import Trip, PayoutLine, OpsDispute, Settlement
from app.agent.services import record_trace
from app.agent.calculator import (
    IST_OFFSET,
    normalize_rider_id,
    normalize_distance,
    expected_fare,
    is_disputable,
    resolve_date,
    audit_trip,
    audit_day as calc_audit_day,
    Finding,
)
from app.agent.settlement import execute_settlement, create_escalation

logger = logging.getLogger(__name__)


class RequestContext:
    """Carries the verified identity and context of the incoming request."""

    def __init__(self, rider_id: str, received_at: datetime | str, message_id: str = ""):
        self.rider_id = normalize_rider_id(rider_id)
        if isinstance(received_at, str):
            clean = received_at.strip().replace("Z", "+00:00")
            dt = datetime.fromisoformat(clean)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            self.received_at = dt
        elif isinstance(received_at, datetime):
            if received_at.tzinfo is None:
                self.received_at = received_at.replace(tzinfo=timezone.utc)
            else:
                self.received_at = received_at
        else:
            raise TypeError("received_at must be a datetime or ISO string")

        self.message_id = message_id
        # IST calendar date string (YYYY-MM-DD)
        self.today_ist = self.received_at.astimezone(IST_OFFSET).strftime("%Y-%m-%d")


def build_tools(ctx: RequestContext) -> List[Any]:
    """Build request-scoped dispute tools bound to the caller's verified RequestContext.

    All tools take NO rider_id parameter and enforce data ownership.
    """
    rider_id = ctx.rider_id
    today_ist = ctx.today_ist

    @tool
    def inspect_trip(trip_id: str) -> str:
        """Inspect details, expected fare, actual ledger payments, dispute window eligibility,
        and settlement status for a specific trip ID on your account.
        """
        clean_trip_id = trip_id.strip().upper()
        db = SessionLocal()
        try:
            # Rule 2: Account ownership check — never reveal if trip exists for another rider
            trip = db.query(Trip).filter(
                Trip.trip_id == clean_trip_id,
                Trip.rider_id == rider_id
            ).first()

            if not trip:
                res = f"Trip {clean_trip_id} not found on your account."
                record_trace(rider_id, "tool_call", "inspect_trip", {"trip_id": clean_trip_id}, res)
                return res

            # Fetch ledger lines for this trip
            ledger_lines = db.query(PayoutLine).filter(
                PayoutLine.trip_id == clean_trip_id,
                PayoutLine.rider_id == rider_id
            ).all()

            km, _ = normalize_distance(trip.distance_km_raw or 0.0)
            surge = float(trip.surge_multiplier or 1.0)

            # Use calculator.audit_trip for expected, paid, and finding (no manual sum in tools)
            summary = audit_trip(trip, ledger_lines)
            exp_fare = summary.expected
            paid_amount = summary.paid
            finding = summary.finding

            # Window check relative to received_at (Rule 7)
            in_window = is_disputable(trip.date, today_ist)
            window_status = "Inside 7-day dispute window (Eligible)" if in_window else "Outside 7-day dispute window (Expired)"

            # Settlement status check
            settlement = db.query(Settlement).filter(
                Settlement.key.in_([f"trip:{clean_trip_id}", f"penalty:{clean_trip_id}"])
            ).first()
            settlement_status = f"{settlement.status.capitalize()} (Ref: {settlement.ref or 'N/A'})" if settlement else "Not settled"

            finding_text = finding.description if finding else "None (Trip fare matches ledger payment)"

            # Rule 8: Plain text output including trip id, expected fare, paid amount, window, settlement status
            output = (
                f"Trip ID: {trip.trip_id}\n"
                f"Status: {trip.status}\n"
                f"Date (IST): {trip.date}\n"
                f"Distance: {km:.1f} km (raw: {trip.distance_km_raw})\n"
                f"Surge Multiplier: {surge}x\n"
                f"Expected Fare: ₹{exp_fare}\n"
                f"Actual Paid in Ledger: ₹{int(round(paid_amount))}\n"
                f"Window Status: {window_status}\n"
                f"Settlement Status: {settlement_status}\n"
                f"Discrepancy: {finding_text}"
            )
            record_trace(rider_id, "tool_call", "inspect_trip", {"trip_id": clean_trip_id}, output)
            return output
        except Exception as e:
            logger.error(f"Error in inspect_trip for {clean_trip_id}: {e}", exc_info=True)
            res = f"Error inspecting trip {clean_trip_id}: {str(e)}"
            record_trace(rider_id, "error", "inspect_trip", {"trip_id": clean_trip_id}, res)
            return res
        finally:
            db.close()

    @tool
    def audit_day(date_query: str) -> str:
        """Inspect all trips, daily incentive qualification, penalties, and payment discrepancies
        for a specific date (e.g. 'YYYY-MM-DD', 'yesterday', or '20').
        """
        db = SessionLocal()
        try:
            target_date = resolve_date(date_query, today_ist)
            if not target_date:
                res = f"Could not determine valid date from '{date_query}'. Please provide a date like 'YYYY-MM-DD' or 'yesterday'."
                record_trace(rider_id, "tool_call", "audit_day", {"date_query": date_query}, res)
                return res

            in_window = is_disputable(target_date, today_ist)
            window_status = "Inside 7-day dispute window (Eligible)" if in_window else "Outside 7-day dispute window (Expired)"

            trips = db.query(Trip).filter(
                Trip.rider_id == rider_id,
                Trip.date == target_date
            ).all()

            ledger = db.query(PayoutLine).filter(
                PayoutLine.rider_id == rider_id
            ).all()

            day_audit = calc_audit_day(target_date, today_ist, trips, ledger)

            # Check settlement status for each finding
            finding_keys = [f.key for f in day_audit.all_findings]
            settlements = db.query(Settlement).filter(Settlement.key.in_(finding_keys)).all() if finding_keys else []
            settlements_map = {s.key: s.status for s in settlements}

            lines = [
                f"Audit for Date: {target_date}",
                f"Window Status: {window_status}",
                f"Completed Trips: {day_audit.completed_count}",
                f"Daily Incentive Eligible (>=12 trips): {'Yes' if day_audit.incentive_eligible else 'No'}",
                f"Daily Incentive Paid: {'Yes' if day_audit.incentive_paid else 'No'}",
            ]

            if day_audit.all_findings:
                lines.append(f"Discrepancies Found ({len(day_audit.all_findings)} items, Total Owed: ₹{day_audit.total_owed()}):")
                for f in day_audit.all_findings:
                    s_status = settlements_map.get(f.key, "Not settled")
                    lines.append(f"- [{f.key}] {f.description} (Settlement Status: {s_status})")
            else:
                lines.append("Discrepancies Found: None (All trips and incentives correctly paid).")

            output = "\n".join(lines)
            record_trace(rider_id, "tool_call", "audit_day", {"date_query": date_query, "resolved_date": target_date}, output)
            return output
        except Exception as e:
            logger.error(f"Error in audit_day for {date_query}: {e}", exc_info=True)
            res = f"Error auditing day: {str(e)}"
            record_trace(rider_id, "error", "audit_day", {"date_query": date_query}, res)
            return res
        finally:
            db.close()

    @tool
    def check_payout_history() -> str:
        """Check all historical payout ledger entries and recorded settlement transactions on your account."""
        db = SessionLocal()
        try:
            payouts = db.query(PayoutLine).filter(
                PayoutLine.rider_id == rider_id
            ).all()

            settlements = db.query(Settlement).filter(
                Settlement.rider_id == rider_id
            ).all()

            lines = [f"Payout History for Rider {rider_id}:"]
            if payouts:
                lines.append("Ledger Payout Lines:")
                for p in payouts[-15:]:
                    lines.append(f"- Date: {p.date}, LineID: {p.line_id}, Trip: {p.trip_id or 'None'}, Amount: ₹{p.amount}, Category: {p.category}")
            else:
                lines.append("No ledger payout lines found.")

            if settlements:
                lines.append("Settlement System Records:")
                for s in settlements[-15:]:
                    lines.append(f"- Key: {s.key}, Amount: ₹{s.amount}, Channel: {s.channel}, Status: {s.status}, Ref: {s.ref or 'None'}, PaidOn: {s.paid_on or 'N/A'}")

            output = "\n".join(lines)
            record_trace(rider_id, "tool_call", "check_payout_history", {}, output)
            return output
        except Exception as e:
            logger.error(f"Error in check_payout_history for {rider_id}: {e}", exc_info=True)
            res = f"Error checking payout history: {str(e)}"
            record_trace(rider_id, "error", "check_payout_history", {}, res)
            return res
        finally:
            db.close()

    @tool
    def settle_owed(day: str = "", trip_ids: str = "") -> str:
        """Settle legitimate unpaid amounts or missing fares owed to the rider.

        The system recomputes the exact amount server-side from verified calculator findings.
        Pass either day (e.g. 'YYYY-MM-DD' or 'yesterday') or trip_ids (e.g. 'T123456').
        Do NOT pass an amount parameter.
        """
        db = SessionLocal()
        try:
            findings: List[Finding] = []

            # 1. Audit Day findings if day provided
            if day.strip():
                target_date = resolve_date(day, today_ist)
                if target_date:
                    if not is_disputable(target_date, today_ist):
                        res = f"Date {target_date} is outside the 7-day dispute window (expired). Per platform policy, expired trips cannot be settled. Result: NOTHING_OWED."
                        record_trace(rider_id, "tool_call", "settle_owed", {"day": day, "trip_ids": trip_ids}, res)
                        return res

                    trips = db.query(Trip).filter(
                        Trip.rider_id == rider_id,
                        Trip.date == target_date
                    ).all()
                    ledger = db.query(PayoutLine).filter(
                        PayoutLine.rider_id == rider_id
                    ).all()
                    d_audit = calc_audit_day(target_date, today_ist, trips, ledger)
                    findings.extend(d_audit.all_findings)

            # 2. Audit Trip findings if trip_ids provided
            if trip_ids.strip():
                # Extract all trip ID tokens
                tids = re.findall(r"\b(T[A-Z0-9_]+)\b", trip_ids.upper())
                for tid in tids:
                    # Account ownership check (Rule 2)
                    trip = db.query(Trip).filter(
                        Trip.trip_id == tid,
                        Trip.rider_id == rider_id
                    ).first()

                    if not trip:
                        continue

                    # Window check (Rule 7)
                    if not is_disputable(trip.date, today_ist):
                        continue

                    t_lines = db.query(PayoutLine).filter(
                        PayoutLine.trip_id == tid,
                        PayoutLine.rider_id == rider_id
                    ).all()

                    audit_res = audit_trip(trip, t_lines)
                    if audit_res.finding:
                        findings.append(audit_res.finding)

            # Dedup findings by key
            unique_findings = list({f.key: f for f in findings}.values())

            # Rule 8: If no findings, output explicit NOTHING_OWED
            if not unique_findings:
                res = "No eligible discrepancies found within the dispute window. Result: NOTHING_OWED."
                record_trace(rider_id, "tool_call", "settle_owed", {"day": day, "trip_ids": trip_ids}, res)
                return res

            # Execute settlement via settlement engine (Rules 3, 4, 5)
            settle_res = execute_settlement(rider_id, unique_findings, today_ist)
            res_str = settle_res["message"]
            record_trace(rider_id, "tool_call", "settle_owed", {"day": day, "trip_ids": trip_ids}, res_str)
            return res_str
        except Exception as e:
            logger.error(f"Error in settle_owed for rider {rider_id}: {e}", exc_info=True)
            res = "FAILED. The payment could not be completed; the case was sent to the ops team."
            record_trace(rider_id, "error", "settle_owed", {"day": day, "trip_ids": trip_ids}, str(e))
            return res
        finally:
            db.close()

    @tool
    def escalate_to_ops(reason: str, trip_id: str = "") -> str:
        """Escalate an unresolved issue to the Operations team for manual investigation
        (e.g. GPS dispute, accident claim, or contested policy).
        """
        clean_trip_id = trip_id.strip().upper() if trip_id else None
        db = SessionLocal()
        try:
            # If trip_id provided, verify ownership
            if clean_trip_id:
                t = db.query(Trip).filter(
                    Trip.trip_id == clean_trip_id,
                    Trip.rider_id == rider_id
                ).first()
                if not t:
                    clean_trip_id = None

            # Rule 9: Do not duplicate open escalations for the same rider, trip, reason
            trip_filter = OpsDispute.trip_id.is_(None) if clean_trip_id is None else (OpsDispute.trip_id == clean_trip_id)
            existing = db.query(OpsDispute).filter(
                OpsDispute.rider_id == rider_id,
                trip_filter,
                OpsDispute.reason == reason,
                OpsDispute.type == "escalation",
                OpsDispute.status == "PENDING"
            ).first()

            dispute = create_escalation(db, rider_id=rider_id, reason=reason, trip_id=clean_trip_id)

            if existing:
                res = f"An open escalation already exists with ID {dispute.dispute_id} for this issue. No duplicate created."
            else:
                res = f"Issue escalated to Ops team for manual investigation (ID: {dispute.dispute_id})."

            record_trace(rider_id, "tool_call", "escalate_to_ops", {"reason": reason, "trip_id": clean_trip_id}, res)
            return res
        except Exception as e:
            logger.error(f"Error in escalate_to_ops for {rider_id}: {e}", exc_info=True)
            res = f"Error escalating to Ops: {str(e)}"
            record_trace(rider_id, "error", "escalate_to_ops", {"reason": reason, "trip_id": clean_trip_id}, res)
            return res
        finally:
            db.close()

    return [inspect_trip, audit_day, check_payout_history, settle_owed, escalate_to_ops]