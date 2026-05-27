"""Rating system — unified scale from velocity curves through to Edge computation.

All outputs in rating points (1 point = 58ms sprint / 77ms route ≈ 0.3-0.5 lengths).
Anchor: 100 = CLM $5K-$10K, open/male, 4yo+, non-state-bred, Fast dirt winner.
"""

import numpy as np
import pandas as pd

# Canonical winner curve params by distance and surface.
# Dirt/Synthetic: CLM $5K-$10K winners → these produce rating 100.
# Turf: CLM $10K-$25K winners → these produce rating 112 (turf canonical class is higher,
#   because $5K-$10K claiming barely exists on turf. $10K-$25K turf ≈ rating 112 on the
#   universal scale derived from dirt class ladder analysis).
_CANONICAL_PARAMS = {
    # (surface, furlongs): (adj_v0, adj_decay, anchor_rating)
    # Dirt CLM $5K-$10K → rating 100
    ("Dirt", 4.5): (65.475, 4.3711, 100.0),
    ("Dirt", 5.0): (65.962, 4.5886, 100.0),
    ("Dirt", 5.5): (65.519, 4.2354, 100.0),
    ("Dirt", 6.0): (64.805, 3.8911, 100.0),
    ("Dirt", 6.5): (65.021, 3.9763, 100.0),
    ("Dirt", 7.0): (61.945, 2.7248, 100.0),
    ("Dirt", 8.0): (60.525, 2.2537, 100.0),
    ("Dirt", 8.32): (60.262, 2.1625, 100.0),
    ("Dirt", 8.5): (60.117, 2.0951, 100.0),
    ("Dirt", 9.0): (59.841, 2.0040, 100.0),
    # Synthetic CLM $5K-$10K → rating 100
    ("Synthetic", 5.5): (63.637, 3.5354, 100.0),
    ("Synthetic", 6.0): (63.412, 3.3545, 100.0),
    ("Synthetic", 6.5): (64.216, 3.5776, 100.0),
    ("Synthetic", 8.0): (58.897, 1.7607, 100.0),
    ("Synthetic", 8.5): (57.936, 1.4602, 100.0),
    # Turf CLM $10K-$25K → rating 112 (universal scale)
    ("Turf", 5.0): (63.005, 2.3976, 112.0),
    ("Turf", 5.5): (61.747, 1.8380, 112.0),
    ("Turf", 6.5): (61.875, 1.4819, 112.0),
    ("Turf", 7.0): (59.387, 1.2171, 112.0),
    ("Turf", 7.5): (57.052, 0.7335, 112.0),
    ("Turf", 8.0): (56.178, 0.4995, 112.0),
    ("Turf", 8.5): (55.910, 0.3645, 112.0),
    ("Turf", 9.0): (55.543, 0.2271, 112.0),
}

MS_PER_POINT = {"sprint": 58.0, "route": 77.0}
TEMPERATURE = 6500.0


def _get_anchor(surface: str, furlongs: float) -> tuple[float, float]:
    """Get the formula-projected anchor time and its rating value for this distance/surface.

    Returns (anchor_time_ms, anchor_rating). For dirt, anchor_rating=100.
    For turf, anchor_rating=112 (turf canonical class is higher).

    Interpolates between known canonical distances if exact match unavailable.
    """
    distance_ft = furlongs * 660.0
    key = (surface, furlongs)
    if key in _CANONICAL_PARAMS:
        v0, decay, rating = _CANONICAL_PARAMS[key]
        avg_v = v0 - decay * (distance_ft / 2000.0)
        return distance_ft / avg_v * 1000.0, rating

    # Interpolate: find two nearest distances on this surface
    surface_keys = sorted([(f, v0, d, r) for (s, f), (v0, d, r) in _CANONICAL_PARAMS.items() if s == surface])
    if not surface_keys:
        surface_keys = sorted([(f, v0, d, r) for (s, f), (v0, d, r) in _CANONICAL_PARAMS.items() if s == "Dirt"])

    if furlongs <= surface_keys[0][0]:
        f, v0, decay, rating = surface_keys[0]
        avg_v = v0 - decay * (distance_ft / 2000.0)
        return distance_ft / max(avg_v, 30.0) * 1000.0, rating
    if furlongs >= surface_keys[-1][0]:
        f, v0, decay, rating = surface_keys[-1]
        avg_v = v0 - decay * (distance_ft / 2000.0)
        return distance_ft / max(avg_v, 30.0) * 1000.0, rating

    for i in range(len(surface_keys) - 1):
        f_lo, v0_lo, d_lo, r_lo = surface_keys[i]
        f_hi, v0_hi, d_hi, r_hi = surface_keys[i + 1]
        if f_lo <= furlongs <= f_hi:
            t = (furlongs - f_lo) / (f_hi - f_lo)
            v0 = v0_lo + t * (v0_hi - v0_lo)
            decay = d_lo + t * (d_hi - d_lo)
            rating = r_lo + t * (r_hi - r_lo)
            avg_v = v0 - decay * (distance_ft / 2000.0)
            return distance_ft / max(avg_v, 30.0) * 1000.0, rating

    return 70000.0, 100.0

# Relative A/E baselines (population A/E ≈ 0.80 for dirt/fast)
BASELINE_AE = 0.800


def projected_time_ms(adj_v0: float, decay_rate: float, distance_ft: float) -> float:
    """Project finishing time in milliseconds from curve parameters."""
    avg_v = adj_v0 - decay_rate * (distance_ft / 2000.0)
    if avg_v <= 0:
        avg_v = 30.0
    return distance_ft / avg_v * 1000.0


def compute_rating(adj_v0: float, decay_rate: float, distance_ft: float,
                   zone: str = "route", surface: str = "Dirt",
                   furlongs: float | None = None) -> float:
    """Convert velocity curve to rating points.

    Returns rating where 100 = canonical race winner at this distance/surface.
    Faster = higher rating.
    """
    time_ms = projected_time_ms(adj_v0, decay_rate, distance_ft)
    if furlongs is None:
        furlongs = distance_ft / 660.0
    anchor_time, anchor_rating = _get_anchor(surface, furlongs)
    ms_per_pt = MS_PER_POINT[zone]
    return anchor_rating + (anchor_time - time_ms) / ms_per_pt


def compute_ratings_for_field(card: pd.DataFrame, race_number: int) -> pd.Series:
    """Compute ratings for all starters in a race.

    Returns Series indexed by starter_id with rating values.
    Starters without curves get NaN.
    """
    race = card[card["race_number"] == race_number].copy()
    zone = "sprint" if race["furlongs"].iloc[0] <= 6.5 else "route"
    distance_ft = float(race["furlongs"].iloc[0]) * 660.0

    ratings = {}
    for _, row in race.iterrows():
        if pd.notna(row.get("adj_v0")) and pd.notna(row.get("adj_decay")):
            ratings[row["starter_id"]] = compute_rating(
                row["adj_v0"], row["adj_decay"], distance_ft, zone
            )
        else:
            ratings[row["starter_id"]] = np.nan
    return pd.Series(ratings, name="rating")


def odds_to_rating(odds: float, field_odds: list[float], zone: str = "route",
                   field_ratings: list[float] | None = None) -> float:
    """Convert a horse's odds to implied rating given the field context.

    Maps odds-implied probability rank to the field's rating distribution.
    A horse with the highest implied probability gets the highest rated horse's
    rating (according to the market). This preserves the rating-point scale
    while expressing "what the market thinks this horse is worth."
    """
    if odds <= 0:
        return np.nan

    implied_prob = 1.0 / (odds + 1.0)
    total_implied = sum(1.0 / (o + 1.0) for o in field_odds if o > 0)
    normalized_prob = implied_prob / total_implied

    if field_ratings is not None:
        valid_ratings = sorted([r for r in field_ratings if not np.isnan(r)])
        if len(valid_ratings) < 2:
            return 100.0

        # Map probability to percentile rank within the field
        # Then map that percentile to the corresponding rating
        sorted_probs = sorted([1.0 / (o + 1.0) / total_implied for o in field_odds if o > 0])
        # Find this horse's rank in the probability distribution
        rank = sum(1 for p in sorted_probs if p <= normalized_prob) / len(sorted_probs)
        # Map rank [0,1] to the rating distribution
        idx = min(int(rank * len(valid_ratings)), len(valid_ratings) - 1)
        return valid_ratings[idx]
    else:
        return 100.0


def bias_multiplier(bias_df: pd.Series) -> float:
    """Compute multiplicative bias factor from a starter's market bias row.

    Each applicable factor contributes its relative A/E (factor_ae / baseline).
    Factors with insufficient sample (< 10 starts) are skipped.
    Factors combine multiplicatively.

    Args:
        bias_df: a single row from load_market_bias() result

    Returns:
        Multiplier to apply to model probability (1.0 = no bias, >1 = underbet, <1 = overbet)
    """
    multiplier = 1.0

    def _flag(key):
        v = bias_df.get(key)
        if v is None:
            return False
        if hasattr(v, 'item'):
            return bool(v.item())
        return bool(v)

    def _val(key, default=None):
        v = bias_df.get(key, default)
        if v is None:
            return default
        if hasattr(v, 'item'):
            return v.item()
        return v

    # First-time Lasix: relative A/E = 0.818 / 0.800 = 1.022
    if _flag("first_time_lasix"):
        multiplier *= 1.022

    # Blinkers off: relative A/E = 0.881 / 0.800 = 1.101
    if _flag("blinkers_off"):
        multiplier *= 1.101

    # First-time blinkers: relative A/E = 0.776 / 0.800 = 0.970
    if _flag("first_time_blinkers"):
        multiplier *= 0.970

    # Off-turf + short-priced (favorite context handled by caller)
    if _flag("off_turf"):
        multiplier *= 1.050

    # Jockey upgrade/downgrade
    switch = _val("jockey_switch_type", "SAME")
    if switch == "UPGRADE":
        multiplier *= 1.051
    elif switch == "DOWNGRADE":
        multiplier *= 0.888

    # Jockey allowance (5lb bug): relative A/E = 0.825 / 0.800 = 1.031
    allowance = _val("jockey_allowance", 0) or 0
    if allowance == 5:
        multiplier *= 1.031

    # Surface switch
    if _flag("surface_switch"):
        prev = _val("prev_surface", "")
        curr = _val("surface", "")
        if prev == "Synthetic" and curr == "Turf":
            multiplier *= 1.075
        elif prev == "Synthetic" and curr == "Dirt":
            multiplier *= 1.036
        elif prev == "Turf" and curr == "Dirt":
            multiplier *= 0.969

    # Class drop
    if _val("class_move") == "DROP":
        multiplier *= 1.029
    elif _val("class_move") == "RISE":
        multiplier *= 0.961

    # Claimed last race (first start with new trainer)
    if _flag("claimed_last_race"):
        trainer_claim_ae = _val("trainer_claim_ae")
        if trainer_claim_ae and (_val("trainer_claim_starts", 0) or 0) >= 10:
            multiplier *= float(trainer_claim_ae) / BASELINE_AE
        else:
            multiplier *= 1.034  # population average claim edge

    # Trainer FTS (only applies if horse is FTS)
    if _flag("is_fts"):
        trainer_fts_ae = _val("trainer_fts_ae")
        if trainer_fts_ae and (_val("trainer_fts_starts", 0) or 0) >= 10:
            multiplier *= float(trainer_fts_ae) / BASELINE_AE
        # If trainer has no FTS record, use population FTS A/E = 0.776
        # which means FTS are overbet: 0.776 / 0.800 = 0.970
        elif (_val("trainer_fts_starts", 0) or 0) < 10:
            multiplier *= 0.970

    # Trainer layoff (only if returning from 90+ days)
    if _flag("is_layoff"):
        trainer_layoff_ae = _val("trainer_layoff_ae")
        if trainer_layoff_ae and (_val("trainer_layoff_starts", 0) or 0) >= 10:
            multiplier *= float(trainer_layoff_ae) / BASELINE_AE

    # Trainer surface switch (only if switching surface AND trainer has record)
    if _flag("surface_switch"):
        trainer_switch_ae = _val("trainer_switch_ae")
        if trainer_switch_ae and (_val("trainer_switch_starts", 0) or 0) >= 10:
            multiplier *= float(trainer_switch_ae) / BASELINE_AE

    # Trainer class drop (only if dropping AND trainer has record)
    if _val("class_move") == "DROP":
        trainer_drop_ae = _val("trainer_drop_ae")
        if trainer_drop_ae and (_val("trainer_drop_starts", 0) or 0) >= 10:
            # Replace the generic drop factor with trainer-specific
            multiplier /= 1.029  # undo generic
            multiplier *= float(trainer_drop_ae) / BASELINE_AE

    return multiplier


def compute_edge(rating: float, odds: float, field_odds: list[float],
                 bias_mult: float = 1.0, zone: str = "route",
                 field_ratings: list[float] | None = None) -> dict:
    """Compute Edge in rating points.

    Returns dict with:
        rating: model rating (base, before bias)
        market: odds-implied rating
        edge: rating - market (in points)
        bias_mult: the applied multiplier (for transparency)
    """
    market_rating = odds_to_rating(odds, field_odds, zone, field_ratings)

    # Bias adjusts the effective rating (model thinks horse is better/worse
    # than the raw curve says, due to group-level signals)
    ms_per_pt = MS_PER_POINT[zone]
    # Convert multiplicative probability shift to rating point shift
    # bias_mult of 1.10 means 10% more likely to win → how many rating points is that?
    # Approximation: at mid-field odds (~5/1), 10% prob shift ≈ 2-3 rating points
    if bias_mult != 1.0 and bias_mult > 0:
        # Use log scale: rating adjustment = log(bias_mult) × scaling factor
        # Calibrated so that bias_mult=1.10 at typical odds ≈ +2 pts
        bias_pts = np.log(bias_mult) * 20.0
    else:
        bias_pts = 0.0

    adjusted_rating = rating + bias_pts

    edge = adjusted_rating - market_rating if not np.isnan(market_rating) else np.nan

    return {
        "rating": round(rating, 1),
        "adjusted_rating": round(adjusted_rating, 1),
        "market_rating": round(market_rating, 1) if not np.isnan(market_rating) else None,
        "edge": round(edge, 1) if not np.isnan(edge) else None,
        "bias_mult": round(bias_mult, 3),
        "bias_pts": round(bias_pts, 1),
    }


def format_race_ratings(card: pd.DataFrame, bias_df: pd.DataFrame,
                        race_number: int) -> pd.DataFrame:
    """Produce the unified display table for a race.

    Returns DataFrame with columns: horse, rating, market, edge, bias_factors, confidence
    """
    race = card[card["race_number"] == race_number].copy()
    furlongs = float(race["furlongs"].iloc[0])
    surface = str(race["surface"].iloc[0])
    zone = "sprint" if furlongs <= 6.5 else "route"
    distance_ft = furlongs * 660.0
    field_odds = race["closing_odds"].dropna().tolist()

    race_bias = bias_df[bias_df["race_number"] == race_number] if bias_df is not None else None

    # Pre-compute all ratings using confidence-weighted blending:
    # - current_form (point-in-time safe) is the primary physics source
    # - adj_v0 from career curve is fallback (with first_race filter for leakage safety)
    # - w_physics = 1 - exp(-n_recent / 5): how much to trust the curve
    # - When w_physics is low, the rating carries a wide band (expressed in output)
    all_ratings = []
    all_w_physics = []
    for _, starter in race.iterrows():
        # Determine physics source and confidence
        has_current = pd.notna(starter.get("current_v0"))
        has_career = pd.notna(starter.get("adj_v0"))
        n_recent = int(starter.get("n_recent_races") or 0) if has_current else 0
        curve_races = int(starter.get("curve_races") or 0) if has_career else 0

        if has_current:
            v0 = float(starter["current_v0"])
            decay = float(starter["current_decay"])
            # w_physics from recent race count (current_form is time-weighted,
            # so n_recent reflects how much recent data backs it)
            w = 1.0 - np.exp(-n_recent / 5.0)
        elif has_career:
            v0 = float(starter["adj_v0"])
            decay = float(starter["adj_decay"])
            # Career curve (filtered by first_race < race_date) — less reliable
            # but at least the horse has SOME history in this zone
            w = 1.0 - np.exp(-curve_races / 8.0)  # slower ramp (career is noisier)
            w *= 0.7  # discount for using career instead of current form
        else:
            v0 = None
            decay = None
            w = 0.0

        if v0 is not None and decay is not None:
            all_ratings.append(compute_rating(
                v0, decay, distance_ft, zone,
                surface=surface, furlongs=furlongs
            ))
        else:
            all_ratings.append(np.nan)
        all_w_physics.append(w)

    rows = []
    for i, (_, starter) in enumerate(race.iterrows()):
        sid = starter["starter_id"]
        rating = all_ratings[i]

        # Bias multiplier
        bias_row = None
        if race_bias is not None and not race_bias.empty:
            match = race_bias[race_bias["starter_id"] == sid]
            if not match.empty:
                bias_row = match.iloc[0].to_dict()

        mult = bias_multiplier(bias_row) if bias_row is not None else 1.0

        # Edge (pass field_ratings for calibrated market rating)
        odds = starter.get("closing_odds", np.nan)
        if not np.isnan(rating) and not np.isnan(odds) and odds > 0:
            result = compute_edge(rating, odds, field_odds, mult, zone, all_ratings)
        else:
            result = {
                "rating": round(rating, 1) if not np.isnan(rating) else None,
                "adjusted_rating": None,
                "market_rating": None,
                "edge": None,
                "bias_mult": round(mult, 3),
                "bias_pts": 0.0,
            }

        # Confidence band (± rating points) from w_physics
        # Higher w_physics = tighter band (more data = more trust)
        w = all_w_physics[i]
        if w >= 0.85:
            band = 3
        elif w >= 0.60:
            band = 6
        elif w >= 0.30:
            band = 10
        elif w > 0:
            band = 15
        else:
            band = None  # no physics data at all

        # Format edge with band
        if result["edge"] is not None and band is not None:
            edge_str = f"{result['edge']:+.0f} (±{band})"
        elif result["edge"] is not None:
            edge_str = f"{result['edge']:+.0f}"
        else:
            edge_str = ""

        rows.append({
            "program": starter.get("program", ""),
            "horse": starter.get("horse_name", ""),
            "rating": result["rating"],
            "market": result["market_rating"],
            "edge": result["edge"],
            "edge_display": edge_str,
            "band": band,
            "bias_mult": result["bias_mult"],
            "form": round(float(starter.get("v0_trend", 0) or 0) / (MS_PER_POINT[zone] / 1000.0), 1),
            "odds": odds,
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("edge", ascending=False, na_position="last")
    return out
