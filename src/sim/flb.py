"""Favorite-longshot bias calibration + per-tier minimum-edge thresholds.

Calibration: an isotonic shrinkage curve fit on 7.7M starter-races (1997-2016)
that maps odds-implied probability to its empirically-calibrated counterpart.
The public's odds systematically overprice longshots and underprice favorites;
this curve corrects the implied probability to match historical strike rates.

Per-tier thresholds: minimum FLB-edge (combined_prob - calibrated_p) required
to register a conviction pick, indexed by the raw odds_prob tier the horse
falls into. Tuned on rolling-window OOS validation across 2010-2016 — every
bettable tier was profitable in 7/7 years; extreme 50/1+ unprofitable in 0/7.

See docs/flb-calibration-poc-2026-05-29.md for the methodology and OOS results.
"""

import json
from pathlib import Path

import numpy as np

_MODELS_DIR = Path(__file__).resolve().parents[2] / "models"
_CALIBRATION_PATH = _MODELS_DIR / "flb_calibration.json"


# (tier_name, lo_inclusive, hi_exclusive, min_edge_or_None_to_block)
# odds_prob ranges and OOS-validated thresholds from FLB POC step 7+8.
TIER_TABLE = [
    ("chalk_<2/1",     0.40, 1.00, 0.125),
    ("short_2-5/1",    0.20, 0.40, 0.20),
    ("mid_5-10/1",     0.10, 0.20, 0.10),
    ("long_10-20/1",   0.05, 0.10, 0.025),
    ("longer_20-50/1", 0.02, 0.05, 0.025),
    ("extreme_50/1+",  0.00, 0.02, None),  # hard-block: -46% mean OOS, 0/7 years +EV
]


def _load_grid():
    with open(_CALIBRATION_PATH) as f:
        d = json.load(f)
    return np.asarray(d["implied_grid"]), np.asarray(d["actual_grid"])


_IMPLIED, _ACTUAL = _load_grid()


def calibrate(odds_prob: float | np.ndarray) -> float | np.ndarray:
    """Map an odds-implied probability to its FLB-calibrated counterpart.

    Out-of-grid values are clipped to the grid endpoints (longshots below
    0.001 stay at the lowest calibrated rate; favorites above 0.95 stay
    at the highest).
    """
    return np.interp(odds_prob, _IMPLIED, _ACTUAL)


def tier_for(odds_prob: float) -> tuple[str, float | None]:
    """Return (tier_name, min_edge_or_None) for a starter's raw odds_prob.

    `min_edge=None` means the tier is hard-blocked from conviction picks
    regardless of computed edge.
    """
    for name, lo, hi, thr in TIER_TABLE:
        if lo <= odds_prob < hi:
            return name, thr
    # odds_prob = 1.0 exactly (degenerate) — treat as chalk
    return TIER_TABLE[0][0], TIER_TABLE[0][3]


def passes_conviction_filter(odds_prob: float, combined_prob: float) -> tuple[bool, str, float, float | None]:
    """Apply the FLB+tier conviction filter.

    Returns (passes, tier_name, edge_flb, threshold). `edge_flb` is
    `combined_prob - calibrated(odds_prob)`. `passes` is True iff the
    tier is bettable AND edge_flb >= threshold.
    """
    tier_name, thr = tier_for(odds_prob)
    edge_flb = float(combined_prob - calibrate(odds_prob))
    if thr is None:
        return False, tier_name, edge_flb, None
    return edge_flb >= thr, tier_name, edge_flb, thr
