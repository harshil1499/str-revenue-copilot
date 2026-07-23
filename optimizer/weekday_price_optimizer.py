#!/usr/bin/env python3
"""
Weekday Price Optimizer

Checks open weekday nights and applies date-specific price overrides based on lead-time
thresholds derived from each property's historical booking data. Thresholds, listing IDs and
notification addresses all live in config.py (gitignored) — see config.example.py.

Tiering, per property:
  beyond the intervention window   no action; the pricing engine runs free
  inside the intervention window   push the softer intervention price
  inside the force-clear window    push the force-clear price
Set intervention_days == force_clear_days to disable the middle tier for a property.

Weekend safety net — ALERT-ONLY, never writes a price:
  Fri/Sat are never priced, but an open weekend that has fallen behind its property's own
  booking pace is flagged in the daily email for a human. Thresholds are that property's
  median (watch) and 25th-percentile (urgent) weekend booking lead. See watch_weekend().

Run daily. Safe to re-run — never raises a price already set lower.
"""

import os
import json
import subprocess
import datetime
import io
import smtplib
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

try:
    from config import PROPERTIES, LOOK_AHEAD_DAYS, MAX_CHANGES_PER_RUN, EMAIL_FROM, EMAIL_TO
except ImportError:  # pragma: no cover
    raise SystemExit(
        "Missing config.py — copy config.example.py to config.py and fill in your values.")

WEEKDAYS = {0, 1, 2, 3}      # Mon=0 Tue=1 Wed=2 Thu=3 — the only nights the optimizer PRICES
WEEKEND_NIGHTS = {4, 5}      # Fri=4 Sat=5 — never priced; open ones are watched (alert-only)

# Structured event log for the learning loop (stage 1: capture). Append-only JSONL, one record
# per intervention-window night evaluated. Later joined against booking outcomes to measure
# whether interventions actually converted. Best-effort — must never interfere with pricing.
INTERVENTION_LOG = os.environ.get(
    'INTERVENTION_LOG',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'private', 'interventions.jsonl'))


def log_event(record):
    try:
        with open(INTERVENTION_LOG, 'a') as f:
            f.write(json.dumps(record) + '\n')
    except Exception:
        pass


def fetch_bookings(prop_id):
    base_url = 'https://api.ownerrez.com'
    all_bookings = []
    url = f'/v2/bookings?property_ids={prop_id}&limit=100'
    while url:
        r = subprocess.run(
            ['curl', '-s', '-u',
             f"{os.environ['OWNERREZ_EMAIL']}:{os.environ['OWNERREZ_PAT']}",
             f'{base_url}{url}'],
            capture_output=True, text=True)
        data = json.loads(r.stdout)
        all_bookings.extend(data.get('items', []))
        nxt = data.get('next_page_url')
        url = nxt.replace(base_url, '') if nxt else None
    return [b for b in all_bookings
            if b.get('status') == 'active' and not b.get('is_block')]


def get_booked_nights(bookings):
    booked = set()
    for b in bookings:
        arr = datetime.date.fromisoformat(b['arrival'])
        dep = datetime.date.fromisoformat(b['departure'])
        d = arr
        while d < dep:
            booked.add(d)
            d += datetime.timedelta(days=1)
    return booked


def get_current_overrides(listing_id):
    r = subprocess.run(
        ['curl', '-s', '-H', f"X-API-Key: {os.environ['PRICELABS_API_KEY']}",
         f'https://api.pricelabs.co/v1/listings/{listing_id}/overrides?pms=ownerrez'],
        capture_output=True, text=True)
    data = json.loads(r.stdout)
    items = data if isinstance(data, list) else data.get('overrides', data.get('items', []))
    return {item['date']: item for item in items if 'date' in item}


def fetch_pl_prices(listing_id, date_from, date_to):
    """PriceLabs' own price context for every night in the horizon — ONE call per listing.

    Captured at DECISION TIME (before we push), because on a night we have NOT overridden yet
    PL's `price` IS the counterfactual: what that night would have been priced at without us.
    That baseline is not retrievable after the night passes, which is why it must be logged
    rather than derived later.

    Field semantics verified against the PL Customer API spec + an empirical check (2026-07-23):
      price               PL's effective recommendation, INCLUDING any override we've set.
      uncustomized_price  PL price with customizations stripped. NOT a clean counterfactual —
                          it differs from `price` even on nights with no override at all.
      user_price          Last price the PMS actually saw ("-1" = day unavailable). The gap
                          vs `price` exposes the known DSO->PMS sync lag.

    Best-effort: returns {} on ANY failure. Price context is observability — it must never
    block or break pricing.
    """
    try:
        body = json.dumps({'listings': [{'id': listing_id, 'pms': 'ownerrez',
                                         'dateFrom': date_from, 'dateTo': date_to,
                                         'reason': False}]})
        r = subprocess.run(
            ['curl', '-s', '--max-time', '60', '-X', 'POST',
             'https://api.pricelabs.co/v1/listing_prices',
             '-H', f"X-API-Key: {os.environ['PRICELABS_API_KEY']}",
             '-H', 'Content-Type: application/json',
             '-d', body],
            capture_output=True, text=True)
        data = json.loads(r.stdout)
        rec = data[0] if isinstance(data, list) and data else {}
        out = {}
        for n in rec.get('data', []):
            if 'date' in n:
                out[n['date']] = {
                    'pl_price': n.get('price'),
                    'pl_uncustomized_price': n.get('uncustomized_price'),
                    'pms_last_seen_price': n.get('user_price'),
                    'pl_min_stay': n.get('min_stay'),
                }
        return out
    except Exception:
        return {}


def price_ctx(pl_prices, date_str):
    """PL price context for one night; empty dict if the best-effort fetch failed."""
    return pl_prices.get(date_str, {})


def push_override(listing_id, date_str, price):
    body = json.dumps({"overrides": [{"date": date_str, "price": str(price), "price_type": "fixed", "currency": "USD"}]})
    r = subprocess.run(
        ['curl', '-s', '-X', 'POST',
         f'https://api.pricelabs.co/v1/listings/{listing_id}/overrides?pms=ownerrez',
         '-H', f"X-API-Key: {os.environ['PRICELABS_API_KEY']}",
         '-H', 'Content-Type: application/json',
         '-d', body],
        capture_output=True, text=True)
    resp = json.loads(r.stdout)
    if 'error' in resp:
        print(f"    [API ERROR: {resp['error']}]")
    return resp


def render_email_html(today, report, total_actions):
    """Build a readable HTML email: a summary banner + a per-property table with the
    action taken on each evaluated night. Inline styles only (email-client safe)."""
    changed = total_actions > 0
    banner_txt = (f"{total_actions} price change{'s' if total_actions != 1 else ''} made"
                  if changed else "No price changes — all open weekday nights already optimal")
    banner_bg, banner_fg = ("#e6f4ea", "#0a7d33") if changed else ("#f0f0f0", "#555")

    p = ['<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
         'color:#1a1a1a;max-width:640px;">']
    p.append('<h2 style="margin:0 0 2px;font-size:20px;">Weekday Price Optimizer</h2>')
    p.append(f'<div style="color:#999;font-size:13px;margin-bottom:14px;">{today}</div>')
    p.append(f'<div style="display:inline-block;padding:6px 12px;border-radius:6px;'
             f'background:{banner_bg};color:{banner_fg};font-weight:600;font-size:14px;">{banner_txt}</div>')

    total_watch = sum(len(prop.get('weekend_watch', [])) for prop in report)
    if total_watch:
        p.append(f'<div style="display:inline-block;margin-left:8px;padding:6px 12px;border-radius:6px;'
                 f'background:#fff8e6;color:#8a5a00;font-weight:600;font-size:14px;">'
                 f'⚑ {total_watch} weekend{"s" if total_watch != 1 else ""} to watch</div>')

    for prop in report:
        p.append(f'<h3 style="margin:20px 0 6px;font-size:15px;">{prop["name"]}</h3>')
        if prop['status'] == 'abort':
            p.append(f'<div style="padding:8px 12px;background:#fdecec;color:#b3261e;'
                     f'border-radius:6px;font-size:13px;">⚠️ Aborted — {prop["reason"]}</div>')
            continue
        if prop['rows']:
            p.append('<table style="border-collapse:collapse;width:100%;font-size:13px;">')
            p.append('<tr style="background:#f5f5f5;text-align:left;">' + ''.join(
                f'<th style="padding:6px 10px;border-bottom:1px solid #e0e0e0;">{h}</th>'
                for h in ['Night', 'Day', 'Lead', 'Before', 'After', 'Change']) + '</tr>')
            for r in prop['rows']:
                prior, target = r['prior'], r['target']
                if r['action'] == 'push':
                    bg = '#e6f4ea'
                    after = f'<span style="color:#0a7d33;font-weight:600;">${target}</span>'
                    if prior:
                        before = f'${prior}'
                        drop = prior - target
                        pct = f' ({round(-100 * drop / prior)}%)' if prior else ''
                        change = f'<span style="color:#0a7d33;font-weight:600;">-${drop}</span><span style="color:#888;">{pct}</span>'
                    else:
                        before = '<span style="color:#999;">no override</span>'
                        change = '<span style="color:#0a7d33;">new</span>'
                else:  # skip_already_lower — already at/below target, held
                    bg = '#ffffff'
                    before = f'${prior}'
                    after = f'<span style="color:#999;">${prior}</span>'
                    change = '<span style="color:#999;">held</span>'
                cells = [r['night'], r['dow'], f'{r["lead"]}d', before, after, change]
                p.append(f'<tr style="background:{bg};">' + ''.join(
                    f'<td style="padding:6px 10px;border-bottom:1px solid #f2f2f2;">{c}</td>'
                    for c in cells) + '</tr>')
            p.append('</table>')
        else:
            p.append('<div style="color:#999;font-size:13px;">No open weekday nights needed review.</div>')

        # Weekend watch (PROP_B) — alert only. Rendered whether or not there were weekday changes.
        watch = prop.get('weekend_watch', [])
        if watch:
            p.append('<div style="margin-top:12px;font-size:13px;font-weight:600;color:#8a5a00;">'
                     '⚑ Weekends to watch '
                     '<span style="font-weight:400;color:#999;">(no price changes — review)</span></div>')
            p.append('<table style="border-collapse:collapse;width:100%;font-size:13px;margin-top:4px;">')
            p.append('<tr style="background:#f5f5f5;text-align:left;">' + ''.join(
                f'<th style="padding:6px 10px;border-bottom:1px solid #e0e0e0;">{h}</th>'
                for h in ['Night', 'Day', 'Lead', 'Status', 'Current price']) + '</tr>')
            for w in watch:
                urgent = w['flag'] == 'urgent'
                bg = '#fdecec' if urgent else '#fff8e6'
                status = ('<span style="color:#b3261e;font-weight:600;">urgent</span>' if urgent
                          else '<span style="color:#8a5a00;font-weight:600;">watch</span>')
                price = (f"${w['existing']}" if w['existing'] is not None
                         else '<span style="color:#999;">PL live rate</span>')
                cells = [w['night'], w['dow'], f'{w["lead"]}d', status, price]
                p.append(f'<tr style="background:{bg};">' + ''.join(
                    f'<td style="padding:6px 10px;border-bottom:1px solid #f2f2f2;">{c}</td>'
                    for c in cells) + '</tr>')
            p.append('</table>')

    p.append('</div>')
    return ''.join(p)


def send_email(subject, text_body, html_body=None):
    app_password = os.environ.get('GMAIL_APP_PASSWORD')
    if not app_password:
        print("[email skipped — GMAIL_APP_PASSWORD not set]")
        return
    if html_body:
        msg = MIMEMultipart('alternative')
        msg.attach(MIMEText(text_body, 'plain'))       # fallback for plain-text clients
        msg.attach(MIMEText(html_body, 'html'))        # preferred: the table
    else:
        msg = MIMEText(text_body)
    msg['Subject'] = subject
    msg['From'] = EMAIL_FROM
    msg['To'] = EMAIL_TO
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as s:
        s.login(EMAIL_FROM, app_password)
        s.send_message(msg)


def decide_night(cfg, weekday, lead_days, is_booked, existing_price):
    """Pure pricing decision for ONE night — no I/O, fully deterministic. This is the
    safety-critical core; it is unit-tested in tests/test_optimizer.py.

    Returns a dict with 'action' in:
      skip_booked | skip_weekend | none | skip_already_lower | push
    plus 'target' and 'tier' when a price applies.

    Guarantees enforced here (and asserted by the tests):
      - booked nights and weekends are never priced
      - a night is only touched inside its property's intervention window
      - a price is only pushed if it LOWERS an existing override (never raises)
    """
    if is_booked:
        return {'action': 'skip_booked'}
    if weekday not in WEEKDAYS:
        return {'action': 'skip_weekend'}

    if lead_days <= cfg['force_clear_days']:
        target, tier = cfg['force_clear_price'], 'force-clear'
    elif lead_days <= cfg['intervention_days']:
        target, tier = cfg['intervention_price'], 'intervention'
    else:
        return {'action': 'none'}

    # Never raise a price already set at/below target.
    if existing_price is not None and int(existing_price) <= target:
        return {'action': 'skip_already_lower', 'target': target, 'tier': tier}
    return {'action': 'push', 'target': target, 'tier': tier}


def watch_weekend(cfg, weekday, lead_days, is_booked):
    """Pure detection for ONE weekend night — does it warrant a human look? ALERT-ONLY.

    This is deliberately separate from decide_night: the pricing core never touches weekends,
    so the money-safety guarantees there are unaffected. This function NEVER returns a price or
    an action — only a severity flag. It is the seam a future phase-2 weekend auto-push would
    extend (that phase would return an action/target here), which is why it's isolated now.

    Returns {'flag': ...} where flag is:
      none   — not a watched night (booked, not Fri/Sat, property opted out, or still early)
      watch  — open and at/below the median booking lead (behind pace, ~2wk runway to act)
      urgent — open and at/below p25 (bottom quartile — most comparable weekends have booked)
    """
    watch_cfg = cfg.get('weekend_watch')
    if not watch_cfg:                       # property opted out (e.g. PROP_A)
        return {'flag': 'none'}
    if is_booked:                           # already sold — nothing to watch
        return {'flag': 'none'}
    if weekday not in WEEKEND_NIGHTS:
        return {'flag': 'none'}
    if lead_days <= watch_cfg['urgent_days']:
        return {'flag': 'urgent'}
    if lead_days <= watch_cfg['watch_days']:
        return {'flag': 'watch'}
    return {'flag': 'none'}


# --- Guardrails for unattended operation -----------------------------------------------------
# Normal run pushes 0-3 nights/property; the intervention window caps the natural max ~10.


def sanity_check_bookings(bookings):
    """GUARD 1 — don't act on bad data. An EMPTY bookings pull almost always means the API
    call failed, not that the calendar is truly empty (both properties always have future
    bookings). Acting on empty data would treat booked nights as open. Returns (ok, reason)."""
    if not bookings:
        return False, "bookings pull returned 0 active bookings (likely API failure, not an empty calendar)"
    return True, ""


def check_circuit_breaker(num_pushes, cap=MAX_CHANGES_PER_RUN):
    """GUARD 2 — bound the blast radius. If a single run would push more changes than a healthy
    run ever should, halt and alert instead of executing. Returns (ok, reason)."""
    if num_pushes > cap:
        return False, f"{num_pushes} pushes planned (cap {cap}) — abnormal, not executing; needs review"
    return True, ""


def run():
    today = datetime.date.today()
    run_ts = datetime.datetime.now().isoformat(timespec='seconds')
    # Explicit UTC stamp. `run_ts` is local-naive, so it means UTC in the cloud but ET locally —
    # ambiguous to compare against OwnerRez's booked_utc. Measurement uses this field.
    run_ts_utc = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')
    buf = io.StringIO()
    original_stdout = sys.stdout
    sys.stdout = buf

    print(f"\n=== Weekday Price Optimizer — {today} ===")

    total_actions = 0
    total_watch = 0
    report = []
    for prop_id, cfg in PROPERTIES.items():
        name = cfg['name']
        print(f"\n--- {name} ---")

        bookings = fetch_bookings(prop_id)

        # GUARD 1: don't act on bad/empty data.
        ok, reason = sanity_check_bookings(bookings)
        if not ok:
            print(f"  ⚠️  ABORT {name}: {reason}")
            report.append({'name': name, 'status': 'abort', 'reason': reason, 'rows': []})
            continue

        booked_nights = get_booked_nights(bookings)
        overrides = get_current_overrides(prop_id)

        # PL's own price context for the horizon — one call, captured BEFORE we push anything
        # (on an un-overridden night PL's `price` is the counterfactual). Best-effort.
        pl_prices = fetch_pl_prices(
            prop_id,
            (today + datetime.timedelta(days=1)).isoformat(),
            (today + datetime.timedelta(days=LOOK_AHEAD_DAYS)).isoformat())

        # Plan all decisions first (pure), so we can inspect the batch before touching prices.
        planned = []
        weekend_watch = []
        control_events = []
        for i in range(1, LOOK_AHEAD_DAYS + 1):
            night = today + datetime.timedelta(days=i)
            date_str = night.isoformat()
            wd = night.weekday()
            is_booked = night in booked_nights
            existing_price = overrides.get(date_str, {}).get('price')
            decision = decide_night(cfg, wd, i, is_booked, existing_price)
            if decision['action'] == 'skip_weekend':
                # Weekends are never priced, but open ones are watched (alert-only).
                w = watch_weekend(cfg, wd, i, is_booked)
                if w['flag'] != 'none':
                    weekend_watch.append({
                        'night': date_str, 'dow': night.strftime('%a'), 'lead': i,
                        'flag': w['flag'],
                        'existing': int(existing_price) if existing_price is not None else None,
                    })
                continue
            if decision['action'] == 'skip_booked':
                continue
            if decision['action'] == 'none':
                # CONTROL GROUP: an open weekday night we deliberately leave to PriceLabs.
                # Recorded so intervened-night conversion has something to be compared against —
                # without this the log can only say "priced nights booked X%", never "vs Y%".
                control_events.append({
                    'night': date_str, 'dow': night.strftime('%a'), 'lead': i,
                    'existing': int(existing_price) if existing_price is not None else None,
                })
                continue
            planned.append((i, night, date_str, night.strftime('%a'), existing_price, decision))

        # GUARD 2: circuit breaker — abnormal push volume halts execution for this property.
        num_push = sum(1 for p in planned if p[5]['action'] == 'push')
        ok, reason = check_circuit_breaker(num_push)
        if not ok:
            print(f"  ⚠️  ABORT {name}: {reason}")
            report.append({'name': name, 'status': 'abort', 'reason': reason, 'rows': []})
            continue

        actions = 0
        rows = []
        for (i, night, date_str, day_name, existing_price, decision) in planned:
            action, target, tier = decision['action'], decision['target'], decision['tier']
            prior = int(existing_price) if existing_price is not None else None
            event = {
                'run_ts': run_ts, 'run_ts_utc': run_ts_utc,
                'record_type': 'weekday_price',
                'property': name, 'listing_id': prop_id,
                'night': date_str, 'dow': day_name, 'lead_days': i,
                'tier': tier, 'target_price': target,
                'prior_override_price': prior, 'action': action,
                **price_ctx(pl_prices, date_str),
            }
            rows.append({'night': date_str, 'dow': day_name, 'lead': i,
                         'tier': tier, 'action': action, 'target': target, 'prior': prior})

            if action == 'skip_already_lower':
                print(f"  {date_str} {day_name}  already at ${existing_price} ≤ ${target}, skip")
                log_event(event)
                continue

            # action == 'push'
            push_override(prop_id, date_str, target)
            prev = f"was ${existing_price}" if existing_price else "no prior override"
            print(f"  ✓ {date_str} {day_name}  → ${target} [{tier}] ({prev})")
            log_event(event)
            actions += 1
            total_actions += 1

        if actions == 0:
            print(f"  No changes needed.")

        if weekend_watch:
            total_watch += len(weekend_watch)
            print(f"  Weekends to watch ({len(weekend_watch)}) — alert only, no price changes:")
            for w in weekend_watch:
                tag = 'URGENT' if w['flag'] == 'urgent' else 'watch '
                price = f"${w['existing']}" if w['existing'] is not None else "no override (PL live rate)"
                print(f"    ⚑ [{tag}] {w['night']} {w['dow']}  {w['lead']}d out  {price}")

        # Learning-loop capture for the non-pricing observations. Logged only after the guards
        # pass, so nothing from an aborted/bad-data run enters the record. Best-effort.
        for c in control_events:
            log_event({
                'run_ts': run_ts, 'run_ts_utc': run_ts_utc,
                'record_type': 'control',
                'property': name, 'listing_id': prop_id,
                'night': c['night'], 'dow': c['dow'], 'lead_days': c['lead'],
                'tier': None, 'target_price': None,
                'prior_override_price': c['existing'], 'action': 'none',
                **price_ctx(pl_prices, c['night']),
            })
        for w in weekend_watch:
            log_event({
                'run_ts': run_ts, 'run_ts_utc': run_ts_utc,
                'record_type': 'weekend_watch',
                'property': name, 'listing_id': prop_id,
                'night': w['night'], 'dow': w['dow'], 'lead_days': w['lead'],
                'flag': w['flag'], 'prior_override_price': w['existing'],
                **price_ctx(pl_prices, w['night']),
            })

        report.append({'name': name, 'status': 'ok', 'reason': '', 'rows': rows,
                       'weekend_watch': weekend_watch})

    print(f"\nDone — {today}")

    sys.stdout = original_stdout
    output = buf.getvalue()
    print(output)

    watch_suffix = (f", {total_watch} weekend flag{'s' if total_watch != 1 else ''}"
                    if total_watch else "")
    subject = (f"Weekday Optimizer — {today} "
               f"({total_actions} change{'s' if total_actions != 1 else ''}{watch_suffix})")
    send_email(subject, output, render_email_html(today, report, total_actions))


if __name__ == '__main__':
    run()
