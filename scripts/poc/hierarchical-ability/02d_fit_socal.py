"""POC step 2d: hierarchical canonical-time-residual model on SoCal 2010-2017.

Same model spec as 02c (NYRA extended), different circuit. Trained on
2010-2016, holdout = 2017.

Note: SoCal has higher chart-data drop rate (~34% of starters lack
sufficient fractional points to fit a curve, vs. NYRA's ~13%). Mean
starts per horse in the slice drops from NYRA's 5.4 to SoCal's 4.6.
This is a real data-quality difference inherited from chart-parser;
both POC and live see the same data so the comparison stays fair.
"""

import os
import pickle
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2
import statsmodels.formula.api as smf

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"
warnings.filterwarnings("ignore")

SIM_SRC = POC_DIR.parents[2] / "src"
sys.path.insert(0, str(SIM_SRC))
from sim.ratings import _CANONICAL_PARAMS  # noqa: E402

CIRCUIT_NAME = "socal"


def canonical_time_ms(surface: str, furlongs: float) -> float:
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


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def main():
    parquet = TMP / f"{CIRCUIT_NAME}_2010_2017_starters.parquet"
    csv = TMP / f"{CIRCUIT_NAME}_2010_2017_starters.csv"
    df = pd.read_parquet(parquet) if parquet.exists() else pd.read_csv(csv)
    df["race_date"] = pd.to_datetime(df["race_date"])
    df["year"] = df["race_date"].dt.year
    print(f"Loaded {len(df):,} starter-observations from {CIRCUIT_NAME.upper()}")

    print("Loading actual finish times...")
    starter_ids = [int(x) for x in df["starter_id"].unique()]
    finish_parts = []
    chunk = 50000
    with get_conn() as conn:
        for i in range(0, len(starter_ids), chunk):
            ids = starter_ids[i:i + chunk]
            q = """
              SELECT starter_id, max(feet) AS max_feet,
                     (array_agg(millis ORDER BY feet DESC))[1] AS finish_millis
              FROM handycapper.indiv_fractionals
              WHERE starter_id = ANY(%s) AND feet > 0 AND millis > 0
              GROUP BY starter_id
            """
            finish_parts.append(pd.read_sql(q, conn, params=(ids,)))
    finish_df = pd.concat(finish_parts, ignore_index=True)
    df = df.merge(finish_df, on="starter_id", how="left")
    df["distance_ft"] = df["furlongs"].astype(float) * 660.0
    df["dist_match"] = (df["max_feet"] - df["distance_ft"]).abs() < 50.0
    df = df[df["dist_match"]].copy()
    print(f"  {len(df):,} after distance-match filter")

    df["canonical_ms"] = df.apply(
        lambda r: canonical_time_ms(r["surface"], float(r["furlongs"])), axis=1)
    df["time_residual"] = df["canonical_ms"] - df["finish_millis"]

    train = df[df["year"] <= 2016].copy()
    holdout = df[df["year"] == 2017].copy()
    print(f"\nTrain (2010-2016): {len(train):,}    Holdout (2017): {len(holdout):,}")
    print(f"Train horses: {train['horse'].nunique():,}")
    print(f"Holdout horses: {holdout['horse'].nunique():,}")
    print(f"Holdout horses also in train: "
          f"{holdout[holdout['horse'].isin(train['horse'])]['horse'].nunique():,}")

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        train[col] = train[col].astype(str)
        holdout[col] = holdout[col].astype(str)
    train["state_bred_flag"] = train["state_bred_flag"].astype(int)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)
    train = train.dropna(subset=["time_residual"]).copy()

    print("\nFitting hierarchical time-residual model on 2010-2016 SoCal...")
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
    import time
    t0 = time.time()
    result = md.fit(method="lbfgs", maxiter=200)
    print(f"\nFit time: {time.time() - t0:.1f}s   Converged: {result.converged}")

    print("\n" + "="*70)
    print(f"MODEL FIT (canonical-time, {CIRCUIT_NAME} 2010-2016, year-out)")
    print("="*70)
    print(result.summary())

    with open(TMP / f"model_fit_{CIRCUIT_NAME}.pkl", "wb") as f:
        pickle.dump({"result": result,
                     "train_horse_keys": list(train["horse"].unique())}, f)

    if parquet.exists():
        df.to_parquet(TMP / f"split_{CIRCUIT_NAME}.parquet")
    else:
        df.to_csv(TMP / f"split_{CIRCUIT_NAME}.csv", index=False)


if __name__ == "__main__":
    main()
