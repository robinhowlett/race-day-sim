"""POC step 1b: extract NYRA 2010-2017 tagged-observation table.

Same shape as 01_extract_data.py but on an 8-year window. Mean starts
per horse rises from ~2.8 (2014 only) to ~5.8 — the multi-year per-horse
density that the (II-extended) test was set up to evaluate.

Vocabulary (new):
  peak_speed_observed = polyfit intercept on velocity-vs-distance
  fade_rate_observed  = -slope * 1000, "ft/s lost per 1000 ft"

These are what production calls v0 / decay_rate respectively. Using the
new names in new code; the schema columns remain v0/adj_v0/decay_rate
until a separate migration commit.
"""

import os
import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

warnings.filterwarnings("ignore")
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


# Same schema as the 2014 extraction, just a wider date range.
QUERY = """
SELECT
    s.id              AS starter_id,
    s.horse,
    r.id              AS race_id,
    r.date            AS race_date,
    r.track,
    r.surface,
    r.track_condition AS track_condition_raw,
    r.type            AS race_type,
    r.grade,
    r.purse,
    r.min_age, r.max_age,
    r.sexes_code,
    r.state_bred,
    r.number_of_runners,
    r.furlongs,
    s.official_position,
    s.finish_position,
    s.odds,
    inf.feet,
    inf.millis,
    (inf.feet::numeric / NULLIF(inf.millis, 0)) * 1000.0 AS velocity_ft_per_sec
FROM handycapper.indiv_fractionals inf
JOIN handycapper.starters s ON s.id = inf.starter_id
JOIN handycapper.races r ON r.id = s.race_id
WHERE r.breed = 'TB'
  AND r.date BETWEEN '2010-01-01' AND '2017-12-31'
  AND r.track IN ('AQU','BEL','SAR')
  AND r.surface IN ('Dirt','Turf')
  AND r.number_of_runners >= 5
  AND inf.feet > 0
  AND inf.millis > 0
ORDER BY s.id, inf.feet
"""


def classify_going(track_condition):
    if track_condition is None:
        return "fast"
    off_set = {"Muddy", "Sloppy", "Heavy", "Wet Fast", "Slow", "Yielding", "Soft", "Good"}
    return "off" if track_condition in off_set else "fast"


def classify_class(race_type, grade):
    if race_type is None:
        return "CLAIMING"
    rt = race_type.upper()
    if grade is not None and grade in (1, 2, 3):
        return "STAKES_GRADED"
    if "STAKES" in rt or "HANDICAP" in rt:
        return "STAKES_UNGRADED"
    if "MAIDEN SPECIAL" in rt:
        return "MAIDEN_SW"
    if "MAIDEN" in rt:
        return "MAIDEN_CLM"
    if "ALLOWANCE" in rt or "STARTER" in rt or "OPTIONAL" in rt:
        return "ALLOWANCE"
    return "CLAIMING"


def classify_age(min_age, max_age):
    if min_age == 2 and max_age == 2:
        return "2yo"
    if min_age == 3 and max_age == 3:
        return "3yo"
    return "older"


def classify_sex(sexes_code):
    if sexes_code in ("F", "F&M"):
        return "F_M"
    return "open"


def fit_peak_and_fade(group):
    """Return (peak_speed_observed, fade_rate_observed, n_points).

    Same algorithm as production rkm.curves: linear polyfit of velocity
    on cumulative-distance, with sanity gates on intercept (40-85 ft/s)
    and a positive-slope clamp at 0.001.
    """
    feet = group["feet"].astype(float).values
    velocities = group["velocity_ft_per_sec"].astype(float).values
    mask = (velocities >= 30.0) & (velocities <= 70.0)
    feet, velocities = feet[mask], velocities[mask]
    if len(feet) < 4:
        return pd.Series({"peak_speed_observed": np.nan,
                          "fade_rate_observed": np.nan, "n_points": len(feet)})
    try:
        coeffs = np.polyfit(feet, velocities, 1)
    except (np.linalg.LinAlgError, ValueError):
        return pd.Series({"peak_speed_observed": np.nan,
                          "fade_rate_observed": np.nan, "n_points": len(feet)})
    slope, intercept = coeffs[0], coeffs[1]
    if intercept < 40 or intercept > 85:
        return pd.Series({"peak_speed_observed": np.nan,
                          "fade_rate_observed": np.nan, "n_points": len(feet)})
    if slope > 0.001:
        slope = 0.0
    return pd.Series({
        "peak_speed_observed": round(float(intercept), 2),
        "fade_rate_observed":  round(float(-slope * 1000), 4),
        "n_points": len(feet),
    })


def main():
    print("Loading 2010-2017 NYRA observations...")
    with get_conn() as conn:
        df_raw = pd.read_sql(QUERY, conn)
    print(f"  {len(df_raw):,} rows ({df_raw['starter_id'].nunique():,} starters, "
          f"{df_raw['race_id'].nunique():,} races, {df_raw['horse'].nunique():,} horses)")

    print("Fitting per-starter (peak_speed, fade_rate) curves...")
    curves = df_raw.groupby("starter_id").apply(
        fit_peak_and_fade, include_groups=False).reset_index()

    context = df_raw.groupby("starter_id").agg(
        horse=("horse", "first"),
        race_id=("race_id", "first"),
        race_date=("race_date", "first"),
        track=("track", "first"),
        surface=("surface", "first"),
        track_condition_raw=("track_condition_raw", "first"),
        race_type=("race_type", "first"),
        grade=("grade", "first"),
        purse=("purse", "first"),
        min_age=("min_age", "first"),
        max_age=("max_age", "first"),
        sexes_code=("sexes_code", "first"),
        state_bred=("state_bred", "first"),
        number_of_runners=("number_of_runners", "first"),
        furlongs=("furlongs", "first"),
        official_position=("official_position", "first"),
        finish_position=("finish_position", "first"),
        odds=("odds", "first"),
    ).reset_index()
    df = context.merge(curves, on="starter_id")

    df["going"] = df["track_condition_raw"].apply(classify_going)
    df["race_class"] = df.apply(
        lambda r: classify_class(r["race_type"], r.get("grade")), axis=1)
    df["age_group"] = df.apply(
        lambda r: classify_age(r["min_age"], r["max_age"]), axis=1)
    df["sex_group"] = df["sexes_code"].apply(classify_sex)
    df["state_bred_flag"] = df["state_bred"].fillna(False).astype(bool)
    df["zone"] = (df["furlongs"].astype(float) > 6.5).map(
        {True: "route", False: "sprint"})
    df["year"] = pd.to_datetime(df["race_date"]).dt.year

    before = len(df)
    df = df.dropna(subset=["peak_speed_observed", "fade_rate_observed"]).copy()
    print(f"  {before - len(df):,} starters dropped (insufficient/invalid points)")
    print(f"  {len(df):,} starter-observations remain")

    # Diagnostics
    print(f"\nYear distribution:")
    print(df.groupby("year").size().to_dict())
    horse_starts = df.groupby("horse").size()
    print(f"\nMean starts per horse (in slice): {horse_starts.mean():.2f}")
    print(f"Horses with >=3 starts: {(horse_starts >= 3).sum():,}")
    print(f"Horses with >=5 starts: {(horse_starts >= 5).sum():,}")
    print(f"Horses with >=10 starts: {(horse_starts >= 10).sum():,}")

    out_path = TMP / "nyra_2010_2017_starters.parquet"
    try:
        df.to_parquet(out_path)
        print(f"\nWrote {out_path} ({len(df):,} rows)")
    except (ImportError, ModuleNotFoundError):
        out_path = TMP / "nyra_2010_2017_starters.csv"
        df.to_csv(out_path, index=False)
        print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
