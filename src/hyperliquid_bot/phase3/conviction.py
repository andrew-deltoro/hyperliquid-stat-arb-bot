"""Conviction-weight counterfactual — observation only, never used for sizing.

Why this exists
---------------
The conviction-sizing study (reports/CROSSING_TENDENCY_REVIEW.md §7) found that
scaling notional by |z| *looked* like it lifted Sharpe 0.33 -> 0.84, but that the
lift lived entirely in an unbounded weight where one episode became 22% of the
book. The only shippable form -- a bounded tilt -- was NOT significant
(permutation p ~ 0.07) on the ~50 independent episodes available.

So we do NOT size by it. Instead we LOG, at each entry, the weight a frozen rule
*would* have assigned. To stay reproducible from the log alone (the bot runs as a
4-hour oneshot, so no in-memory state survives), the rule is a pure function of
|z| with constants frozen NOW:

    w = clip(1 + LAM * (|z| - Z_REF) / Z_SCALE, 1 - RHO, 1 + RHO)

At |z| = Z_REF (the entry threshold) w = 1.0; the weight saturates at the cap for
|z| >= Z_REF + RHO*Z_SCALE/LAM. No history, no fitting, no feedback into
execution: position sizes, gates and exits are untouched.

This is deliberately dumb. After another ~150-200 independent episodes we can ask,
honestly, whether `cf_weight` predicts risk-adjusted outcome (e.g. w * realized
return vs flat). If it does not, we delete this and nothing was risked.
"""
from __future__ import annotations

# Frozen constants. Changing these invalidates prior logs -- the whole point is
# that `cf_weight` is reproducible, so treat them as append-only history.
CF_LAM = 1.0      # tilt strength
CF_RHO = 0.5      # max weight = 1 + CF_RHO  (and min = 1 - CF_RHO)
CF_Z_REF = 2.0    # |z| at the entry threshold maps to weight 1.0
CF_Z_SCALE = 3.0  # |z| units per 1.0 of LAM step


def counterfactual_weight(abs_z: float) -> float:
    """Frozen bounded conviction weight for this entry. Never raises.

    Logging must not be able to break a trading cycle, so any bad input returns
    the neutral weight 1.0.
    """
    try:
        x = abs(float(abs_z))
    except (TypeError, ValueError):
        return 1.0
    if x != x:                      # NaN guard
        return 1.0
    w = 1.0 + CF_LAM * (x - CF_Z_REF) / CF_Z_SCALE
    return max(1.0 - CF_RHO, min(1.0 + CF_RHO, w))
