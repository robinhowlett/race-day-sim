"""POC step 2: bet-everything baseline ROI per bet type.

For each bet type, compute ROI of buying $1 on every possible
combination on every race in the sim_candidates universe. This
establishes the takeout-eaten floor: any combo-selection rule
must beat this to be useful.

Math:
  - For each race with a published exotic result and pool data:
      n_combos = (vertical: P(field_size, n_pos))
                 (horizontal: prod(field_size_i for i in 1..n_legs))
      total_invested = n_combos × $1
      payout         = published per-$1 payoff (chart paid this much
                       per $1 stake on the winning combo)
      net            = payout − total_invested
  - Aggregate ROI = sum(net) / sum(total_invested)

The chart's payoff is for ONE winning combo (the actual finish order
or leg-winners). When you bet every combination, you collect that
single payout. So per-race net = payout − n_combos.

Output: tmp/bet_everything_baseline.csv

Skipped pools (per step-1 scoping):
  - CONSOLATION (partial-credit payouts, separate game)
  - JACKPOT (audited as -EV-by-default; not the question)
  - HI_5, FUTURE, HEAD_TO_HEAD, ODD_OR_EVEN
"""

import math
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


# Vertical exotics: n_combos = field × (field-1) × ... × (field - n_pos + 1)
# Horizontal exotics: need leg field sizes for each consecutive race.
VERTICAL = {
    "EXACTA":     2,
    "QUINELLA":   2,  # order-independent: combos = C(field, 2)
    "TRIFECTA":   3,
    "SUPERFECTA": 4,
}
HORIZONTAL_LEGS = {
    "DAILY_DOUBLE": 2,
    "PICK_3":       3,
    "PICK_4":       4,
    "PICK_5":       5,
    "PICK_6":       6,
}


def vertical_combos(field: int, n_pos: int, ordered: bool = True) -> int:
    if field < n_pos:
        return 0
    if ordered:
        n = 1
        for i in range(n_pos):
            n *= field - i
        return n
    return math.comb(field, n_pos)


def main():
    print("Loading vertical exotic results...")
    t0 = time.time()
    with get_conn() as conn:
        # Verticals: one row per (race, bet_type) with the per-$1 payoff
        # and the field size from races.number_of_runners.
        vert = pd.read_sql("""
            SELECT e.race_id, e.bet_type,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   r.number_of_runners::int AS field_size,
                   EXTRACT(YEAR FROM r.date)::int AS year
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = ANY(%(types)s)
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND r.number_of_runners IS NOT NULL
              AND r.number_of_runners >= 2
        """, conn, params={"types": list(VERTICAL.keys())})
    print(f"  vertical: {len(vert):,} rows in {time.time()-t0:.1f}s")

    print("Loading horizontal exotic results + leg field sizes...")
    t0 = time.time()
    with get_conn() as conn:
        # Horizontals: each result links to N legs via exotic_race_legs.
        # Get the field size of each leg's race so we can compute combo
        # cardinality.
        horiz = pd.read_sql("""
            WITH leg_meta AS (
                SELECT erl.exotic_id, erl.leg_number,
                       lr.number_of_runners AS leg_field_size
                FROM handycapper.exotic_race_legs erl
                JOIN handycapper.races lr ON lr.id = erl.race_id
            ),
            -- Aggregate legs per exotic into a sorted array of field sizes
            legs_agg AS (
                SELECT exotic_id,
                       array_agg(leg_field_size ORDER BY leg_number) AS leg_fields,
                       COUNT(*) AS n_legs
                FROM leg_meta
                GROUP BY exotic_id
            )
            SELECT e.id AS exotic_id,
                   e.race_id,
                   e.bet_type,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   la.leg_fields,
                   la.n_legs,
                   EXTRACT(YEAR FROM r.date)::int AS year,
                   COALESCE(e.carryover, 0)::float AS carryover
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            JOIN legs_agg la ON la.exotic_id = e.id
            WHERE e.bet_type = ANY(%(types)s)
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
        """, conn, params={"types": list(HORIZONTAL_LEGS.keys())})
    print(f"  horizontal: {len(horiz):,} rows in {time.time()-t0:.1f}s")

    rows = []

    # --- Vertical ---
    for bt, n_pos in VERTICAL.items():
        sub = vert[vert["bet_type"] == bt].copy()
        if sub.empty:
            continue
        ordered = bt != "QUINELLA"
        sub["n_combos"] = sub["field_size"].apply(
            lambda f: vertical_combos(int(f), n_pos, ordered=ordered)
        )
        sub = sub[sub["n_combos"] > 0]
        sub["invested"] = sub["n_combos"].astype(float)
        sub["net"] = sub["pay_per_1"] - sub["invested"]
        invested = sub["invested"].sum()
        net = sub["net"].sum()
        roi = net / invested if invested > 0 else None
        # SE estimate: per-race net has high variance; use std of per-race
        # ROI weighted by invested as an approximation.
        if invested > 0:
            per_race_roi = sub["net"] / sub["invested"]
            # Weight by sqrt(invested) for ~variance-stabilizing weighted SE
            se = float(per_race_roi.std(ddof=1) / (len(sub) ** 0.5))
        else:
            se = None
        rows.append({
            "category":     "vertical",
            "bet_type":     bt,
            "n_races":      len(sub),
            "median_field": float(sub["field_size"].median()),
            "median_combos": float(sub["n_combos"].median()),
            "total_invested": invested,
            "total_payout":   sub["pay_per_1"].sum(),
            "roi_pct":        100 * roi if roi is not None else None,
            "se_pct":         100 * se if se is not None else None,
            "ci_lo_pct":      100 * (roi - 1.96 * se) if roi is not None and se is not None else None,
            "ci_hi_pct":      100 * (roi + 1.96 * se) if roi is not None and se is not None else None,
        })

    # --- Horizontal ---
    for bt, n_legs in HORIZONTAL_LEGS.items():
        sub = horiz[horiz["bet_type"] == bt].copy()
        if sub.empty:
            continue
        # n_combos = product of leg field sizes (with all-leg arrays present).
        # Drop rows with missing/short leg arrays.
        sub = sub[sub["n_legs"] == n_legs]
        sub = sub.dropna(subset=["leg_fields"])
        # leg_fields is a Python list (psycopg2 returns array as list)
        sub = sub[sub["leg_fields"].apply(lambda lf: lf is not None and all(x is not None and x >= 2 for x in lf))]
        sub["n_combos"] = sub["leg_fields"].apply(
            lambda lf: int(math.prod(int(x) for x in lf))
        )
        if sub.empty:
            continue
        sub["invested"] = sub["n_combos"].astype(float)
        sub["net"] = sub["pay_per_1"] - sub["invested"]
        invested = sub["invested"].sum()
        net = sub["net"].sum()
        roi = net / invested if invested > 0 else None
        per_race_roi = sub["net"] / sub["invested"]
        se = float(per_race_roi.std(ddof=1) / (len(sub) ** 0.5))
        rows.append({
            "category":     "horizontal",
            "bet_type":     bt,
            "n_races":      len(sub),
            "median_field": float(sub["leg_fields"].apply(
                lambda lf: sum(int(x) for x in lf) / len(lf)).median()),
            "median_combos": float(sub["n_combos"].median()),
            "total_invested": invested,
            "total_payout":   sub["pay_per_1"].sum(),
            "roi_pct":        100 * roi if roi is not None else None,
            "se_pct":         100 * se if se is not None else None,
            "ci_lo_pct":      100 * (roi - 1.96 * se) if roi is not None and se is not None else None,
            "ci_hi_pct":      100 * (roi + 1.96 * se) if roi is not None and se is not None else None,
        })

        # Carryover split for P5/P6 (~10K horizontal races have carryover>0)
        if bt in ("PICK_5", "PICK_6"):
            for label, mask in [
                ("with_carryover",    sub["carryover"] > 0),
                ("without_carryover", sub["carryover"] <= 0),
            ]:
                ssub = sub[mask]
                if ssub.empty:
                    continue
                inv = ssub["invested"].sum()
                ne = ssub["net"].sum()
                rr = ne / inv if inv > 0 else None
                pse = float((ssub["net"] / ssub["invested"]).std(ddof=1) / (len(ssub) ** 0.5))
                rows.append({
                    "category":      "horizontal",
                    "bet_type":      f"{bt} ({label})",
                    "n_races":       len(ssub),
                    "median_field":  float(ssub["leg_fields"].apply(
                        lambda lf: sum(int(x) for x in lf) / len(lf)).median()),
                    "median_combos": float(ssub["n_combos"].median()),
                    "total_invested": inv,
                    "total_payout":   ssub["pay_per_1"].sum(),
                    "roi_pct":        100 * rr,
                    "se_pct":         100 * pse,
                    "ci_lo_pct":      100 * (rr - 1.96 * pse),
                    "ci_hi_pct":      100 * (rr + 1.96 * pse),
                })

    out_df = pd.DataFrame(rows)
    out = TMP / "bet_everything_baseline.csv"
    out_df.to_csv(out, index=False)

    print("\n=== Bet-everything baseline ROI by bet type ===")
    print(f"{'bet_type':<28}{'n_races':>10}{'med field':>11}{'med combos':>13}"
          f"{'ROI':>10}{'95% CI':>22}")
    print("-" * 100)
    for _, r in out_df.iterrows():
        ci = (f"({r['ci_lo_pct']:+5.1f}%, {r['ci_hi_pct']:+5.1f}%)"
              if r["ci_lo_pct"] is not None else "—")
        print(f"{r['bet_type']:<28}{int(r['n_races']):>10,}"
              f"{r['median_field']:>11.1f}{int(r['median_combos']):>13,}"
              f"{r['roi_pct']:>+8.1f}% {ci:>22}")

    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
