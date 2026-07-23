#!/usr/bin/env python3
"""
Evals for the optimizer's decision logic (decide_night).

This is a rule-based system, so "eval" = assert the exactly-correct action for known inputs.
The point is to lock the SAFETY GUARANTEES that touch live money into tests, not comments:
  - booked nights and weekends are never priced
  - a night is only touched inside its property's intervention window
  - a price is only pushed when it LOWERS an existing override (never raises)

Run: python3 tests/test_optimizer.py   (no dependencies)
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

import weekday_price_optimizer as opt

# Representative configs (mirror the two live properties' shape, not their identity).
PROP_A = {"force_clear_days": 7, "intervention_days": 16, "force_clear_price": 95, "intervention_price": 110}
# PROP_B has the middle tier DISABLED (intervention_days == force_clear_days) → 8d+ gets no action.
PROP_B = {"force_clear_days": 7, "intervention_days": 7, "force_clear_price": 120, "intervention_price": 150}
# Weekend watch configs (alert-only). Both live properties are watched in production; NO_WATCH
# below is a bare config (no 'weekend_watch' key) standing in for an opted-out property.
SSC_WATCH = {**PROP_B, "weekend_watch": {"watch_days": 15, "urgent_days": 8}}
HH_WATCH = {**PROP_A, "weekend_watch": {"watch_days": 13, "urgent_days": 9}}
NO_WATCH = PROP_A  # PROP_A shape without a weekend_watch key → opted out

MON, TUE, WED, THU, FRI, SAT, SUN = 0, 1, 2, 3, 4, 5, 6

CASES = [
    # (name, cfg, weekday, lead, is_booked, existing_price, expected_action, expected_target)

    # --- safety: booked + weekend are never priced ---
    ("booked night never touched",        PROP_A, WED, 3,  True,  None, "skip_booked", None),
    ("booked outranks everything",        PROP_A, WED, 3,  True,  200,  "skip_booked", None),
    ("Friday never touched",              PROP_A, FRI, 3,  False, None, "skip_weekend", None),
    ("Saturday never touched",            PROP_A, SAT, 3,  False, None, "skip_weekend", None),
    ("Sunday never touched",              PROP_A, SUN, 3,  False, None, "skip_weekend", None),

    # --- PROP_A tier boundaries (force_clear<=7, intervention 8-16, none >16) ---
    ("PROP_A lead 7 = force-clear",           PROP_A, MON, 7,  False, None, "push", 95),
    ("PROP_A lead 8 = intervention",          PROP_A, MON, 8,  False, None, "push", 110),
    ("PROP_A lead 16 = intervention (edge)",  PROP_A, MON, 16, False, None, "push", 110),
    ("PROP_A lead 17 = no action",            PROP_A, MON, 17, False, None, "none", None),

    # --- never-raise guard (the core money-safety rule) ---
    ("existing == target → skip",         PROP_A, MON, 5,  False, 95,   "skip_already_lower", 95),
    ("existing below target → skip",      PROP_A, MON, 5,  False, 80,   "skip_already_lower", 95),
    ("existing above target → push down", PROP_A, MON, 5,  False, 120,  "push", 95),
    ("no existing → push",                PROP_A, MON, 5,  False, None, "push", 95),
    ("existing above interv → push down", PROP_A, MON, 10, False, 130,  "push", 110),

    # --- PROP_B middle tier disabled ---
    ("PROP_B lead 7 = force-clear",          PROP_B, TUE, 7, False, None, "push", 120),
    ("PROP_B lead 8 = NO action (no tier)",  PROP_B, TUE, 8, False, None, "none", None),
    ("PROP_B lead 20 = NO action",           PROP_B, TUE, 20, False, None, "none", None),
    ("PROP_B existing 98 <= 120 → skip",     PROP_B, TUE, 3, False, 98,   "skip_already_lower", 120),
    ("PROP_B existing 150 > 120 → push down",PROP_B, TUE, 3, False, 150,  "push", 120),
]

# Weekend watch is ALERT-ONLY — it must never carry a price or an action, only a severity flag.
# (name, cfg, weekday, lead, is_booked, expected_flag)
WATCH_CASES = [
    ("PROP_B Fri lead 20 → none (outside window)", SSC_WATCH, FRI, 20, False, "none"),
    ("PROP_B Fri lead 16 → none (just outside)",   SSC_WATCH, FRI, 16, False, "none"),
    ("PROP_B Fri lead 15 → watch (median edge)",   SSC_WATCH, FRI, 15, False, "watch"),
    ("PROP_B Sat lead 9 → watch",                  SSC_WATCH, SAT, 9,  False, "watch"),
    ("PROP_B Fri lead 8 → urgent (p25 edge)",      SSC_WATCH, FRI, 8,  False, "urgent"),
    ("PROP_B Sat lead 2 → urgent",                 SSC_WATCH, SAT, 2,  False, "urgent"),
    ("PROP_B Fri booked → none (already sold)",    SSC_WATCH, FRI, 5,  True,  "none"),
    ("PROP_B weekday is not a watch night",        SSC_WATCH, MON, 5,  False, "none"),
    ("PROP_B Sunday is not a watch night",         SSC_WATCH, SUN, 5,  False, "none"),

    # PROP_A watch: tighter thresholds (watch <=13, urgent <=9).
    ("PROP_A Fri lead 14 → none (outside window)",  HH_WATCH,  FRI, 14, False, "none"),
    ("PROP_A Fri lead 13 → watch (median edge)",    HH_WATCH,  FRI, 13, False, "watch"),
    ("PROP_A Sat lead 10 → watch",                  HH_WATCH,  SAT, 10, False, "watch"),
    ("PROP_A Fri lead 9 → urgent (p25 edge)",       HH_WATCH,  FRI, 9,  False, "urgent"),
    ("PROP_A Sat lead 3 → urgent",                  HH_WATCH,  SAT, 3,  False, "urgent"),
    ("PROP_A Fri booked → none (already sold)",     HH_WATCH,  FRI, 5,  True,  "none"),

    ("property without weekend_watch key → none", NO_WATCH, SAT, 3, False, "none"),
]


def run():
    passed = failed = 0
    for name, cfg, wd, lead, booked, existing, exp_action, exp_target in CASES:
        d = opt.decide_night(cfg, wd, lead, booked, existing)
        ok = d["action"] == exp_action and d.get("target") == exp_target
        # Extra invariant on every push: it must never RAISE an existing price.
        if d["action"] == "push" and existing is not None:
            ok = ok and d["target"] < int(existing)
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}\n    got {d}, expected action={exp_action} target={exp_target}")

    # Property invariant sweep: across a wide grid, a push NEVER raises an existing price.
    for cfg in (PROP_A, PROP_B):
        for lead in range(1, 30):
            for existing in (None, 50, 95, 110, 120, 200, 400):
                d = opt.decide_night(cfg, MON, lead, False, existing)
                if d["action"] == "push" and existing is not None:
                    assert d["target"] < int(existing), f"RAISE BUG: {cfg} lead={lead} existing={existing} → {d}"
    print("  invariant sweep: a push never raises an existing price ✓")

    # GUARD 1 — data sanity: empty bookings must abort; non-empty must pass.
    assert opt.sanity_check_bookings([])[0] is False, "empty bookings should FAIL sanity"
    assert opt.sanity_check_bookings([{"arrival": "2026-08-01"}])[0] is True, "non-empty should pass"

    # GUARD 2 — circuit breaker: at/under cap ok, over cap aborts.
    assert opt.check_circuit_breaker(0, 15)[0] is True
    assert opt.check_circuit_breaker(3, 15)[0] is True
    assert opt.check_circuit_breaker(15, 15)[0] is True, "boundary (== cap) should pass"
    assert opt.check_circuit_breaker(16, 15)[0] is False, "over cap should abort"
    print("  guardrails: data-sanity + circuit-breaker ✓")

    # --- weekend watch (alert-only) ---
    for name, cfg, wd, lead, booked, exp_flag in WATCH_CASES:
        w = opt.watch_weekend(cfg, wd, lead, booked)
        # alert-only invariant: a watch result NEVER carries a price or an action.
        ok = (w["flag"] == exp_flag
              and "target" not in w and "action" not in w and "price" not in w)
        if ok:
            passed += 1
        else:
            failed += 1
            print(f"  FAIL: {name}\n    got {w}, expected flag={exp_flag}")

    # Invariant sweep: weekend watch never emits a price/action and its flag stays in-domain.
    for cfg in (SSC_WATCH, HH_WATCH):
        for wd in (FRI, SAT):
            for lead in range(1, 40):
                for booked in (True, False):
                    w = opt.watch_weekend(cfg, wd, lead, booked)
                    assert w["flag"] in ("none", "watch", "urgent"), f"bad flag: {w}"
                    assert "target" not in w and "action" not in w and "price" not in w, \
                        f"WEEKEND WATCH LEAKED A PRICE/ACTION: {w}"
                    if booked:
                        assert w["flag"] == "none", f"booked weekend must not flag: {w}"
    # The pricing core still never touches weekends, even with the watch enabled.
    assert opt.decide_night(SSC_WATCH, FRI, 5, False, None)["action"] == "skip_weekend"
    assert opt.decide_night(SSC_WATCH, SAT, 5, False, 300)["action"] == "skip_weekend"
    print("  weekend watch: alert-only (never prices), pricing core still skips weekends ✓")

    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
