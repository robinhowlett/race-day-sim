"""POC step 3: train/holdout validation of FLB calibration.

Re-fit the isotonic shrinkage curve on 1997-2014 only, then evaluate
two predictors on 2015-2016:
  - Baseline: raw odds_prob as predicted win probability
  - FLB-corrected: odds_prob * shrinkage(odds_prob), re-normalized
    per-race to sum to 1.0

Metrics:
  - Log-loss per starter
  - Brier score
  - Calibration plot — actual vs predicted in 20 buckets
  - Per-race normalization check — both predictors should sum ≈1.0
    per race after race-level renormalization

Output: tmp/validation_metrics.csv
        tmp/validation_calibration.csv
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
    print("Loading rkm_market_analysis with race dates...")
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT ma.starter_id, ma.race_id,
                   ma.odds_prob::float, ma.won::int,
                   r.date AS race_date
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.races r ON r.id = ma.race_id
            WHERE ma.odds_prob IS NOT NULL AND ma.odds_prob > 0
        """, conn)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["year"] = df["race_date"].dt.year
    print(f"  {len(df):,} rows (years {df['year'].min()}-{df['year'].max()})")

    train = df[df["year"] <= 2014].copy()
    holdout = df[df["year"] >= 2015].copy()
    print(f"  Train: {len(train):,}    Holdout: {len(holdout):,}")

    # ---- Fit isotonic on training set ----
    train["bucket"] = pd.qcut(train["odds_prob"], 50, labels=False, duplicates="drop")
    by_bucket = train.groupby("bucket").agg(
        n=("won", "size"), n_wins=("won", "sum"),
        mean_implied=("odds_prob", "mean"),
    ).reset_index()
    by_bucket["actual_rate"] = by_bucket["n_wins"] / by_bucket["n"]

    # Add high-chalk anchor from training set only
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

    # ---- Apply to holdout ----
    holdout["calibrated_p_raw"] = iso.predict(holdout["odds_prob"].values)

    # Re-normalize within race so the sum across starters in each race
    # equals 1.0. Both predictors get this treatment so they're directly
    # comparable as race-level probability distributions.
    def renorm(s):
        total = s.sum()
        return s / total if total > 0 else s
    holdout["odds_prob_norm"] = holdout.groupby("race_id")["odds_prob"].transform(renorm)
    holdout["calibrated_p"] = holdout.groupby("race_id")["calibrated_p_raw"].transform(renorm)

    # ---- Metrics ----
    eps = 1e-9
    def log_loss(p, y):
        p = np.clip(p, eps, 1 - eps)
        # Per-starter log-loss for the binary outcome
        return -(y * np.log(p) + (1 - y) * np.log(1 - p)).mean()

    def brier(p, y):
        return ((p - y) ** 2).mean()

    metrics = {
        "n_holdout_starters": len(holdout),
        "n_holdout_races":    holdout["race_id"].nunique(),
        "baseline": {
            "log_loss": float(log_loss(holdout["odds_prob_norm"].values,
                                       holdout["won"].values)),
            "brier":    float(brier(holdout["odds_prob_norm"].values,
                                    holdout["won"].values)),
        },
        "flb_calibrated": {
            "log_loss": float(log_loss(holdout["calibrated_p"].values,
                                       holdout["won"].values)),
            "brier":    float(brier(holdout["calibrated_p"].values,
                                    holdout["won"].values)),
        },
    }
    metrics["log_loss_improvement"] = (
        metrics["baseline"]["log_loss"] - metrics["flb_calibrated"]["log_loss"])
    metrics["brier_improvement"] = (
        metrics["baseline"]["brier"] - metrics["flb_calibrated"]["brier"])

    # Per-race log-likelihood improvement (just summing log-prob for the
    # actual winner — what a wagering model genuinely cares about)
    per_race_winners = holdout[holdout["won"] == 1].copy()
    per_race_winners["base_logp"] = np.log(np.clip(
        per_race_winners["odds_prob_norm"], eps, 1 - eps))
    per_race_winners["flb_logp"] = np.log(np.clip(
        per_race_winners["calibrated_p"], eps, 1 - eps))
    metrics["winner_log_likelihood"] = {
        "baseline": float(per_race_winners["base_logp"].mean()),
        "flb_calibrated": float(per_race_winners["flb_logp"].mean()),
        "improvement_per_race":
            float(per_race_winners["flb_logp"].mean() -
                  per_race_winners["base_logp"].mean()),
    }

    print("\n=== Holdout metrics ===")
    print(json.dumps(metrics, indent=2))

    with open(TMP / "validation_metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)

    # ---- Calibration plot data ----
    cal_rows = []
    for label, col in [("baseline", "odds_prob_norm"),
                        ("flb_calibrated", "calibrated_p")]:
        h = holdout.copy()
        h["bucket"] = pd.qcut(h[col], 20, labels=False, duplicates="drop")
        cal = h.groupby("bucket").agg(
            n=("won", "size"), n_wins=("won", "sum"),
            mean_pred=(col, "mean"),
        ).reset_index()
        cal["actual_rate"] = cal["n_wins"] / cal["n"]
        cal["model"] = label
        cal_rows.append(cal)
    cal_df = pd.concat(cal_rows, ignore_index=True)
    cal_df.to_csv(TMP / "validation_calibration.csv", index=False)

    print("\n=== Calibration: baseline (raw odds_prob) ===")
    print(cal_df[cal_df["model"] == "baseline"][
        ["bucket", "n", "mean_pred", "actual_rate"]].to_string(index=False))
    print("\n=== Calibration: FLB-corrected ===")
    print(cal_df[cal_df["model"] == "flb_calibrated"][
        ["bucket", "n", "mean_pred", "actual_rate"]].to_string(index=False))

    # Save the holdout-applicable shrinkage table for later steps
    grid_implied = np.linspace(0.001, 0.95, 200)
    grid_actual = iso.predict(grid_implied)
    grid_shrinkage = grid_actual / grid_implied
    payload = {
        "method": "isotonic regression on actual_win_rate vs mean_implied, "
                   "weighted by bucket sample size; trained on 1997-2014",
        "train_n": int(len(train)),
        "train_years": "1997-2014",
        "holdout_years": "2015-2016",
        "holdout_metrics": metrics,
        "implied_grid": grid_implied.tolist(),
        "actual_grid": grid_actual.tolist(),
        "shrinkage_grid": grid_shrinkage.tolist(),
    }
    with open(TMP / "flb_calibration_holdout.json", "w") as f:
        json.dump(payload, f, indent=2)
    print(f"\nWrote holdout-fit calibration to {TMP / 'flb_calibration_holdout.json'}")


if __name__ == "__main__":
    main()
