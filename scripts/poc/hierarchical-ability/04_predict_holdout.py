"""POC step 4: predictive comparison on the race-level holdout.

For each holdout race, compute two predictions of the actual finishing
order:

  (A) POC model: rank starters by predicted v0_residual + horse_ability
      random intercept. Higher = predicted faster.

  (B) Live pipeline: rank by adj_v0 from rkm_velocity_curves (joined
      via SPLIT_PART(horse_key, '|', 1) = horse, the existing pattern).

Score: probability that the actual winner is the model's #1-ranked
horse, plus mean reciprocal rank of the actual winner.

Run as: python 04_predict_holdout.py
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

SIM_SRC = POC_DIR.parents[2] / "src"
sys.path.insert(0, str(SIM_SRC))


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

    df = pd.read_csv(TMP / "split.csv") if (TMP / "split.csv").exists() else pd.read_parquet(TMP / "split.parquet")
    holdout = df[df["holdout"]].dropna(subset=["v0_residual"]).copy()

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        holdout[col] = holdout[col].astype(str)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)

    print(f"Holdout: {len(holdout):,} starters across "
          f"{holdout['race_id'].nunique():,} races")

    # POC prediction: fixed-effect prediction + horse random intercept
    # (where available). The interpretation: how much faster than the
    # canonical anchor the model expects this horse to run.
    horse_re_map = {h: r.iloc[0] for h, r in result.random_effects.items()}
    holdout["horse_re"] = holdout["horse"].map(horse_re_map).fillna(0.0)
    fe_pred = result.predict(holdout)
    holdout["poc_pred"] = fe_pred + holdout["horse_re"]

    # Live pipeline prediction: pull adj_v0 from rkm_velocity_curves.
    # This is what the existing system would use (modulo the bare-name
    # join issue T1.2; for the POC we accept that since the live pipeline
    # uses it too).
    print("Loading live adj_v0 for holdout horses...")
    horse_names = tuple(holdout["horse"].dropna().unique())
    with get_conn() as conn:
        live = pd.read_sql("""
            SELECT split_part(horse_key, '|', 1) AS horse, surface,
                   distance_zone, adj_v0
            FROM handycapper.rkm_velocity_curves
            WHERE adj_v0 IS NOT NULL
              AND split_part(horse_key, '|', 1) = ANY(%s)
        """, conn, params=(list(horse_names),))
    holdout["zone"] = (holdout["furlongs"].astype(float) > 6.5).map(
        {True: "route", False: "sprint"})
    holdout = holdout.merge(
        live, left_on=["horse", "surface", "zone"],
        right_on=["horse", "surface", "distance_zone"], how="left",
        suffixes=("", "_live"))

    # Live pipeline ranking score is just adj_v0 (higher = faster).
    holdout["live_score"] = holdout["adj_v0"]

    # POC ranking score is poc_pred (higher = faster than canonical).
    holdout["poc_score"] = holdout["poc_pred"]

    # Per-race ranking: who finished 1st (official_position == 1)?
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
            rank_of_winner = int(actual_winner_idx[0]) + 1  # 1-based
            results[f"{name}_winner_rank"] = rank_of_winner
            results[f"{name}_picked_winner"] = (rank_of_winner == 1)
            results[f"{name}_mrr"] = 1.0 / rank_of_winner
            results[f"{name}_field_size"] = len(sub)
        return pd.Series(results) if results else None

    metrics = holdout.groupby("race_id").apply(per_race_metrics, include_groups=False)
    # per_race_metrics returns a Series per group → unstacking gives a DF
    metrics = metrics.dropna(how="all")
    if isinstance(metrics, pd.Series):
        metrics = metrics.unstack()
    metrics = metrics.dropna(subset=["poc_picked_winner", "live_picked_winner"], how="any")

    print(f"\nRaces evaluated: {len(metrics):,} (both POC and live had ≥2 starters)")
    print(f"\n=== POC model ===")
    print(f"  Picked winner: {metrics['poc_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['poc_mrr'].mean():.3f}")
    print(f"  Mean winner rank: {metrics['poc_winner_rank'].mean():.2f}")

    print(f"\n=== Live pipeline (adj_v0) ===")
    print(f"  Picked winner: {metrics['live_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['live_mrr'].mean():.3f}")
    print(f"  Mean winner rank: {metrics['live_winner_rank'].mean():.2f}")

    print(f"\n=== Random baseline (1 / mean_field_size) ===")
    avg_field = metrics['poc_field_size'].mean()
    print(f"  Mean field size: {avg_field:.1f}")
    print(f"  Random pick winner: {1/avg_field:.1%}")
    print(f"  Random MRR (harmonic): "
          f"{(1/np.arange(1, int(avg_field)+1)).mean():.3f}")


if __name__ == "__main__":
    main()
