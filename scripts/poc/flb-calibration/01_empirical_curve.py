"""POC step 1: empirical FLB curve.

Bucket all rkm_market_analysis rows by odds_prob (overround-normalized
public-belief probability). Compute actual win rate per bucket. The
gap between mid-bucket implied probability and actual win rate is the
favorite-longshot bias signature.

Output: tmp/flb_curve.csv — one row per bucket
        - bucket_id, odds_lo, odds_hi, n, n_wins
        - mean_implied, actual_rate, raw_shrinkage = actual / mean_implied

Use rkm_market_analysis directly (already overround-normalized via the
audit's WA #19 fix to load_market_bias). Time window: full 1997-2016.
Uses 50 quantile buckets so each contains roughly 150K observations —
plenty of statistical power per bucket.
"""

import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"
TMP.mkdir(exist_ok=True)


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


N_BUCKETS = 50  # ~150K obs per bucket given 7.7M total


def main():
    print("Loading odds_prob and won from rkm_market_analysis...")
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT odds_prob::float, won::int, race_id
            FROM handycapper.rkm_market_analysis
            WHERE odds_prob IS NOT NULL AND odds_prob > 0
        """, conn)
    print(f"  {len(df):,} rows, {df['won'].sum():,} winners "
          f"({df['won'].mean()*100:.2f}% baseline win rate)")

    # Quantile buckets — equal-count partition of the distribution.
    df["bucket"] = pd.qcut(df["odds_prob"], N_BUCKETS,
                            labels=False, duplicates="drop")
    by_bucket = df.groupby("bucket").agg(
        n=("won", "size"),
        n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
        odds_lo=("odds_prob", "min"),
        odds_hi=("odds_prob", "max"),
    ).reset_index()
    by_bucket["actual_rate"] = by_bucket["n_wins"] / by_bucket["n"]
    by_bucket["raw_shrinkage"] = by_bucket["actual_rate"] / by_bucket["mean_implied"]
    # Standard error on actual_rate (binomial)
    p = by_bucket["actual_rate"]
    by_bucket["se_actual"] = np.sqrt(p * (1 - p) / by_bucket["n"])

    out = TMP / "flb_curve.csv"
    by_bucket.to_csv(out, index=False)
    print(f"\nWrote {out} ({len(by_bucket)} buckets)")

    print("\n=== Empirical FLB curve (50 buckets) ===")
    print(f"{'bucket':>6} {'mean_imp':>9} {'actual':>9} {'shrink':>7} "
          f"{'n':>8} {'wins':>6}")
    for _, r in by_bucket.iterrows():
        print(f"{int(r['bucket']):>6} {r['mean_implied']:>9.4f} "
              f"{r['actual_rate']:>9.4f} {r['raw_shrinkage']:>7.3f} "
              f"{int(r['n']):>8} {int(r['n_wins']):>6}")

    # Coarse summary
    print("\n=== Coarse buckets (for narrative) ===")
    coarse_bins = [0, 0.02, 0.05, 0.10, 0.20, 0.40, 1.0]
    coarse_labels = ["<2%", "2-5%", "5-10%", "10-20%", "20-40%", "40%+"]
    df["coarse"] = pd.cut(df["odds_prob"], bins=coarse_bins,
                           labels=coarse_labels, include_lowest=True)
    by_coarse = df.groupby("coarse", observed=True).agg(
        n=("won", "size"), n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
    ).reset_index()
    by_coarse["actual_rate"] = by_coarse["n_wins"] / by_coarse["n"]
    by_coarse["shrinkage"] = by_coarse["actual_rate"] / by_coarse["mean_implied"]
    print(by_coarse.to_string(index=False))


if __name__ == "__main__":
    main()
