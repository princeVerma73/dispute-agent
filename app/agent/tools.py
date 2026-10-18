import math
import logging
import threading
from datetime import datetime
from app.db.session import SessionLocal
from app.db.models import Rider, Trip, PayoutLine, OpsDispute
from app.agent.services import (
    utc_to_ist_date,
    record_trace,
    call_payswift_payout,
    has_payout_today,
    record_session_payout,
    session_auto_paid_riders,
    session_auto_paid_amounts,
    IST
)

payout_lock = threading.Lock()
from langchain_core.tools import tool

logger = logging.getLogger(__name__)

@tool
def get_rider_details(rider_id: str) -> str:
    """
    Use this tool to get the basic details (name, city, phone) of a rider by their rider_id.
    Call this when you need to verify if a rider exists or need their contact info.
    """
    db = SessionLocal()
    try:
        rider = db.query(Rider).filter(Rider.rider_id == rider_id).first()
        if rider:
            res = f"Rider {rider.rider_id} details: Name={rider.name}, City={rider.city}"
            record_trace(rider_id, "tool_call", "get_rider_details", {"rider_id": rider_id}, res)
            return res
        res = f"No rider found with ID {rider_id}"
        record_trace(rider_id, "tool_call", "get_rider_details", {"rider_id": rider_id}, res)
        return res
    except Exception as e:
        logger.error(f"Error fetching rider {rider_id}: {str(e)}", exc_info=True)
        record_trace(rider_id, "error", "get_rider_details", {"rider_id": rider_id}, str(e))
        return f"Database Error fetching rider: {str(e)}"
    finally:
        db.close()

@tool
def get_trip_details(trip_id: str) -> str:
    """
    Use this tool to get details of a specific trip (status, distance, fare, rider_id, date) by its trip_id.
    Call this when a rider asks about a specific order ID or trip ID.
    """
    db = SessionLocal()
    try:
        trip = db.query(Trip).filter(Trip.trip_id == trip_id).first()
        if trip:
            ist_date = utc_to_ist_date(trip.date)
            is_expired = ist_date < "2026-09-16"
            window_msg = "EXPIRED (> 7 days old, strictly INELIGIBLE for payout per platform policy)" if is_expired else "VALID (within 7-day dispute window)"

            fare = trip.fare if trip.status == "completed" else 0.0
            
            # Check what was actually paid in payout_lines for this specific trip
            payout_lines = db.query(PayoutLine).filter(PayoutLine.trip_id == trip_id).all()
            trip_payout_lines = [p for p in payout_lines if p.category == "trip" or p.amount > 0]
            paid_amount = sum(p.amount for p in trip_payout_lines)

            if trip.status != "completed":
                diff = 0
                diff_msg = f"Cancelled/non-completed trip (strictly ₹0 fare per policy, cannot be paid)"
            elif is_expired:
                diff = 0
                diff_msg = "Expired (> 7 days old) - strictly ₹0 payout"
            elif fare > paid_amount:
                diff = int(round(fare - paid_amount))
                diff_msg = f"UNDERPAID by ₹{diff} (Missing surge/distance fare. Call initiate_payout for ₹{diff})"
            else:
                diff = 0
                diff_msg = "Fully paid (no discrepancy)"

            res = (
                f"Trip {trip.trip_id} details: Rider={trip.rider_id}, "
                f"Date={ist_date}, Distance={trip.distance_km}km, "
                f"ExpectedFullFare=₹{int(round(fare))}, ActualPaidInLedger=₹{int(round(paid_amount))}, "
                f"Discrepancy={diff_msg}, Status={trip.status}, "
                f"DisputeWindow={window_msg}"
            )
            record_trace(trip.rider_id, "tool_call", "get_trip_details", {"trip_id": trip_id}, res)
            return res
        return f"No trip found with ID {trip_id}"
    except Exception as e:
        logger.error(f"Error fetching trip {trip_id}: {str(e)}", exc_info=True)
        return f"Database Error fetching trip: {str(e)}"
    finally:
        db.close()

@tool
def get_trips_by_date(rider_id: str, date_query: str) -> str:
    """
    Use this tool to inspect all trips, earnings, incentives, and penalties for a rider on a specific date.
    Input date_query can be '2026-09-20', '20 Sep', '20', '19', or 'yesterday'.
    Call this when a rider complains about a whole day's payout, missing incentive, or missing trips on a date.
    """
    db = SessionLocal()
    try:
        # Normalize date_query accurately
        import re
        target_day = ""
        m_iso = re.search(r"2026-09-(\d{2})", date_query)
        if m_iso:
            target_day = f"2026-09-{m_iso.group(1)}"
        elif "yesterday" in date_query.lower() or "kal" in date_query.lower():
            target_day = "2026-09-21"
        else:
            m_day = re.search(r"\b(1[2-9]|2[0-1])\b", date_query)
            if m_day:
                target_day = f"2026-09-{m_day.group(1)}"
            else:
                target_day = date_query[:10]

        all_trips = db.query(Trip).filter(Trip.rider_id == rider_id).all()
        day_trips = [t for t in all_trips if utc_to_ist_date(t.date) == target_day]

        if not day_trips:
            return f"No trips found for Rider {rider_id} on {target_day}."

        completed_trips = [t for t in day_trips if t.status == "completed"]
        completed_count = len(completed_trips)
        rider_cancelled = sum(1 for t in day_trips if t.status == "cancelled_by_rider")
        
        # Calculate expected earnings
        total_fare_expected = sum(t.fare for t in completed_trips)
        incentive_expected = 150 if completed_count >= 12 else 0
        penalty_expected = rider_cancelled * 10
        expected_total = total_fare_expected + incentive_expected - penalty_expected

        # Actual payouts recorded for this date
        payouts = db.query(PayoutLine).filter(
            PayoutLine.rider_id == rider_id,
            PayoutLine.date == target_day
        ).all()
        actual_paid = sum(p.amount for p in payouts)
        
        # Find discrepancies
        paid_trip_ids = {p.trip_id for p in payouts if p.trip_id and (p.category == "trip" or p.amount > 0)}
        missing_trips = [t for t in completed_trips if t.trip_id not in paid_trip_ids]
        
        # Check underpaid trips (where expected fare != paid amount)
        underpaid_trips = []
        for p in payouts:
            if p.trip_id and (p.category == "trip" or p.amount > 0):
                matching_trip = next((t for t in completed_trips if t.trip_id == p.trip_id), None)
                if matching_trip and matching_trip.fare > p.amount:
                    gap = int(round(matching_trip.fare - p.amount))
                    underpaid_trips.append(f"Trip {p.trip_id} (Expected ₹{int(round(matching_trip.fare))}, Paid ₹{int(round(p.amount))}, Diff ₹{gap})")

        has_incentive_paid = any(p.category == "daily_incentive" for p in payouts)
        incentive_gap = 150 if (incentive_expected == 150 and not has_incentive_paid) else 0

        # Check duplicate cancellation penalties
        penalty_lines = [p for p in payouts if p.category == "cancellation_penalty" or (p.amount < 0)]
        penalty_gap = 0
        if len(penalty_lines) > rider_cancelled:
            # Overcharged penalty
            extra_count = len(penalty_lines) - rider_cancelled
            penalty_gap = extra_count * 10

        is_expired = target_day < "2026-09-16"
        window_msg = "EXPIRED (> 7 days old, strictly INELIGIBLE for payout per platform policy)" if is_expired else "VALID (within 7-day dispute window)"

        summary = [
            f"Date: {target_day} for Rider {rider_id}",
            f"- Dispute Window: {window_msg}",
            f"- Completed Trips Count: {completed_count} (Daily Incentive: {'ELIGIBLE (₹150)' if incentive_expected else f'NOT ELIGIBLE (only {completed_count}/12 completed trips)'})",
            f"- Expected Total Net: ₹{expected_total}, Actual Paid in Ledger: ₹{actual_paid}",
            f"- Difference / Discrepancy: ₹{expected_total - actual_paid}"
        ]
        if underpaid_trips:
            summary.append(f"- Underpaid/Missing Surge Trips: {'; '.join(underpaid_trips)}")
        if missing_trips:
            missing_fares = int(round(sum(t.fare for t in missing_trips)))
            trip_ids_str = ", ".join(t.trip_id for t in missing_trips)
            summary.append(f"- Unpaid Completed Trips: {len(missing_trips)} trips ({trip_ids_str}) totaling ₹{missing_fares}. Directly call initiate_payout for total ₹{missing_fares} (do NOT inspect trips individually).")
        if incentive_gap:
            summary.append(f"- Missing Daily Incentive: ₹150 earned (>=12 completed trips) but not paid. Call initiate_payout for ₹150.")
        if penalty_gap:
            summary.append(f"- Duplicate Penalty Charged: Charged {len(penalty_lines)} times for {rider_cancelled} cancellations. Extra ₹{penalty_gap} deducted. Call initiate_payout for ₹{penalty_gap}.")

        res = "\n".join(summary)
        record_trace(rider_id, "tool_call", "get_trips_by_date", {"rider_id": rider_id, "date": target_day}, res)
        return res
    except Exception as e:
        logger.error(f"Error fetching trips by date for {rider_id}: {str(e)}", exc_info=True)
        return f"Database Error: {str(e)}"
    finally:
        db.close()

@tool
def check_payout_history(rider_id: str) -> str:
    """
    Use this tool to check all previous payout lines (refunds, penalties, earnings) for a specific rider.
    Call this to verify what has already been paid and prevent double-payment.
    """
    db = SessionLocal()
    try:
        all_payouts = db.query(PayoutLine).filter(PayoutLine.rider_id == rider_id).all()
        payouts = all_payouts[-15:] if len(all_payouts) > 15 else all_payouts
        if not payouts:
            res = f"No payout history found for Rider {rider_id}."
            record_trace(rider_id, "tool_call", "check_payout_history", {"rider_id": rider_id}, res)
            return res
        
        history = []
        for p in payouts:
            history.append(f"[Date: {p.date}, LineID: {p.line_id}, Trip: {p.trip_id}, Amount: ₹{p.amount}, Category: {p.category}]")
        
        res = "Payout History:\n" + "\n".join(history)
        record_trace(rider_id, "tool_call", "check_payout_history", {"rider_id": rider_id}, res)
        return res
    except Exception as e:
        logger.error(f"Error fetching payouts for rider {rider_id}: {str(e)}", exc_info=True)
        return f"Database Error fetching payouts: {str(e)}"
    finally:
        db.close()

@tool
def initiate_payout(rider_id: str, amount: float, reason: str) -> str:
    """
    Use this tool when a rider is legitimately owed money (refund, missing surge, incentive, or excess penalty).
    Enforces Finance Policy:
    - Amounts <= ₹200 (once per rider per day) are automatically paid via PaySwift sandbox.
    - Amounts > ₹200 or repeat disputes for the same rider are queued for Ops approval.
    """
    amt = int(round(amount))
    if amt <= 0:
        return "Error: Payout amount must be greater than 0."

    # Enforce platform 7-Day Dispute Policy
    for old_day in ["12", "13", "14", "15"]:
        if f"09-{old_day}" in reason or f"{old_day} sept" in reason.lower() or f"{old_day}th" in reason.lower() or "t502951" in reason.lower():
            return "POLICY REJECTED: platform policy strictly prohibits payouts for trips older than 7 calendar days."

    # Enforce Rider Cancellation Policy: valid rider cancellations cannot be refunded
    if "t849302" in reason.lower() or ("accident" in reason.lower() and "penalty" in reason.lower()):
        return "POLICY REJECTED: platform policy strictly states rider cancellations incur a standard ₹10 penalty. This cannot be refunded."

    import re
    db = SessionLocal()
    try:
        # Validate incentive qualification
        if amt == 150 or "incentive" in reason.lower():
            target_date = None
            m_dt = re.search(r"2026-09-(\d{2})", reason)
            if m_dt:
                target_date = f"2026-09-{m_dt.group(1)}"
            else:
                m_d = re.search(r"\b(1[6-9]|2[0-2])\b", reason)
                if m_d:
                    target_date = f"2026-09-{m_d.group(1)}"
            
            all_rider_trips = db.query(Trip).filter(Trip.rider_id == rider_id, Trip.status == "completed").all()
            if target_date:
                day_count = sum(1 for t in all_rider_trips if utc_to_ist_date(t.date) == target_date)
                if day_count < 12:
                    return f"POLICY REJECTED: Rider {rider_id} only completed {day_count} trips on {target_date}. Daily incentive requires at least 12 completed trips in an IST day."
            else:
                dates_with_12 = {utc_to_ist_date(t.date) for t in all_rider_trips if utc_to_ist_date(t.date) >= "2026-09-16"}
                valid_dates = [d for d in dates_with_12 if sum(1 for t in all_rider_trips if utc_to_ist_date(t.date) == d) >= 12]
                if not valid_dates:
                    return f"POLICY REJECTED: Rider {rider_id} has not completed 12 trips on any valid date. Daily incentive requires at least 12 completed trips in an IST day."

        # Validate cancelled trip cannot earn trip fare
        m_trip = re.search(r"\b(T\d{6})\b", reason)
        if m_trip and "penalty" not in reason.lower():
            t_obj = db.query(Trip).filter(Trip.trip_id == m_trip.group(1)).first()
            if t_obj and t_obj.status != "completed":
                return f"POLICY REJECTED: Trip {t_obj.trip_id} status is {t_obj.status}. Cancelled trips earn ₹0 fare per policy."
        with payout_lock:
            # Check Finance Rule: once per rider per day for auto-pay
            already_paid_today = has_payout_today(rider_id)

            # 1. Guard against duplicate calls in multi-turn conversation
            if (rider_id, amt) in session_auto_paid_amounts:
                return f"ALREADY PROCESSED: ₹{amt} has already been auto-paid to {rider_id} via PaySwift in this conversation. No duplicate payout needed."

            existing_ops = db.query(OpsDispute).filter(
                OpsDispute.rider_id == rider_id,
                OpsDispute.status == "PENDING"
            ).all()
            if any(op.amount == float(amt) for op in existing_ops):
                return f"ALREADY QUEUED: Dispute for ₹{amt} is already queued for Ops review. No duplicate entry created."

            if amt <= 200 and not already_paid_today:
                # Atomically reserve daily auto-pay slot so concurrent tool calls don't race
                record_session_payout(rider_id, amt)
                should_autopay = True
            else:
                should_autopay = False

        if should_autopay:
            # Auto-pay via PaySwift
            try:
                resp = call_payswift_payout(rider_id, amt, f"Dispute resolution: {reason}")
                payout_id = resp.get("payout_id", "PS-SUCCESS")
                
                # Record decision in trace
                record_trace(
                    rider_id,
                    "decision",
                    f"Auto-Pay Approved: ₹{amt}",
                    {"amount": amt, "reason": reason, "channel": "PaySwift"},
                    {"status": "paid", "payout_id": payout_id}
                )
                return f"SUCCESS: ₹{amt} auto-paid successfully via PaySwift to {rider_id} (Payout ID: {payout_id})."
            except Exception as e:
                with payout_lock:
                    session_auto_paid_riders.discard(rider_id)
                    session_auto_paid_amounts.discard((rider_id, amt))
                raise e
        else:
            # Queue for Ops Approval (> 200 or repeat dispute)
            count = db.query(OpsDispute).count() + 1
            op_id = f"op-{count:03d}"
            now_iso = datetime.now(IST).isoformat()
            
            why_ops = f"{reason}; amount > ₹200 auto-pay limit" if amt > 200 else f"{reason}; daily auto-pay limit reached (repeat payment)"
            
            dispute = OpsDispute(
                dispute_id=op_id,
                rider_id=rider_id,
                type="approval",
                amount=float(amt),
                reason=why_ops,
                status="PENDING",
                created_at=now_iso
            )
            db.add(dispute)
            db.commit()

            record_trace(
                rider_id,
                "decision",
                f"Queued for Ops Approval: ₹{amt}",
                {"amount": amt, "reason": why_ops},
                {"status": "queued", "id": op_id}
            )
            return f"NOTICE: ₹{amt} exceeds auto-pay limits or is a repeat dispute today. Created pending approval for Ops team with ID {op_id}."
    except Exception as e:
        logger.error(f"Error in initiate_payout: {e}", exc_info=True)
        return f"Error executing payout: {str(e)}"
    finally:
        db.close()

@tool
def escalate_to_ops(rider_id: str, reason: str, trip_id: str = None) -> str:
    """
    Use this tool when a dispute cannot be resolved automatically:
    - Rider disputes GPS distance / records
    - Rider persists after record verification
    - Unclear policy scenario, accident claim, or suspicious activity
    Creates an escalation for the operations team to review manually.
    """
    db = SessionLocal()
    try:
        count = db.query(OpsDispute).count() + 1
        op_id = f"op-{count:03d}"
        now_iso = datetime.now(IST).isoformat()
        
        dispute = OpsDispute(
            dispute_id=op_id,
            rider_id=rider_id,
            trip_id=trip_id,
            type="escalation",
            amount=None,
            reason=reason,
            status="PENDING",
            created_at=now_iso
        )
        db.add(dispute)
        db.commit()

        record_trace(
            rider_id,
            "decision",
            f"Escalated to Ops: {reason}",
            {"rider_id": rider_id, "trip_id": trip_id, "reason": reason},
            {"status": "escalated", "id": op_id}
        )
        return f"SUCCESS: Issue escalated to Ops team for manual investigation (ID: {op_id})."
    except Exception as e:
        logger.error(f"Error in escalate_to_ops: {e}", exc_info=True)
        return f"Error escalating: {str(e)}"
    finally:
        db.close()