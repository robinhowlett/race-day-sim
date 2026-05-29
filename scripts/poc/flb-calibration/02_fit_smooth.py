"""POC step 2: fit a smooth shrinkage curve.

The bucketed shrinkage from step 1 is monotonic but noisy at extremes
(bucket 0 has 574 winners → wide CI). Fit a smooth function that
preserves monotonicity and gives a defensible value at any odds_prob.

Approach: isotonic regression on actual_win_rate vs mean_implied. This
is non-parametric, monotonic by construction, fits the data exactly
where there's enough sample, and smooths the extremes via the pool-
adjacent-violators algorithm. Inverse: shrinkage(p) = isotonic(p) / p.

Output: tmp/flb_calibration.json — usable shrinkage lookup
        - implied_grid: array of mean_implied probability values
        - actual_grid:  matching isotonic-regressed actual win rates
        - shrinkage_at(p) helper applies linear interpolation
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.isotonic import IsotonicRegression

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"


def main():
    df = pd.read_csv(TMP / "flb_curve.csv")
    df = df.sort_values("mean_implied").reset_index(drop=True)
    print(f"Loaded {len(df)} buckets from step 1.")

    # Weight by sample size when fitting — bucket 0 (574 winners) gets
    # less weight than bucket 49 (83K winners).
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(df["mean_implied"], df["actual_rate"], sample_weight=df["n"])

    # The 50-bucket fit doesn't cover odds_prob > ~0.50 (highest bucket
    # mean is 0.4994). For deep-chalk territory (0.50 < p < 0.99) we
    # need extrapolation. Pull a separate sample of high-chalk starters
    # to anchor that region.
    print("Loading deep-chalk anchor for extrapolation (odds_prob >= 0.50)...")
    import psycopg2
    import os
    conn = psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )
    chalk_q = """
        WITH bins AS (
          SELECT odds_prob::float, won::int,
                 ntile(8) OVER (ORDER BY odds_prob) AS sub_bucket
          FROM handycapper.rkm_market_analysis
          WHERE odds_prob >= 0.50
        )
        SELECT sub_bucket,
               count(*) AS n,
               sum(won)::int AS n_wins,
               avg(odds_prob)::float AS mean_implied,
               (sum(won)::float / count(*))::float AS actual_rate
        FROM bins GROUP BY sub_bucket ORDER BY mean_implied
    """
    chalk_df = pd.read_sql(chalk_q, conn)
    conn.close()
    print(f"  Loaded {len(chalk_df)} high-chalk sub-buckets")
    print(chalk_df.to_string(index=False))

    # Combine the original 50 buckets with the high-chalk sub-buckets,
    # but exclude the original highest bucket (it overlaps with the
    # anchor region). Re-fit isotonic on the combined data.
    main_df = df[df["mean_implied"] < 0.50].copy()
    combined = pd.concat([
        main_df[["mean_implied", "actual_rate", "n"]],
        chalk_df[["mean_implied", "actual_rate", "n"]],
    ], ignore_index=True).sort_values("mean_implied").reset_index(drop=True)
    print(f"\nRe-fitting on combined {len(combined)} bins ...")
    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(combined["mean_implied"], combined["actual_rate"],
            sample_weight=combined["n"])

    # Build a fine grid for the calibration JSON. Cap at p=0.95
    # (anything beyond is data-thin even with the chalk anchor).
    grid_implied = np.linspace(0.001, 0.95, 200)
    grid_actual = iso.predict(grid_implied)
    grid_shrinkage = grid_actual / grid_implied

    # Also save a coarser human-readable view
    df["smooth_actual"] = iso.predict(df["mean_implied"])
    df["smooth_shrinkage"] = df["smooth_actual"] / df["mean_implied"]

    out_csv = TMP / "flb_smoothed.csv"
    df.to_csv(out_csv, index=False)
    print(f"Wrote {out_csv}")

    out_json = TMP / "flb_calibration.json"
    payload = {
        "method": "isotonic regression on actual_win_rate vs mean_implied, "
                   "weighted by bucket sample size",
        "n_observations": int(df["n"].sum()),
        "n_buckets": len(df),
        "date_range": "1997-01-01 to 2016-12-31",
        "fit_n_grid_points": len(grid_implied),
        "implied_grid": grid_implied.tolist(),
        "actual_grid": grid_actual.tolist(),
        "shrinkage_grid": grid_shrinkage.tolist(),
        "usage": "shrinkage = np.interp(odds_prob, implied_grid, shrinkage_grid); "
                  "calibrated_p = odds_prob * shrinkage",
    }
    with open(out_json, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {out_json}")

    # Print a summary at coarse decision points
    print("\n=== Smoothed shrinkage at decision points ===")
    print(f"{'odds_prob':>10} {'odds (X-1)':>10} {'isotonic_p':>10} {'shrink':>8}")
    for p in [0.005, 0.010, 0.020, 0.033, 0.050, 0.067, 0.10, 0.15, 0.20, 0.30, 0.40, 0.50, 0.60]:
        actual = float(iso.predict([p])[0])
        shr = actual / p
        odds = (1.0 / p) - 1.0
        print(f"{p:>10.4f} {odds:>10.1f} {actual:>10.4f} {shr:>8.3f}")


if __name__ == "__main__":
    main()
