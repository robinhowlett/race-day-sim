"""Diagnostic: same combined-vs-odds analysis on EXACTA in 2014.

EXACTA chosen as minimum-viable vertical. Simplest math (depth-2),
deepest pools (median $46K), tightest tail variance, most data
(~30K races/year).

For 2014 EXACTA:
  1. Bet-everything ROI on rkm-covered races (sanity check vs step 2)
  2. ER ≥ X filter using Stern/Harville on combined_prob (model)
  3. Same filter using odds_prob (no model; pure cross-pool overlay)
  4. Check tail concentration (top-N races' contribution to ROI)
  5. Pool-fraction at each threshold

Diagnostic interpretation, mirroring the SUPERFECTA findings:
  - If combined and odds give similar ROI → cross-pool overlay alone
  - If combined >> odds → model adds genuine skill
  - If both give modest +ROI on the simplest exotic → cross-pool
    overlay is real but bounded; the +143% on SUPERFECTA was
    amplified by depth, not a fundamentally different phenomenon
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
ER_THRESHOLDS = [1.0, 1.05, 1.10, 1.20, 1.30, 1.50, 2.0, 3.0]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def stern_harville_2pos(p_array: np.ndarray, i: int, j: int, k: float) -> float:
    """P(i 1st, j 2nd) under Stern/Harville."""
    p_k = p_array ** k
    total = p_k.sum()
    if total <= 0:
        return 0.0
    p_first = p_k[i] / total
    remain = total - p_k[i]
    if remain <= 0:
        return 0.0
    p_second = p_k[j] / remain
    return p_first * p_second


def main():
    print("Loading 2014 EXACTA data...")
    t0 = time.time()
    with get_conn() as conn:
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.official_position::int AS finish_pos
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
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   e.pool::float AS pool
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = 'EXACTA'
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) = 2014
        """, conn)
    print(f"  {len(ma):,} starters, {len(ex):,} payoffs in {time.time()-t0:.1f}s")

    pay_lookup = {int(r.race_id): (float(r.pay_per_1), float(r.pool))
                  for r in ex.itertuples()}

    races = defaultdict(lambda: {"combined": [], "odds": [], "finish": []})
    for rid, c, o, f in zip(ma["race_id"], ma["combined_prob"],
                             ma["odds_prob"], ma["finish_pos"]):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["odds"].append(float(o))
        d["finish"].append(int(f))

    # Aggregate buckets: (model, threshold) -> totals
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                 "n_winners": 0, "race_nets": [], "race_invested": [],
                 "race_pool": [], "race_invested_for_pool_pct": []}
    )

    print("Streaming...")
    t0 = time.time()
    n_processed = 0
    bet_everything_invested = 0.0
    bet_everything_returned = 0.0
    bet_everything_n_combos = 0

    for race_id, d in races.items():
        n = len(d["combined"])
        if n < 2:
            continue
        info = pay_lookup.get(race_id)
        if info is None:
            continue
        payoff, pool = info
        try:
            winner_idx = (d["finish"].index(1), d["finish"].index(2))
        except ValueError:
            continue

        combined = np.asarray(d["combined"])
        odds_raw = np.asarray(d["odds"])
        odds_norm = odds_raw / odds_raw.sum() if odds_raw.sum() > 0 else odds_raw

        # Bet-everything baseline (rkm-covered races)
        n_combos_total = n * (n - 1)
        bet_everything_invested += n_combos_total
        bet_everything_returned += payoff  # only the actual winner pays
        bet_everything_n_combos += n_combos_total

        # Per-model per-race buckets
        race_buckets = {
            "combined": defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
            "odds":     defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
        }

        for i in range(n):
            for j in range(n):
                if i == j:
                    continue
                is_winner = (i, j) == winner_idx
                for model_name, p_arr in [("combined", combined), ("odds", odds_norm)]:
                    proj = stern_harville_2pos(p_arr, i, j, STERN_K)
                    if proj <= 0:
                        continue
                    er = proj * payoff
                    for thr in ER_THRESHOLDS:
                        if er < thr:
                            continue
                        rb = race_buckets[model_name][thr]
                        rb["n_combos"] += 1
                        rb["invested"] += 1.0
                        if is_winner:
                            rb["returned"] += payoff
                            rb["n_winners"] += 1

        # Commit
        for model_name, by_thr in race_buckets.items():
            for thr, rb in by_thr.items():
                key = (model_name, thr)
                a = agg[key]
                a["n_combos"] += rb["n_combos"]
                a["invested"] += rb["invested"]
                a["returned"] += rb["returned"]
                a["n_winners"] += rb["n_winners"]
                a["race_nets"].append(rb["returned"] - rb["invested"])
                a["race_invested"].append(rb["invested"])
                a["race_pool"].append(pool)
                a["race_invested_for_pool_pct"].append(rb["invested"])

        n_processed += 1
        if n_processed % 5000 == 0:
            print(f"  {n_processed:,}/{len(races):,} races, "
                  f"{time.time()-t0:.0f}s elapsed")

    print(f"Done in {time.time()-t0:.0f}s ({n_processed:,} races)")

    print(f"\n=== EXACTA 2014 ===")
    print(f"Races processed: {n_processed:,}")
    print(f"\nBet-everything baseline (rkm-covered races, all combos):")
    if bet_everything_invested > 0:
        roi = bet_everything_returned / bet_everything_invested - 1
        print(f"  invested ${bet_everything_invested:,.0f}, "
              f"returned ${bet_everything_returned:,.0f}, "
              f"ROI {100*roi:+.1f}%, "
              f"n_combos {bet_everything_n_combos:,}")

    print(f"\n{'thr':>5} {'model':<10} {'n_combos':>10} {'wins':>6}"
          f"{'invested':>11} {'returned':>11} {'ROI':>9} {'95% CI':>22}")
    print("-" * 95)
    for thr in ER_THRESHOLDS:
        for model_name in ["combined", "odds"]:
            a = agg[(model_name, thr)]
            if a["invested"] <= 0:
                continue
            roi = a["returned"] / a["invested"] - 1
            race_nets = np.asarray(a["race_nets"])
            race_inv = np.asarray(a["race_invested"])
            n_races = len(race_nets)
            if n_races > 1 and race_inv.sum() > 0:
                per_race_roi = race_nets / np.maximum(race_inv, 1e-9)
                se = float(per_race_roi.std(ddof=1) / np.sqrt(n_races))
                ci_lo = roi - 1.96 * se
                ci_hi = roi + 1.96 * se
                ci_s = f"({100*ci_lo:+6.1f}%, {100*ci_hi:+6.1f}%)"
            else:
                ci_s = "—"
            print(f"{thr:>5.2f} {model_name:<10} {int(a['n_combos']):>10,} "
                  f"{int(a['n_winners']):>6,}"
                  f"${a['invested']:>9,.0f}${a['returned']:>9,.0f}"
                  f"{100*roi:>+8.1f}% {ci_s:>22}")
        print()

    # Pool-fraction analysis on combined model
    print("\n=== Pool-fraction analysis (combined model) ===")
    print(f"{'thr':>5} {'p25':>8} {'p50':>8} {'p75':>8} {'p95':>8}")
    for thr in ER_THRESHOLDS:
        a = agg[("combined", thr)]
        if not a["race_invested_for_pool_pct"]:
            continue
        invs = np.asarray(a["race_invested_for_pool_pct"])
        pools = np.asarray(a["race_pool"])
        pcts = 100 * invs / np.maximum(pools, 1)
        print(f"{thr:>5.2f} {np.percentile(pcts, 25):>7.2f}% "
              f"{np.percentile(pcts, 50):>7.2f}% "
              f"{np.percentile(pcts, 75):>7.2f}% "
              f"{np.percentile(pcts, 95):>7.2f}%")


if __name__ == "__main__":
    main()
