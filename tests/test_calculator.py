"""
tests/test_calculator.py

Unit tests for app/agent/calculator.py.
Pure functions only -- no DB, no LLM, no network.

Run with:
    pytest tests/test_calculator.py -v
"""

import pytest
from types import SimpleNamespace
from app.agent.calculator import (
    round_half_up,
    expected_fare,
    normalize_distance,
    normalize_rider_id,
    to_ist_date,
    is_disputable,
    resolve_date,
    audit_trip,
    audit_day,
    Finding,
    DayAudit,
    TripAudit,
)


# ===========================================================================
# round_half_up
# ===========================================================================

class TestRoundHalfUp:
    def test_half_rounds_up_not_to_even(self):
        # 46.5 -> Python round() gives 46 (banker's); we must get 47
        assert round_half_up(46.5) == 47

    def test_half_rounds_up_odd_base(self):
        assert round_half_up(47.5) == 48

    def test_below_half(self):
        assert round_half_up(46.4) == 46

    def test_above_half(self):
        assert round_half_up(46.6) == 47

    def test_exact_integer(self):
        assert round_half_up(25.0) == 25

    def test_another_half_case(self):
        # 37.5 -> 38 (never 37)
        assert round_half_up(37.5) == 38


# ===========================================================================
# expected_fare  — the critical 3 km at 1.5x test
# ===========================================================================

class TestExpectedFare:
    def test_3km_1_5x_surge_must_be_47(self):
        """Canonical test: 3 km at 1.5x -> (25 + 1*6)*1.5 = 46.5 -> 47."""
        assert expected_fare(3.0, 1.5) == 47

    def test_base_only_2km_no_surge(self):
        """2 km, 1x -> base fare only = 25."""
        assert expected_fare(2.0, 1.0) == 25

    def test_base_only_less_than_2km(self):
        """1 km, 1x -> still 25 (base only, no per-km component)."""
        assert expected_fare(1.0, 1.0) == 25

    def test_no_surge_10km(self):
        """10 km, no surge -> 25 + 8*6 = 73."""
        assert expected_fare(10.0, 1.0) == 73

    def test_surge_multiplier_applied_once(self):
        """5 km at 2.0x -> (25 + 3*6)*2 = (25+18)*2 = 86."""
        assert expected_fare(5.0, 2.0) == 86

    def test_no_double_rounding(self):
        """Rounding happens only at end; intermediate values must not be rounded."""
        # 3 km at 1.5x: base = 31.0, fare_float = 46.5 -> 47 (not 31 rounded first)
        result = expected_fare(3.0, 1.5)
        assert result == 47, f"Expected 47, got {result}"

    def test_default_surge_is_1(self):
        assert expected_fare(3.0) == expected_fare(3.0, 1.0)

    def test_surge_1_half_boundary(self):
        """4 km at 1x -> 25 + 2*6 = 37 (clean integer, no rounding ambiguity)."""
        assert expected_fare(4.0, 1.0) == 37


# ===========================================================================
# normalize_distance
# ===========================================================================

class TestNormalizeDistance:
    def test_metres_5000_converts_to_5km(self):
        km, converted = normalize_distance(5000.0)
        assert km == pytest.approx(5.0)
        assert converted is True

    def test_metres_threshold_boundary(self):
        """Exactly 100 is treated as km (not converted)."""
        km, converted = normalize_distance(100.0)
        assert km == pytest.approx(100.0)
        assert converted is False

    def test_above_threshold_converts(self):
        km, converted = normalize_distance(101.0)
        assert km == pytest.approx(0.101)
        assert converted is True

    def test_normal_km_not_converted(self):
        km, converted = normalize_distance(8.4)
        assert km == pytest.approx(8.4)
        assert converted is False

    def test_zero_distance(self):
        km, converted = normalize_distance(0.0)
        assert km == pytest.approx(0.0)
        assert converted is False


# ===========================================================================
# normalize_rider_id
# ===========================================================================

class TestNormalizeRiderId:
    def test_short_id_padded(self):
        assert normalize_rider_id("R7") == "R007"

    def test_two_digit_padded(self):
        assert normalize_rider_id("r19") == "R019"

    def test_whitespace_stripped(self):
        assert normalize_rider_id(" R019 ") == "R019"

    def test_already_canonical(self):
        assert normalize_rider_id("R019") == "R019"

    def test_three_digit_unchanged(self):
        assert normalize_rider_id("R007") == "R007"

    def test_lowercase_r(self):
        assert normalize_rider_id("r001") == "R001"

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            normalize_rider_id("RIDER7")


# ===========================================================================
# to_ist_date — UTC -> IST day change
# ===========================================================================

class TestToIstDate:
    def test_utc_midnight_becomes_ist_morning_same_day(self):
        # 2026-09-21T00:00:00Z = 05:30 IST on 21 Sep
        assert to_ist_date("2026-09-21T00:00:00Z") == "2026-09-21"

    def test_utc_late_night_becomes_next_ist_day(self):
        # 2026-09-21T22:30:00Z = 04:00 IST on 22 Sep -> date changes
        assert to_ist_date("2026-09-21T22:30:00Z") == "2026-09-22"

    def test_utc_18_30_is_ist_midnight_boundary(self):
        # 2026-09-21T18:30:00Z = 00:00 IST 22 Sep (exact boundary)
        assert to_ist_date("2026-09-21T18:30:00Z") == "2026-09-22"

    def test_utc_18_29_still_same_ist_day(self):
        # 2026-09-21T18:29:59Z = 23:59:59 IST on 21 Sep
        assert to_ist_date("2026-09-21T18:29:59Z") == "2026-09-21"

    def test_plain_date_string_passthrough(self):
        assert to_ist_date("2026-09-20") == "2026-09-20"

    def test_empty_string_returns_empty(self):
        assert to_ist_date("") == ""

    def test_z_suffix_handled(self):
        assert to_ist_date("2026-09-19T02:50:00Z") == "2026-09-19"


# ===========================================================================
# is_disputable — window boundary with two different 'today' values
# ===========================================================================

class TestIsDisputable:
    # Window: received_at = 2026-09-22 -> window [2026-09-16, 2026-09-22]

    def test_boundary_day_in_window(self):
        """16 Sep with today=22 Sep: exactly on the boundary -> disputable."""
        assert is_disputable("2026-09-16", "2026-09-22") is True

    def test_one_day_before_boundary_expired(self):
        """15 Sep with today=22 Sep: one day outside window -> expired."""
        assert is_disputable("2026-09-15", "2026-09-22") is False

    def test_same_day_as_received(self):
        """22 Sep with today=22 Sep: same day -> disputable."""
        assert is_disputable("2026-09-22", "2026-09-22") is True

    def test_future_date_not_disputable(self):
        """23 Sep with today=22 Sep: future -> not disputable."""
        assert is_disputable("2026-09-23", "2026-09-22") is False

    # Second 'today' value: received_at = 2026-09-19 -> window [2026-09-13, 2026-09-19]

    def test_boundary_different_today(self):
        """13 Sep with today=19 Sep: boundary -> disputable."""
        assert is_disputable("2026-09-13", "2026-09-19") is True

    def test_expired_different_today(self):
        """12 Sep with today=19 Sep: expired."""
        assert is_disputable("2026-09-12", "2026-09-19") is False

    def test_received_at_as_timestamp(self):
        """received_at can be a full UTC timestamp; must be converted to IST first."""
        # 2026-09-22T00:00:00Z = 2026-09-22 IST -> boundary = 2026-09-16
        assert is_disputable("2026-09-16", "2026-09-22T00:00:00Z") is True
        assert is_disputable("2026-09-15", "2026-09-22T00:00:00Z") is False


# ===========================================================================
# resolve_date
# ===========================================================================

class TestResolveDate:
    TODAY = "2026-09-22"

    def test_iso_date_in_text(self):
        assert resolve_date("trip on 2026-09-20 wrong fare", self.TODAY) == "2026-09-20"

    def test_aaj(self):
        assert resolve_date("aaj ka payout nahi mila", self.TODAY) == self.TODAY

    def test_today_english(self):
        assert resolve_date("today's incentive missing", self.TODAY) == self.TODAY

    def test_kal(self):
        assert resolve_date("kal ki trip ka paise nahi mile", self.TODAY) == "2026-09-21"

    def test_yesterday_english(self):
        assert resolve_date("yesterday fare was wrong", self.TODAY) == "2026-09-21"

    def test_bare_day_number(self):
        assert resolve_date("20 Sep ka payout nahi aaya", self.TODAY) == "2026-09-20"

    def test_bare_day_number_19(self):
        assert resolve_date("19 wale 5 order missing hain", self.TODAY) == "2026-09-19"

    def test_bare_day_number_rolls_back_month_29(self):
        """today=2026-10-02 with '29' -> 2026-09-29 (most recent day 29 on or before today)."""
        assert resolve_date("29", "2026-10-02") == "2026-09-29"

    def test_bare_day_number_rolls_back_month_20(self):
        """today=2026-10-02 with '20' -> 2026-09-20."""
        assert resolve_date("20", "2026-10-02") == "2026-09-20"

    def test_month_name_mismatch_returns_none_20_aug(self):
        """'20 aug' with today=2026-09-22 -> None (august != september)."""
        assert resolve_date("20 aug", "2026-09-22") is None

    def test_month_name_match_returns_date_20_sep(self):
        """'20 sep' with today=2026-09-22 -> 2026-09-20."""
        assert resolve_date("20 sep", "2026-09-22") == "2026-09-20"

    def test_impossible_day_in_month_returns_none_30_feb(self):
        """'30 feb' with today=2026-10-02 -> None."""
        assert resolve_date("30 feb", "2026-10-02") is None

    def test_31_never_raises(self):
        """'31' must never raise an exception even if impossible in rolled-back month."""
        # September has 30 days, so 31 rolls back to Sep and should safely return None, not raise
        res = resolve_date("31", "2026-10-02")
        assert res is None
        # In a month with 31 days or rolled back to August (31 days):
        res2 = resolve_date("31", "2026-09-22")
        assert res2 == "2026-08-31"

    def test_no_date_returns_none(self):
        assert resolve_date("main ek rider hoon", self.TODAY) is None


# ===========================================================================
# audit_trip
# ===========================================================================

def _trip(trip_id, status, distance_km, surge=1.0):
    return SimpleNamespace(
        trip_id=trip_id, status=status,
        distance_km_raw=distance_km, distance_km=distance_km, surge_multiplier=surge
    )


def _line(trip_id, amount, category="trip"):
    return SimpleNamespace(trip_id=trip_id, amount=amount, category=category)


class TestAuditTrip:
    def test_completed_unpaid_returns_finding(self):
        trip = _trip("T001", "completed", 3.0, 1.5)
        res = audit_trip(trip, [])
        assert isinstance(res, TripAudit)
        assert res.expected == 47
        assert res.paid == 0
        finding = res.finding
        assert finding is not None
        assert finding.kind == "unpaid"
        assert finding.amount == 47   # (25+1*6)*1.5 = 46.5 -> 47
        assert finding.key == "trip:T001"

    def test_completed_fully_paid_no_finding(self):
        trip = _trip("T001", "completed", 3.0, 1.5)
        lines = [_line("T001", 47.0)]
        res = audit_trip(trip, lines)
        assert res.expected == 47
        assert res.paid == 47
        assert res.finding is None

    def test_completed_underpaid_surge_missing(self):
        # Paid without surge (46), expected 47
        trip = _trip("T001", "completed", 3.0, 1.5)
        lines = [_line("T001", 31.0)]   # paid base only, forgot surge
        res = audit_trip(trip, lines)
        assert res.expected == 47
        assert res.paid == 31
        finding = res.finding
        assert finding is not None
        assert finding.kind == "underpaid"
        assert finding.amount == 16    # 47 - 31

    def test_completed_overpaid_no_finding(self):
        """Platform never claws back overpayment."""
        trip = _trip("T001", "completed", 3.0, 1.0)
        lines = [_line("T001", 999.0)]
        res = audit_trip(trip, lines)
        assert res.expected == 31
        assert res.paid == 999
        assert res.finding is None

    def test_cancelled_single_penalty_no_finding(self):
        trip = _trip("T002", "cancelled_by_rider", 5.0)
        lines = [_line("T002", -10.0, "cancellation_penalty")]
        res = audit_trip(trip, lines)
        assert res.finding is None

    def test_cancelled_duplicate_penalty_finding(self):
        trip = _trip("T002", "cancelled_by_rider", 5.0)
        lines = [
            _line("T002", -10.0, "cancellation_penalty"),
            _line("T002", -10.0, "cancellation_penalty"),
        ]
        res = audit_trip(trip, lines)
        finding = res.finding
        assert finding is not None
        assert finding.kind == "duplicate_penalty"
        assert finding.amount == 10
        assert finding.key == "penalty:T002"

    def test_non_completed_non_cancelled_no_finding(self):
        trip = _trip("T003", "cancelled_by_driver", 5.0)
        res = audit_trip(trip, [])
        assert res.finding is None

    def test_metres_distance_auto_converted(self):
        """distance_km = 5000 (metres) -> 5.0 km; fare at 1x = 25 + (5-2)*6 = 43."""
        trip = _trip("T004", "completed", 5000.0, 1.0)  # stored as metres
        res = audit_trip(trip, [])
        assert res.expected == 43
        assert res.paid == 0
        finding = res.finding
        assert finding is not None
        # 5000 metres -> 5.0 km -> 25 + max(0, 5-2)*6 = 25 + 18 = 43
        assert finding.amount == 43

    def test_audit_trip_converts_raw_distance_5000_to_5km(self):
        """audit_trip converts a raw distance of 5000 (metres) to 5.0 km (fare = 43 at 1x)."""
        trip = SimpleNamespace(
            trip_id="T004", status="completed", distance_km_raw=5000.0, surge_multiplier=1.0
        )
        res = audit_trip(trip, [])
        assert res.expected == 43
        assert res.paid == 0
        finding = res.finding
        assert finding is not None
        assert finding.kind == "unpaid"
        # 5000 raw -> converted to 5.0 km -> (25 + (5 - 2) * 6) * 1.0 = 43
        # If unconverted (5000 km), fare would be 25 + 4998 * 6 = 30013
        assert finding.amount == 43


# ===========================================================================
# audit_day
# ===========================================================================

class TestAuditDay:
    def test_incentive_owed_when_12_trips_no_payment(self):
        trips = [
            _trip(f"T{i:03d}", "completed", 3.0)
            for i in range(12)
        ]
        # No ledger lines at all
        result = audit_day("2026-09-21", "2026-09-22", trips, [])
        assert result.incentive_eligible is True
        assert result.incentive_paid is False
        assert result.incentive_finding is not None
        assert result.incentive_finding.amount == 150
        assert result.incentive_finding.key == "incentive:2026-09-21"

    def test_incentive_not_owed_when_already_paid(self):
        trips = [_trip(f"T{i:03d}", "completed", 3.0) for i in range(12)]
        incentive_line = SimpleNamespace(
            trip_id=None, amount=150.0,
            category="daily_incentive", date="2026-09-21"
        )
        result = audit_day("2026-09-21", "2026-09-22", trips, [incentive_line])
        assert result.incentive_paid is True
        assert result.incentive_finding is None

    def test_less_than_12_trips_no_incentive(self):
        trips = [_trip(f"T{i:03d}", "completed", 3.0) for i in range(11)]
        result = audit_day("2026-09-21", "2026-09-22", trips, [])
        assert result.incentive_eligible is False
        assert result.incentive_finding is None

    def test_trip_finding_aggregated(self):
        trips = [_trip("T001", "completed", 5.0, 1.0)]  # expect 43
        # No ledger lines -> unpaid finding
        result = audit_day("2026-09-21", "2026-09-22", trips, [])
        assert len(result.findings) == 1
        assert result.findings[0].kind == "unpaid"
        assert result.findings[0].amount == 43

    def test_total_owed_sums_all_findings(self):
        trips = [_trip(f"T{i:03d}", "completed", 3.0) for i in range(12)]
        # No payments -> 12 unpaid trips @ 31 each + 150 incentive
        result = audit_day("2026-09-21", "2026-09-22", trips, [])
        expected_trip_total = 31 * 12
        assert result.total_owed() == expected_trip_total + 150

    def test_completed_count_correct(self):
        trips = [
            _trip("T001", "completed", 3.0),
            _trip("T002", "cancelled_by_rider", 3.0),
            _trip("T003", "completed", 3.0),
        ]
        result = audit_day("2026-09-21", "2026-09-22", trips, [])
        assert result.completed_count == 2
