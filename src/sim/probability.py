"""Probability engine — Benter combination + Stern-corrected Harville matrix."""

import numpy as np


def benter_combine(model_probs: np.ndarray, odds_probs: np.ndarray,
                   alpha: float = 1.89, beta: float = 1.0) -> np.ndarray:
    """Combine model and market probabilities using Benter conditional logit."""
    log_model = np.log(np.clip(model_probs, 1e-10, 1))
    log_odds = np.log(np.clip(odds_probs, 1e-10, 1))
    combined = alpha * log_model + beta * log_odds
    exp_vals = np.exp(combined - combined.max())  # numerical stability
    return exp_vals / exp_vals.sum()


def model_probs_from_curves(adj_v0s: list[float], decay_rates: list[float],
                            race_distance_ft: float, temperature: float = 1000.0) -> np.ndarray:
    """Compute model win probabilities from velocity curves via predicted finishing times.

    temperature (ms) controls softmax sharpness over predicted finishing times.
    1000ms is a defensible default given empirical within-race time spreads
    (mean stddev ~972 ms across 581 sample 2014 races). Lower T = sharper
    favorite, higher T = flatter probabilities. Joint MLE calibration with
    Benter alpha is a follow-up (see RDS-T1.1 audit).
    """
    predicted_times = []
    for v0, decay in zip(adj_v0s, decay_rates):
        avg_v = v0 - decay * (race_distance_ft / 2000.0)
        if avg_v <= 0:
            avg_v = 30.0
        predicted_times.append(race_distance_ft / avg_v * 1000.0)

    times_arr = np.array(predicted_times)
    margins = times_arr.min() - times_arr
    exp_margins = np.exp(margins / temperature)
    return exp_margins / exp_margins.sum()


def odds_to_probs(odds: list[float]) -> np.ndarray:
    """Convert tote odds to normalized implied probabilities."""
    raw = np.array([1.0 / (o + 1) if o > 0 else 0.01 for o in odds])
    return raw / raw.sum()


# Stern exponent. Calibrated 0.81 → 0.86 in wagering-analytics
# (audit WA-T1.1, 2026-05-27) via grid search on top-3 ordering
# log-likelihood across 80,042 clean races. wagering-analytics's
# populate_stern_fair.py uses 0.86; this default was harmonized
# 2026-05-29 to match. The numerical impact on per-combo
# probability is small (~1-2%) but cross-repo consistency
# matters for results to agree.
STERN_K = 0.86


def stern_transform(p: np.ndarray, k: float = STERN_K) -> np.ndarray:
    """Stern power transformation of win probabilities."""
    p_k = np.power(np.clip(p, 1e-10, 1), k)
    return p_k / p_k.sum()


def harville_ordered_prob(p: np.ndarray, positions: list[int], k: float = STERN_K) -> float:
    """Probability of horses finishing in exact order using Stern-corrected Harville.

    Args:
        p: win probability vector (original, not Stern-transformed)
        positions: indices (0-based) in order, e.g. [2, 0, 4] means horse 3 wins, horse 1 2nd, horse 5 3rd
        k: Stern exponent (default STERN_K=0.86 — empirically calibrated)
    """
    remaining = np.ones(len(p), dtype=bool)
    prob = 1.0
    for idx in positions:
        p_remaining = np.where(remaining, np.power(np.clip(p, 1e-10, 1), k), 0)
        total = p_remaining.sum()
        if total <= 0:
            return 0.0
        conditional = p_remaining[idx] / total
        prob *= conditional
        remaining[idx] = False
    return prob


def fair_price(harville_prob: float, takeout: float) -> float:
    """Fair payout per $1 wagered, after takeout."""
    if harville_prob <= 0:
        return 0.0
    return (1.0 - takeout) / harville_prob
