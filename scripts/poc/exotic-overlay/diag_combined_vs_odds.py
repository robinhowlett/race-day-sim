"""Diagnostic: Stern/Harville on combined_prob vs odds_prob.

Question: is the +160% SUPERFECTA ROI real overlay (model contributes
skill above public's odds), or just inverse-correlation arithmetic
(any ER > 1 filter produces the same number because payoff and
projected probability are mechanically inversely related)?

Test: same 2014 SUPERFECTA data, same ER ≥ 1.0 filter, but compute
projection two ways:
  (a) Stern/Harville on combined_prob (current POC) — model + odds blend
  (b) Stern/Harville on odds_prob_normalized — pure public belief

odds_prob is overround-normalized (sums to 1.0 per race) by dividing
by sum(odds_prob_i) so it represents the public's true probability
estimate after stripping takeout.

Diagnostic interpretation:
  - If (a) and (b) give similar ROI → +160% is arithmetic, not skill.
    The model adds nothing.
  - If (a) >> (b) → the model genuinely identifies overlays the public
    misses. Real skill.
  - If (a) << (b) → the model is hurting (unlikely but possible).
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
ER_THRESHOLDS = [1.0, 1.10, 1.30, 2.0, 3.0]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def stern_harville_ordered(p_array: np.ndarray, idx: tuple, k: float) -> float:
    p_k = p_array ** k
    remaining = p_k.sum()
    prob = 1.0
    for i in idx:
        if remaining <= 0:
            return 0.0
        prob *= p_k[i] / remaining
        remaining -= p_k[i]
    return prob


def main():
    print("Loading 2014 SUPERFECTA data...")
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
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = 'SUPERFECTA'
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) = 2014
        """, conn)
    print(f"  {len(ma):,} starters, {len(ex):,} payoffs in {time.time()-t0:.1f}s")

    pay_lookup = {int(r.race_id): float(r.pay_per_1) for r in ex.itertuples()}

    races = defaultdict(lambda: {"combined": [], "odds": [], "finish": []})
    for rid, c, o, f in zip(ma["race_id"], ma["combined_prob"],
                             ma["odds_prob"], ma["finish_pos"]):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["odds"].append(float(o))
        d["finish"].append(int(f))

    # Aggregate buckets: (model, threshold) -> totals (per-race nets)
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                 "n_winners": 0, "race_nets": [], "race_invested": []}
    )

    print("Streaming...")
    t0 = time.time()
    n_processed = 0
    for race_id, d in races.items():
        n = len(d["combined"])
        if n < 4:
            continue
        payoff = pay_lookup.get(race_id)
        if payoff is None:
            continue
        try:
            winner_idx = tuple(d["finish"].index(p) for p in range(1, 5))
        except ValueError:
            continue

        combined = np.asarray(d["combined"])
        odds_raw = np.asarray(d["odds"])
        # Overround-normalize odds_prob to sum to 1.0 (strip takeout)
        odds_norm = odds_raw / odds_raw.sum() if odds_raw.sum() > 0 else odds_raw

        # Per-model per-race buckets
        race_buckets = {
            "combined": defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
            "odds":     defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
        }

        for combo in permutations(range(n), 4):
            is_winner = combo == winner_idx
            for model_name, p_arr in [("combined", combined), ("odds", odds_norm)]:
                proj = stern_harville_ordered(p_arr, combo, STERN_K)
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

        n_processed += 1
        if n_processed % 5000 == 0:
            print(f"  {n_processed:,}/{len(races):,} races, "
                  f"{time.time()-t0:.0f}s elapsed")

    print(f"Done in {time.time()-t0:.0f}s ({n_processed:,} races)")

    print(f"\n=== SUPERFECTA 2014: combined_prob vs odds_prob (Stern/Harville × payoff) ===")
    print(f"{'thr':>5} {'model':<10} {'n_combos':>10} {'wins':>6}"
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


if __name__ == "__main__":
    main()
