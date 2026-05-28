"""Horizontal wagering utilities — equity estimation for Pick 3/4/5/6.

Uses jitter calibration from wagering-analytics to model uncertainty in
later-leg odds, and payoff projection to estimate expected returns.
"""

import json
import math
from pathlib import Path

import numpy as np

JITTER_PATH = Path(__file__).resolve().parents[2] / "models" / "jitter_calibration.json"

_jitter_data = None


def _load_jitter() -> dict:
    global _jitter_data
    if _jitter_data is None:
        if JITTER_PATH.exists():
            with open(JITTER_PATH) as f:
                _jitter_data = json.load(f)
        else:
            _jitter_data = {"sigma_by_leg_position": {}}
    return _jitter_data


def get_leg_sigma(leg_position: int) -> float:
    """Get log-normal sigma for a given leg position (1-based).

    Leg 1 = 0 (odds are known). Legs 2+ ≈ 1.01 (significant uncertainty).
    """
    data = _load_jitter()
    sigmas = data.get("sigma_by_leg_position", {})
    return float(sigmas.get(str(leg_position), 1.0))


def estimate_leg_equity(
    horse_odds: float,
    n_horses_used: int,
    ticket_cost_per_combo: float = 1.0,
) -> float:
    """Equity ratio for one horse winning their leg in a horizontal ticket.

    Returns >1.0 = gained equity, <1.0 = lost equity. The unused
    ticket_cost_per_combo argument is retained for API compatibility; the
    ratio is dimensionless.

    Formula (equivalent to simulation-protocol.md Step E.4):

        ratio = (odds + 1) × surviving_combos / total_combos

    For one leg in a horizontal ticket where this leg has `n_horses_used`
    selections and the rest of the legs are unconstrained, the ratio
    `surviving_combos / total_combos` equals `1 / n_horses_used`. So:

        ratio = (odds + 1) / n_horses_used

    This collapses the spec example:
      $120 Pick 3, 3×2×2=12 combos, leg 1 width 3, horse at 3/1:
        spec:   (3+1) × 4 / 12 = 1.33
        impl:   (3+1) / 3       = 1.33

    Examples:
      - 4-deep, 2/1 horse:  (3) / 4 = 0.75  → LOST equity
      - 4-deep, 4/1 horse:  (5) / 4 = 1.25  → GAINED equity
    """
    if n_horses_used <= 0:
        return 0.0
    return (horse_odds + 1.0) / n_horses_used


def evaluate_leg_selections(
    selections: list[dict],
    n_total_in_leg: int,
) -> dict:
    """Evaluate all selections in a horizontal leg for equity.

    Args:
        selections: list of {horse, odds, ...} dicts for horses used in this leg
        n_total_in_leg: how many horses you're using in this leg

    Returns:
        dict with per-horse equity ratios and overall leg assessment
    """
    results = []
    gain_count = 0
    lose_count = 0

    for sel in selections:
        odds = sel.get("odds", 0)
        equity = estimate_leg_equity(odds, n_total_in_leg, 1.0)
        results.append({
            "horse": sel.get("horse", "?"),
            "odds": odds,
            "equity_ratio": round(equity, 3),
            "gains_equity": equity > 1.0,
        })
        if equity > 1.0:
            gain_count += 1
        else:
            lose_count += 1

    # ITP "flashing stop sign": if 3 of 4 lose equity, this leg is bad
    flashing_stop = lose_count >= 3 and n_total_in_leg >= 4

    return {
        "selections": results,
        "n_gain_equity": gain_count,
        "n_lose_equity": lose_count,
        "flashing_stop_sign": flashing_stop,
        "avg_equity_ratio": round(np.mean([r["equity_ratio"] for r in results]), 3) if results else 0,
    }


# Bet-type-specific default takeouts. Approximate NA averages — caller
# should pass actual track/race takeout when available.
#
# Precision note: takeout is informational only here (synthetic-parlay
# advantage display, fair-payoff approximation). P&L is computed from
# actual paid amounts at evaluation. Future enhancement: time-versioned
# takeouts from Larmey's @derby1592 takeout PDF — deferred.
_DEFAULT_HORIZONTAL_TAKEOUT_BY_LEGS = {
    2: 0.21,  # DAILY_DOUBLE
    3: 0.20,  # PICK_3
    4: 0.18,  # PICK_4
    5: 0.15,  # PICK_5
    6: 0.20,  # PICK_6
}


def estimate_horizontal_value(
    legs: list[dict],
    pool_size: float,
    takeout: float | None = None,
) -> dict:
    """Estimate value of a horizontal ticket.

    Args:
        legs: list of {selections: [{horse, odds}], all_odds: [float, ...], n_used: int}.
            `all_odds` should be the closing odds of every starter in that leg's
            race (used to compute the field overround for normalization). If
            omitted, falls back to the unnormalized raw 1/(odds+1) sum, which
            is biased upward by the takeout factor and produces pessimistic
            payoff estimates.
        pool_size: Pick N pool size
        takeout: pool takeout. If not supplied, uses a default by leg count
            (DD=0.21, P3=0.20, P4=0.18, P5=0.15, P6=0.20).

    Returns:
        dict with per-leg equity assessment, combined parlay probability,
        estimated payoff, and comparison to synthetic parlay takeout.
    """
    if takeout is None:
        takeout = _DEFAULT_HORIZONTAL_TAKEOUT_BY_LEGS.get(len(legs), 0.20)

    leg_assessments = []
    parlay_prob = 1.0
    any_stop_sign = False

    for i, leg in enumerate(legs):
        n_used = leg.get("n_used", len(leg.get("selections", [])))
        selections = leg.get("selections", [])
        assessment = evaluate_leg_selections(selections, n_used)
        leg_assessments.append(assessment)

        if assessment["flashing_stop_sign"]:
            any_stop_sign = True

        # Combined probability for this leg = sum of true win probs of selections.
        # Raw 1/(odds+1) sums to ~1.17 across the field (overround from 17% takeout),
        # so each horse's raw value over-estimates true probability. Normalize by
        # full-field overround when available, then sum across selections.
        all_odds = leg.get("all_odds")
        sel_raw = sum(1.0 / (s.get("odds", 99) + 1) for s in selections)
        if all_odds:
            field_overround = sum(1.0 / (o + 1) for o in all_odds if o > 0)
            leg_prob = sel_raw / field_overround if field_overround > 0 else sel_raw
        else:
            leg_prob = sel_raw  # fallback: biased upward, payoff estimate is pessimistic
        parlay_prob *= min(leg_prob, 0.99)

    # Estimated payoff from parlay probability
    fair_payoff = (1.0 - takeout) / parlay_prob if parlay_prob > 0 else 0

    # Compare to synthetic parlay (win bets compounding)
    # Win pool takeout ≈ 17%, compounded across N legs
    n_legs = len(legs)
    synthetic_parlay_takeout = 1.0 - (1.0 - 0.17) ** n_legs
    horizontal_advantage_pct = (synthetic_parlay_takeout - takeout) * 100

    return {
        "leg_assessments": leg_assessments,
        "parlay_prob": round(parlay_prob, 6),
        "estimated_fair_payoff": round(fair_payoff, 2),
        "pool_size": pool_size,
        "any_stop_sign": any_stop_sign,
        "n_legs": n_legs,
        "horizontal_takeout": takeout,
        "synthetic_parlay_takeout": round(synthetic_parlay_takeout, 3),
        "horizontal_advantage_pct": round(horizontal_advantage_pct, 1),
    }
