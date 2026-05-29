"""POC step 6: tune the odds-tier minimum-edge threshold table.

The recommended integration pairs FLB calibration with an odds-tier
minimum edge threshold. The recommendation in the writeup used round
numbers (0.05 / 0.075 / 0.10 / 0.15) — this script does a holdout
ROI sweep over a grid of threshold combinations to find the actual
optimum (or at least pin down whether the recommendation is in the
right ballpark).

Approach: for each odds tier, sweep the FLB-edge threshold over a
range and report ROI + n_bets at each point. The right threshold is
the one that maximizes ROI while keeping n_bets enough that
statistical noise is bounded.

Output: tmp/threshold_grid.csv
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


# Odds tiers (by odds_prob)
TIERS = [
    ("chalk_<2/1",     0.40, 1.00),
    ("short_2-5/1",    0.20, 0.40),
    ("mid_5-10/1",     0.10, 0.20),
    ("long_10-20/1",   0.05, 0.10),
    ("longer_20-50/1", 0.02, 0.05),
    ("extreme_50/1+",  0.00, 0.02),
]

EDGE_THRESHOLDS = [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30]


def main():
    print("Loading market analysis + closing odds...")
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

    train = df[df["year"] <= 2014].copy()
    holdout = df[df["year"] >= 2015].copy()

    # Re-fit FLB on training set
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

    holdout["calibrated_p"] = iso.predict(holdout["odds_prob"].values)
    holdout["edge_flb"] = holdout["combined_prob"] - holdout["calibrated_p"]
    holdout["net_per_bet"] = np.where(
        holdout["won"] == 1, holdout["closing_odds"], -1.0)

    rows = []
    for tier_name, lo, hi in TIERS:
        tier_mask = (holdout["odds_prob"] >= lo) & (holdout["odds_prob"] < hi)
        tier_df = holdout[tier_mask]
        for thr in EDGE_THRESHOLDS:
            sub = tier_df[tier_df["edge_flb"] >= thr]
            if len(sub) == 0:
                continue
            n_bets = len(sub)
            n_wins = int(sub["won"].sum())
            roi = float(sub["net_per_bet"].sum() / n_bets)
            # Binomial-ish SE on ROI is hard; use SE of net_per_bet mean
            se_roi = float(sub["net_per_bet"].std(ddof=0) / np.sqrt(n_bets))
            rows.append({
                "tier": tier_name,
                "tier_lo": lo, "tier_hi": hi,
                "edge_thr": thr,
                "n_bets": n_bets,
                "n_wins": n_wins,
                "win_rate": n_wins / n_bets,
                "avg_odds": float(sub["closing_odds"].mean()),
                "roi": roi,
                "roi_se": se_roi,
                "roi_lower_95": roi - 1.96 * se_roi,
                "roi_upper_95": roi + 1.96 * se_roi,
            })

    out = pd.DataFrame(rows)
    out.to_csv(TMP / "threshold_grid.csv", index=False)

    print("\n=== ROI grid by tier × edge threshold ===")
    print(f"{'tier':<18} {'thr':>5} {'n':>6} {'wins':>5} "
          f"{'avg_odds':>9} {'ROI%':>7} {'95%CI lo':>10} {'95%CI hi':>10}")
    for _, r in out.iterrows():
        print(f"{r['tier']:<18} {r['edge_thr']:>5.3f} {int(r['n_bets']):>6} "
              f"{int(r['n_wins']):>5} {r['avg_odds']:>9.2f} "
              f"{100*r['roi']:>6.2f}% {100*r['roi_lower_95']:>9.2f}% "
              f"{100*r['roi_upper_95']:>9.2f}%")

    # Per-tier optimal threshold (max ROI with n >= 200 to limit noise)
    print("\n=== Per-tier optimal threshold (max ROI subject to n_bets >= 200) ===")
    optimal = []
    for tier_name, _, _ in TIERS:
        tier_rows = out[(out["tier"] == tier_name) & (out["n_bets"] >= 200)]
        if tier_rows.empty:
            print(f"  {tier_name}: no thresholds yield n >= 200")
            continue
        best = tier_rows.loc[tier_rows["roi"].idxmax()]
        optimal.append({
            "tier": tier_name,
            "best_thr": best["edge_thr"],
            "n_bets": int(best["n_bets"]),
            "roi_pct": 100 * best["roi"],
            "ci_lo_pct": 100 * best["roi_lower_95"],
            "ci_hi_pct": 100 * best["roi_upper_95"],
        })
        print(f"  {tier_name:<18} edge>={best['edge_thr']:.3f}  "
              f"n={int(best['n_bets'])}  "
              f"ROI={100*best['roi']:.2f}%  "
              f"(95% CI {100*best['roi_lower_95']:.1f}% to "
              f"{100*best['roi_upper_95']:.1f}%)")

    with open(TMP / "threshold_grid_optimal.json", "w") as f:
        json.dump(optimal, f, indent=2)


if __name__ == "__main__":
    main()
