"""POC step 4b: predictive comparison using time-residual model.

For each holdout race:
  (A) POC-time model: rank by predicted time_residual + horse random
      intercept (higher = faster than canonical, so picks the highest
      predicted residual as the projected winner).
  (B) Live pipeline: rank by adj_v0 from rkm_velocity_curves
      (joined on bare horse name, same as production).

Score: pick-winner rate, mean reciprocal rank, mean winner rank.
Uses the same race-level holdout (seed=42) as the v0 POC for direct
comparison.
"""

import os
import pickle
import sys
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
    with open(TMP / "model_fit_time.pkl", "rb") as f:
        bundle = pickle.load(f)
    result = bundle["result"]

    df = pd.read_csv(TMP / "split_time.csv") if (TMP / "split_time.csv").exists() \
        else pd.read_parquet(TMP / "split_time.parquet")
    holdout = df[df["holdout"]].dropna(subset=["time_residual"]).copy()

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        holdout[col] = holdout[col].astype(str)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)

    print(f"Holdout: {len(holdout):,} starters across "
          f"{holdout['race_id'].nunique():,} races")

    # POC-time prediction: fixed-effects + horse random intercept.
    # Higher = faster than canonical (positive sign convention).
    horse_re_map = {h: r.iloc[0] for h, r in result.random_effects.items()}
    holdout["horse_re"] = holdout["horse"].map(horse_re_map).fillna(0.0)
    fe_pred = result.predict(holdout)
    holdout["poc_pred"] = fe_pred + holdout["horse_re"]
    holdout["poc_score"] = holdout["poc_pred"]

    # Live pipeline prediction: adj_v0 from rkm_velocity_curves.
    print("Loading live adj_v0 for holdout horses...")
    horse_names = list(holdout["horse"].dropna().unique())
    with get_conn() as conn:
        live = pd.read_sql("""
            SELECT split_part(horse_key, '|', 1) AS horse, surface,
                   distance_zone, adj_v0
            FROM handycapper.rkm_velocity_curves
            WHERE adj_v0 IS NOT NULL
              AND split_part(horse_key, '|', 1) = ANY(%s)
        """, conn, params=(horse_names,))
    holdout["zone"] = (holdout["furlongs"].astype(float) > 6.5).map(
        {True: "route", False: "sprint"})
    holdout = holdout.merge(
        live, left_on=["horse", "surface", "zone"],
        right_on=["horse", "surface", "distance_zone"], how="left",
        suffixes=("", "_live"))
    holdout["live_score"] = holdout["adj_v0"]

    def per_race_metrics(group):
        g = group.dropna(subset=["official_position"]).copy()
        if g.empty or g["official_position"].min() != 1:
            return None
        results = {}
        for name, score_col in [("poc", "poc_score"), ("live", "live_score")]:
            sub = g.dropna(subset=[score_col]).copy()
            if len(sub) < 2:
                continue
            sub = sub.sort_values(score_col, ascending=False).reset_index(drop=True)
            actual_winner_idx = sub.index[sub["official_position"] == 1]
            if len(actual_winner_idx) == 0:
                continue
            rank_of_winner = int(actual_winner_idx[0]) + 1
            results[f"{name}_winner_rank"] = rank_of_winner
            results[f"{name}_picked_winner"] = (rank_of_winner == 1)
            results[f"{name}_mrr"] = 1.0 / rank_of_winner
            results[f"{name}_field_size"] = len(sub)
        return pd.Series(results) if results else None

    metrics = holdout.groupby("race_id").apply(per_race_metrics, include_groups=False)
    metrics = metrics.dropna(how="all")
    if isinstance(metrics, pd.Series):
        metrics = metrics.unstack()
    metrics = metrics.dropna(subset=["poc_picked_winner",
                                      "live_picked_winner"], how="any")

    print(f"\nRaces evaluated: {len(metrics):,} (both POC and live had ≥2 starters)")
    print("\n=== POC-time model (canonical-time residual) ===")
    print(f"  Picked winner:        {metrics['poc_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['poc_mrr'].mean():.3f}")
    print(f"  Mean winner rank:     {metrics['poc_winner_rank'].mean():.2f}")

    print("\n=== Live pipeline (adj_v0) ===")
    print(f"  Picked winner:        {metrics['live_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['live_mrr'].mean():.3f}")
    print(f"  Mean winner rank:     {metrics['live_winner_rank'].mean():.2f}")

    print("\n=== Random baseline ===")
    avg_field = metrics['poc_field_size'].mean()
    print(f"  Mean field size:    {avg_field:.1f}")
    print(f"  Random pick winner: {1/avg_field:.1%}")
    print(f"  Random MRR:         "
          f"{(1/np.arange(1, int(avg_field)+1)).mean():.3f}")

    print("\n=== Top-horses sanity check (POC-time model) ===")
    horse_ability = pd.DataFrame([
        {"horse": h, "ability_ms": r.iloc[0]}
        for h, r in result.random_effects.items()
    ]).sort_values("ability_ms", ascending=False)
    print("Top 15 by horse-ability (ms faster than canonical):")
    print(horse_ability.head(15).to_string(index=False))
    print("\nWell-known 2014 NYRA horses for comparison:")
    for name in ["Tonalist", "Wise Dan", "California Chrome", "Mia Poppy",
                 "Kevin's Steel"]:
        row = horse_ability[horse_ability["horse"] == name]
        if not row.empty:
            print(f"  {name:25s} ability = {row['ability_ms'].iloc[0]:+.0f} ms")
        else:
            print(f"  {name:25s} (not in fit)")


if __name__ == "__main__":
    main()
