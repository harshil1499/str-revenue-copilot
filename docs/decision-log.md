# Decision Log

The pricing decisions behind the system, and the reasoning. This is the part that matters — the
code just executes what these decisions define. Written as I'd defend them in a review.

Standing rule underneath all of it: **the two properties are never compared to each other.**
Different markets, sizes, and demand shapes — each is judged only against its own baseline.

---

## Decision 1 — Threshold design: derive intervention prices from lead time, not gut

**Context.** The optimizer needs to know, for each open midweek night, whether to intervene and
at what price. The naive version picks a round number ("just set slow nights to $99").

**What I did instead.** Pulled each property's full booking history and built the **lead-time
distribution** — how many days before arrival bookings actually come in. Two tiers per property:
a moderate "intervention" price for the mid-range window, and a lower "force-clear" price for the
final days before a night would otherwise go empty. The exact day-cutoffs and prices are set per
property from where each one's demand actually lands.

**Why it matters.** A mountain-market cabin and a lake-market cottage have different booking
rhythms; a single threshold would over-discount one and under-serve the other. Tying the rule to
observed lead time makes it defensible and property-specific.

---

## Decision 2 — Narrow an aggressive discount window when the data says it's hurting

**Context.** One property's force-clear logic was discounting open nights up to ~two weeks out.

**The signal.** When I actually read the conversion data, nights in that 8–14-day window were
already booking at the pricing engine's *natural* (higher) rate. The aggressive discount wasn't
rescuing unsold inventory — it was **discounting nights that would have sold at full price anyway.**

**The call.** Narrow the force-clear window to the final week only; let the pricing engine run the
8+ day window untouched. This is a decision to **remove automation** where evidence showed it was
value-destructive — the opposite of the reflex to add more rules.

**Guardrail that made it safe.** The optimizer only ever *lowers* toward the floor and never
raises a manually-set-lower price, so tightening the window couldn't accidentally spike prices —
worst case, a couple of nights ride the engine's rate and get caught by the final-week floor.

---

## Decision 3 — Read a slow calendar through the lead-time lens before acting

**Context.** A forward month looked worryingly empty and the instinct was to cut prices.

**The reframe.** Both properties book close-in. Measured against their own lead-time windows, most
of the "empty" month was simply *upstream* of when its demand normally arrives. An empty calendar
30–40 days out is only alarming if that property *books* 30–40 days out — and neither does.

**The call.** Hold pricing; position correctly (open up single-night stays on the visibility-
constrained property to capture demand as it arrives); do **not** panic-discount weeks early.
Different action for each property, from the same principle.

---

## Decision 4 — Catch the shift the averages were hiding

**Context.** Double-checking Decision 3, I pulled lead time **by booking cohort** (grouping
bookings by *when they were made*) instead of one all-time average.

**The finding.** One property's median booking window had **collapsed from ~22 days to ~8** over
recent months — a large move completely invisible in the all-time average I'd been citing (and
which had briefly led me to the wrong read on the calendar).

**Why it's the most valuable moment here.** It changed how *every* "why is this night still open?"
question gets answered for that property, and it independently validated Decision 2. It also
came with intellectual honesty about its limits: the recent sample is small, and the shift is
probably seasonal rather than structural — but I couldn't confirm that with a year-over-year
comparison because the listing is too new. Flagged to revisit next season rather than
over-concluding.

---

## Decision 5 — Scope discipline: the gap was visibility, not automation

**The ask.** Weekends were uncovered. The system priced Mon–Thu only, leaving Fri/Sat — the
highest-value inventory — with no automated coverage. The obvious roadmap item was "extend
pricing to weekends."

**The question I actually asked.** Does this need the system to *act*, or to *notice*? I checked
whether weekends were failing to sell, and they weren't — they clear at the pricing engine's own
rates. So automated discounting would have solved a problem I didn't have, and paid for it by
giving away nights that were going to book anyway. The real gap was that when a weekend *did*
lag, nobody found out until it was too late to respond.

**What I shipped.** Alert-only. The system flags an open weekend that has fallen behind that
property's pace; a human decides. It's the smaller, fully reversible intervention, and it's
staged: gather evidence now, automate later only if the evidence justifies it. The build is
structured so that later step is a drop-in, not a rewrite.

**One thing I refused to average.** The two properties book on visibly different curves, so a
single shared threshold would have been wrong for both. Each property's alert thresholds come
from its own booking-lead distribution — median for the soft flag, 25th percentile for the urgent
one. Segment, don't average.

**The tradeoff I accepted.** More manual touches, in exchange for not automating a decision I
couldn't yet defend. Automating something you can't justify is how you lose money confidently.

---

## Decision 6 — Instrument before the measurement window closes

**The problem.** The system recorded what it did, but not what *would* have happened otherwise.
The pricing engine's own recommendation for a given night is only retrievable while that night is
still in the future — once it passes, the number is gone permanently.

**Why that's a prioritization call, not a technical one.** It means the cost of *not* building
instrumentation isn't "we'll add it later" — it's "that evidence is destroyed daily until we do."
I'd planned to let data accumulate for six weeks and then analyze it. Auditing what was actually
being captured showed that plan would have ended with six weeks of data that couldn't answer the
question. Everything else on the list could wait; this couldn't.

**The part worth flagging.** The API exposes a field whose name reads exactly like the number I
wanted. I checked it against nights with no override applied, expecting it to match the live
price. It disagreed on **18 of 18** — it strips all custom rules, so it answers a different
question entirely. Had I trusted the name, every downstream measure would have been quietly wrong,
and unfixable after the fact.

**The habit underneath.** Confirm what a metric actually measures before building decisions on
top of it. A plausible field name is not a definition.

---

## Decision 7 — Killed a comparison I had just built, and picked a cheaper instrument

**What I built.** To know whether a discount *caused* a booking, you need comparable nights that
didn't get one. I added a control group: nights outside the intervention window.

**Why I killed it.** A night is only outside that window because it's further out in time.
Checking live data, treated nights sat at 6–14 days out and controls at 18–60 — no overlap at
all. The comparison couldn't separate "the discount worked" from "close-in nights behave
differently," and it never would have. Worse, it would have *looked* rigorous in a review.

**The call I made instead.** The textbook fix is a randomized holdout — withhold the discount
from a random share of eligible nights. Methodologically correct, and wrong here. At this
portfolio's volume it produces roughly six treated and six control nights per six-week window:
underpowered to detect anything but a very large effect, while spending real revenue on every
withheld night to buy it. That's a bad trade, and no amount of statistical correctness fixes the
price tag.

The alternative costs nothing: vary *how far* prices move among nights that all get treated,
rather than treating some and not others. No night goes unprotected, there's no lead-time
confound, and it answers the more useful question anyway — not "does discounting work?" but
"how high can I hold price and still clear?"

**The judgment.** Match the instrument to the sample you actually have. Rigor has a cost, and
choosing the method you can afford to run beats the one you can only afford to describe.

---

## Meta-principle

Every one of these is the same discipline: **before recommending a pricing change, surface what I
can't see, pull the data that would answer it, and let evidence — not the size of a scary-looking
gap — drive the call.** Half the good decisions here were decisions *not* to act.
