"""Prospective pace prediction from field velocity curve distribution.

Empirical context (dirt, 2012-2015, 100K+ races):
- Sprint adj_v0: mean 64.4, std 2.1. Median within-race gap (1st-2nd): 0.88
- Route adj_v0: mean 60.8, std 1.9. Median within-race gap (1st-2nd): 0.77
- The horse with highest adj_v0 wins only 12-16% and misses the board ~62% of the time.
- adj_v0 characterizes who will be ON THE LEAD (energy expenditure), not who wins.
- Decay rate determines who sustains — the real predictor of outcome, especially in routes.
"""

import numpy as np


def predict_pace(adj_v0s: list[float], decay_rates: list[float],
                 furlongs: float = 8.0) -> dict:
    """Predict pace scenario from the field's velocity curve profiles.

    Args:
        adj_v0s: adjusted initial velocities for each horse
        decay_rates: deceleration rates for each horse
        furlongs: race distance (affects interpretation — sprints vs routes)

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

    is_route = furlongs > 6.5
    speed_threshold = 1.0 if is_route else 1.5
    speed_count = sum(1 for v in v0_sorted if v >= v0_sorted[0] - speed_threshold)

    # Use distributional context for gap classification
    # Median gaps: 0.77 (route), 0.88 (sprint)
    # Gaps below median = contested; well above 75th pctile = lone speed
    if is_route:
        contested_threshold = 0.77
        lone_speed_threshold = 1.5
    else:
        contested_threshold = 0.88
        lone_speed_threshold = 1.7

    if gap_1_2 < contested_threshold:
        scenario = "CONTESTED"
    elif gap_1_2 < contested_threshold and gap_2_3 < contested_threshold * 0.6:
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
