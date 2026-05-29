"""POC step 2 (revised): canonical-anchored hierarchical model.

The first version fit absolute v0 with a free intercept — model-internal
reference cell, not anchored to anything externally meaningful. Every
context effect is expressed relative to that internal cell, which makes
horse_ability values uninterpretable in the rating system's frame.

This version fits the RESIDUAL against the canonical anchor table from
ratings._CANONICAL_PARAMS — the (v0, decay) values that produce rating
100 for a CLM $5K-$10K winner on fast dirt at each distance:

    canonical_v0[surface, furlongs] = the v0 a canonical winner runs at

    residual = actual_v0 - canonical_v0[surface, furlongs]

    residual ~ horse_ability + track_effect + condition_effect + class_effect
             + age_effect + sex_effect + state_bred_effect + zone_effect
             + (1 | horse) + variance components on (track, race_id)

Why this is better:
  - Effects have an interpretable zero-point. "Class-stakes effect" =
    "stakes adds X ft/s above the canonical winner."
  - Cross-distance anchoring is automatic. The canonical anchor table
    encodes the right v0 shape per distance; the model learns deviations.
  - horse_ability is "ability vs canonical winner" in absolute ft/s —
    the cross-context comparable signal we actually want.

Surface is folded INTO the canonical lookup (Dirt vs Turf vs Synthetic
each have their own anchor curve), so we don't include surface as a
fixed effect — the anchor already accounts for it. Same for distance
(zone is implicit in the per-furlong anchor).
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

# Make ratings._get_anchor importable from race-day-sim/src
SIM_SRC = POC_DIR.parents[2] / "src"
sys.path.insert(0, str(SIM_SRC))
from sim.ratings import _get_anchor, _CANONICAL_PARAMS  # noqa: E402


def canonical_v0(surface: str, furlongs: float) -> float:
    """Return the canonical-anchor v0 for this (surface, furlongs).

    _CANONICAL_PARAMS gives (v0, decay, anchor_rating). For our purposes
    we want the v0 directly — the speed at the start. Interpolate by
    asking _get_anchor for the time and back-solving — but that's
    convoluted. Easier: replicate the interpolation here on v0 only.
    """
    key = (surface, furlongs)
    if key in _CANONICAL_PARAMS:
        return _CANONICAL_PARAMS[key][0]

    # Interpolate within surface
    sk = sorted([(f, v0) for (s, f), (v0, _, _) in _CANONICAL_PARAMS.items() if s == surface])
    if not sk:
        sk = sorted([(f, v0) for (s, f), (v0, _, _) in _CANONICAL_PARAMS.items() if s == "Dirt"])
    if furlongs <= sk[0][0]:
        return sk[0][1]
    if furlongs >= sk[-1][0]:
        return sk[-1][1]
    for i in range(len(sk) - 1):
        f_lo, v_lo = sk[i]
        f_hi, v_hi = sk[i + 1]
        if f_lo <= furlongs <= f_hi:
            t = (furlongs - f_lo) / (f_hi - f_lo)
            return v_lo + t * (v_hi - v_lo)
    return sk[-1][1]


def load_data():
    parquet = TMP / "nyra_2014_starters.parquet"
    csv = TMP / "nyra_2014_starters.csv"
    if parquet.exists():
        return pd.read_parquet(parquet)
    return pd.read_csv(csv)


def split_train_holdout(df, holdout_frac=0.20, seed=42):
    """Race-level holdout — 20% of races held out entirely."""
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

    # Compute canonical-anchored residual
    df["canonical_v0"] = df.apply(
        lambda r: canonical_v0(r["surface"], float(r["furlongs"])), axis=1
    )
    df["v0_residual"] = df["v0"] - df["canonical_v0"]
    print(f"\nResidual distribution (v0 - canonical_v0):")
    print(f"  mean={df['v0_residual'].mean():.3f}  std={df['v0_residual'].std():.3f}")
    print(f"  P05 ={df['v0_residual'].quantile(0.05):.3f}  "
          f"P50 ={df['v0_residual'].quantile(0.50):.3f}  "
          f"P95 ={df['v0_residual'].quantile(0.95):.3f}")
    print(f"  min ={df['v0_residual'].min():.3f}  max={df['v0_residual'].max():.3f}")

    # Sanity check: residual mean should be close to 0 if the canonical
    # anchor is calibrated reasonably for this slice. Big drift would
    # indicate the anchor is off for NYRA 2014 specifically.
    print("\nResidual mean by surface (should both be near 0 if anchor is right):")
    print(df.groupby("surface")["v0_residual"].agg(["mean", "std", "count"]).to_string())
    print("\nResidual mean by class (expect: stakes positive, maiden_clm negative):")
    print(df.groupby("race_class")["v0_residual"].agg(["mean", "std", "count"]).to_string())

    df = split_train_holdout(df)
    train = df[~df["holdout"]].copy()
    holdout = df[df["holdout"]].copy()
    print(f"\nTrain: {len(train):,}    Holdout: {len(holdout):,}")

    for col in ["going", "age_group", "sex_group", "race_class", "track"]:
        train[col] = train[col].astype(str)
        holdout[col] = holdout[col].astype(str)
    train["state_bred_flag"] = train["state_bred_flag"].astype(int)
    holdout["state_bred_flag"] = holdout["state_bred_flag"].astype(int)
    train = train.dropna(subset=["v0_residual"]).copy()

    print("\nFitting canonical-anchored hierarchical model on training set...")
    print("Formula: v0_residual ~ going + age_group + sex_group + state_bred_flag")
    print("                       + race_class")
    print("Random:  (1 | horse), variance components: track, race_id")
    print("(surface and distance/zone folded into canonical anchor — not")
    print("modeled as separate fixed effects)")

    md = smf.mixedlm(
        "v0_residual ~ going + age_group + sex_group + state_bred_flag + race_class",
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
    print("MODEL FIT (canonical-anchored)")
    print("="*70)
    print(result.summary())
    print(f"\nConverged: {result.converged}")

    # Save
    with open(TMP / "model_fit.pkl", "wb") as f:
        pickle.dump({
            "result": result,
            "train_horse_keys": list(train["horse"].unique()),
        }, f)
    print(f"\nSaved fitted model to {TMP / 'model_fit.pkl'}")

    if (TMP / "nyra_2014_starters.parquet").exists():
        df.to_parquet(TMP / "split.parquet")
    else:
        df.to_csv(TMP / "split.csv", index=False)


if __name__ == "__main__":
    main()
