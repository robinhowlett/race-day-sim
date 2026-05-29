"""POC step 3a: probability-driven combo selection for vertical exotics.

For each EXACTA / QUINELLA / TRIFECTA / SUPERFECTA on the sim_candidates
universe, enumerate every possible combo, compute the Stern/Harville
projected probability, multiply by chart payoff, threshold by ER,
and aggregate ROI by (bet_type × ER threshold).

We dropped the per-tier dimension after the first pass: in vertical
exotics a single combo involves multiple horses from different odds
tiers, so "key-horse tier" doesn't map cleanly to a single segment of
the market the way it does for FLB win bets. The clean question is
"does ER-threshold filtering on Stern/Harville × payoff produce +EV?"
— that's what this measures.

Methodology:
  - Stream race-by-race so we don't materialize ~500M combos in RAM.
  - For each race: pull combined_prob, odds_prob, chart payoff, actual
    finish order. Compute Stern/Harville for every ordered finish
    combo. ER = projected_prob × payoff_per_$1.
  - Filter combos by ER >= threshold; bet $1 on each survivor.
  - SE based on per-RACE net (combos within a race are maximally
    correlated; per-combo SE would understate variance).

Output: tmp/vertical_overlay_grid.csv

What "winning combo" means: only the actual ordered finish (or QUINELLA
unordered pair) earns the chart's payoff; all other combos in the
filter set earn $0. Net = (winner_paid if winner survived filter else 0)
- count_survivors.
"""

import argparse
import math
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

# Stern/Harville exponent. Audit WA-T1.1 (resolved 2026-05-27) ran a
# grid search on top-3 ordering log-likelihood across 80,042 clean
# races and updated this from 0.81 → 0.86. wagering-analytics
# populate_stern_fair.py uses 0.86. race-day-sim/src/sim/probability.py
# still hardcodes 0.81 in its function defaults — that's a
# follow-up audit item to harmonize.
STERN_K = 0.86

ER_THRESHOLDS = [1.0, 1.05, 1.10, 1.20, 1.30, 1.50, 2.0, 3.0]

VERTICAL = {"EXACTA": 2, "QUINELLA": 2, "TRIFECTA": 3, "SUPERFECTA": 4}


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def stern_harville_ordered(combined: np.ndarray, idx: tuple, k: float) -> float:
    """P(idx[0] 1st, idx[1] 2nd, ..., idx[-1] last in tuple) given combined-prob array."""
    p = np.asarray(combined, dtype=float)
    p_k = p ** k
    remaining_total = p_k.sum()
    prob = 1.0
    used = []
    for i in idx:
        if remaining_total <= 0:
            return 0.0
        prob *= p_k[i] / remaining_total
        used.append(i)
        remaining_total -= p_k[i]
    return prob


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=None,
                    help="Restrict to single year (for sanity-check runs)")
    ap.add_argument("--start-year", type=int, default=None)
    ap.add_argument("--end-year", type=int, default=None)
    args = ap.parse_args()

    if args.year is not None:
        start, end = args.year, args.year
    else:
        start = args.start_year if args.start_year else 1997
        end = args.end_year if args.end_year else 2017
    print(f"Year range: {start}..{end}")

    print("Loading rkm_market_analysis (combined + odds prob + finish order)...")
    t0 = time.time()
    with get_conn() as conn:
        # Pull per-starter on sim_candidates universe with finish_position
        # so we can identify the actual winning combo.
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.official_position::int AS finish_pos,
                   r.number_of_runners::int AS field_size,
                   EXTRACT(YEAR FROM r.date)::int AS year
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.starters s ON s.id = ma.starter_id
            JOIN handycapper.races r ON r.id = ma.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE ma.combined_prob IS NOT NULL
              AND ma.odds_prob IS NOT NULL
              AND s.official_position IS NOT NULL
              AND EXTRACT(YEAR FROM r.date) BETWEEN %(start)s AND %(end)s
        """, conn, params={"start": start, "end": end})
    print(f"  {len(ma):,} starter rows in {time.time()-t0:.1f}s")

    print("Loading vertical exotic payoffs...")
    t0 = time.time()
    with get_conn() as conn:
        ex = pd.read_sql("""
            SELECT e.race_id, e.bet_type,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = ANY(%(types)s)
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) BETWEEN %(start)s AND %(end)s
        """, conn, params={"types": list(VERTICAL.keys()), "start": start, "end": end})
    print(f"  {len(ex):,} exotic rows in {time.time()-t0:.1f}s")

    pay_lookup: dict[tuple[int, str], float] = {}
    for race_id, bet_type, p in zip(ex["race_id"], ex["bet_type"], ex["pay_per_1"]):
        pay_lookup[(int(race_id), bet_type)] = float(p)

    # Group market_analysis rows by race_id; pre-build per-race state.
    # (race_id, starter_id, combined, odds, finish_pos)
    print("Grouping per-race state...")
    t0 = time.time()
    races: dict[int, dict] = defaultdict(lambda: {"sids": [], "combined": [], "odds": [], "finish": []})
    for sid, rid, c, o, f in zip(
        ma["starter_id"].values, ma["race_id"].values,
        ma["combined_prob"].values, ma["odds_prob"].values,
        ma["finish_pos"].values,
    ):
        d = races[int(rid)]
        d["sids"].append(int(sid))
        d["combined"].append(float(c))
        d["odds"].append(float(o))
        d["finish"].append(int(f))
    print(f"  {len(races):,} races built in {time.time()-t0:.1f}s")

    # Aggregate buckets: (bet_type, threshold, year) -> dict.
    # Per-year buckets enable year-by-year stability check, mirroring
    # FLB POC step 8.
    agg: dict[tuple, dict] = defaultdict(
        lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                 "n_winners": 0,
                 "race_nets": [],
                 "race_invested": []}
    )

    # Map race_id -> year (we already pulled `year` in the ma query)
    race_year = {}
    for rid, yr in zip(ma["race_id"].values, ma["year"].values):
        race_year[int(rid)] = int(yr)

    print("Streaming race-by-race overlay analysis...")
    t0 = time.time()
    n_processed = 0
    for race_id, d in races.items():
        n = len(d["sids"])
        if n < 2:
            continue
        combined = np.asarray(d["combined"])
        odds = np.asarray(d["odds"])
        finish = d["finish"]

        # Index of actual finishers
        try:
            idx_by_pos = {p: i for i, p in enumerate(finish)}
        except Exception:
            continue

        for bt, n_pos in VERTICAL.items():
            payoff = pay_lookup.get((race_id, bt))
            if payoff is None or n < n_pos:
                continue
            # Identify actual-winning indices
            try:
                winner_idx = tuple(idx_by_pos[p] for p in range(1, n_pos + 1))
            except KeyError:
                # Race didn't have a clean 1-2-3-4 finish (rare; skip)
                continue
            quin_winner_set = frozenset(winner_idx) if bt == "QUINELLA" else None

            # Per-race accumulator: thr -> {"invested", "returned",
            # "n_combos", "n_winners"}
            race_buckets: dict[float, dict] = defaultdict(
                lambda: {"invested": 0.0, "returned": 0.0,
                         "n_combos": 0, "n_winners": 0})

            for combo in permutations(range(n), n_pos):
                if bt == "QUINELLA":
                    if combo[0] >= combo[1]:
                        continue
                    p1 = stern_harville_ordered(combined, combo, STERN_K)
                    p2 = stern_harville_ordered(combined, combo[::-1], STERN_K)
                    proj = p1 + p2
                    is_winner = frozenset(combo) == quin_winner_set
                else:
                    proj = stern_harville_ordered(combined, combo, STERN_K)
                    is_winner = combo == winner_idx
                if proj <= 0:
                    continue
                er = proj * payoff

                for thr in ER_THRESHOLDS:
                    if er < thr:
                        continue
                    rb = race_buckets[thr]
                    rb["n_combos"] += 1
                    rb["invested"] += 1.0
                    if is_winner:
                        rb["returned"] += payoff
                        rb["n_winners"] += 1

            # Commit per-race buckets to global aggregate (keyed by year)
            year = race_year.get(race_id)
            for thr, rb in race_buckets.items():
                key = (bt, thr, year)
                a = agg[key]
                a["n_combos"] += rb["n_combos"]
                a["invested"] += rb["invested"]
                a["returned"] += rb["returned"]
                a["n_winners"] += rb["n_winners"]
                a["race_nets"].append(rb["returned"] - rb["invested"])
                a["race_invested"].append(rb["invested"])

        n_processed += 1
        if n_processed % 25000 == 0:
            print(f"  {n_processed:,}/{len(races):,} races, "
                  f"{time.time()-t0:.0f}s elapsed")

    print(f"Streaming done in {time.time()-t0:.0f}s ({n_processed:,} races)")

    rows = []
    for (bt, thr, year), a in agg.items():
        if a["invested"] <= 0:
            continue
        roi = a["returned"] / a["invested"] - 1

        # SE based on per-RACE net, not per-combo. Combos within a race
        # are maximally correlated (one common outcome) so per-combo SE
        # would dramatically understate variance. Per-race net is the
        # i.i.d. unit. ROI = sum(race_nets) / sum(race_invested);
        # SE ≈ sqrt(var(per_race_roi)) / sqrt(n_races) where per_race_roi
        # is weighted by per-race invested (since races invest different
        # amounts).
        race_nets = np.asarray(a["race_nets"])
        race_inv = np.asarray(a["race_invested"])
        n_races = len(race_nets)
        if n_races > 1 and race_inv.sum() > 0:
            # Per-race ROI; weight-average matches the aggregate ROI exactly
            per_race_roi = race_nets / np.maximum(race_inv, 1e-9)
            # Weighted SE of weighted mean: use invested as weight.
            # Var(weighted mean) ≈ sum(w² var) / (sum w)² assuming w_i ~ inv_i
            # We use the simpler formula: per-race ROI std / sqrt(n) is an
            # unbiased estimator if races are equally-weighted. With unequal
            # weights, more variance. Use the unweighted std as a conservative
            # estimator.
            se = float(per_race_roi.std(ddof=1) / math.sqrt(n_races))
            ci_lo = roi - 1.96 * se
            ci_hi = roi + 1.96 * se
        else:
            se = None
            ci_lo = None
            ci_hi = None

        rows.append({
            "bet_type": bt, "threshold": thr, "year": year,
            "n_races": n_races,
            "n_combos": int(a["n_combos"]),
            "n_winners": int(a["n_winners"]),
            "invested": round(a["invested"], 2),
            "returned": round(a["returned"], 2),
            "roi_pct": round(100 * roi, 2),
            "hit_rate_pct": round(100 * a["n_winners"] / a["n_combos"], 4),
            "se_pct":    None if se is None else round(100 * se, 2),
            "ci_lo_pct": None if ci_lo is None else round(100 * ci_lo, 2),
            "ci_hi_pct": None if ci_hi is None else round(100 * ci_hi, 2),
        })

    out = pd.DataFrame(rows).sort_values(
        ["bet_type", "threshold", "year"]).reset_index(drop=True)
    out_path = TMP / "vertical_overlay_grid.csv"
    out.to_csv(out_path, index=False)

    # Per-year stability table: print one row per (bet_type, threshold)
    # showing year-by-year ROI. Filter to thresholds 1.00 / 1.10 / 1.30
    # (the most informative slice — bare ER, modest filter, strict filter).
    HIGHLIGHT_THRESHOLDS = [1.00, 1.10, 1.30, 2.00]
    years = sorted(out["year"].dropna().unique().astype(int))
    print(f"\n=== Per-year ROI stability (vertical exotics) ===")
    print(f"{'bet_type':<12}{'thr':>5} ", end="")
    for y in years:
        print(f"{y:>9}", end="")
    print(f" {'mean':>8} {'+yrs':>5}")
    for bt in ["EXACTA", "QUINELLA", "TRIFECTA", "SUPERFECTA"]:
        for thr in HIGHLIGHT_THRESHOLDS:
            row = f"{bt:<12}{thr:>5.2f} "
            yr_rois = []
            for y in years:
                m = out[(out["bet_type"] == bt) &
                        (out["threshold"] == thr) &
                        (out["year"] == y)]
                if m.empty or m.iloc[0]["n_races"] < 50:
                    row += f"{'—':>9}"
                else:
                    r = m.iloc[0]["roi_pct"]
                    yr_rois.append(r)
                    row += f"{r:>+8.1f}%"
            if yr_rois:
                mean_ = sum(yr_rois) / len(yr_rois)
                pos = sum(1 for r in yr_rois if r > 0)
                row += f" {mean_:>+7.1f}% {pos}/{len(yr_rois)}"
            print(row)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
