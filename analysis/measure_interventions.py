#!/usr/bin/env python3
"""
measure_interventions.py — Learning loop, STAGE 2: measurement (READ-ONLY).

Turns the optimizer's recording into signal. Joins each logged intervention (interventions.jsonl)
against actual booking outcomes from OwnerRez to answer the core question:

    When we set a price on an open midweek night, did that night then book — and roughly when
    and at what rate — versus nights we let the pricing engine run?

Aggregates conversion by (property x lead-bucket x price) so that, once enough data accumulates,
the thresholds can be re-derived from outcomes instead of a one-time hand analysis.

Writes NOTHING to any live system. Pure analysis. Safe to run anytime.

Usage:  python3 measure_interventions.py
Reads credentials from .env.optimizer (same file the optimizer uses).
"""

import os
import json
import base64
import datetime
import subprocess
import urllib.request
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
INTERVENTION_LOG = os.environ.get("INTERVENTION_LOG",
                                 os.path.join(HERE, "private", "interventions.jsonl"))
ENV_FILE = os.path.join(HERE, "private", ".env")

try:
    from config import PROPERTIES as _PROPS
except ImportError:  # pragma: no cover
    raise SystemExit("Missing config.py — copy config.example.py to config.py.")

# {listing_id: display name}
PROPERTIES = {pid: cfg["name"] for pid, cfg in _PROPS.items()}


def load_env():
    """Load .env.optimizer into os.environ if the vars aren't already set (for standalone runs)."""
    if os.environ.get("OWNERREZ_PAT") and os.environ.get("PRICELABS_API_KEY"):
        return
    if not os.path.exists(ENV_FILE):
        return
    for line in open(ENV_FILE):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


def or_get(url):
    email = os.environ["OWNERREZ_EMAIL"]
    pat = os.environ["OWNERREZ_PAT"]
    auth = base64.b64encode(f"{email}:{pat}".encode()).decode()
    req = urllib.request.Request(url, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def fetch_bookings(prop_id):
    base = "https://api.ownerrez.com"
    url = f"/v2/bookings?property_ids={prop_id}&limit=100"
    out = []
    while url:
        d = or_get(base + url)
        out.extend(d.get("items", []))
        nxt = d.get("next_page_url")
        url = nxt.replace(base, "") if nxt else None
    return [b for b in out if b.get("status") == "active" and not b.get("is_block")]


def booking_index(bookings):
    """Map each booked night -> the booking that covers it (for outcome lookup).

    Keeps the FULL booked_utc timestamp (not just the date) so same-day attribution can be
    resolved precisely — a night booked earlier on the same day the optimizer ran must not be
    credited to that run.
    """
    idx = {}
    for b in bookings:
        arr = datetime.date.fromisoformat(b["arrival"])
        dep = datetime.date.fromisoformat(b["departure"])
        nights = (dep - arr).days or 1
        approx_nightly = float(b.get("total_amount") or 0) / nights
        booked_utc = b.get("booked_utc") or None
        d = arr
        while d < dep:
            idx[d.isoformat()] = {
                "booked_on": (booked_utc or "")[:10] or None,
                "booked_utc": booked_utc,
                "approx_gross_per_night": round(approx_nightly, 2),
            }
            d += datetime.timedelta(days=1)
    return idx


def record_type_of(r):
    """Legacy rows (logged before record_type existed) are weekday pricing decisions."""
    return r.get("record_type") or "weekday_price"


def collapse_to_night_bucket(rows):
    """Pure: reduce daily observations to ONE per (property, night, lead-bucket).

    The optimizer logs every in-scope night on EVERY run, so a single night produces up to ~16
    rows as it counts down. Treating each row as an independent observation inflates n and
    weights long-open nights more heavily. The unit of analysis is the night-in-a-lead-bucket,
    not the daily observation. Keeps the LAST row per group (the price state closest to the
    outcome). Unit-tested in tests/test_measurement.py.
    """
    best = {}
    for r in rows:
        key = (r.get("property"), r.get("night"), lead_bucket(r["lead_days"]))
        prev = best.get(key)
        stamp = r.get("run_ts_utc") or r.get("run_ts") or ""
        if prev is None or stamp >= prev[0]:
            best[key] = (stamp, r)
    return [v[1] for v in best.values()]


def lead_bucket(days):
    if days <= 7:
        return "<=7d"
    if days <= 14:
        return "8-14d"
    if days <= 21:
        return "15-21d"
    return "22d+"


def outcome_for(night, run_ts, bidx, run_ts_utc=None):
    """Did this night book, and after our observation? Returns a classification + detail.

    Prefers a full UTC timestamp comparison (run_ts_utc vs booked_utc) so a night booked
    EARLIER on the same calendar day is not miscredited to that run. Falls back to the old
    date-only comparison for legacy rows that predate run_ts_utc.
    """
    hit = bidx.get(night)
    if not hit:
        return "still_open", None
    if run_ts_utc and hit.get("booked_utc"):
        # Both ISO-8601 UTC; lexical compare is chronological. Normalize the trailing Z.
        a = hit["booked_utc"].replace("Z", "+00:00")
        b = run_ts_utc.replace("Z", "+00:00")
        return ("booked_after" if a >= b else "booked_before_or_unknown"), hit
    booked_on = hit["booked_on"]
    run_day = (run_ts or "")[:10]
    if booked_on and run_day and booked_on >= run_day:
        return "booked_after", hit
    return "booked_before_or_unknown", hit


def aggregate(rows, bidx_by_name):
    """Pure: group intervention records by (property, lead-bucket, target_price) with outcome
    counts. No I/O. Returns (groups, resolved_count). Unit-tested in tests/test_measurement.py."""
    groups = defaultdict(lambda: {"n": 0, "booked_after": 0, "rates": []})
    resolved = 0
    for r in rows:
        bidx = bidx_by_name.get(r.get("property"), {})
        cls, hit = outcome_for(r["night"], r.get("run_ts"), bidx, r.get("run_ts_utc"))
        # target_price is None for CONTROL rows (nights left to PriceLabs), so they form their
        # own group per (property, lead-bucket) — that group is the comparison baseline.
        g = groups[(r.get("property"), lead_bucket(r["lead_days"]), r["target_price"])]
        g["n"] += 1
        if cls != "still_open":
            resolved += 1
        if cls == "booked_after":
            g["booked_after"] += 1
            if hit:
                g["rates"].append(hit["approx_gross_per_night"])
    return groups, resolved


def analyze_log(bidx_by_name):
    if not os.path.exists(INTERVENTION_LOG):
        print("No interventions.jsonl yet — nothing logged.")
        return
    raw = [json.loads(l) for l in open(INTERVENTION_LOG) if l.strip()]
    by_type = defaultdict(list)
    for r in raw:
        by_type[record_type_of(r)].append(r)
    print(f"\n=== FORWARD: intervention log ({len(raw)} records) ===")
    print("  by type: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(by_type.items())))
    if not raw:
        return

    # Conversion analysis covers priced nights + control nights. Weekend-watch rows are
    # observations with no price action, so they're excluded from the conversion table.
    rows = by_type["weekday_price"] + by_type["control"]
    if not rows:
        print("  (no priced/control rows yet)")
        return
    collapsed = collapse_to_night_bucket(rows)
    print(f"  collapsed {len(rows)} daily observations -> {len(collapsed)} night-bucket units")

    groups, resolved = aggregate(collapsed, bidx_by_name)
    print(f"Units with a resolved outcome so far: {resolved}/{len(collapsed)}")
    if resolved == 0:
        print("(Too early — the log started recently. Signal appears as logged nights reach their dates.)")
    print(f"\n{'property':9} {'lead':7} {'price':>8} {'n':>4} {'booked':>7} {'conv%':>6} {'~gross/nt':>10}")
    for (prop, lb, price), g in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1], kv[0][2] or 0)):
        avg = round(sum(g["rates"]) / len(g["rates"]), 0) if g["rates"] else "-"
        label = "CONTROL" if price is None else f"${price}"
        conv = round(100 * g["booked_after"] / g["n"], 1) if g["n"] else 0
        print(f"{prop:9} {lb:7} {label:>8} {g['n']:>4} {g['booked_after']:>7} {conv:>5}% {str(avg):>10}")
    print("  CONTROL = open nights we deliberately left to PriceLabs (the comparison baseline).")


def analyze_current_overrides(bidx_by_name):
    """Backward-looking, APPROXIMATE: existing price overrides vs whether the night booked.
    Caveat: only reflects overrides still present (overwritten history is gone), so treat as
    directional, not authoritative."""
    print("\n=== BACKWARD (approx, from current overrides — incomplete history) ===")
    api = os.environ["PRICELABS_API_KEY"]
    today = datetime.date.today()
    for prop_id, name in PROPERTIES.items():
        r = subprocess.run(
            ["curl", "-s", "-H", f"X-API-Key: {api}",
             f"https://api.pricelabs.co/v1/listings/{prop_id}/overrides?pms=ownerrez"],
            capture_output=True, text=True)
        ov = json.loads(r.stdout or "[]")
        ov = ov if isinstance(ov, list) else ov.get("overrides", ov.get("items", []))
        priced = [o for o in ov if o.get("price") and "date" in o]
        bidx = bidx_by_name[name]
        booked = sum(1 for o in priced if o["date"] in bidx)
        future = [o for o in priced if datetime.date.fromisoformat(o["date"]) >= today]
        fb = sum(1 for o in future if o["date"] in bidx)
        print(f"  {name}: {len(priced)} price overrides on record "
              f"| {booked} on now-booked nights "
              f"| future open+overridden: {len(future)-fb}/{len(future)} still open")


def main():
    load_env()
    bidx_by_name = {name: booking_index(fetch_bookings(pid)) for pid, name in PROPERTIES.items()}
    analyze_log(bidx_by_name)
    analyze_current_overrides(bidx_by_name)
    print("\n(Read-only analysis — no prices were changed.)")


if __name__ == "__main__":
    main()
