"""SUPERFECTA audit — 2014 only, focused diagnostic.

Goal: explain why bet-everything-with-ER>1.0 returns +160% ROI on
SUPERFECTA. Possible explanations:

  1. Real, just inflates with zero-market-impact assumption
  2. Tail-dominated: a few enormous payoffs drive the mean
  3. Stern/Harville systematically miscalibrates longshot supers
  4. Data alignment bug: payoffs not matching the right combos
  5. Survivorship: published payoffs differ from real-betting reality

We surface diagnostics that distinguish these:
  - Per-race net distribution (how heavy is the tail?)
  - Did the actual winning combo pass the filter? At what ER?
  - For races where winner survived: what was projected vs realized?
  - Top-payout races: what made them so big?
  - Compare bet-everything (no filter) ROI to ER-filtered ROI on
    SAME races — strip away combos vs strategy effects
"""

import os
import sys
import time
from collections import defaultdict
from itertools import permutations
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"

STERN_K = 0.86


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
    print("Loading 2014 SUPERFECTA data + per-race state...")
    t0 = time.time()
    with get_conn() as conn:
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.official_position::int AS finish_pos,
                   s.program::text AS program,
                   s.horse::text AS horse,
                   s.odds::float AS odds_decimal
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.starters s ON s.id = ma.starter_id
            JOIN handycapper.races r ON r.id = ma.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE ma.combined_prob IS NOT NULL
              AND ma.odds_prob IS NOT NULL
              AND s.official_position IS NOT NULL
              AND EXTRACT(YEAR FROM r.date) = 2014
        """, conn)
        ex = pd.read_sql("""
            SELECT e.race_id,
                   e.payoff::float AS payoff,
                   e.unit::float AS unit,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   e.pool::float AS pool,
                   e.winning_numbers,
                   r.track::text AS track, r.date::text AS race_date,
                   r.number AS race_number,
                   r.number_of_runners::int AS field_size
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = 'SUPERFECTA'
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) = 2014
        """, conn)
    print(f"  Loaded in {time.time()-t0:.1f}s")
    print(f"  {len(ma):,} starter rows, {len(ex):,} superfecta payoffs")

    pay_lookup = {int(r.race_id): r for r in ex.itertuples()}

    # Group by race
    races = defaultdict(lambda: {"sids": [], "combined": [], "odds": [], "finish": [], "program": []})
    for sid, rid, c, o, f, pgm in zip(
        ma["starter_id"], ma["race_id"], ma["combined_prob"],
        ma["odds_prob"], ma["finish_pos"], ma["program"],
    ):
        d = races[int(rid)]
        d["sids"].append(int(sid))
        d["combined"].append(float(c))
        d["odds"].append(float(o))
        d["finish"].append(int(f))
        d["program"].append(str(pgm))

    print(f"  {len(races):,} races")

    # ----- Per-race analysis -----
    # Limit to first ~21K (all of 2014)
    rows = []
    for race_id, d in races.items():
        n = len(d["sids"])
        if n < 4:
            continue
        ex_row = pay_lookup.get(race_id)
        if ex_row is None:
            continue
        combined = np.asarray(d["combined"])
        odds = np.asarray(d["odds"])
        finish = d["finish"]
        try:
            winner_idx = tuple(finish.index(i) for i in range(1, 5))
        except ValueError:
            continue

        # Project for the actual winning combo
        winner_proj = stern_harville_ordered(combined, winner_idx, STERN_K)
        winner_er = winner_proj * ex_row.pay_per_1

        # Bet-everything stats
        n_combos_total = n * (n-1) * (n-2) * (n-3)
        # Filter ER >= 1.0 stats: enumerate all and count
        n_pass_er1 = 0
        winner_passes_er1 = winner_er >= 1.0
        for combo in permutations(range(n), 4):
            if combo == winner_idx:
                continue  # already counted
            proj = stern_harville_ordered(combined, combo, STERN_K)
            if proj * ex_row.pay_per_1 >= 1.0:
                n_pass_er1 += 1
        # add winner if it passes
        if winner_passes_er1:
            n_pass_er1 += 1

        invested_all = float(n_combos_total)
        invested_er1 = float(n_pass_er1)
        returned_all = ex_row.pay_per_1
        returned_er1 = ex_row.pay_per_1 if winner_passes_er1 else 0.0

        rows.append({
            "race_id": race_id,
            "track": ex_row.track,
            "race_date": ex_row.race_date,
            "race_number": ex_row.race_number,
            "field_size": n,
            "pool": ex_row.pool,
            "unit": ex_row.unit,
            "payoff": ex_row.payoff,
            "pay_per_1": ex_row.pay_per_1,
            "winning_nums": ex_row.winning_numbers,
            "winner_finish": "-".join(str(d["program"][i]) for i in winner_idx),
            "winner_proj": winner_proj,
            "winner_er": winner_er,
            "winner_passes_er1": winner_passes_er1,
            "n_combos_total": n_combos_total,
            "n_pass_er1": n_pass_er1,
            "invested_all": invested_all,
            "returned_all": returned_all,
            "net_all": returned_all - invested_all,
            "invested_er1": invested_er1,
            "returned_er1": returned_er1,
            "net_er1": returned_er1 - invested_er1,
        })
    df = pd.DataFrame(rows)
    print(f"\n  {len(df):,} races with full data")

    # ----- Diagnostics -----
    print("\n=== Aggregate ROI sanity checks ===")
    print(f"  Bet-everything: invested ${df['invested_all'].sum():,.0f}, "
          f"returned ${df['returned_all'].sum():,.0f}, "
          f"ROI {100*(df['returned_all'].sum() / df['invested_all'].sum() - 1):+.1f}%")
    print(f"  ER ≥ 1.0:       invested ${df['invested_er1'].sum():,.0f}, "
          f"returned ${df['returned_er1'].sum():,.0f}, "
          f"ROI {100*(df['returned_er1'].sum() / df['invested_er1'].sum() - 1):+.1f}%")

    print("\n=== ER >= 1.0 race-level stats ===")
    print(f"  Races where winner passed filter: {df['winner_passes_er1'].sum():,} / {len(df):,}")
    print(f"  Median surviving combos / race:   {df['n_pass_er1'].median():.0f}")
    print(f"  Mean surviving combos / race:     {df['n_pass_er1'].mean():.0f}")
    print(f"  Median total combos / race:       {df['n_combos_total'].median():.0f}")
    print(f"  Median field size:                {df['field_size'].median():.0f}")

    # Tail analysis: top-10 payoffs
    print("\n=== Top 10 payoffs (per-$1) on races where winner survived ER>=1 filter ===")
    surv = df[df["winner_passes_er1"]].copy()
    surv["surv_roi_per_dollar"] = surv["pay_per_1"] / surv["n_pass_er1"]
    top10 = surv.nlargest(10, "pay_per_1")[
        ["track", "race_date", "race_number", "field_size", "pool",
         "unit", "payoff", "pay_per_1", "winner_finish",
         "winner_proj", "winner_er", "n_pass_er1"]
    ]
    print(top10.to_string(index=False))

    # Sum contribution of top N payouts
    print("\n=== Tail concentration ===")
    surv_sorted = surv.sort_values("pay_per_1", ascending=False)
    cum_returns = surv_sorted["pay_per_1"].cumsum()
    total_returned = surv["pay_per_1"].sum()
    for n in [1, 5, 10, 50, 100, 500]:
        if n <= len(surv_sorted):
            pct = 100 * cum_returns.iloc[n-1] / total_returned
            print(f"  Top {n:>3} winning races contribute {pct:.1f}% of total returns")

    # Distribution of winner_proj (in races where winner passed filter)
    print("\n=== Winner projection percentiles (filter-passing winners) ===")
    print(f"  p1   {surv['winner_proj'].quantile(0.01):.5f}")
    print(f"  p5   {surv['winner_proj'].quantile(0.05):.5f}")
    print(f"  p25  {surv['winner_proj'].quantile(0.25):.5f}")
    print(f"  p50  {surv['winner_proj'].quantile(0.50):.5f}")
    print(f"  p75  {surv['winner_proj'].quantile(0.75):.5f}")
    print(f"  p99  {surv['winner_proj'].quantile(0.99):.5f}")

    print("\n=== Pool size vs payoff per $1 ===")
    print(f"  median pool: ${df['pool'].median():,.0f}")
    print(f"  median per-$1 payoff among winners passing filter: ${surv['pay_per_1'].median():.2f}")
    # Critical metric: invested_er1 as fraction of pool
    surv["invested_pct_of_pool"] = 100 * surv["invested_er1"] / surv["pool"]
    print(f"  median invested as % of pool: {surv['invested_pct_of_pool'].median():.2f}%")
    print(f"  p75 invested as % of pool:    {surv['invested_pct_of_pool'].quantile(0.75):.2f}%")
    print(f"  p95 invested as % of pool:    {surv['invested_pct_of_pool'].quantile(0.95):.2f}%")

    out = TMP / "audit_superfecta_2014.csv"
    df.to_csv(out, index=False)
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
