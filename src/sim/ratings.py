"""Rating system — unified scale from velocity curves through to Edge computation.

All outputs in rating points (1 point = 58ms sprint / 77ms route ≈ 0.3-0.5 lengths).
Anchor: 100 = CLM $5K-$10K, open/male, 4yo+, non-state-bred, Fast dirt winner.
"""

import numpy as np
import pandas as pd

ANCHOR_TIME_MS = {"sprint": 71577.0, "route": 99192.0}
MS_PER_POINT = {"sprint": 58.0, "route": 77.0}
TEMPERATURE = 6500.0

# Relative A/E baselines (population A/E ≈ 0.80 for dirt/fast)
BASELINE_AE = 0.800


def projected_time_ms(adj_v0: float, decay_rate: float, distance_ft: float) -> float:
    """Project finishing time in milliseconds from curve parameters."""
    avg_v = adj_v0 - decay_rate * (distance_ft / 2000.0)
    if avg_v <= 0:
        avg_v = 30.0
    return distance_ft / avg_v * 1000.0


def compute_rating(adj_v0: float, decay_rate: float, distance_ft: float,
                   zone: str = "route") -> float:
    """Convert velocity curve to rating points.

    Returns rating where 100 = canonical race anchor time.
    Faster = higher rating.
    """
    time_ms = projected_time_ms(adj_v0, decay_rate, distance_ft)
    anchor = ANCHOR_TIME_MS[zone]
    ms_per_pt = MS_PER_POINT[zone]
    return 100.0 + (anchor - time_ms) / ms_per_pt


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
    zone = "sprint" if race["furlongs"].iloc[0] <= 6.5 else "route"
    distance_ft = float(race["furlongs"].iloc[0]) * 660.0
    field_odds = race["closing_odds"].dropna().tolist()

    race_bias = bias_df[bias_df["race_number"] == race_number] if bias_df is not None else None

    # Pre-compute all ratings for field_ratings context
    all_ratings = []
    for _, starter in race.iterrows():
        if pd.notna(starter.get("adj_v0")) and pd.notna(starter.get("adj_decay")):
            all_ratings.append(compute_rating(starter["adj_v0"], starter["adj_decay"], distance_ft, zone))
        else:
            all_ratings.append(np.nan)

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

        # Confidence from curve sample size
        n_races = starter.get("curve_races", 0) or 0
        if n_races >= 15:
            confidence = "HIGH"
        elif n_races >= 8:
            confidence = "MOD"
        elif n_races >= 3:
            confidence = "LOW"
        else:
            confidence = "INSUF"

        rows.append({
            "program": starter.get("program", ""),
            "horse": starter.get("horse_name", ""),
            "rating": result["rating"],
            "market": result["market_rating"],
            "edge": result["edge"],
            "bias_mult": result["bias_mult"],
            "form": round(float(starter.get("v0_trend", 0) or 0) / (MS_PER_POINT[zone] / 1000.0), 1),
            "confidence": confidence,
            "odds": odds,
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values("edge", ascending=False, na_position="last")
    return out
