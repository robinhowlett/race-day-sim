"""Prospective pace prediction from field velocity curve distribution.

Empirical context (dirt, 2012-2015, 100K+ races):
- Sprint adj_v0: mean 64.4, std 2.1. Median within-race gap (1st-2nd): 0.88
- Route adj_v0: mean 60.8, std 1.9. Median within-race gap (1st-2nd): 0.77
- The horse with highest adj_v0 wins only 12-16% and misses the board ~62% of the time.
- adj_v0 characterizes who will be ON THE LEAD (energy expenditure), not who wins.
- Decay rate determines who sustains — the real predictor of outcome, especially in routes.
"""

import numpy as np


# Dirt baseline median v0 used to derive scale-relative gap thresholds.
# Empirical from 2012-2015: sprint mean 64.4, route mean 60.8 (≈ 62.5 average).
# Fields with median v0 well below this are typically turf — gap thresholds
# need to scale down proportionally so a 0.55 ft/s gap on a 55-v0 turf field
# is treated as "tight" (the same fraction of v0) rather than as "open."
_DIRT_BASELINE_V0 = 62.5

# Gap thresholds expressed as a fraction of field median v0. Derived from
# the dirt baseline: 0.77/62.5 ≈ 0.012 (route), 0.88/62.5 ≈ 0.014 (sprint).
# These auto-scale across surfaces because actual field median v0 varies:
#   dirt sprint  ≈ 64.4 → threshold ≈ 0.90 ft/s (matches old 0.88)
#   turf sprint  ≈ 55.5 → threshold ≈ 0.78 ft/s (vs old 0.88, less spurious "open" calls)
#   dirt route   ≈ 60.8 → threshold ≈ 0.73 ft/s (matches old 0.77)
#   turf route   ≈ 53.9 → threshold ≈ 0.65 ft/s
_CONTESTED_FRAC_ROUTE  = 0.0123  # 0.77 / 62.5
_CONTESTED_FRAC_SPRINT = 0.0141  # 0.88 / 62.5
_LONE_SPEED_FRAC_ROUTE  = 0.024   # 1.5 / 62.5
_LONE_SPEED_FRAC_SPRINT = 0.027   # 1.7 / 62.5


def predict_pace(adj_v0s: list[float], decay_rates: list[float],
                 furlongs: float = 8.0) -> dict:
    """Predict pace scenario from the field's velocity curve profiles.

    Args:
        adj_v0s: adjusted initial velocities for each horse
        decay_rates: deceleration rates for each horse
        furlongs: race distance (affects sprint/route classification)

    Gap thresholds for CONTESTED / LONE_SPEED are expressed as a fraction of
    the field's median v0, scaled from the dirt baseline (≈62.5 ft/s). This
    means turf races (median v0 ~54) get proportionally tighter thresholds —
    a 0.6 ft/s gap on a turf field is "tight" because it's the same fraction
    of v0 as a 0.7 ft/s gap on dirt. Otherwise turf races get over-classified
    as CONTESTED because the absolute scale differs.

    Returns dict with:
        scenario: CONTESTED / CONTESTED_HIGH_DECAY / LONE_SPEED / MODERATE / etc.
        speed_count: how many speed types in the field
        leader_decay: the fastest horse's decay rate
        narrative: human-readable description
        profiles: list of (index, profile_type) for each horse
    """
    if len(adj_v0s) < 3:
        return {"scenario": "UNKNOWN", "speed_count": 0, "leader_decay": 0,
                "narrative": "Insufficient data", "profiles": []}

    sorted_idx = np.argsort(adj_v0s)[::-1]
    v0_sorted = [adj_v0s[i] for i in sorted_idx]
    decay_sorted = [decay_rates[i] for i in sorted_idx]

    gap_1_2 = v0_sorted[0] - v0_sorted[1]
    gap_2_3 = v0_sorted[1] - v0_sorted[2] if len(v0_sorted) > 2 else 99

    leader_decay = decay_sorted[0]
    median_decay = np.median(decay_rates)
    median_v0    = float(np.median(adj_v0s))

    is_route = furlongs > 6.5

    # Speed-count threshold also scales with field magnitude.
    # Dirt baseline: 1.0 ft/s (route) / 1.5 ft/s (sprint) at v0 ≈ 62.5
    speed_frac = (1.0 / _DIRT_BASELINE_V0) if is_route else (1.5 / _DIRT_BASELINE_V0)
    speed_threshold = max(0.3, speed_frac * median_v0)
    speed_count = sum(1 for v in v0_sorted if v >= v0_sorted[0] - speed_threshold)

    # Gap thresholds: scaled by field median v0 vs dirt baseline.
    if is_route:
        contested_threshold = _CONTESTED_FRAC_ROUTE  * median_v0
        lone_speed_threshold = _LONE_SPEED_FRAC_ROUTE * median_v0
    else:
        contested_threshold = _CONTESTED_FRAC_SPRINT  * median_v0
        lone_speed_threshold = _LONE_SPEED_FRAC_SPRINT * median_v0

    # Note: the original code had a duplicate `elif gap_1_2 < contested_threshold`
    # branch (dead code, RDS pace M2). Removed.
    if gap_1_2 < contested_threshold:
        scenario = "CONTESTED"
    elif gap_1_2 >= lone_speed_threshold:
        scenario = "LONE_SPEED"
    else:
        scenario = "MODERATE"

    high_decay = leader_decay > median_decay + 0.3
    if high_decay:
        scenario += "_HIGH_DECAY"

    # Classify each horse's running profile
    profiles = []
    for i, (v0, decay) in enumerate(zip(adj_v0s, decay_rates)):
        v0_rank_pctile = sum(1 for v in adj_v0s if v <= v0) / len(adj_v0s)
        if v0_rank_pctile >= 0.7:  # top 30% by v0
            if decay <= median_decay:
                profiles.append((i, "SUSTAINED_SPEED"))
            else:
                profiles.append((i, "SPEED_AND_FADE"))
        elif v0_rank_pctile >= 0.3:  # middle
            if decay <= median_decay:
                profiles.append((i, "STALKER"))
            else:
                profiles.append((i, "ONE_DIMENSIONAL"))
        else:  # bottom third by v0
            if decay <= median_decay * 0.5:
                profiles.append((i, "DEEP_CLOSER"))
            else:
                profiles.append((i, "TRAILER"))

    # Narrative
    if "CONTESTED" in scenario:
        narrative = f"{speed_count} speed types contest the lead"
        if high_decay:
            narrative += " — leader's high decay suggests collapse likely, closers advantaged"
        elif is_route:
            narrative += " — sustained pressure over distance will test stamina"
        else:
            narrative += " — in a sprint, dueling speed can still hold"
    elif "LONE_SPEED" in scenario:
        narrative = f"Lone speed ({gap_1_2:.1f} ft/s clear)"
        if high_decay:
            narrative += " — but high decay means vulnerable without pressure"
        else:
            narrative += " — low decay, can dictate tempo and sustain. Dangerous."
    else:
        narrative = "No clear pace dynamic — outcome depends on individual ability"

    return {
        "scenario": scenario,
        "speed_count": speed_count,
        "leader_decay": leader_decay,
        "median_decay": median_decay,
        "gap_1_2": gap_1_2,
        "narrative": narrative,
        "profiles": profiles,
    }
