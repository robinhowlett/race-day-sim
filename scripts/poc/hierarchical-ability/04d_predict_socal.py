"""POC step 4d: predictive comparison on SoCal 2017 holdout.

Same shape as 04c (NYRA extended) but for the SoCal slice.
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

CIRCUIT_NAME = "socal"


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def main():
    with open(TMP / f"model_fit_{CIRCUIT_NAME}.pkl", "rb") as f:
        bundle = pickle.load(f)
    result = bundle["result"]

    parquet = TMP / f"split_{CIRCUIT_NAME}.parquet"
    csv = TMP / f"split_{CIRCUIT_NAME}.csv"
    df = pd.read_parquet(parquet) if parquet.exists() else pd.read_csv(csv)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["year"] = df["race_date"].dt.year
    holdout = df[df["year"] == 2017].dropna(subset=["time_residual"]).copy()

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        holdout[col] = holdout[col].astype(str)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)

    print(f"Holdout (SoCal 2017): {len(holdout):,} starters across "
          f"{holdout['race_id'].nunique():,} races")

    horse_re_map = {h: r.iloc[0] for h, r in result.random_effects.items()}
    holdout["horse_re"] = holdout["horse"].map(horse_re_map).fillna(0.0)
    fe_pred = result.predict(holdout)
    holdout["poc_pred"] = fe_pred + holdout["horse_re"]
    holdout["poc_score"] = holdout["poc_pred"]
    n_seen = holdout["horse"].isin(horse_re_map).sum()
    print(f"  Horses seen in training: {n_seen:,} / {len(holdout):,} starters "
          f"({100*n_seen/len(holdout):.1f}%)")

    print("\nLoading live adj_v0 (point-in-time filtered)...")
    horse_names = list(holdout["horse"].dropna().unique())
    with get_conn() as conn:
        live = pd.read_sql("""
            SELECT split_part(horse_key, '|', 1) AS horse, surface,
                   distance_zone, adj_v0, first_race
            FROM handycapper.rkm_velocity_curves
            WHERE adj_v0 IS NOT NULL
              AND split_part(horse_key, '|', 1) = ANY(%s)
        """, conn, params=(horse_names,))
    live["first_race"] = pd.to_datetime(live["first_race"])
    holdout["zone"] = (holdout["furlongs"].astype(float) > 6.5).map(
        {True: "route", False: "sprint"})

    h2 = holdout.merge(
        live, left_on=["horse", "surface", "zone"],
        right_on=["horse", "surface", "distance_zone"], how="left",
        suffixes=("", "_live"))
    h2["live_pre_race_ok"] = (h2["first_race"].notna() &
                              (h2["first_race"] < h2["race_date"]))
    h2.loc[~h2["live_pre_race_ok"], "adj_v0"] = np.nan
    holdout = h2
    holdout["live_score"] = holdout["adj_v0"]
    n_live = holdout["adj_v0"].notna().sum()
    print(f"  Starters with point-in-time-safe live adj_v0: "
          f"{n_live:,} / {len(holdout):,} ({100*n_live/len(holdout):.1f}%)")

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

    print(f"\nRaces evaluated: {len(metrics):,}")
    print("\n=== POC-extended (SoCal 2010-2016 fit, 2017 holdout) ===")
    print(f"  Picked winner:        {metrics['poc_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['poc_mrr'].mean():.3f}")
    print(f"  Mean winner rank:     {metrics['poc_winner_rank'].mean():.2f}")

    print("\n=== Live pipeline (point-in-time-safe adj_v0) ===")
    print(f"  Picked winner:        {metrics['live_picked_winner'].mean():.1%}")
    print(f"  Mean reciprocal rank: {metrics['live_mrr'].mean():.3f}")
    print(f"  Mean winner rank:     {metrics['live_winner_rank'].mean():.2f}")

    print("\n=== Random baseline ===")
    avg_field = metrics['poc_field_size'].mean()
    print(f"  Mean field size:    {avg_field:.1f}")
    print(f"  Random pick winner: {1/avg_field:.1%}")

    print("\n=== Top 20 horses by 2010-2016 SoCal ability ===")
    horse_ability = pd.DataFrame([
        {"horse": h, "ability_ms": r.iloc[0]}
        for h, r in result.random_effects.items()
    ]).sort_values("ability_ms", ascending=False)
    print(horse_ability.head(20).to_string(index=False))

    print("\nFamiliar 2010-2016 SoCal stakes names:")
    for name in ["California Chrome", "Beholder", "Goldencents", "Shared Belief",
                 "Game On Dude", "Stay Thirsty", "Dortmund", "American Pharoah",
                 "Mucho Gusto", "Bayern", "Songbird", "Stellar Wind",
                 "Mor Spirit", "Lookin At Lucky"]:
        row = horse_ability[horse_ability["horse"] == name]
        if not row.empty:
            print(f"  {name:25s} ability = {row['ability_ms'].iloc[0]:+.0f} ms")
        else:
            print(f"  {name:25s} (not in fit)")


if __name__ == "__main__":
    main()
