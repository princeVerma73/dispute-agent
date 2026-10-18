"""
calculator.py — Pure business-logic functions for the dispute agent.

No database access, no LLM calls, no I/O of any kind.
Every function is deterministic and fully unit-testable.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

IST_OFFSET = timezone(timedelta(hours=5, minutes=30))

BASE_FARE = 25.0          # Rs for first 2 km
PER_KM_RATE = 6.0         # Rs per km after 2 km
INCENTIVE_AMOUNT = 150    # Rs daily incentive
INCENTIVE_TRIP_MIN = 12   # minimum completed trips for incentive
CANCELLATION_PENALTY = 10 # Rs per rider-cancelled trip
DISPUTE_WINDOW_DAYS = 7   # calendar days (inclusive of message day)
METRES_THRESHOLD = 100.0  # raw distance values above this are in metres


# ---------------------------------------------------------------------------
# Rounding
# ---------------------------------------------------------------------------

def round_half_up(x: float) -> int:
    """Round to nearest integer, with 0.5 always rounding UP (never to even).

    Uses floor(x + 0.5) which is the classic "round half away from zero"
    for positive values.  Never delegates to Python's built-in round() which
    uses banker's rounding.

    Examples:
        round_half_up(46.5) == 47   (not 46 as Python round() gives)
        round_half_up(46.4) == 46
        round_half_up(47.5) == 48
    """
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Fare calculation
# ---------------------------------------------------------------------------

def expected_fare(distance_km: float, surge: float = 1.0) -> int:
    """Compute the expected fare for a completed trip.

    Formula: (25 + max(0, km - 2) x 6) x surge, rounded ONCE at the end
    using round_half_up.  Rounding is applied only to the final result, NOT
    to any intermediate value.

    Args:
        distance_km: trip distance in kilometres (already normalised).
        surge: surge multiplier (e.g. 1.5 for 1.5x surge).

    Returns:
        Integer rupees owed for the trip.

    Example:
        expected_fare(3, 1.5) == 47   # (25 + 1*6)*1.5 = 46.5 -> 47
        expected_fare(2, 1.0) == 25   # base only
    """
    base = BASE_FARE + max(0.0, distance_km - 2.0) * PER_KM_RATE
    return round_half_up(base * surge)


# ---------------------------------------------------------------------------
# Distance normalisation
# ---------------------------------------------------------------------------

def normalize_distance(raw: float) -> tuple:
    """Convert raw distance to kilometres.

    The CSV occasionally stores distances in metres (e.g. 5000 instead of 5.0).
    Any value > METRES_THRESHOLD (100) is assumed to be metres and divided by
    1000.

    Returns:
        (km, was_converted) -- was_converted is True when conversion happened.
    """
    if raw > METRES_THRESHOLD:
        return raw / 1000.0, True
    return float(raw), False


# ---------------------------------------------------------------------------
# Rider ID normalisation
# ---------------------------------------------------------------------------

def normalize_rider_id(raw: str) -> str:
    """Normalise any rider ID variant to canonical 'RXXX' (3-digit, zero-padded).

    Handles:
        'R7'    -> 'R007'
        'r19'   -> 'R019'
        ' R019 '-> 'R019'
        'R007'  -> 'R007'

    Raises:
        ValueError if the input doesn't contain a recognisable numeric part.
    """
    stripped = raw.strip().upper()
    m = re.fullmatch(r"R(\d+)", stripped)
    if not m:
        raise ValueError(f"Unrecognisable rider ID format: {raw!r}")
    return f"R{int(m.group(1)):03d}"


# ---------------------------------------------------------------------------
# UTC -> IST date
# ---------------------------------------------------------------------------

def to_ist_date(ts: str) -> str:
    """Convert a UTC ISO-8601 timestamp to an IST calendar date (YYYY-MM-DD).

    Handles trailing 'Z' and plain YYYY-MM-DD inputs as fallback.

    Args:
        ts: e.g. '2026-09-21T22:30:00Z'  (just past midnight IST on 22 Sep)

    Returns:
        e.g. '2026-09-22'
    """
    if not ts:
        return ""
    try:
        clean = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            # Treat naive datetimes as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        ist_dt = dt.astimezone(IST_OFFSET)
        return ist_dt.strftime("%Y-%m-%d")
    except Exception:
        # Graceful fallback: assume first 10 chars are already a date
        return str(ts)[:10]


# ---------------------------------------------------------------------------
# Dispute window
# ---------------------------------------------------------------------------

def is_disputable(trip_ist_date: str, received_at: str) -> bool:
    """Return True if the trip date falls within the 7-calendar-day window.

    The window is [received_date - 6 days, received_date] inclusive (i.e.
    the message day counts as day 1, so 7 days back = received - 6).

    Both arguments may be YYYY-MM-DD strings or ISO timestamps (converted
    via to_ist_date for received_at).

    Boundary test (received_at = 2026-09-22):
        earliest disputable = 2026-09-16
        2026-09-15 -> False (expired)
        2026-09-16 -> True  (boundary)
        2026-09-22 -> True  (same day)
    """
    # Normalise received_at to a date string if it looks like a full timestamp
    if len(received_at) > 10:
        today_str = to_ist_date(received_at)
    else:
        today_str = received_at

    today_d = date.fromisoformat(today_str)
    trip_d = date.fromisoformat(trip_ist_date[:10])
    earliest = today_d - timedelta(days=DISPUTE_WINDOW_DAYS - 1)
    return earliest <= trip_d <= today_d


# ---------------------------------------------------------------------------
# Date resolution from natural-language text
# ---------------------------------------------------------------------------

def resolve_date(text: str, today: str) -> Optional[str]:
    """Resolve a date reference in Hinglish/English text to YYYY-MM-DD.

    Rules (checked in order):
      1. ISO date already present (2026-09-20) -> return it.
      2. 'aaj' or 'today' -> today.
      3. 'kal' or 'yesterday' -> today - 1 day.
      4. Bare day number in the text (e.g. '20', '19') -> same year+month as
         today, with that day number.  Only numbers 1-31 are considered.

    Args:
        text:  The raw message text.
        today: YYYY-MM-DD string of the message's IST date.

    Returns:
        YYYY-MM-DD string, or None if no date could be resolved.
    """
    try:
        lower = text.lower().strip()
        today_d = date.fromisoformat(today)

        # Check if text mentions any month name (jan..dec, common spellings)
        month_names = {
            "jan": 1, "january": 1, "janurary": 1,
            "feb": 2, "february": 2, "feburary": 2,
            "mar": 3, "march": 3,
            "apr": 4, "april": 4,
            "may": 5,
            "jun": 6, "june": 6,
            "jul": 7, "july": 7,
            "aug": 8, "august": 8,
            "sep": 9, "sept": 9, "september": 9,
            "oct": 10, "october": 10,
            "nov": 11, "november": 11,
            "dec": 12, "december": 12,
        }
        m_month = re.search(
            r"\b(jan(?:uary|urary)?|feb(?:ruary|urary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
            lower
        )
        expected_month = month_names.get(m_month.group(1).lower()) if m_month else None

        candidate: Optional[str] = None

        # 1. Explicit ISO date
        m_iso = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
        if m_iso:
            candidate = m_iso.group(1)
        # 2. 'aaj' / 'today'
        elif re.search(r"\baaj\b|\btoday\b", lower):
            candidate = today
        # 3. 'kal' / 'yesterday'
        elif re.search(r"\bkal\b|\byesterday\b", lower):
            candidate = (today_d - timedelta(days=1)).isoformat()
        else:
            # 4. Bare day number (1-31): resolves to most recent such day on or before today
            m_day = re.search(r"\b([1-9]|[12]\d|3[01])\b", lower)
            if m_day:
                day_num = int(m_day.group(1))
                if day_num <= today_d.day:
                    try:
                        candidate = today_d.replace(day=day_num).isoformat()
                    except ValueError:
                        candidate = None
                else:
                    # Day number is in the future for current month -> roll back one month
                    prev_year = today_d.year if today_d.month > 1 else today_d.year - 1
                    prev_month = today_d.month - 1 if today_d.month > 1 else 12
                    try:
                        candidate = date(prev_year, prev_month, day_num).isoformat()
                    except ValueError:
                        candidate = None

        if candidate is None:
            return None

        # Validate candidate month matches mentioned month (if any)
        cand_d = date.fromisoformat(candidate)
        if expected_month is not None and cand_d.month != expected_month:
            return None

        return candidate
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Trip-level audit
# ---------------------------------------------------------------------------

class Finding:
    """Represents a single discrepancy finding."""

    __slots__ = ("key", "trip_id", "kind", "amount", "description", "date")

    def __init__(self, key: str, trip_id: Optional[str], kind: str,
                 amount: int, description: str, date: Optional[str] = None):
        self.key = key            # stable, e.g. 'trip:T123', 'penalty:T123'
        self.trip_id = trip_id
        self.kind = kind          # 'underpaid' | 'unpaid' | 'duplicate_penalty' | 'incentive'
        self.amount = amount      # rupees owed to the rider
        self.description = description
        self.date = date

    def __repr__(self) -> str:
        return f"Finding({self.key!r}, Rs{self.amount}, {self.kind!r})"


@dataclass
class TripAudit:
    """Consistent result of auditing a single trip."""
    expected: int
    paid: int
    finding: Optional[Finding]

    @property
    def expected_fare(self) -> int:
        return self.expected

    @property
    def paid_amount(self) -> int:
        return self.paid

    def __iter__(self):
        return iter((self.expected, self.paid, self.finding))

    def __repr__(self) -> str:
        return f"TripAudit(expected={self.expected}, paid={self.paid}, finding={self.finding!r})"


# Alias for backward compatibility
TripAuditSummary = TripAudit


def audit_trip(trip: object, ledger_lines: list) -> TripAudit:
    """Check a single trip for discrepancies against ledger payout lines.

    Args:
        trip: any object with attributes:
            .trip_id          (str)
            .status           (str)  -- 'completed' | 'cancelled_by_rider' | ...
            .distance_km_raw / .distance_km (float) -- raw value (normalisation applied here)
            .surge_multiplier (float)
        ledger_lines: all PayoutLine-like objects for this trip, each with:
            .trip_id  (str | None)
            .amount   (float)
            .category (str)

    Returns:
        TripAudit dataclass with (expected, paid, finding).

    Notes:
        - Overpayments are NOT findings (platform never claws back).
        - For cancelled_by_rider trips, checks only for duplicate -10 penalty
          lines (more than one deduction means extra*10 is owed).
        - For completed trips, checks whether total positive payments cover
          the expected fare.
    """
    trip_id = trip.trip_id

    if trip.status == "cancelled_by_rider":
        # Duplicate cancellation penalty detection
        penalty_lines = [
            ln for ln in ledger_lines
            if (getattr(ln, "category", "") == "cancellation_penalty" or
                getattr(ln, "amount", 0) < 0)
        ]
        trip_date = getattr(trip, "date", None)
        extra_count = len(penalty_lines) - 1  # 1 legitimate, rest are extras
        finding = None
        if extra_count > 0:
            amount_owed = extra_count * CANCELLATION_PENALTY
            finding = Finding(
                key=f"penalty:{trip_id}",
                trip_id=trip_id,
                kind="duplicate_penalty",
                amount=amount_owed,
                description=(
                    f"Trip {trip_id}: {len(penalty_lines)} cancellation penalty "
                    f"lines found (expected 1). Extra Rs{amount_owed} deducted in error."
                ),
                date=trip_date,
            )
        return TripAudit(expected=0, paid=0, finding=finding)

    if trip.status != "completed":
        # Other non-completed statuses earn Rs0 -- no finding
        return TripAudit(expected=0, paid=0, finding=None)

    # Normalise distance (handle metres-stored-as-km, raw unconverted value)
    raw_dist = float(getattr(trip, "distance_km_raw", getattr(trip, "distance_km", 0.0)))
    km, _ = normalize_distance(raw_dist)
    surge = float(getattr(trip, "surge_multiplier", 1.0))
    earned = expected_fare(km, surge)

    # Positive trip-fare payments only (exclude penalty/incentive lines)
    trip_payments = [
        ln for ln in ledger_lines
        if getattr(ln, "amount", 0) > 0 and
           getattr(ln, "category", "") not in ("daily_incentive",)
    ]
    paid = round_half_up(sum(ln.amount for ln in trip_payments))

    trip_date = getattr(trip, "date", None)
    finding = None
    if paid == 0:
        finding = Finding(
            key=f"trip:{trip_id}",
            trip_id=trip_id,
            kind="unpaid",
            amount=earned,
            description=(
                f"Trip {trip_id}: completed, expected Rs{earned}, "
                f"zero payment found in ledger."
            ),
            date=trip_date,
        )
    elif earned > paid:
        diff = earned - paid
        finding = Finding(
            key=f"trip:{trip_id}",
            trip_id=trip_id,
            kind="underpaid",
            amount=diff,
            description=(
                f"Trip {trip_id}: expected Rs{earned}, paid Rs{paid}, "
                f"underpaid by Rs{diff}."
            ),
            date=trip_date,
        )

    return TripAudit(expected=earned, paid=paid, finding=finding)


# ---------------------------------------------------------------------------
# Day-level audit
# ---------------------------------------------------------------------------

class DayAudit:
    """Result of auditing all trips + ledger for a single IST calendar day."""

    __slots__ = ("date", "findings", "completed_count", "incentive_eligible",
                 "incentive_paid", "incentive_finding")

    def __init__(self, date: str, findings: list, completed_count: int,
                 incentive_eligible: bool, incentive_paid: bool,
                 incentive_finding: Optional[Finding]):
        self.date = date
        self.findings = findings                  # list[Finding] -- trip-level
        self.completed_count = completed_count
        self.incentive_eligible = incentive_eligible
        self.incentive_paid = incentive_paid
        self.incentive_finding = incentive_finding  # Finding or None

    @property
    def all_findings(self) -> list:
        out = list(self.findings)
        if self.incentive_finding:
            out.append(self.incentive_finding)
        return out

    def total_owed(self) -> int:
        return sum(f.amount for f in self.all_findings)

    def __repr__(self) -> str:
        return (f"DayAudit({self.date!r}, completed={self.completed_count}, "
                f"findings={len(self.all_findings)}, owed=Rs{self.total_owed()})")


def audit_day(ist_date: str, today: str, trips: list, ledger: list) -> DayAudit:
    """Audit all trips and ledger entries for a single IST calendar day.

    Args:
        ist_date: the IST calendar date being audited, YYYY-MM-DD.
        today:    the message's IST date (YYYY-MM-DD), used for window check.
        trips:    list of Trip-like objects for this rider on ist_date, each
                  with .trip_id, .status, .distance_km, .surge_multiplier.
        ledger:   ALL PayoutLine-like objects for this rider -- this function
                  filters by trip_id / date internally.

    Returns:
        DayAudit with all findings and incentive status.

    Notes:
        - A day must be within the dispute window for findings to be actionable;
          this function still computes findings for expired days (callers decide).
        - Incentive: >= 12 completed trips in the IST day AND no
          'daily_incentive' line already present for that day -> Rs150 owed.
    """
    # Build per-trip ledger index
    trip_ledger: dict = {}
    for ln in ledger:
        if getattr(ln, "trip_id", None):
            trip_ledger.setdefault(ln.trip_id, []).append(ln)

    completed_count = sum(1 for t in trips if t.status == "completed")

    findings: list = []
    for trip in trips:
        trip_lines = trip_ledger.get(trip.trip_id, [])
        audit_res = audit_trip(trip, trip_lines)
        if audit_res.finding:
            findings.append(audit_res.finding)

    # Incentive check
    incentive_eligible = completed_count >= INCENTIVE_TRIP_MIN
    # Look for a daily_incentive payout line dated for this IST day
    incentive_paid = any(
        getattr(ln, "category", "") == "daily_incentive"
        and (
            getattr(ln, "date", None) == ist_date
            or getattr(ln, "payout_date", None) == ist_date
        )
        for ln in ledger
    )

    incentive_finding: Optional[Finding] = None
    if incentive_eligible and not incentive_paid:
        incentive_finding = Finding(
            key=f"incentive:{ist_date}",
            trip_id=None,
            kind="incentive",
            amount=INCENTIVE_AMOUNT,
            description=(
                f"{ist_date}: {completed_count} completed trips (>= {INCENTIVE_TRIP_MIN}), "
                f"daily incentive of Rs{INCENTIVE_AMOUNT} not yet paid."
            ),
            date=ist_date,
        )

    return DayAudit(
        date=ist_date,
        findings=findings,
        completed_count=completed_count,
        incentive_eligible=incentive_eligible,
        incentive_paid=incentive_paid,
        incentive_finding=incentive_finding,
    )
