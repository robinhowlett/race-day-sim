"""POC step 1: data inventory + bet-type viability for exotic-overlay POC.

Confirms what historical exotic data we have on the simulator's
playable universe (sim_candidates) before designing methodology.

Per bet type, reports:
  - Number of historical exotic results (rows in exotics with payoff > 0)
  - Number of distinct races with that bet type's result
  - Median/p25/p75 pool size, payoff, takeout
  - Carryover-day count (for horizontals; carryover is +EV per audit memory)
  - Pool-type breakdown (STANDARD vs JACKPOT vs CONSOLATION; we
    primarily want STANDARD; JACKPOT is the -EV-by-default product;
    CONSOLATION is partial-credit payouts)

Goal: tell us which bet types have enough data to study at all,
identify any data-quality red flags, and surface population sizes
for power calculations.

Output: tmp/exotic_inventory.csv + console summary.
"""

import os
import sys
import time
from pathlib import Path

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


# Bet types in scope. WIN/PLACE/SHOW are not in `exotics` (they live in
# `wps`). HEAD_TO_HEAD, FUTURE, ODD_OR_EVEN are off-scope (specialty pools).
IN_SCOPE = [
    "EXACTA", "QUINELLA", "TRIFECTA", "SUPERFECTA",
    "DAILY_DOUBLE", "PICK_3", "PICK_4", "PICK_5", "PICK_6",
]


SQL = """
WITH sim_universe_races AS (
    -- The simulator's playable universe: sim_candidates is keyed by
    -- (track, date), so expand to race_ids on those days.
    SELECT r.id AS race_id, r.date, r.track
    FROM handycapper.races r
    JOIN handycapper.sim_candidates sc
      ON sc.track = r.track AND sc.date = r.date
)
SELECT
    e.bet_type,
    e.pool_type,
    COUNT(*) AS n_results,
    COUNT(DISTINCT e.race_id) AS n_distinct_races,
    -- Pool stats (per-result, since multiple winners share)
    PERCENTILE_CONT(0.25) WITHIN GROUP (ORDER BY e.pool) AS pool_p25,
    PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY e.pool) AS pool_p50,
    PERCENTILE_CONT(0.75) WITHIN GROUP (ORDER BY e.pool) AS pool_p75,
    -- Per-$1 payoff stats
    PERCENTILE_CONT(0.25) WITHIN GROUP (
        ORDER BY (e.payoff / NULLIF(e.unit, 0))) AS pay_per_1_p25,
    PERCENTILE_CONT(0.50) WITHIN GROUP (
        ORDER BY (e.payoff / NULLIF(e.unit, 0))) AS pay_per_1_p50,
    PERCENTILE_CONT(0.75) WITHIN GROUP (
        ORDER BY (e.payoff / NULLIF(e.unit, 0))) AS pay_per_1_p75,
    -- Carryover presence (horizontals): days where carryover existed
    SUM(CASE WHEN e.carryover IS NOT NULL AND e.carryover > 0
             THEN 1 ELSE 0 END) AS n_with_carryover,
    -- Year coverage
    MIN(EXTRACT(YEAR FROM r.date))::int AS year_min,
    MAX(EXTRACT(YEAR FROM r.date))::int AS year_max
FROM handycapper.exotics e
JOIN sim_universe_races r ON r.race_id = e.race_id
WHERE e.payoff > 0 AND e.unit > 0
  AND e.bet_type = ANY(%(types)s)
GROUP BY e.bet_type, e.pool_type
ORDER BY e.bet_type, e.pool_type
"""


def main():
    print("Loading inventory...")
    t0 = time.time()
    with get_conn() as conn:
        df = pd.read_sql(SQL, conn, params={"types": IN_SCOPE})
    print(f"  {len(df)} rows in {time.time()-t0:.1f}s")

    df["pool_p50"] = df["pool_p50"].astype(float).round(0).astype("Int64")
    df["pool_p25"] = df["pool_p25"].astype(float).round(0).astype("Int64")
    df["pool_p75"] = df["pool_p75"].astype(float).round(0).astype("Int64")
    df["pay_per_1_p50"] = df["pay_per_1_p50"].astype(float).round(2)
    df["pay_per_1_p25"] = df["pay_per_1_p25"].astype(float).round(2)
    df["pay_per_1_p75"] = df["pay_per_1_p75"].astype(float).round(2)

    print("\n=== sim_candidates universe — exotic data inventory ===")
    print(f"\n{'bet_type':<14}{'pool_type':<14}{'n_results':>11}{'n_races':>10}"
          f"{'pool p25/50/75':>22}{'pay/1 p25/50/75':>26}"
          f"{'carry':>7}{'years':>11}")
    print("-" * 115)
    for _, r in df.iterrows():
        pool_s = f"{r['pool_p25']}/{r['pool_p50']}/{r['pool_p75']}"
        pay_s = f"{r['pay_per_1_p25']}/{r['pay_per_1_p50']}/{r['pay_per_1_p75']}"
        years_s = f"{r['year_min']}-{r['year_max']}"
        print(f"{r['bet_type']:<14}{r['pool_type']:<14}"
              f"{r['n_results']:>11,}{r['n_distinct_races']:>10,}"
              f"{pool_s:>22}{pay_s:>26}"
              f"{r['n_with_carryover']:>7,}{years_s:>11}")

    out = TMP / "exotic_inventory.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {out}")

    # Quick viability summary — bet types with enough STANDARD-pool
    # results to support a 3-way OOS split (need >=10K per year for
    # any per-tier analysis to have power).
    print("\n=== Viability per bet type (STANDARD pool only) ===")
    std = df[df["pool_type"] == "STANDARD"]
    for _, r in std.iterrows():
        years = r["year_max"] - r["year_min"] + 1
        per_year = r["n_results"] / years
        bins = "abundant" if per_year > 30000 else "ample" if per_year > 10000 else "thin" if per_year > 2000 else "marginal"
        print(f"  {r['bet_type']:<14} {r['n_results']:>9,} results / {years} years "
              f"= ~{int(per_year):,}/yr  ({bins})")


if __name__ == "__main__":
    main()
