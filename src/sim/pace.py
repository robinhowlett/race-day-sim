"""Prospective pace prediction from field velocity curve distribution."""

import numpy as np


def predict_pace(adj_v0s: list[float], decay_rates: list[float]) -> dict:
    """Predict pace scenario from the field's velocity curve profiles.

    Returns dict with:
        scenario: CONTESTED / CONTESTED_HIGH_DECAY / LONE_SPEED / MODERATE / etc.
        speed_count: how many speed types in the field
        leader_decay: the fastest horse's decay rate
        narrative: human-readable description
    """
    if len(adj_v0s) < 3:
        return {"scenario": "UNKNOWN", "speed_count": 0, "leader_decay": 0, "narrative": "Insufficient data"}

    sorted_idx = np.argsort(adj_v0s)[::-1]  # fastest first
    v0_sorted = [adj_v0s[i] for i in sorted_idx]
    decay_sorted = [decay_rates[i] for i in sorted_idx]

    gap_1_2 = v0_sorted[0] - v0_sorted[1]
    gap_2_3 = v0_sorted[1] - v0_sorted[2] if len(v0_sorted) > 2 else 99

    leader_decay = decay_sorted[0]
    median_decay = np.median(decay_rates)

    # Count speed types (v0 within 1.0 ft/s of leader)
    speed_count = sum(1 for v in v0_sorted if v >= v0_sorted[0] - 1.0)

    # Classify
    if gap_1_2 < 0.5:
        scenario = "CONTESTED"
    elif gap_1_2 < 1.0 and gap_2_3 < 0.5:
        scenario = "CONTESTED"  # 2nd and 3rd are close, will pressure leader
    elif gap_1_2 >= 1.5:
        scenario = "LONE_SPEED"
    else:
        scenario = "MODERATE"

    # Flag if leader has high decay (likely to tire)
    high_decay = leader_decay > median_decay + 0.3
    if high_decay:
        scenario += "_HIGH_DECAY"

    # Narrative
    if "CONTESTED" in scenario:
        narrative = f"{speed_count} speed types within 1 ft/s — expect fast pace, closers advantaged"
        if high_decay:
            narrative += ". Leader's high decay rate suggests fade likely."
    elif "LONE_SPEED" in scenario:
        narrative = f"Clear lone speed ({gap_1_2:.1f} ft/s clear of field) — may control on easy lead"
        if high_decay:
            narrative += ". But high decay rate means vulnerable if pressured."
        else:
            narrative += ". Low decay rate — difficult to run down."
    else:
        narrative = "Moderate pace scenario — no clear speed advantage or disadvantage"

    return {
        "scenario": scenario,
        "speed_count": speed_count,
        "leader_decay": leader_decay,
        "median_decay": median_decay,
        "gap_1_2": gap_1_2,
        "narrative": narrative,
    }
