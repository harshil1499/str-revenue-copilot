# Architecture & Reliability

How the system runs unattended — and the reliability engineering that actually made it work,
which is the unglamorous half that determines whether an automation is real or just a script that
ran once in a demo.

## Components

```
                 ┌─────────────────────────────┐
   macOS launchd │  daily schedule (per job)   │
   (scheduler)   └──────────────┬──────────────┘
                                │
             ┌──────────────────┴───────────────────┐
             ▼                                       ▼
   ┌───────────────────┐                 ┌────────────────────────┐
   │  price optimizer  │                 │   analysis / dashboard │
   │  (writes DSOs)    │                 │   (read-only reporting)│
   └─────────┬─────────┘                 └───────────┬────────────┘
             │                                       │
             ▼                                       ▼
   ┌───────────────────┐                 ┌────────────────────────┐
   │ Pricing API       │                 │ Booking (PMS) + Pricing│
   │ (day overrides)   │                 │ APIs → HTML report     │
   └───────────────────┘                 └────────────────────────┘
```

- **Optimizer** — writes day-specific price overrides on open midweek nights, per the
  lead-time thresholds in the [decision log](decision-log.md). Idempotent and safe to re-run.
- **Analysis layer** — pulls live booking + pricing data and regenerates a dashboard; never
  writes anything.
- **Credentials** — live in a local, git-ignored env file (mode `600`), loaded by each wrapper.
  Nothing secret is ever committed or embedded in the scheduler config.

## The reliability war story

The system originally "worked," then silently stopped after a machine migration. Getting it
genuinely unattended surfaced four failure modes — each the kind that makes a scheduled job
*look* healthy while doing nothing useful:

1. **The scheduler config didn't survive migration.** OS-level scheduled jobs live outside the
   project folder, so they weren't in the backup and didn't come along. Lesson baked in: the job
   definition is treated as an artifact to reinstall, not assume.

2. **The OS silently blocked file access.** The background job couldn't even read its own script
   until the shell binary was explicitly granted full-disk access — before that it failed with a
   bare "operation not permitted" and *no output at all*, the worst kind of failure to diagnose.

3. **Login-shell coupling.** The job had been sourcing the user's shell profile for credentials,
   which is unstable under a headless scheduler. Decoupled it: credentials come from the dedicated
   env file, so the job no longer depends on interactive-shell config.

4. **The silent stale-data killer.** The scheduler fires deferred jobs *at machine wake* — often
   before Wi-Fi/DNS is back up. The data pulls would fail, but the report-generation step ran
   anyway and **re-rendered yesterday's data as if it were fresh.** Green checkmark, stale numbers.
   Fixed with a **network-readiness guard**: each wrapper polls the API host for up to ~60s and,
   if the network never comes up, **exits without overwriting good data** rather than producing a
   confident-looking stale artifact.

The through-line: an automation isn't "done" when it runs once — it's done when it fails *safely*
and *loudly*. Most of this work was making the failure modes visible and non-destructive.

## Extending to a learning loop

The system is designed to get smarter as data accumulates, in two staged steps:

**Stage 1 — capture (built).** Every intervention-window night the optimizer evaluates is written
to an append-only structured log (`interventions.jsonl`): the date, property, lead time at the
moment of decision, tier, price set, prior price, and action taken. This is deliberately the
lightest thing that closes the real gap — most inputs (lead-time distributions) are recomputable
from the booking system on demand, so there's no need to warehouse them. What *isn't*
reconstructable after the fact is the intervention-in-context and its outcome, so that's the one
thing captured deliberately, as it happens.

**Stage 2 — measure (built, read-only).** A join tool (`analysis/measure_interventions.py`) reads
the log and matches each intervention against booking outcomes — did the night book, when, at what
rate — aggregating conversion by property × lead-bucket × price. It writes nothing; it only reads.
Output is honest about its own limits: it surfaces *correlation* ("this discounted night booked"),
and explicitly flags that this is not *causation* ("booked because discounted") — which needs
either enough volume to control for lead time and season, or the active experiment below.

**Stage 3 — derive & probe (roadmap).** The measured signal feeds back into the thresholds:
re-derive them from accumulating data, **segmented by daypart** (weekday / weekend / holiday). The
sharper version isn't passive measurement but an **active price-ceiling experiment** — on a
*fraction* of comparable nights (matched on daypart and lead time), step the intervention price
*up* until conversion breaks; the highest price that still reliably clears is the revenue-maximizing
floor. That sidesteps the counterfactual problem (raising the price *tests* whether the discount
mattered rather than assuming it). It's gated behind real guardrails: it moves live prices up and
can cost an empty night, so it needs a proper experiment spec (test fraction, step size, stop-loss)
and enough baseline data first — an explore/exploit problem, run deliberately, not a background job.

**Honest framing:** this is a *statistical feedback loop* — accumulate data, periodically
re-derive rules with human oversight — not a self-training model. Stage 2 is intentionally
deferred until Stage 1 has logged enough to analyze; building the analysis before the data exists
would be scaffolding for a phase that can't run yet.

## Evals — how the decision logic is trusted

This is a rule-based system, so "eval" means asserting the exactly-correct action for known
inputs — not scoring a probabilistic model. The safety-critical decision is extracted into a
pure function (`decide_night`) with no I/O, so it's testable in isolation, and the guarantees
that touch live money are locked into tests rather than comments:

- **19 decision cases** covering tier boundaries (force-clear vs. intervention vs. no-action),
  the disabled-middle-tier config, and every branch of the never-raise guard.
- **An invariant sweep** across a grid of lead times × existing prices asserting the single most
  important property: **a push never raises an existing price.** If a refactor ever broke that,
  the sweep fails loudly.
- **Measurement tests** verifying the outcome join classifies booked-after / booked-before /
  still-open correctly and aggregates conversion accurately against synthetic fixtures.

Dependency-free (plain `assert`, no framework), so they run anywhere: `python3 tests/test_optimizer.py`.

## Design choices worth calling out

- **Write path is deliberately narrow.** The optimizer only lowers toward a floor, only on open
  midweek nights, and never touches booked nights, weekends, or the engine's base/min/max. The
  blast radius of a bug is small by construction.
- **Read freely, gate on writes.** Analysis pulls run without ceremony; anything that changes a
  live price is the guarded, reviewed path.
- **Human-in-the-loop by default.** Judgment-heavy levers (weekend pricing, min-stay strategy)
  are intentionally *not* automated — the system does the mechanical, high-frequency work and
  leaves the calls that need context to a person.

## Guardrails for unattended operation

Because the agent acts with no human watching, it fails safe rather than acting on doubt. Two
guards run before any price is touched, both pure functions with their own eval cases:

- **Data sanity check.** If the bookings pull comes back *empty* — which almost always means the
  API failed, not that the calendar is genuinely empty — the agent **aborts that property and
  alerts** rather than treating every night as open (which would let it price nights that are
  actually booked). Same fail-safe philosophy as the network guard.
- **Circuit breaker.** The agent plans all its intended changes first, then checks the batch: if
  a single run would push more changes than a healthy run ever should, it **halts and alerts**
  instead of executing. The never-raise guard bounds *direction*; this bounds *volume*, so a bug
  or bad data can't quietly rewrite the whole calendar.

The design pattern is **plan → guard → execute**: decisions are computed as a pure batch, the
guards inspect that batch, and only then does anything touch a live price.
