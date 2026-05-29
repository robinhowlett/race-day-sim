"""POC step 2b: hierarchical model on canonical-time residual.

The v0-only POC (02_fit_model.py) was set up to fail: v0 is the
regression intercept of velocity-vs-distance, heavily weighted toward
early-race speed. A closer winning a 12f stamina race (low v0, low
decay) gets ranked below a fast-breaking claimer (high v0, high decay)
who finishes mid-pack. Wrong response variable.

Switch the response to canonical-anchored finish-time residual:

    canonical_time[surface, furlongs] = the time a canonical winner
        runs at this distance (CLM $5K-$10K dirt, CLM $10K-$25K turf).

    time_residual = canonical_time - actual_finish_time

Higher time_residual = faster than canonical winner. Sign convention
matches the v0 POC's "higher = faster," so downstream ranking and
sanity-check code flow naturally.

Same hierarchical structure as before:
    time_residual ~ going + age_group + sex_group + state_bred_flag
                  + race_class
                  + (1 | horse) + (1 | track) + (1 | race_id)

Surface, distance, zone are folded into the canonical anchor — not
modeled as separate fixed effects.
"""

import os
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"
warnings.filterwarnings("ignore")

SIM_SRC = POC_DIR.parents[2] / "src"
sys.path.insert(0, str(SIM_SRC))
from sim.ratings import _CANONICAL_PARAMS  # noqa: E402


def canonical_time_ms(surface: str, furlongs: float) -> float:
    """Return the canonical-anchor finish time in ms for this (surface, furlongs).

    Uses the canonical (v0, decay) for the cell, computes the model's
    finish time via t = distance / avg_v, where
    avg_v = v0 - decay * distance/2000.
    Mirrors ratings._get_anchor's interpolation logic on (v0, decay) jointly.
    """
    distance_ft = furlongs * 660.0
    key = (surface, furlongs)
    if key in _CANONICAL_PARAMS:
        v0, decay, _ = _CANONICAL_PARAMS[key]
    else:
        sk = sorted([(f, v0, d) for (s, f), (v0, d, _) in _CANONICAL_PARAMS.items()
                     if s == surface])
        if not sk:
            sk = sorted([(f, v0, d) for (s, f), (v0, d, _) in _CANONICAL_PARAMS.items()
                         if s == "Dirt"])
        if furlongs <= sk[0][0]:
            v0, decay = sk[0][1], sk[0][2]
        elif furlongs >= sk[-1][0]:
            v0, decay = sk[-1][1], sk[-1][2]
        else:
            for i in range(len(sk) - 1):
                f_lo, v_lo, d_lo = sk[i]
                f_hi, v_hi, d_hi = sk[i + 1]
                if f_lo <= furlongs <= f_hi:
                    t = (furlongs - f_lo) / (f_hi - f_lo)
                    v0 = v_lo + t * (v_hi - v_lo)
                    decay = d_lo + t * (d_hi - d_lo)
                    break
    avg_v = v0 - decay * (distance_ft / 2000.0)
    if avg_v <= 0:
        avg_v = 30.0
    return distance_ft / avg_v * 1000.0


def load_data():
    parquet = TMP / "nyra_2014_starters.parquet"
    csv = TMP / "nyra_2014_starters.csv"
    if parquet.exists():
        return pd.read_parquet(parquet)
    return pd.read_csv(csv)


def split_train_holdout(df, holdout_frac=0.20, seed=42):
    """Race-level holdout — same seed/fraction as v0 POC for direct comparison."""
    rng = np.random.default_rng(seed)
    races = df["race_id"].unique()
    rng.shuffle(races)
    n_holdout = int(len(races) * holdout_frac)
    holdout_races = set(races[:n_holdout])
    df = df.copy()
    df["holdout"] = df["race_id"].isin(holdout_races)
    return df


def main():
    df = load_data()
    print(f"Loaded {len(df):,} starter-observations")

    # Each starter has the actual finish time captured indirectly via
    # their fractional points. We need the actual finish time at the
    # actual race distance — pull from the underlying data.
    # The 01_extract_data step captured one row per (starter_id, feet)
    # already aggregated; what we have here is one row per starter
    # with v0/decay fitted. We need actual_finish_millis instead.
    # Simplest: rerun the data extraction joining indiv_fractionals at
    # the FINAL distance for each starter. But we don't need full re-run —
    # the v0/decay we have IS the curve fit; we can compute actual
    # finish time as distance / actual_avg_v. But "actual avg v" isn't
    # in the per-starter table.
    #
    # Better: pull actual finish time directly from the DB and merge.

    import psycopg2
    conn = psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )
    print("Loading actual finish times from DB...")
    starter_ids = tuple(int(x) for x in df["starter_id"].unique())
    finish_q = """
        SELECT starter_id, max(feet) AS max_feet,
               (array_agg(millis ORDER BY feet DESC))[1] AS finish_millis
        FROM handycapper.indiv_fractionals
        WHERE starter_id = ANY(%s) AND feet > 0 AND millis > 0
        GROUP BY starter_id
    """
    with conn:
        finish_df = pd.read_sql(finish_q, conn, params=(list(starter_ids),))
    conn.close()
    df = df.merge(finish_df, on="starter_id", how="left")
    print(f"  {df['finish_millis'].notna().sum():,} starters with finish times")

    # Sanity: max_feet should equal furlongs * 660 (with rounding tolerance)
    df["distance_ft"] = df["furlongs"].astype(float) * 660.0
    df["dist_match"] = (df["max_feet"] - df["distance_ft"]).abs() < 50.0
    print(f"  {df['dist_match'].sum():,} have max_feet ≈ distance_ft "
          f"(rest may have partial fractional data)")
    df = df[df["dist_match"]].copy()

    # Compute canonical time and residual.
    df["canonical_ms"] = df.apply(
        lambda r: canonical_time_ms(r["surface"], float(r["furlongs"])), axis=1)
    # Higher = faster: canonical - actual (positive when actual is faster)
    df["time_residual"] = df["canonical_ms"] - df["finish_millis"]

    print(f"\ntime_residual distribution (canonical_ms - actual_finish_ms):")
    print(f"  mean={df['time_residual'].mean():.0f}ms  std={df['time_residual'].std():.0f}ms")
    print(f"  P05 ={df['time_residual'].quantile(0.05):.0f}  "
          f"P50 ={df['time_residual'].quantile(0.50):.0f}  "
          f"P95 ={df['time_residual'].quantile(0.95):.0f}")
    print(f"  Negative = slower than canonical winner; positive = faster")
    print("\nResidual mean by surface (anchor calibration check):")
    print(df.groupby("surface")["time_residual"].agg(["mean", "std", "count"]).to_string())
    print("\nResidual mean by class (expect: stakes positive, maiden_clm negative):")
    print(df.groupby("race_class")["time_residual"].agg(["mean", "std", "count"]).to_string())

    df = split_train_holdout(df)
    train = df[~df["holdout"]].copy()
    holdout = df[df["holdout"]].copy()
    print(f"\nTrain: {len(train):,}    Holdout: {len(holdout):,}")

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        train[col] = train[col].astype(str)
        holdout[col] = holdout[col].astype(str)
    train["state_bred_flag"] = train["state_bred_flag"].astype(int)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)
    train = train.dropna(subset=["time_residual"]).copy()

    print("\nFitting hierarchical time-residual model...")
    print("Formula: time_residual ~ going + age_group + sex_group + state_bred_flag")
    print("                         + race_class")
    print("Random:  (1 | horse), variance components: track, race_id")

    md = smf.mixedlm(
        "time_residual ~ going + age_group + sex_group + state_bred_flag + race_class",
        data=train,
        groups=train["horse"],
        re_formula="~1",
        vc_formula={
            "track": "0 + C(track)",
            "race_id": "0 + C(race_id)",
        },
    )
    result = md.fit(method="lbfgs", maxiter=200)
    print("\n" + "="*70)
    print("MODEL FIT (canonical-time residual)")
    print("="*70)
    print(result.summary())
    print(f"\nConverged: {result.converged}")

    with open(TMP / "model_fit_time.pkl", "wb") as f:
        pickle.dump({"result": result,
                     "train_horse_keys": list(train["horse"].unique())}, f)
    print(f"\nSaved to {TMP / 'model_fit_time.pkl'}")

    # Save the time-anchored split for the predict step
    if (TMP / "nyra_2014_starters.parquet").exists():
        df.to_parquet(TMP / "split_time.parquet")
    else:
        df.to_csv(TMP / "split_time.csv", index=False)


if __name__ == "__main__":
    main()
