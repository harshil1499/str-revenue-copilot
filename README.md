# STR Revenue Copilot

**An AI-paired revenue-management system for a two-property short-term-rental portfolio.**

Built by a product manager working alongside an AI coding agent: the human sets strategy and
makes the pricing calls; the agent handles the data analysis, writes the code, and runs the
daily execution. This repo is a sanitized case study of that system — the problem it solves,
how it was built, and what it changed.

> **Note on data:** Properties are anonymized (Property A / Property B), all credentials and
> personal/business financials are removed, and results are stated in relative terms. This is a
> portfolio artifact, not the live production repo.

---

## The problem

I own and operate two short-term rentals in different markets:

| | Market | Size | Demand shape |
|---|---|---|---|
| **Property A** | Mountain cabin | 2BR | Books close-in; visibility-constrained after a renovation gap |
| **Property B** | Lake cottage | 1BR | Mid-lead weekends; strong top-of-funnel, softer mid-funnel |

Off-the-shelf dynamic-pricing software sets a baseline, but it can't make judgment calls: *when*
to override its price on a slow midweek night, how aggressively, or when a slow calendar is
actually a problem versus just early. I was doing that by hand, inconsistently, and there was no
reliable daily process. Two questions kept recurring:

1. **Which open nights need a pricing intervention today, and at what price?**
2. **Is this month's "empty" calendar a demand problem, or just a lead-time illusion?**

The goal was a system that answers #1 automatically every day, and gives me the analysis to
answer #2 on demand — without turning pricing into a full-time job.

---

## The approach

A two-part system, both parts AI-built and human-directed:

### 1. A daily pricing optimizer (automated execution)
A scheduled job that, every morning, checks each property's open midweek nights and pushes a
price override only where lead time says the night is at risk of going unsold — never touching
booked nights, weekends, or the pricing engine's own base/min/max. It's deliberately narrow:
**it lowers prices toward a floor as a last resort; it never raises a price a human set lower.**

The intervention thresholds aren't guesses — they're derived from each property's historical
**booking lead-time distribution** (see [decision log](docs/decision-log.md)). Different markets,
different thresholds. The two properties are never compared to each other.

Weekends get a **safety net, not automation**: Fri/Sat are never priced by the job, but an open
weekend that has fallen behind its property's own booking pace is flagged for a human. The
watch/urgent thresholds are that property's median and 25th-percentile weekend booking lead.
Weekends largely sell themselves — automating a discount there would give away nights that were
going to book anyway.

### 2. An analysis layer (on-demand judgment support)
A daily dashboard plus ad-hoc pulls from the booking and pricing APIs that let me answer the
"is this actually a problem?" question with data instead of anxiety — lead-time cohorts,
occupancy pacing, funnel/visibility reads, and prior-period benchmarking.

See [architecture](docs/architecture.md) for how it runs unattended (and the reliability
engineering that took — the boring-but-real part).

---

## The decisions (this is the actual PM work)

The code is the easy half. The value is in the calls made *with* it. Three examples, each
written up in the [decision log](docs/decision-log.md):

- **Narrowing a discount window on evidence, not instinct.** One property's "force-clear" logic
  was set to discount nights up to two weeks out. Conversion data showed those nights were
  already booking at the pricing engine's higher natural rate — so the discount was leaving money
  on the table. I narrowed the aggressive window to the final week only and let the engine run
  the rest. *Cutting* automation where the data said it was hurting.

- **Reading a slow calendar correctly.** A near-empty forward month looked alarming until the
  lead-time analysis reframed it: both properties book close-in, so most of the month was simply
  *before* its own booking window. The right move was patience + positioning, not panic discounts.

- **Catching a non-obvious shift.** Pulling lead-time by booking cohort surfaced something the
  averages hid: one property's booking window had **collapsed from ~22 days to ~8** over recent
  months. That single finding changed how every "why is this open?" question gets answered — and
  validated the discount-window decision above.

---

## Results & honest caveats

**What the system delivers, concretely:**
- Two pricing automations run **unattended every day**, hardened to survive machine sleep, missing
  network at wake, and OS permission quirks (the failure modes that silently kill cron-style jobs).
- Pricing interventions are **lead-time-derived and property-specific**, not one-size-fits-all.
- Pricing and occupancy questions get answered **from live data in minutes**, not guesswork.

**Context metrics (stated relative, and honestly):**
- The mountain property's search visibility recovered strongly over the tracked period
  (first-page impressions grew roughly **4.5×**) under a booking-velocity strategy.
- The lake property has been **tracking ahead of the prior owner's same-month figures** in
  recent months (high single-digit %).

**The honest caveat:** I'm not claiming the tooling *caused* the revenue trends — seasonality, a
maturing listing, and the market all move these numbers, and I can't cleanly isolate the system's
contribution. What the system demonstrably improved is the **decision process**: faster, evidence-
based, consistent, and repeatable. That's the claim I'll stand behind.

---

## Scaling this

**The system scales by configuration, not rewriting.** Each property is a config block whose
thresholds are *derived from that listing's own booking data*, not hardcoded assumptions — so
onboarding another property is a data entry, not a code change. Proven at two properties;
designed to extend to more.

**The pattern scales beyond this domain.** The reusable capability isn't STR pricing — it's the
build: frame a messy operational problem, pair with an AI agent to automate the mechanical,
high-frequency decisions, wrap it in a data layer for the judgment calls, and keep a human on
the levers that need context. That transfers to most ops problems, not just rentals.

**The human-in-the-loop is a data-maturity stage, not a ceiling.** Today a person makes the calls
the automation doesn't — *because these are young listings with thin data*: one property's booking
window is still shifting, there isn't yet enough weekend-specific signal to trust automated
weekend pricing, and seasonality can't be confirmed without a full year of history. The path from
here is a **learning loop**: automatically re-derive the lead-time thresholds — segmented by
daypart (weekday / weekend / holiday) — as data accumulates, so pricing rules self-update instead
of being hand-set from a one-time analysis. As that data matures, decisions that are human today
(weekend pricing, holiday premiums) fold into the automated layer. What stays human is **oversight
and exceptions** — you don't let a model auto-price on thin or seasonally-confounded data without a
check. The human moves up a level, not out.

## What this demonstrates (for anyone reviewing this as work sample)

- Framing a fuzzy operational problem into a scoped, staged build
- Deriving decision rules from data (lead-time distributions) instead of intuition
- Knowing when to *remove* automation, not just add it
- Separating signal from noise (a slow calendar that wasn't actually a problem)
- Directing an AI agent to build and operate real infrastructure — spec, judgment, and QA from
  the human; implementation and execution from the agent
- Designing for scale while being honest about where it doesn't yet reach — and mapping the
  learning loop that closes the gap as data matures
- Shipping something that runs itself

## Repo contents
- [`README.md`](README.md) — this case study
- [`config.example.py`](config.example.py) — the configuration template. All property-identifying
  values (listing IDs, price thresholds, notification addresses) live in a gitignored `config.py`,
  so **this is the real production code, not a sanitized copy** — only config and data are withheld
- [`optimizer/`](optimizer/) — the daily pricing optimizer + wrapper. Also captures, per night:
  the action taken, the pricing engine's own recommendation (the counterfactual), and what the
  PMS last saw
- [`analysis/`](analysis/) — the read-only measurement join: matches logged decisions against
  booking outcomes, collapsed to one observation per night-and-lead-bucket; honest about
  correlation vs. causation
- [`tests/`](tests/) — dependency-free evals, 43 cases. Includes an invariant sweep proving a
  price is **never raised** above an existing override, and that the weekend tier can never emit
  a price at all
- [`docs/decision-log.md`](docs/decision-log.md) — the pricing decisions and their reasoning,
  including two calls to *not* build something
- [`docs/architecture.md`](docs/architecture.md) — how it runs unattended, and the reliability war story
- [`.env.example`](.env.example) — credential shape (no real values)

---

## Honest limits

This will not become a self-tuning system at this portfolio size, and the repo doesn't pretend
otherwise. Two properties yield roughly 10–15 priceable nights each per six-week window. That is
too little to establish causation, and the underlying market shifts seasonally faster than signal
accumulates — one property's booking window collapsed from ~22 days to ~8–15 in a single season,
which would invalidate anything learned from the prior year.

So the honest description is: **a disciplined executor with guardrails, plus a record that makes
periodic human re-tuning well-informed instead of guesswork.** The capture infrastructure is built
to scale — at 20 properties the same design has enough volume for real randomization — but
claiming "self-learning" here would be marketing, not engineering.
