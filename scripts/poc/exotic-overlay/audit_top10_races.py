"""For the 10 races flagged in audit_superfecta_2014, show full
per-starter detail so we can see what actually happened.

For each race:
  - All starters: program, horse, odds, combined_prob, finish position
  - The actual winning combo (top-4 finish)
  - The chart's superfecta payoff details
  - Stern/Harville projection for the actual winner
  - Was it a high-ER combo (top-1, top-10, etc.)?
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"

STERN_K = 0.86

# (track, race_date, race_number) for the 10 to inspect
TOP_RACES = [
    ("MTH", "2014-08-17", 9),
    ("AQU", "2014-01-23", 9),
    ("LRL", "2014-10-18", 5),
    ("LRL", "2014-09-27", 7),
    ("LRL", "2014-02-26", 7),
    ("LRL", "2014-11-19", 3),
    ("DED", "2014-01-11", 4),
    ("HAW", "2014-11-05", 7),
    ("FG",  "2014-12-18", 9),
    ("EVD", "2014-06-28", 4),
]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def stern_harville_ordered(combined: np.ndarray, idx: tuple, k: float) -> float:
    p = np.asarray(combined, dtype=float)
    p_k = p ** k
    remaining_total = p_k.sum()
    prob = 1.0
    for i in idx:
        if remaining_total <= 0:
            return 0.0
        prob *= p_k[i] / remaining_total
        remaining_total -= p_k[i]
    return prob


def main():
    conn = get_conn()

    for track, race_date, race_number in TOP_RACES:
        # Pull race + per-starter
        df = pd.read_sql("""
            SELECT s.program, s.horse, s.odds AS odds_decimal,
                   s.official_position::int AS finish_pos,
                   ma.odds_prob::float, ma.combined_prob::float,
                   r.id AS race_id, r.purse, r.surface, r.furlongs,
                   r.type AS race_type
            FROM handycapper.races r
            JOIN handycapper.starters s ON s.race_id = r.id
            LEFT JOIN handycapper.rkm_market_analysis ma ON ma.starter_id = s.id
            WHERE r.track = %(track)s AND r.date = %(date)s AND r.number = %(rn)s
            ORDER BY s.official_position NULLS LAST, s.odds NULLS LAST
        """, conn, params={"track": track, "date": race_date, "rn": race_number})

        if df.empty:
            print(f"\n!! {track} {race_date} R{race_number}: no data")
            continue

        race_id = int(df["race_id"].iloc[0])

        # Pull superfecta payoff + winning_numbers
        ex = pd.read_sql("""
            SELECT payoff, unit, (payoff/NULLIF(unit,0))::float AS pay_per_1,
                   pool, winning_numbers
            FROM handycapper.exotics
            WHERE race_id = %(rid)s AND bet_type = 'SUPERFECTA'
              AND pool_type = 'STANDARD' AND payoff > 0
        """, conn, params={"rid": race_id})

        # Race header
        print(f"\n{'='*100}")
        print(f"  {track} {race_date} R{race_number}  "
              f"({df['surface'].iloc[0]} {df['furlongs'].iloc[0]}f, "
              f"{df['race_type'].iloc[0]}, purse ${int(df['purse'].iloc[0]):,})")
        if not ex.empty:
            e = ex.iloc[0]
            print(f"  SUPERFECTA: {e['winning_numbers']}  "
                  f"pool ${int(e['pool']):,}  unit ${e['unit']}  "
                  f"payoff ${e['payoff']:,.2f}  per-$1 ${e['pay_per_1']:,.2f}")

        # Per-starter table
        print(f"\n  {'pgm':>4} {'horse':<28} {'odds':>6} {'odds_p':>7} "
              f"{'comb_p':>7} {'fin':>4} {'note':<20}")
        for _, r in df.iterrows():
            note = ""
            if pd.isna(r["combined_prob"]):
                note = "(no rkm)"
            print(f"  {str(r['program']):>4} {str(r['horse'])[:28]:<28} "
                  f"{r['odds_decimal']:>6.1f} "
                  f"{r['odds_prob'] if pd.notna(r['odds_prob']) else 0:>7.4f} "
                  f"{r['combined_prob'] if pd.notna(r['combined_prob']) else 0:>7.4f} "
                  f"{int(r['finish_pos']) if pd.notna(r['finish_pos']) else 0:>4} {note:<20}")

        # Compute Stern/Harville for the actual top-4 finish
        rkm_df = df[df["combined_prob"].notna()].reset_index(drop=True)
        if not rkm_df.empty and not ex.empty:
            combined = rkm_df["combined_prob"].values
            try:
                top4 = []
                for pos in range(1, 5):
                    matches = rkm_df.index[rkm_df["finish_pos"] == pos].tolist()
                    if not matches:
                        raise ValueError(f"no finisher at position {pos}")
                    top4.append(matches[0])
                proj = stern_harville_ordered(combined, tuple(top4), STERN_K)
                er = proj * ex.iloc[0]["pay_per_1"]
                print(f"\n  Stern/Harville projection of actual top-4 finish: "
                      f"{proj:.6f}  →  ER = {proj:.6f} × ${ex.iloc[0]['pay_per_1']:.2f} = {er:.3f}")
                print(f"  (filter passes if ER ≥ 1.0)")
                print(f"  Top-4 finishers in rkm: combined_probs = {[f'{combined[i]:.4f}' for i in top4]}")
                print(f"  Combined of all-rkm: sums to {combined.sum():.4f} "
                      f"(rkm covers {len(rkm_df)}/{len(df)} starters)")
            except (ValueError, IndexError) as e:
                print(f"  (couldn't compute projection: {e})")

    conn.close()


if __name__ == "__main__":
    main()
