#!/usr/bin/env python3
"""
Evals for the measurement tool's pure logic (measure_interventions).

Verifies the join/aggregation is correct against synthetic interventions + synthetic bookings —
so the "did it book after we intervened?" signal can be trusted before we act on it.

Run: python3 tests/test_measurement.py   (no dependencies)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "optimizer"))
sys.path.insert(0, os.path.join(ROOT, "analysis"))
import types as _types

# The modules under test import `config`; tests supply their own fixtures, so inject a stub
# rather than requiring a real config.py to exist.
if "config" not in sys.modules:
    _cfg = _types.ModuleType("config")
    _cfg.PROPERTIES = {}
    _cfg.LOOK_AHEAD_DAYS = 60
    _cfg.MAX_CHANGES_PER_RUN = 15
    _cfg.EMAIL_FROM = _cfg.EMAIL_TO = "test@example.com"
    sys.modules["config"] = _cfg

import measure_interventions as mi


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def test_lead_bucket():
    assert mi.lead_bucket(1) == "<=7d"
    assert mi.lead_bucket(7) == "<=7d"
    assert mi.lead_bucket(8) == "8-14d"
    assert mi.lead_bucket(14) == "8-14d"
    assert mi.lead_bucket(15) == "15-21d"
    assert mi.lead_bucket(21) == "15-21d"
    assert mi.lead_bucket(22) == "22d+"
    assert mi.lead_bucket(95) == "22d+"


def test_booking_index():
    # A 3-night booking at $300 total → each night indexed at ~$100 gross/night.
    bookings = [{"arrival": "2026-08-01", "departure": "2026-08-04",
                 "total_amount": 300, "booked_utc": "2026-07-20T10:00:00Z"}]
    idx = mi.booking_index(bookings)
    assert set(idx.keys()) == {"2026-08-01", "2026-08-02", "2026-08-03"}, idx.keys()
    assert "2026-08-04" not in idx  # departure night is NOT occupied
    assert approx(idx["2026-08-01"]["approx_gross_per_night"], 100.0)
    assert idx["2026-08-01"]["booked_on"] == "2026-07-20"
    # Full timestamp retained for same-day attribution precision.
    assert idx["2026-08-01"]["booked_utc"] == "2026-07-20T10:00:00Z"


def test_outcome_for():
    bidx = {"2026-08-10": {"booked_on": "2026-07-22", "approx_gross_per_night": 120.0}}
    # night not booked → still_open
    assert mi.outcome_for("2026-08-11", "2026-07-20T09:00:00", bidx)[0] == "still_open"
    # booked AFTER the intervention run → booked_after
    assert mi.outcome_for("2026-08-10", "2026-07-20T09:00:00", bidx)[0] == "booked_after"
    # booked BEFORE the intervention → not attributable
    assert mi.outcome_for("2026-08-10", "2026-07-25T09:00:00", bidx)[0] == "booked_before_or_unknown"
    # unknown booked_on → not attributable
    bidx2 = {"2026-08-10": {"booked_on": None, "approx_gross_per_night": 0.0}}
    assert mi.outcome_for("2026-08-10", "2026-07-20T09:00:00", bidx2)[0] == "booked_before_or_unknown"


def test_aggregate():
    # Two interventions on PROP at ≤7d/$120. One night booked after; one still open.
    rows = [
        {"property": "PROP", "night": "2026-08-10", "run_ts": "2026-07-20T09:00:00",
         "lead_days": 5, "target_price": 120, "action": "push"},
        {"property": "PROP", "night": "2026-08-12", "run_ts": "2026-07-20T09:00:00",
         "lead_days": 5, "target_price": 120, "action": "push"},
    ]
    bidx_by_name = {"PROP": {
        "2026-08-10": {"booked_on": "2026-07-22", "approx_gross_per_night": 175.0},  # booked after
        # 2026-08-12 absent → still open
    }}
    groups, resolved = mi.aggregate(rows, bidx_by_name)
    key = ("PROP", "<=7d", 120)
    assert resolved == 1, resolved                      # one night resolved (booked)
    assert groups[key]["n"] == 2
    assert groups[key]["booked_after"] == 1
    assert groups[key]["rates"] == [175.0]              # realized rate of the booked night


def test_record_type_of():
    assert mi.record_type_of({}) == "weekday_price"                    # legacy row
    assert mi.record_type_of({"record_type": "control"}) == "control"
    assert mi.record_type_of({"record_type": "weekend_watch"}) == "weekend_watch"


def test_collapse_to_night_bucket():
    # One night seen on 3 consecutive runs, all inside <=7d → ONE unit (not 3).
    rows = [
        {"property": "P", "night": "2026-08-10", "lead_days": 5, "target_price": 120,
         "run_ts_utc": "2026-08-05T07:30:00+00:00"},
        {"property": "P", "night": "2026-08-10", "lead_days": 4, "target_price": 120,
         "run_ts_utc": "2026-08-06T07:30:00+00:00"},
        {"property": "P", "night": "2026-08-10", "lead_days": 3, "target_price": 95,
         "run_ts_utc": "2026-08-07T07:30:00+00:00"},   # latest in bucket → the one kept
        # Same night in a DIFFERENT bucket is its own unit (preserves the lead dimension).
        {"property": "P", "night": "2026-08-10", "lead_days": 10, "target_price": 110,
         "run_ts_utc": "2026-07-31T07:30:00+00:00"},
    ]
    out = mi.collapse_to_night_bucket(rows)
    assert len(out) == 2, out
    by_bucket = {mi.lead_bucket(r["lead_days"]): r for r in out}
    assert by_bucket["<=7d"]["target_price"] == 95, by_bucket
    assert by_bucket["8-14d"]["target_price"] == 110
    # Different properties must never collapse together.
    assert len(mi.collapse_to_night_bucket([rows[0], dict(rows[0], property="Q")])) == 2


def test_outcome_for_same_day_precision():
    # Booked 06:00Z; run was 07:30Z the SAME day → must NOT be credited to the run.
    bidx = {"2026-08-10": {"booked_on": "2026-08-05", "booked_utc": "2026-08-05T06:00:00Z",
                           "approx_gross_per_night": 120.0}}
    cls, _ = mi.outcome_for("2026-08-10", "2026-08-05T07:30:00", bidx,
                            run_ts_utc="2026-08-05T07:30:00+00:00")
    assert cls == "booked_before_or_unknown", cls
    # Booked 09:00Z, after the 07:30Z run → attributable.
    bidx2 = {"2026-08-10": {"booked_on": "2026-08-05", "booked_utc": "2026-08-05T09:00:00Z",
                            "approx_gross_per_night": 120.0}}
    cls2, _ = mi.outcome_for("2026-08-10", "2026-08-05T07:30:00", bidx2,
                             run_ts_utc="2026-08-05T07:30:00+00:00")
    assert cls2 == "booked_after", cls2
    # Legacy row with no run_ts_utc falls back to date-only (old, coarser behavior).
    assert mi.outcome_for("2026-08-10", "2026-08-05T07:30:00", bidx)[0] == "booked_after"


def test_aggregate_control_group():
    # A priced night and a control night in the same bucket land in SEPARATE groups —
    # the control group is the baseline the whole learning loop depends on.
    rows = [
        {"property": "P", "night": "2026-08-10", "run_ts_utc": "2026-08-01T07:30:00+00:00",
         "lead_days": 5, "target_price": 120},
        {"property": "P", "night": "2026-08-11", "run_ts_utc": "2026-08-01T07:30:00+00:00",
         "lead_days": 5, "target_price": None},        # CONTROL (left to PriceLabs)
    ]
    bidx = {"P": {"2026-08-10": {"booked_on": "2026-08-03", "booked_utc": "2026-08-03T10:00:00Z",
                                 "approx_gross_per_night": 150.0}}}
    groups, resolved = mi.aggregate(rows, bidx)
    assert ("P", "<=7d", 120) in groups
    assert ("P", "<=7d", None) in groups, "control group must exist as its own baseline"
    assert groups[("P", "<=7d", 120)]["booked_after"] == 1
    assert groups[("P", "<=7d", None)]["booked_after"] == 0
    assert resolved == 1


TESTS = [test_lead_bucket, test_booking_index, test_outcome_for, test_aggregate,
         test_record_type_of, test_collapse_to_night_bucket,
         test_outcome_for_same_day_precision, test_aggregate_control_group]


def run():
    passed = failed = 0
    for t in TESTS:
        try:
            t()
            passed += 1
        except AssertionError as e:
            failed += 1
            print(f"  FAIL: {t.__name__}: {e}")
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
