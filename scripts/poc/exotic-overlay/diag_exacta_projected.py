"""Honest EXACTA diagnostic: ER = stern_harville_prob × PROJECTED payoff.

Replaces the chart's actual paid-this-race payoff with the OLS-projected
expected payoff from src/sim/payoff.py. Eliminates the post-hoc selection
bias of using outcome data (chart payoff) as a filter input.

Per combo (i, j) on a race:
  proj_p   = stern_harville_2pos(model[i], model[j], k=0.86)
  proj_pay = project_exacta_payoff(odds[i], odds[j], pool, field_size,
                                    hhi, fav_position)
  ER       = proj_p × proj_pay

Filter combos by ER >= threshold. For each surviving combo: invested = $1.
For the actual winning combo if it survived: returned = chart_payoff_per_$1.

Comparison:
  - combined: stern on combined_prob (rkm Benter blend)
  - odds:     stern on odds_norm (overround-normalized public)

If the OLS payoff projection is unbiased, then combos with ER >= 1.0
are the ones where the model thinks the realized payoff will exceed
the takeout-adjusted fair value. Realized ROI tells us if the OLS
projection is well-calibrated: ROI ~ -takeout means filter has no
edge above takeout; ROI > -takeout means real overlay edge.
"""

import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from sim.payoff import project_exacta_payoff  # noqa: E402

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


def stern_2pos(p: np.ndarray, i: int, j: int, k: float) -> float:
    p_k = p ** k
    total = p_k.sum()
    if total <= 0:
        return 0.0
    p_first = p_k[i] / total
    remain = total - p_k[i]
    if remain <= 0:
        return 0.0
    return p_first * (p_k[j] / remain)


def main():
    print("Loading 2014 EXACTA data...")
    t0 = time.time()
    with get_conn() as conn:
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.odds::float AS decimal_odds,
                   s.choice::int AS choice,
                   s.official_position::int AS finish_pos,
                   r.number_of_runners::int AS field_size
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.starters s ON s.id = ma.starter_id
            JOIN handycapper.races r ON r.id = ma.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE ma.combined_prob IS NOT NULL
              AND ma.odds_prob IS NOT NULL
              AND s.odds IS NOT NULL
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

    # Build per-race state including odds, choice (for fav identification), field
    races = defaultdict(lambda: {"combined": [], "odds_p": [], "decimal_odds": [],
                                   "choice": [], "finish": [], "field_size": 0})
    for rid, c, op, do, ch, fp, fs in zip(
        ma["race_id"], ma["combined_prob"], ma["odds_prob"],
        ma["decimal_odds"], ma["choice"], ma["finish_pos"], ma["field_size"],
    ):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["odds_p"].append(float(op))
        d["decimal_odds"].append(float(do))
        d["choice"].append(int(ch) if pd.notna(ch) else 99)
        d["finish"].append(int(fp))
        d["field_size"] = int(fs)

    agg: dict[tuple, dict] = defaultdict(
        lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                 "n_winners": 0, "race_nets": [], "race_invested": []}
    )

    print("Streaming...")
    t0 = time.time()
    n_processed = 0
    bet_everything_invested = 0.0
    bet_everything_returned = 0.0

    for race_id, d in races.items():
        n = len(d["combined"])
        if n < 2:
            continue
        info = pay_lookup.get(race_id)
        if info is None:
            continue
        chart_payoff, pool = info
        try:
            winner_idx = (d["finish"].index(1), d["finish"].index(2))
        except ValueError:
            continue

        combined = np.asarray(d["combined"])
        odds_raw = np.asarray(d["odds_p"])
        odds_norm = odds_raw / odds_raw.sum() if odds_raw.sum() > 0 else odds_raw
        decimal_odds = d["decimal_odds"]
        choice = d["choice"]
        field_size = d["field_size"]

        # HHI = sum of p_i^2 (concentration of win probability)
        hhi = float((combined ** 2).sum())

        # Find favorite by choice rank
        fav_idx = choice.index(1) if 1 in choice else None

        # Bet-everything baseline (rkm-covered races)
        n_combos_total = n * (n - 1)
        bet_everything_invested += n_combos_total
        bet_everything_returned += chart_payoff

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
                # fav_position: 1 if fav is in winning slot (i), 2 if in second
                # slot (j), None if not in this combo
                if fav_idx is None:
                    fav_pos = None
                elif i == fav_idx:
                    fav_pos = 1
                elif j == fav_idx:
                    fav_pos = 2
                else:
                    fav_pos = None

                # Project payoff
                proj_pay = project_exacta_payoff(
                    decimal_odds[i], decimal_odds[j],
                    pool, field_size, hhi, 1, fav_pos,
                )
                if proj_pay is None or proj_pay <= 0:
                    continue

                is_winner = (i, j) == winner_idx
                for model_name, p_arr in [("combined", combined), ("odds", odds_norm)]:
                    proj_p = stern_2pos(p_arr, i, j, STERN_K)
                    if proj_p <= 0:
                        continue
                    er = proj_p * proj_pay
                    for thr in ER_THRESHOLDS:
                        if er < thr:
                            continue
                        rb = race_buckets[model_name][thr]
                        rb["n_combos"] += 1
                        rb["invested"] += 1.0
                        if is_winner:
                            rb["returned"] += chart_payoff
                            rb["n_winners"] += 1

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

    print(f"\n=== EXACTA 2014 (PROJECTED-payoff filter; chart payoff used only for realized return) ===")
    print(f"\nBet-everything baseline (rkm-covered races):")
    if bet_everything_invested > 0:
        roi = bet_everything_returned / bet_everything_invested - 1
        print(f"  ROI {100*roi:+.1f}% on {bet_everything_invested:,.0f} combos")

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


if __name__ == "__main__":
    main()
