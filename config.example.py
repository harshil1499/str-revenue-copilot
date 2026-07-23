"""
config.example.py — template for config.py.

Copy this to config.py (which is gitignored) and fill in your real values. The optimizer
imports everything property-identifying from here, so no listing IDs, price thresholds, or
account details ever live in the committed source.

    cp config.example.py config.py   # then edit config.py

Values below are ILLUSTRATIVE PLACEHOLDERS, not a real portfolio.
"""

# --- Properties under management -------------------------------------------------------------
# Keyed by the PMS/pricing-tool listing ID. One entry per property.
#
#   force_clear_days / force_clear_price
#       Inside this lead-time window an open weekday night is pushed to the force-clear price.
#   intervention_days / intervention_price
#       A softer middle tier. Set intervention_days == force_clear_days to DISABLE it (useful
#       when the pricing engine's own rates already convert inside that window).
#   weekend_watch (optional)
#       Alert-only weekend safety net. Omit the key entirely to opt a property out.
#       watch_days / urgent_days should be derived from that property's OWN weekend
#       booking-lead distribution — median and 25th percentile respectively. They are
#       SEASONAL; re-derive off-season.

PROPERTIES = {
    "000001": {
        "name": "PROP_A",
        "intervention_days": 16,
        "force_clear_days": 7,
        "intervention_price": 110,
        "force_clear_price": 95,
        "weekend_watch": {"watch_days": 15, "urgent_days": 8},
    },
    "000002": {
        "name": "PROP_B",
        "intervention_days": 7,        # == force_clear_days, so the middle tier is disabled
        "force_clear_days": 7,
        "intervention_price": 150,     # unreachable while the middle tier is disabled
        "force_clear_price": 120,
        "weekend_watch": {"watch_days": 13, "urgent_days": 9},
    },
}

# --- Run parameters --------------------------------------------------------------------------
LOOK_AHEAD_DAYS = 60      # how far out to evaluate nights
MAX_CHANGES_PER_RUN = 15  # circuit breaker: more pushes than this for one property halts the run

# --- Notifications ---------------------------------------------------------------------------
# The daily summary email. Credentials come from the environment, never from here.
EMAIL_FROM = "you@example.com"
EMAIL_TO = "you@example.com"
