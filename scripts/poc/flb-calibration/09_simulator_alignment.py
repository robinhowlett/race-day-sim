"""POC step 9: validate the FLB tier table on the simulator's playable universe.

The POC (steps 1-8) trained and validated on the FULL rkm_market_analysis
population (1997-2016, 7.7M starter-races). The simulator only plays
sim_candidates — a much narrower universe filtered to:
  - 2005-2017
  - No Grade 1/2 days
  - ≥8 races, ≥7 with trifecta results, avg field ≥7
  - Avg trifecta pool ≥$10K

If the FLB tier table holds on the full population but breaks on
sim_candidates (e.g., because larger fields and active pools have
different microstructure), then run_simulation.py runs will not
deliver the +18.7% / +33% / +46% the POC promised.

This script reuses the step-8 rolling-window methodology but allows
restricting the population to sim_candidates via --sim-candidates.
Compare per-tier ROI between the two populations to judge transfer.

Output: tmp/simulator_alignment.json
"""

import argparse
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


def load_population(restrict_to_sim_candidates: bool):
    """Pull the joined market-analysis + closing odds + actual win payoff data.

    The original POC used closing_odds (decimal-1 → per-$1 net on a winner).
    We pull that AND the actual wps payoff so we can also report what charts
    actually paid (which can differ from odds-implied due to rounding,
    coupled entries, dead heats, etc).
    """
    sql = """
        SELECT ma.starter_id, ma.race_id,
               ma.odds_prob::float, ma.combined_prob::float,
               ma.won::int, s.odds::float AS closing_odds,
               r.date AS race_date,
               (w_win.payoff / NULLIF(w_win.unit, 0))::float AS win_payoff_per_1
        FROM handycapper.rkm_market_analysis ma
        JOIN handycapper.starters s ON s.id = ma.starter_id
        JOIN handycapper.races r ON r.id = ma.race_id
        LEFT JOIN handycapper.wps w_win
          ON w_win.starter_id = s.id
         AND w_win.type = 'Win'
         AND w_win.payoff > 0
        {where}
        WHERE ma.odds_prob IS NOT NULL AND ma.odds_prob > 0
          AND ma.combined_prob IS NOT NULL
          AND s.odds IS NOT NULL AND s.odds > 0
    """
    if restrict_to_sim_candidates:
        # Only keep starters whose (track, date) is a sim_candidate day.
        where = ("JOIN handycapper.sim_candidates sc "
                 "ON sc.track = r.track AND sc.date = r.date")
    else:
        where = ""
    with get_conn() as conn:
        df = pd.read_sql(sql.format(where=where), conn)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["year"] = df["race_date"].dt.year
    return df


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


def evaluate_per_tier(test_df, tuned, min_n=20):
    out = {}
    for tier_name, lo, hi in TIERS:
        if tuned.get(tier_name) is None:
            out[tier_name] = None
            continue
        thr = tuned[tier_name]["threshold"]
        tier_df = test_df[(test_df["odds_prob"] >= lo) & (test_df["odds_prob"] < hi)]
        sub = tier_df[tier_df["edge_flb"] >= thr]
        if len(sub) < min_n:
            out[tier_name] = {"threshold": thr, "n": len(sub), "roi": None,
                              "n_wins": int(sub["won"].sum()) if not sub.empty else 0}
            continue
        n = len(sub)
        n_wins = int(sub["won"].sum())
        roi = float(sub["net_per_bet"].sum() / n)
        roi_actual = float(sub["net_per_bet_actual"].sum() / n) if "net_per_bet_actual" in sub else None
        se = float(sub["net_per_bet"].std(ddof=0) / np.sqrt(n))
        out[tier_name] = {
            "threshold": thr, "n": n, "n_wins": n_wins,
            "roi": roi, "roi_actual_payoff": roi_actual,
            "ci_lo": roi - 1.96 * se, "ci_hi": roi + 1.96 * se,
        }
    return out


def run_validation(df, label, test_years):
    """For each test year T, refit FLB on 1997..(T-2), tune on T-1, score on T."""
    print(f"\n=== {label} (n={len(df):,} starter-races, "
          f"years {df['year'].min()}-{df['year'].max()}) ===")

    out = {"label": label, "n": len(df), "by_year": {}}
    print(f"{'Test':>4} {'Tier':<18} {'Thr':>6} "
          f"{'OOS_n':>6} {'wins':>5} {'OOS_ROI':>9} "
          f"{'ROI_actual':>11} {'CI':>22}")

    for test_year in test_years:
        train_end = test_year - 2
        tune_year = test_year - 1
        train = df[df["year"] <= train_end].copy()
        tune = df[df["year"] == tune_year].copy()
        test = df[df["year"] == test_year].copy()
        if len(train) < 50000 or len(tune) < 5000 or len(test) < 5000:
            print(f"  {test_year}: insufficient data, skipping "
                  f"(train={len(train):,} tune={len(tune):,} test={len(test):,})")
            continue

        iso = fit_flb(train)
        for d in (tune, test):
            d["calibrated_p"] = iso.predict(d["odds_prob"].values)
            d["edge_flb"] = d["combined_prob"] - d["calibrated_p"]
            # net_per_bet uses CLOSING ODDS (POC-original).
            d["net_per_bet"] = np.where(d["won"] == 1, d["closing_odds"], -1.0)
            # net_per_bet_actual uses CHART win payoff (more realistic but
            # missing on older / chart-thin races; fall back to closing).
            d["net_per_bet_actual"] = np.where(
                d["won"] == 1,
                d["win_payoff_per_1"].fillna(d["closing_odds"] + 1) - 1,
                -1.0,
            )

        tuned = tune_per_tier(tune)
        oos = evaluate_per_tier(test, tuned)

        out["by_year"][test_year] = {"tuned": tuned, "oos": oos}

        for tier_name, _, _ in TIERS:
            r = oos.get(tier_name)
            if r is None:
                continue
            if r.get("roi") is None:
                print(f"  {test_year:>4} {tier_name:<18} {r['threshold']:>6.3f} "
                      f"{r['n']:>6} {r.get('n_wins', 0):>5}  (n too small)")
                continue
            ci = f"({100*r['ci_lo']:+5.1f}% to {100*r['ci_hi']:+5.1f}%)"
            actual_s = f"{100*r['roi_actual_payoff']:>+9.2f}%" if r.get("roi_actual_payoff") is not None else "       —"
            print(f"  {test_year:>4} {tier_name:<18} {r['threshold']:>6.3f} "
                  f"{r['n']:>6} {r['n_wins']:>5} {100*r['roi']:>+8.2f}% "
                  f"{actual_s:>11} {ci:>22}")

    # Per-tier rollup
    print(f"\n  --- Per-tier rollup ({label}) ---")
    print(f"  {'Tier':<18} {'years':>6} {'+ROI':>5} {'mean_ROI':>9} "
          f"{'mean_actual':>11} {'tot_n':>7}")
    for tier_name, _, _ in TIERS:
        rois = []
        rois_actual = []
        n_total = 0
        for yr, payload in out["by_year"].items():
            r = payload["oos"].get(tier_name)
            if r and r.get("roi") is not None:
                rois.append(r["roi"])
                if r.get("roi_actual_payoff") is not None:
                    rois_actual.append(r["roi_actual_payoff"])
                n_total += r["n"]
        n_yrs = len(rois)
        n_pos = sum(1 for r in rois if r > 0)
        if n_yrs:
            print(f"  {tier_name:<18} {n_yrs:>6} {n_pos:>3}/{n_yrs:<2} "
                  f"{100*np.mean(rois):>+8.2f}% "
                  f"{(100*np.mean(rois_actual) if rois_actual else 0):>+10.2f}% "
                  f"{n_total:>7}")
        else:
            print(f"  {tier_name:<18} {0:>6}  -    -          -          0")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim-candidates", action="store_true",
                    help="Restrict population to the simulator's playable universe")
    ap.add_argument("--years", default="2010-2016",
                    help="Test year range (e.g. 2010-2016 or 2007-2016). "
                         "Restricted to 2005+ when --sim-candidates is set.")
    args = ap.parse_args()

    y_lo, y_hi = (int(x) for x in args.years.split("-"))
    test_years = list(range(y_lo, y_hi + 1))

    label = "sim_candidates" if args.sim_candidates else "full_population"
    t0 = time.time()
    df = load_population(args.sim_candidates)
    print(f"Loaded {label}: {len(df):,} rows in {time.time()-t0:.1f}s")
    if args.sim_candidates and y_lo < 2005:
        print(f"  (--sim-candidates requires 2005+; truncating test years.)")
        test_years = [y for y in test_years if y >= 2007]  # tune window starts 2005

    result = run_validation(df, label, test_years)

    out_path = TMP / f"simulator_alignment_{label}.json"
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, default=float)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
