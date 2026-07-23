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

## Decision 5 — Give weekends a safety net, but refuse to automate their pricing

**Context.** The optimizer only priced Mon–Thu. Fri/Sat — the highest-value inventory — had no
automated coverage at all. The obvious move was to extend the same force-clear logic to weekends.

**What I did instead.** I checked whether weekends actually needed discounting, and they didn't:
they largely sell themselves at the pricing engine's own rates. Auto-discounting them would have
left money on nights that were going to book anyway. So the weekend tier is **alert-only** — it
flags an open weekend that has fallen behind pace and a human decides.

**Thresholds came from the data, not a guess.** For each property I pulled the distribution of
weekend-night booking lead times and set `watch` at the median and `urgent` at the 25th
percentile. The two properties came out materially different (one books meaningfully closer-in
than the other), which a single shared threshold would have papered over.

**Why it matters.** The instinct was "extend the automation." The right answer was "extend the
*observation*." Automating a decision you can't yet justify is how you lose money confidently.

---

## Decision 6 — Capture the counterfactual before it becomes unrecoverable

**Context.** The system logged every night it priced. It did not log what the pricing engine
*would* have charged on that night without the intervention. That number is queryable only while
the night is in the future — once it passes, it's gone forever.

**The trap I nearly walked into.** The API exposes a field whose name reads exactly like the
counterfactual. I tested it against nights that had no override at all: it disagreed with the
live price on **18 of 18**. It strips all custom rules, so it was never "what we would have
charged" — it was "what a bare engine would have charged." The correct field turned out to be the
plain recommended price, captured *before* the override is written.

**Why it matters.** Field semantics were verified empirically rather than inferred from the name.
Had I trusted the name, every discount-depth measurement built on top would have been quietly
wrong — and unfixable after the fact, because the real value would have already expired.

---

## Decision 7 — Kill a comparison design I had just built

**Context.** To know whether a discount *caused* a booking, you need comparable nights that didn't
get one. I added a control group: nights outside the intervention window.

**The flaw.** A night is only outside that window because it is further out in time. Checking the
live log: treated nights sat at 6–14 days out, control nights at 18–60 — **zero overlap**.
Treatment and lead time were the same variable. No comparison built on it could ever separate
"the discount worked" from "close-in nights behave differently."

**What I recommended.** Not the textbook fix. A randomized holdout would be methodologically
correct, but at this portfolio's volume it yields roughly six treated vs six control nights per
six-week window — underpowered to detect anything but a huge effect, while risking real revenue
on every withheld night. Instead: probe the *price ceiling* among nights that all get treated,
which varies price without leaving any night unprotected and has no lead-time confound.

**Why it matters.** The failure mode here isn't missing analysis — it's shipping a comparison
that looks rigorous and isn't. Recognizing that a correct-on-paper experiment is the wrong
instrument for the available sample is the actual judgment call.

---

## Meta-principle

Every one of these is the same discipline: **before recommending a pricing change, surface what I
can't see, pull the data that would answer it, and let evidence — not the size of a scary-looking
gap — drive the call.** Half the good decisions here were decisions *not* to act.
