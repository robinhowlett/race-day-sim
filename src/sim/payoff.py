"""Payoff projection — estimate expected exotic payoffs before the race runs.

Uses calibrated OLS models from wagering-analytics to predict what a given
combination SHOULD pay based on the odds of the horses involved and race context.
Comparing projected payoff to Stern-corrected Harville fair value gives the
overlay/underlay estimate that drives bet construction.

Model source: wagering-analytics/models/payoff_coefficients.json
"""

import json
import math
from pathlib import Path

import numpy as np

MODELS_PATH = Path(__file__).resolve().parents[2] / "models" / "payoff_coefficients.json"

_coefficients = None


def _load_coefficients() -> dict:
    global _coefficients
    if _coefficients is None:
        if MODELS_PATH.exists():
            with open(MODELS_PATH) as f:
                _coefficients = json.load(f)
        else:
            _coefficients = {}
    return _coefficients


def project_exacta_payoff(
    winner_odds: float,
    second_odds: float,
    pool_size: float,
    field_size: int,
    hhi: float,
    fav_choice: int,
    fav_position: int | None,
) -> float | None:
    """Project expected exacta payoff per $1.

    Args:
        winner_odds: closing odds of projected winner
        second_odds: closing odds of projected 2nd-place horse
        pool_size: exacta pool size in dollars
        field_size: number of starters
        hhi: Herfindahl index of win probability concentration
        fav_choice: choice rank of the race favorite (1 = fav)
        fav_position: where the fav appears in this combo (1=won, 2=second, None=excluded)
    """
    models = _load_coefficients()
    if "EXACTA" not in models:
        return None

    c = models["EXACTA"]["coefficients"]
    log_payoff = (
        c["const"]
        + c["log_odds_1"] * math.log(max(winner_odds, 0.1) + 1)
        + c["log_odds_2"] * math.log(max(second_odds, 0.1) + 1)
        + c["log_pool"] * math.log(max(pool_size, 1))
        + c["field_size"] * field_size
        + c["hhi"] * hhi
        + c["fav_in_combo"] * (1 if fav_position in (1, 2) else 0)
        + c["fav_won"] * (1 if fav_position == 1 else 0)
        + c["fav_second"] * (1 if fav_position == 2 else 0)
        + c.get("log_odds1_x_fav_second", 0) * (
            math.log(max(winner_odds, 0.1) + 1) if fav_position == 2 else 0
        )
    )
    return math.exp(log_payoff)


def project_trifecta_payoff(
    winner_odds: float,
    second_odds: float,
    third_odds: float,
    pool_size: float,
    field_size: int,
    hhi: float,
    fav_choice: int,
    fav_position: int | None,
) -> float | None:
    """Project expected trifecta payoff per $1.

    Args:
        winner_odds, second_odds, third_odds: closing odds of projected finishers
        pool_size: trifecta pool size
        field_size: number of starters
        hhi: Herfindahl index
        fav_choice: choice rank of favorite
        fav_position: where fav appears (1=won, 2=second, 3=third, None=excluded)
    """
    models = _load_coefficients()
    if "TRIFECTA" not in models:
        return None

    c = models["TRIFECTA"]["coefficients"]
    log_payoff = (
        c["const"]
        + c["log_odds_1"] * math.log(max(winner_odds, 0.1) + 1)
        + c["log_odds_2"] * math.log(max(second_odds, 0.1) + 1)
        + c["log_odds_3"] * math.log(max(third_odds, 0.1) + 1)
        + c["log_pool"] * math.log(max(pool_size, 1))
        + c["field_size"] * field_size
        + c["hhi"] * hhi
        + c["fav_in_combo"] * (1 if fav_position in (1, 2, 3) else 0)
        + c["fav_won"] * (1 if fav_position == 1 else 0)
        + c["fav_second"] * (1 if fav_position == 2 else 0)
        + c["fav_third"] * (1 if fav_position == 3 else 0)
        + c.get("log_odds1_x_fav_second", 0) * (
            math.log(max(winner_odds, 0.1) + 1) if fav_position == 2 else 0
        )
        + c.get("log_odds1_x_fav_third", 0) * (
            math.log(max(winner_odds, 0.1) + 1) if fav_position == 3 else 0
        )
    )
    return math.exp(log_payoff)


def project_pick3_payoff(
    leg_winner_odds: list[float],
    pool_size: float,
    avg_hhi: float,
    avg_field_size: float,
    bad_fav_legs: int,
) -> float | None:
    """Project expected Pick 3 payoff per $1.

    Args:
        leg_winner_odds: closing odds of projected winner in each of 3 legs
        pool_size: Pick 3 pool size
        avg_hhi: average HHI across the 3 legs
        avg_field_size: average field size across legs
        bad_fav_legs: number of legs where the favorite is projected to lose
    """
    models = _load_coefficients()
    if "PICK_3" not in models:
        return None
    if len(leg_winner_odds) != 3:
        return None

    c = models["PICK_3"]["coefficients"]
    log_payoff = (
        c["const"]
        + c["log_odds_leg1"] * math.log(max(leg_winner_odds[0], 0.1) + 1)
        + c["log_odds_leg2"] * math.log(max(leg_winner_odds[1], 0.1) + 1)
        + c["log_odds_leg3"] * math.log(max(leg_winner_odds[2], 0.1) + 1)
        + c["log_pool"] * math.log(max(pool_size, 1))
        + c["avg_hhi"] * avg_hhi
        + c["avg_field_size"] * avg_field_size
        + c["bad_fav_legs"] * bad_fav_legs
    )
    return math.exp(log_payoff)


def compute_overlay(
    projected_payoff: float,
    harville_fair: float,
) -> float:
    """Compute overlay ratio: projected / fair. >1.0 = overlay, <1.0 = underlay."""
    if harville_fair <= 0:
        return 0.0
    return projected_payoff / harville_fair


def estimate_combo_value(
    combo_odds: list[float],
    harville_prob: float,
    pool_size: float,
    field_size: int,
    hhi: float,
    fav_position: int | None,
    takeout: float = 0.24,
    bet_type: str = "TRIFECTA",
) -> dict:
    """Full value assessment for a single exotic combination.

    Returns:
        dict with projected_payoff, harville_fair, overlay_ratio, edge_pct
    """
    harville_fair = (1.0 - takeout) / harville_prob if harville_prob > 0 else 0

    if bet_type == "EXACTA" and len(combo_odds) >= 2:
        projected = project_exacta_payoff(
            combo_odds[0], combo_odds[1], pool_size, field_size, hhi, 1, fav_position
        )
    elif bet_type == "TRIFECTA" and len(combo_odds) >= 3:
        projected = project_trifecta_payoff(
            combo_odds[0], combo_odds[1], combo_odds[2],
            pool_size, field_size, hhi, 1, fav_position
        )
    else:
        projected = None

    if projected is None:
        return {"projected_payoff": None, "harville_fair": float(harville_fair),
                "overlay_ratio": None, "edge_pct": None}

    overlay = compute_overlay(projected, harville_fair)
    edge_pct = (overlay - 1.0) * 100

    return {
        "projected_payoff": round(float(projected), 2),
        "harville_fair": round(float(harville_fair), 2),
        "overlay_ratio": round(float(overlay), 3),
        "edge_pct": round(float(edge_pct), 1),
    }
