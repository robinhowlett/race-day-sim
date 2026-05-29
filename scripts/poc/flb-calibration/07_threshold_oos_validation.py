"""POC step 7: out-of-sample validation of the threshold table.

Step 6 found per-tier optimal thresholds via grid search on the
2015-2016 holdout. That's threshold-overfitting on the same data we
score on. Honest test: tune thresholds on 2015 only, then validate
ROI of the tuned table on 2016 only.

If the 2016 ROI on the 2015-tuned thresholds is comparable to the
2015 ROI, the thresholds generalize. If 2016 ROI collapses, the
step-6 numbers were optimistic.
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


TIERS = [
    ("chalk_<2/1",     0.40, 1.00),
    ("short_2-5/1",    0.20, 0.40),
    ("mid_5-10/1",     0.10, 0.20),
    ("long_10-20/1",   0.05, 0.10),
    ("longer_20-50/1", 0.02, 0.05),
    ("extreme_50/1+",  0.00, 0.02),
]
EDGE_THRESHOLDS = [0.0, 0.025, 0.05, 0.075, 0.10, 0.125, 0.15, 0.175, 0.20, 0.25, 0.30]


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


def tier_roi_at_threshold(tier_df, threshold):
    sub = tier_df[tier_df["edge_flb"] >= threshold]
    if len(sub) < 200:
        return None
    n_bets = len(sub)
    n_wins = int(sub["won"].sum())
    roi = float(sub["net_per_bet"].sum() / n_bets)
    se = float(sub["net_per_bet"].std(ddof=0) / np.sqrt(n_bets))
    return {"n_bets": n_bets, "n_wins": n_wins, "roi": roi, "se": se}


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

    # Three-way split:
    # - calibration train: 1997-2014 (used to fit FLB curve)
    # - threshold tune:   2015 (used to pick optimal edge threshold per tier)
    # - true holdout:     2016 (used to score)
    cal_train = df[df["year"] <= 2014].copy()
    tune = df[df["year"] == 2015].copy()
    holdout = df[df["year"] == 2016].copy()
    print(f"  Calibration train (1997-2014): {len(cal_train):,}")
    print(f"  Threshold tune (2015):         {len(tune):,}")
    print(f"  True holdout (2016):           {len(holdout):,}")

    iso = fit_flb(cal_train)

    for d, name in [(tune, "tune"), (holdout, "holdout")]:
        d["calibrated_p"] = iso.predict(d["odds_prob"].values)
        d["edge_flb"] = d["combined_prob"] - d["calibrated_p"]
        d["net_per_bet"] = np.where(
            d["won"] == 1, d["closing_odds"], -1.0)

    # Tune thresholds on 2015
    print("\n=== Tuning per-tier thresholds on 2015 ===")
    tuned = {}
    for tier_name, lo, hi in TIERS:
        tune_tier = tune[(tune["odds_prob"] >= lo) & (tune["odds_prob"] < hi)]
        best = None
        for thr in EDGE_THRESHOLDS:
            r = tier_roi_at_threshold(tune_tier, thr)
            if r is None:
                continue
            if best is None or r["roi"] > best["roi"]:
                best = {"threshold": thr, **r}
        if best is None:
            print(f"  {tier_name}: not enough samples at any threshold")
            tuned[tier_name] = None
        else:
            tuned[tier_name] = best
            print(f"  {tier_name:<18} edge>={best['threshold']:.3f}  "
                  f"n={best['n_bets']}  "
                  f"ROI={100*best['roi']:.2f}% "
                  f"(±{100*best['se']*1.96:.1f}%)")

    # Validate on 2016
    print("\n=== Validating tuned thresholds on 2016 (true OOS) ===")
    print(f"{'tier':<18} {'thr':>6} {'tune_n':>7} {'tune_ROI':>10} "
          f"{'OOS_n':>7} {'OOS_ROI':>10} {'OOS 95% CI':>22}")
    rows = []
    for tier_name, lo, hi in TIERS:
        if tuned[tier_name] is None:
            continue
        thr = tuned[tier_name]["threshold"]
        oos_tier = holdout[(holdout["odds_prob"] >= lo) & (holdout["odds_prob"] < hi)]
        oos = oos_tier[oos_tier["edge_flb"] >= thr]
        if len(oos) < 50:
            print(f"  {tier_name:<18} edge>={thr:.3f}  OOS n={len(oos)} too small")
            continue
        n_bets = len(oos)
        n_wins = int(oos["won"].sum())
        roi = float(oos["net_per_bet"].sum() / n_bets)
        se = float(oos["net_per_bet"].std(ddof=0) / np.sqrt(n_bets))
        ci_lo = roi - 1.96 * se
        ci_hi = roi + 1.96 * se
        rows.append({
            "tier": tier_name,
            "threshold": thr,
            "tune_n": tuned[tier_name]["n_bets"],
            "tune_roi": tuned[tier_name]["roi"],
            "oos_n": n_bets,
            "oos_n_wins": n_wins,
            "oos_roi": roi,
            "oos_roi_lower_95": ci_lo,
            "oos_roi_upper_95": ci_hi,
        })
        print(f"  {tier_name:<18} {thr:>6.3f}  "
              f"{tuned[tier_name]['n_bets']:>7d} "
              f"{100*tuned[tier_name]['roi']:>9.2f}% "
              f"{n_bets:>7d} {100*roi:>9.2f}% "
              f"({100*ci_lo:>+5.1f}% to {100*ci_hi:>+5.1f}%)")

    out_df = pd.DataFrame(rows)
    out_df.to_csv(TMP / "threshold_oos.csv", index=False)
    with open(TMP / "threshold_oos.json", "w") as f:
        json.dump({"tuned_2015": tuned, "validated_2016": rows}, f,
                   indent=2, default=float)

    # Summary verdict
    print("\n=== Verdict ===")
    if rows:
        tune_avg = np.mean([r["tune_roi"] for r in rows])
        oos_avg_weighted = sum(r["oos_n"] * r["oos_roi"] for r in rows) / sum(r["oos_n"] for r in rows)
        print(f"  Tune-set average tier ROI:  {100*tune_avg:.2f}%")
        print(f"  OOS-validated weighted ROI: {100*oos_avg_weighted:.2f}%")
        print(f"  (positive OOS suggests thresholds generalize; negative OOS = overfitting)")


if __name__ == "__main__":
    main()
