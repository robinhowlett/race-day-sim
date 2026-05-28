"""Prospective pace prediction from field velocity curve distribution.

Empirical context (handycapper TB 2014, ~34K races):
- Sprint adj_v0 by surface: dirt=64.6, synthetic=63.6, turf=61.9, all ≈64
- Route adj_v0 by surface: dirt=60.4, synthetic=58.2, turf=56.0, all ≈60
- Within-race gap (1st-2nd) as a fraction of field median v0:
    dirt route=0.0125, dirt sprint=0.0134
    synthetic route=0.0155, synthetic sprint=0.0139
    turf route=0.0137, turf sprint=0.0264 (small n=158, treat with caution)
- The horse with highest adj_v0 wins only 12-16% and misses the board ~62% of the time.
- adj_v0 characterizes who will be ON THE LEAD (energy expenditure), not who wins.
- Decay rate determines who sustains — the real predictor of outcome, especially in routes.
"""

import numpy as np


# Surface×zone-specific gap thresholds as fractions of the field's median v0.
# Sources: handycapper TB 2014, ~34K races, P50 and P85 of (gap_1_2 / median_v0).
# P50 is the "contested" threshold (gaps below median = contested).
# P85 is the "lone-speed" threshold (gaps well above typical = standout speed).
#
# Why surface×zone-specific (vs single auto-scaling fraction):
# - Dirt route, dirt sprint, turf route cluster tightly around 0.013
# - Synthetic route is elevated (0.0155) — synthetic surfaces bunch finishes
# - Turf sprint is markedly higher (0.0264) but small-sample (n=158); the
#   turf-sprint distance market is structurally chaotic (5-5.5f turf is a
#   weird selection of pure speed types)
# Per-surface lookup is more honest than a single global fraction.
_GAP_FRAC = {
    # (surface, zone): (contested_p50, lone_speed_p85)
    ("Dirt",      "route"):  (0.0125, 0.0320),
    ("Dirt",      "sprint"): (0.0134, 0.0334),
    ("Synthetic", "route"):  (0.0155, 0.0387),
    ("Synthetic", "sprint"): (0.0139, 0.0340),
    ("Turf",      "route"):  (0.0137, 0.0354),
    ("Turf",      "sprint"): (0.0264, 0.0722),
}
# Fallback fractions for unknown surface combos (use Dirt as the safe default
# since it's the largest sample and the conservative middle of the cluster)
_DEFAULT_GAP_FRAC = {
    "route":  (0.0125, 0.0320),
    "sprint": (0.0134, 0.0334),
}

# Speed-count inclusion threshold (a horse counts as "speed" if their v0 is
# within this fraction of the leader's v0). Single global fraction — the
# speed-count concept is less surface-sensitive than gap thresholds.
_SPEED_THRESHOLD_FRAC_ROUTE  = 0.016   # ≈ 1.0 ft/s at v0=62.5
_SPEED_THRESHOLD_FRAC_SPRINT = 0.024   # ≈ 1.5 ft/s at v0=62.5


def predict_pace(adj_v0s: list[float], decay_rates: list[float],
                 furlongs: float = 8.0, surface: str | None = None) -> dict:
    """Predict pace scenario from the field's velocity curve profiles.

    Args:
        adj_v0s: adjusted initial velocities for each horse
        decay_rates: deceleration rates for each horse
        furlongs: race distance (affects sprint/route classification)
        surface: "Dirt" / "Turf" / "Synthetic". When supplied, uses
            surface×zone-specific gap-threshold fractions calibrated from
            handycapper TB 2014 data. When None, falls back to dirt
            (the largest, most stable sample).

    Gap thresholds for CONTESTED / LONE_SPEED are expressed as a fraction of
    the field's median v0. This auto-scales across surfaces because absolute
    v0 magnitudes differ (dirt ~64, turf ~56) — the same race "tightness"
    means different absolute gap. Surface-specific fractions further refine
    where the empirical distribution diverges (synthetic and turf sprint).

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
    zone = "route" if is_route else "sprint"

    # Speed-count threshold (single global fraction; less surface-sensitive
    # than gap thresholds). Dirt baseline: 1.0 ft/s route, 1.5 ft/s sprint at
    # v0 ≈ 62.5 → fractions ≈ 0.016 / 0.024.
    speed_frac = _SPEED_THRESHOLD_FRAC_ROUTE if is_route else _SPEED_THRESHOLD_FRAC_SPRINT
    speed_threshold = max(0.3, speed_frac * median_v0)
    speed_count = sum(1 for v in v0_sorted if v >= v0_sorted[0] - speed_threshold)

    # Gap thresholds: surface×zone-specific fractions of field median v0.
    contested_frac, lone_speed_frac = _GAP_FRAC.get(
        (surface, zone),
        _DEFAULT_GAP_FRAC[zone],
    )
    contested_threshold  = contested_frac  * median_v0
    lone_speed_threshold = lone_speed_frac * median_v0

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
