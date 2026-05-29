"""POC step 5: subgroup analysis.

Does FLB strength vary by:
  - Field size (small vs medium vs large)
  - Surface (Dirt vs Turf vs Synthetic)
  - Class tier (claiming vs allowance vs stakes)

If subgroup curves diverge meaningfully from the global curve, the
production integration should use subgroup-specific calibration. If
they're tight, one global curve suffices.

Output: tmp/subgroup_curves.csv — coarse 6-bucket FLB curve per
        subgroup so we can eyeball divergence.
        tmp/subgroup_metrics.json
"""

import json
import os
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

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


COARSE_BINS = [0, 0.02, 0.05, 0.10, 0.20, 0.40, 1.0]
COARSE_LABELS = ["<2%", "2-5%", "5-10%", "10-20%", "20-40%", "40%+"]


def main():
    print("Loading market analysis + race metadata...")
    with get_conn() as conn:
        df = pd.read_sql("""
            SELECT ma.starter_id, ma.race_id,
                   ma.odds_prob::float, ma.won::int,
                   r.surface, r.number_of_runners,
                   r.type AS race_type, r.grade
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.races r ON r.id = ma.race_id
            WHERE ma.odds_prob IS NOT NULL AND ma.odds_prob > 0
              AND r.number_of_runners IS NOT NULL
        """, conn)
    print(f"  {len(df):,} rows")

    # Field size buckets
    df["field"] = pd.cut(df["number_of_runners"],
                          bins=[0, 7, 10, 99],
                          labels=["small (5-7)", "medium (8-10)", "large (11+)"])

    # Class tier
    def classify(rt, grade):
        if rt is None or (isinstance(rt, float) and pd.isna(rt)):
            return "OTHER"
        rt = str(rt).upper()
        if grade in (1, 2, 3): return "STAKES_GRADED"
        if "STAKES" in rt or "HANDICAP" in rt: return "STAKES_UNGRADED"
        if "MAIDEN SPECIAL" in rt: return "MAIDEN_SW"
        if "MAIDEN" in rt: return "MAIDEN_CLM"
        if "ALLOWANCE" in rt or "STARTER" in rt or "OPTIONAL" in rt: return "ALLOWANCE"
        return "CLAIMING"
    df["race_class"] = df.apply(lambda r: classify(r["race_type"], r.get("grade")), axis=1)

    # Coarse implied-prob bins
    df["coarse"] = pd.cut(df["odds_prob"], bins=COARSE_BINS,
                           labels=COARSE_LABELS, include_lowest=True)

    # Build subgroup curves
    rows = []
    for group_col, group_name in [
        ("__all__", "ALL"),
        ("field", "field_size"),
        ("surface", "surface"),
        ("race_class", "race_class"),
    ]:
        if group_col == "__all__":
            sub_df = df.assign(__all__="ALL")
            group_col = "__all__"
        else:
            sub_df = df
        agg = sub_df.groupby([group_col, "coarse"], observed=True).agg(
            n=("won", "size"),
            n_wins=("won", "sum"),
            mean_implied=("odds_prob", "mean"),
        ).reset_index()
        agg["actual_rate"] = agg["n_wins"] / agg["n"]
        agg["shrinkage"] = agg["actual_rate"] / agg["mean_implied"]
        agg = agg.rename(columns={group_col: "subgroup_value"})
        agg["subgroup"] = group_name
        rows.append(agg)

    out = pd.concat(rows, ignore_index=True)
    out = out[["subgroup", "subgroup_value", "coarse", "n", "n_wins",
                "mean_implied", "actual_rate", "shrinkage"]]
    out.to_csv(TMP / "subgroup_curves.csv", index=False)
    print(f"\nWrote {TMP / 'subgroup_curves.csv'}")

    # Pretty-print divergence at each coarse bucket
    print("\n=== ALL ===")
    print(out[out["subgroup"] == "ALL"].to_string(index=False))
    for sg in ["field_size", "surface", "race_class"]:
        print(f"\n=== {sg} ===")
        print(out[out["subgroup"] == sg].to_string(index=False))

    # Compute max shrinkage spread per coarse bucket — if one subgroup
    # has shrinkage 0.7 and another has 0.5 in the same bucket, that's
    # a meaningful divergence we'd want to honor in production.
    print("\n=== Shrinkage spread per coarse bucket (min..max across subgroup values) ===")
    for sg in ["field_size", "surface", "race_class"]:
        sub = out[out["subgroup"] == sg]
        spread = sub.groupby("coarse", observed=True)["shrinkage"].agg(
            min_shr="min", max_shr="max", spread=lambda x: x.max() - x.min()
        ).reset_index()
        print(f"\n{sg}:")
        print(spread.to_string(index=False))


if __name__ == "__main__":
    main()
