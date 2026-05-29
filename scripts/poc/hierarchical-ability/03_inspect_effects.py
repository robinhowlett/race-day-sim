"""POC step 3: inspect estimated random effects.

Pull track effects, race-shock effects, and horse abilities from the
fitted model. Compare track effects to the live rkm_track_offsets
table; compare top-ranked horses against eyeball expectation.
"""

import os
import pickle
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore")

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def main():
    with open(TMP / "model_fit.pkl", "rb") as f:
        bundle = pickle.load(f)
    result = bundle["result"]

    # Random effects: horse intercepts (groups) + variance components
    # (track, race_id) per group
    re_dict = result.random_effects  # keyed by group (horse)
    print(f"Number of horse groups: {len(re_dict)}")

    # Pull horse ability estimates (random intercept per horse)
    rows = []
    for horse, re in re_dict.items():
        rows.append({"horse": horse, "ability": re.iloc[0]})
    horse_ability = pd.DataFrame(rows).sort_values("ability", ascending=False)
    horse_ability.to_csv(TMP / "horse_ability.csv", index=False)
    print(f"\nTop 20 horses by ability (random intercept on v0):")
    print(horse_ability.head(20).to_string(index=False))

    print(f"\nBottom 10 horses by ability:")
    print(horse_ability.tail(10).to_string(index=False))

    # Variance components estimates: hidden inside fe_params and the
    # variance structure. The track variance components are stored
    # separately — extract them.
    print("\n" + "="*70)
    print("VARIANCE COMPONENT ESTIMATES")
    print("="*70)
    print(f"  Group (horse) variance: {result.cov_re.iloc[0,0]:.4f}")
    print(f"  Variance components:    {result.vcomp}")
    print(f"  Residual variance:      {result.scale:.4f}")

    # Try to extract per-track effect from the model.
    # statsmodels stores variance-component effects per group; we can
    # reconstruct by computing predicted - fixed-effects-prediction for
    # races at each track and averaging within track.
    # Actually simpler: use random_effects + variance component recovery.
    # In statsmodels the random_effects dict contains BOTH the group
    # intercept AND the variance components per group (in a stacked Series).
    # Pull a sample to confirm structure:
    sample_re = next(iter(re_dict.values()))
    print(f"\nSample random_effects entry (per-horse) shape: {len(sample_re)}")
    print(f"Index labels: {list(sample_re.index)[:5]} ...")

    # Read the live offsets table for comparison
    print("\n" + "="*70)
    print("COMPARING TO LIVE rkm_track_offsets")
    print("="*70)
    with get_conn() as conn:
        live = pd.read_sql(
            "SELECT track, v0_offset, n_shippers, confidence "
            "FROM handycapper.rkm_track_offsets "
            "WHERE track IN ('AQU','BEL','SAR') ORDER BY track",
            conn)
    print("Live offsets (NYRA tracks):")
    print(live.to_string(index=False))

    # Approximate per-track effect from the POC model: fit residuals
    # avg per track. (Not perfect — overlooks the random horse effect —
    # but a rough guide.)
    print("\nNOTE: extracting POC model's track effect requires a separate")
    print("variance-component decomposition pass — running it next...")

    # Compute fitted values, then per-track residual mean.
    # The model's response is v0_residual = v0 - canonical_v0[surface, fur].
    # Track effect ≈ mean of (residual - fixed_effects_pred - horse_random_intercept)
    # within each track. This is the "what's left after all other modeled
    # effects" — should map to the model's per-track variance component.
    df = pd.read_csv(TMP / "split.csv") if (TMP / "split.csv").exists() else pd.read_parquet(TMP / "split.parquet")
    train = df[~df["holdout"]].copy()
    train = train.dropna(subset=["v0_residual"]).copy()
    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        train[col] = train[col].astype(str)
    train["state_bred_flag"] = train["state_bred_flag"].astype(int)

    fe_pred = result.predict(train)
    train["fe_pred"] = fe_pred
    horse_re_map = {h: r.iloc[0] for h, r in re_dict.items()}
    train["horse_re"] = train["horse"].map(horse_re_map).fillna(0.0)
    # Residual on the canonical-anchored response — what's unexplained
    # by FE + horse RE. Mean within track = approximate track effect.
    train["unexplained"] = train["v0_residual"] - train["fe_pred"] - train["horse_re"]
    track_eff = train.groupby("track")["unexplained"].agg(["mean", "std", "count"]).reset_index()
    track_eff = track_eff.rename(columns={"mean": "poc_track_effect_ftps"})
    print("\nPOC model per-track effect (canonical-anchored, ft/s):")
    print(track_eff.to_string(index=False))

    # Compare directionally to live offsets. Note signs differ by
    # convention: v0_offset in the live table is what gets SUBTRACTED
    # from raw v0 to get adj_v0, so positive offset = inflated track.
    # The POC track_effect is in v0 space directly (positive = faster).
    # So expect them to be approximately opposite-signed.
    print("\nSign convention:")
    print("  Live  v0_offset > 0  → 'track inflated raw v0' → SUBTRACTED to adjust")
    print("  POC   track_effect > 0 → 'this track is faster' → would be subtracted by reverse")
    merged = track_eff.merge(live, on="track")
    merged["live_offset_signed"] = -merged["v0_offset"]  # flip to match POC sign
    print("\nSide-by-side (signed-aligned: positive = track is faster):")
    print(merged[["track", "poc_track_effect_ftps", "live_offset_signed"]].to_string(index=False))


if __name__ == "__main__":
    main()
