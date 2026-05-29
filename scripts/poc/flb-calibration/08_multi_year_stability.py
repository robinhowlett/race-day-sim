"""POC step 8: multi-year rolling-window stability check.

The 2015-tune / 2016-OOS test in step 7 is one year of validation —
sample-of-one. To check that the per-tier threshold table generalizes
across years (vs being overfit to 2016's market microstructure), run
the same three-way-split methodology on rolling windows.

For each test year T in {2010..2016}:
  - Calibration train: 1997 to T-2
  - Threshold tune:    T-1
  - True OOS:          T

Output the per-tier OOS ROI for each test year. If the tier ranking
is stable across years (long 10-20 and longer 20-50 always
significantly +EV, extreme always unprofitable), the threshold table
generalizes. If results bounce wildly year-to-year, the 2016 result
in step 7 was lucky.

Computationally heavy: each test year refits the FLB curve (cheap, ~5s)
and does a grid search per tier (also cheap). Total: ~7 iterations × ~30s
each ≈ 4 minutes.
"""

import json
import os
import time
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


TIERS = [
    ("chalk_<2/1",     0.40, 1.00),
    ("short_2-5/1",    0.20, 0.40),
    ("mid_5-10/1",     0.10, 0.20),
    ("long_10-20/1",   0.05, 0.10),
    ("longer_20-50/1", 0.02, 0.05),
    ("extreme_50/1+",  0.00, 0.02),
]
EDGE_THRESHOLDS = [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30]
TEST_YEARS = list(range(2010, 2017))  # 2010..2016


def fit_flb(train_df):
    train_df = train_df.copy()
    train_df["bucket"] = pd.qcut(train_df["odds_prob"], 50, labels=False, duplicates="drop")
    by_bucket = train_df.groupby("bucket").agg(
        n=("won", "size"), n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
    ).reset_index()
    by_bucket["actual_rate"] = by_bucket["n_wins"] / by_bucket["n"]
    chalk = train_df[train_df["odds_prob"] >= 0.50].copy()
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
    return iso


def tune_per_tier(tune_df, min_n=200):
    tuned = {}
    for tier_name, lo, hi in TIERS:
        tier_df = tune_df[(tune_df["odds_prob"] >= lo) & (tune_df["odds_prob"] < hi)]
        best = None
        for thr in EDGE_THRESHOLDS:
            sub = tier_df[tier_df["edge_flb"] >= thr]
            if len(sub) < min_n:
                continue
            roi = float(sub["net_per_bet"].sum() / len(sub))
            if best is None or roi > best["roi"]:
                best = {"threshold": thr, "n": len(sub), "roi": roi}
        tuned[tier_name] = best
    return tuned


def evaluate_per_tier(test_df, tuned, min_n=50):
    out = {}
    for tier_name, lo, hi in TIERS:
        if tuned.get(tier_name) is None:
            out[tier_name] = None
            continue
        thr = tuned[tier_name]["threshold"]
        tier_df = test_df[(test_df["odds_prob"] >= lo) & (test_df["odds_prob"] < hi)]
        sub = tier_df[tier_df["edge_flb"] >= thr]
        if len(sub) < min_n:
            out[tier_name] = {"threshold": thr, "n": len(sub), "roi": None}
            continue
        n = len(sub)
        n_wins = int(sub["won"].sum())
        roi = float(sub["net_per_bet"].sum() / n)
        se = float(sub["net_per_bet"].std(ddof=0) / np.sqrt(n))
        out[tier_name] = {
            "threshold": thr, "n": n, "n_wins": n_wins,
            "roi": roi, "ci_lo": roi - 1.96 * se, "ci_hi": roi + 1.96 * se,
        }
    return out


def main():
    print("Loading market analysis + closing odds (full slice)...")
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
    print(f"  {len(df):,} rows, years {df['year'].min()}-{df['year'].max()}")

    all_results = {}
    print("\nRolling-window OOS validation:")
    print(f"{'Test':>4} {'Train':>10} {'Tune':>5} {'Tier':<18} {'Thr':>6} "
          f"{'OOS_n':>6} {'OOS_ROI':>9} {'CI':>22}")

    for test_year in TEST_YEARS:
        train_end = test_year - 2
        tune_year = test_year - 1
        train = df[df["year"] <= train_end].copy()
        tune = df[df["year"] == tune_year].copy()
        test = df[df["year"] == test_year].copy()
        if len(train) < 100000 or len(tune) < 50000 or len(test) < 50000:
            print(f"  {test_year}: insufficient data, skipping")
            continue

        t0 = time.time()
        iso = fit_flb(train)
        for d in (tune, test):
            d["calibrated_p"] = iso.predict(d["odds_prob"].values)
            d["edge_flb"] = d["combined_prob"] - d["calibrated_p"]
            d["net_per_bet"] = np.where(d["won"] == 1, d["closing_odds"], -1.0)

        tuned = tune_per_tier(tune)
        oos = evaluate_per_tier(test, tuned)
        elapsed = time.time() - t0

        all_results[test_year] = {
            "train_years": f"1997-{train_end}",
            "tune_year": tune_year,
            "tuned": tuned,
            "oos": oos,
            "fit_seconds": elapsed,
        }

        for tier_name, _, _ in TIERS:
            r = oos.get(tier_name)
            if r is None or r.get("roi") is None:
                continue
            ci = f"({100*r['ci_lo']:+5.1f}% to {100*r['ci_hi']:+5.1f}%)"
            print(f"{test_year:>4} {f'1997-{train_end}':>10} {tune_year:>5} "
                  f"{tier_name:<18} {r['threshold']:>6.3f} {r['n']:>6} "
                  f"{100*r['roi']:>8.2f}% {ci:>22}")
        print()

    out = TMP / "rolling_window_oos.json"
    with open(out, "w") as f:
        json.dump(all_results, f, indent=2, default=float)
    print(f"\nWrote {out}")

    # Summary across years per tier
    print("\n=== Per-tier ROI across test years ===")
    print(f"{'Tier':<18} ", end="")
    for y in TEST_YEARS:
        print(f"{y:>9}", end="")
    print(f"  {'mean':>8} {'+ROIyrs':>8}")
    for tier_name, _, _ in TIERS:
        print(f"{tier_name:<18} ", end="")
        rois = []
        for y in TEST_YEARS:
            yr = all_results.get(y, {})
            r = yr.get("oos", {}).get(tier_name)
            if r is None or r.get("roi") is None:
                print(f"{'—':>9}", end="")
            else:
                rois.append(r["roi"])
                print(f"{100*r['roi']:>+8.2f}%", end="")
        if rois:
            mean_roi = np.mean(rois)
            n_pos = sum(1 for r in rois if r > 0)
            print(f"  {100*mean_roi:>+7.2f}% {n_pos}/{len(rois):>3}")
        else:
            print()


if __name__ == "__main__":
    main()
