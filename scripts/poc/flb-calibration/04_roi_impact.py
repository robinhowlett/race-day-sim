"""POC step 4: ROI impact of FLB calibration on conviction picks.

The FLB curve calibrates ODDS-IMPLIED probability to actual win rate.
For wagering, we want to know: does correcting the public's bias help
us identify +EV bets?

The natural test:
  - Original conviction = model_prob > odds_prob (model thinks horse
    is underbet)
  - FLB-corrected conviction = model_prob > odds_prob * shrinkage
    (model thinks horse is underbet RELATIVE to the bias-corrected
    public belief)

For this POC we use the existing combined_prob in rkm_market_analysis
as a proxy for "model_prob" — it's what the live system would actually
treat as the model's win probability.

Compare three ROI scenarios on the 2015-2016 holdout:
  - All bets: bet $1 to win on every starter at closing odds → baseline
  - Original conviction picks: bet on starters where combined_prob >
    odds_prob (i.e., positive edge) → "live system" overlay strategy
  - FLB-corrected conviction picks: bet where combined_prob >
    odds_prob * shrinkage → FLB-aware overlay

ROI = (sum of payouts when won) - (count of bets) / (count of bets)
       expressed as %.

Output: tmp/roi_comparison.csv, tmp/roi_metrics.json
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
from sklearn.isotonic import IsotonicRegression

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
    print("Loading market analysis + closing odds for ROI calc...")
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT ma.starter_id, ma.race_id,
                   ma.odds_prob::float, ma.combined_prob::float,
                   ma.won::int, s.odds::float AS closing_odds,
                   r.date AS race_date
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.starters s ON s.id = ma.starter_id
            JOIN handycapper.races r ON r.id = ma.race_id
            WHERE ma.odds_prob IS NOT NULL AND ma.odds_prob > 0
              AND ma.combined_prob IS NOT NULL
              AND s.odds IS NOT NULL AND s.odds > 0
        """, conn)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["year"] = df["race_date"].dt.year
    print(f"  {len(df):,} rows")

    train = df[df["year"] <= 2014].copy()
    holdout = df[df["year"] >= 2015].copy()
    print(f"  Train: {len(train):,}    Holdout: {len(holdout):,}")

    # Re-fit FLB on training set (same approach as step 3)
    train["bucket"] = pd.qcut(train["odds_prob"], 50, labels=False, duplicates="drop")
    by_bucket = train.groupby("bucket").agg(
        n=("won", "size"), n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
    ).reset_index()
    by_bucket["actual_rate"] = by_bucket["n_wins"] / by_bucket["n"]
    chalk = train[train["odds_prob"] >= 0.50].copy()
    chalk["sub"] = pd.qcut(chalk["odds_prob"], 8, labels=False, duplicates="drop")
    chalk_b = chalk.groupby("sub").agg(
        n=("won", "size"), n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
    ).reset_index()
    chalk_b["actual_rate"] = chalk_b["n_wins"] / chalk_b["n"]
    main_b = by_bucket[by_bucket["mean_implied"] < 0.50]
    combined = pd.concat([
        main_b[["mean_implied", "actual_rate", "n"]],
        chalk_b[["mean_implied", "actual_rate", "n"]],
    ], ignore_index=True).sort_values("mean_implied").reset_index(drop=True)
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(combined["mean_implied"], combined["actual_rate"],
            sample_weight=combined["n"])

    # Apply to holdout
    holdout["calibrated_p"] = iso.predict(holdout["odds_prob"].values)
    holdout["shrinkage"] = holdout["calibrated_p"] / holdout["odds_prob"]

    # Define overlay edges
    holdout["edge_baseline"] = holdout["combined_prob"] - holdout["odds_prob"]
    holdout["edge_flb"] = holdout["combined_prob"] - holdout["calibrated_p"]

    # Compute payouts: $1 win bet pays $closing_odds + $1 if won, else $0.
    # Net per-bet = (closing_odds + 1) - 1 = closing_odds if won, else -1.
    holdout["net_per_bet"] = np.where(
        holdout["won"] == 1, holdout["closing_odds"], -1.0)

    def roi_summary(name, mask):
        bets = holdout[mask]
        if len(bets) == 0:
            return {"strategy": name, "n_bets": 0}
        n_bets = len(bets)
        n_wins = int(bets["won"].sum())
        gross = float(bets["net_per_bet"].sum())  # $ profit on $1 bets
        roi = gross / n_bets
        avg_odds = float(bets["closing_odds"].mean())
        avg_implied = float(bets["odds_prob"].mean())
        return {
            "strategy": name,
            "n_bets": n_bets,
            "n_wins": n_wins,
            "win_rate": n_wins / n_bets,
            "avg_odds": avg_odds,
            "avg_implied_prob": avg_implied,
            "gross_pnl_per_dollar": gross / n_bets,
            "roi_pct": 100 * roi,
        }

    rows = []
    rows.append(roi_summary("All starters (random)", pd.Series([True] * len(holdout), index=holdout.index)))
    rows.append(roi_summary("Baseline edge >0",
                             holdout["edge_baseline"] > 0))
    rows.append(roi_summary("Baseline edge >0.05",
                             holdout["edge_baseline"] > 0.05))
    rows.append(roi_summary("Baseline edge >0.10",
                             holdout["edge_baseline"] > 0.10))
    rows.append(roi_summary("FLB edge >0",
                             holdout["edge_flb"] > 0))
    rows.append(roi_summary("FLB edge >0.05",
                             holdout["edge_flb"] > 0.05))
    rows.append(roi_summary("FLB edge >0.10",
                             holdout["edge_flb"] > 0.10))

    # Subset where the two strategies AGREE (both flag positive edge) and
    # where they DISAGREE
    rows.append(roi_summary("Both flag edge >0",
                             (holdout["edge_baseline"] > 0) &
                             (holdout["edge_flb"] > 0)))
    rows.append(roi_summary("Baseline edge >0 but FLB edge <=0",
                             (holdout["edge_baseline"] > 0) &
                             (holdout["edge_flb"] <= 0)))
    rows.append(roi_summary("FLB edge >0 but baseline edge <=0",
                             (holdout["edge_baseline"] <= 0) &
                             (holdout["edge_flb"] > 0)))

    # By odds tier — FLB should help most in the tails
    for lo, hi, label in [(0, 0.02, "longshot 50/1+"),
                           (0.02, 0.05, "20-50/1"),
                           (0.05, 0.10, "10-20/1"),
                           (0.10, 0.20, "5-10/1"),
                           (0.20, 0.40, "2-5/1"),
                           (0.40, 1.00, "chalk <2/1")]:
        odds_mask = (holdout["odds_prob"] >= lo) & (holdout["odds_prob"] < hi)
        rows.append(roi_summary(f"All [{label}]", odds_mask))
        rows.append(roi_summary(f"Baseline edge >0 [{label}]",
                                 odds_mask & (holdout["edge_baseline"] > 0)))
        rows.append(roi_summary(f"FLB edge >0 [{label}]",
                                 odds_mask & (holdout["edge_flb"] > 0)))

    out_df = pd.DataFrame(rows)
    out_df.to_csv(TMP / "roi_comparison.csv", index=False)

    print("\n=== ROI summary ===")
    print(out_df[["strategy", "n_bets", "n_wins", "avg_odds", "roi_pct"]].to_string(
        index=False))

    print("\n=== Disagreement analysis ===")
    disagree_baseline_only = (holdout["edge_baseline"] > 0) & (holdout["edge_flb"] <= 0)
    disagree_flb_only = (holdout["edge_baseline"] <= 0) & (holdout["edge_flb"] > 0)
    print(f"Bets baseline calls +EV but FLB calls -EV: {disagree_baseline_only.sum():,}")
    print(f"Bets FLB calls +EV but baseline calls -EV: {disagree_flb_only.sum():,}")

    metrics = {
        "n_holdout": len(holdout),
        "summary": rows,
    }
    with open(TMP / "roi_metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)


if __name__ == "__main__":
    main()
