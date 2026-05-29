"""POC step 4: honest exotic-overlay analysis, all years all types.

Replaces step 3's chart-payoff-as-ER-input bug with:
  ER = projected_prob × project_<bettype>_payoff(...)

Per (bet_type, ER threshold, year):
  - n_combos / n_winners
  - invested = $1 per surviving combo
  - returned = chart_payoff for actual winner if it survived
  - 95% CI on per-race-net SE

Two probability models:
  - combined: Stern/Harville on rkm combined_prob (Benter blend)
  - odds:     Stern/Harville on overround-normalized odds_prob

Bet types covered:
  EXACTA, TRIFECTA  (project_exacta_payoff, project_trifecta_payoff)
  PICK_3            (project_pick3_payoff)

Bet types deferred (no projection model in payoff_coefficients.json or
no implementing function in src/sim/payoff.py):
  SUPERFECTA, DAILY_DOUBLE, PICK_4, PICK_5, PICK_6, QUINELLA

Output: tmp/honest_overlay_per_year_<bet_type>.csv
"""

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from sim.payoff import (project_exacta_payoff, project_trifecta_payoff,
                          project_superfecta_payoff, project_pick3_payoff,
                          project_pick4_payoff)  # noqa

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"

STERN_K = 0.86
ER_THRESHOLDS = [1.0, 1.10, 1.20, 1.30, 1.50, 2.0, 3.0]


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def stern_n_pos(p: np.ndarray, idx: tuple, k: float) -> float:
    """P(idx[0] 1st, idx[1] 2nd, ..., idx[-1] in slot len(idx))."""
    p_k = p ** k
    remaining = p_k.sum()
    prob = 1.0
    for i in idx:
        if remaining <= 0:
            return 0.0
        prob *= p_k[i] / remaining
        remaining -= p_k[i]
    return prob


# ===== EXACTA / TRIFECTA loaders =====

def load_vertical_data(start_year: int, end_year: int):
    print(f"Loading rkm_market_analysis + race meta for {start_year}-{end_year}...")
    t0 = time.time()
    with get_conn() as conn:
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.odds::float AS decimal_odds,
                   s.choice::int AS choice,
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
              AND s.odds IS NOT NULL
              AND s.official_position IS NOT NULL
              AND EXTRACT(YEAR FROM r.date) BETWEEN %(start)s AND %(end)s
        """, conn, params={"start": start_year, "end": end_year})
    print(f"  {len(ma):,} starters in {time.time()-t0:.1f}s")
    return ma


def load_vertical_payoffs(bet_type: str, start_year: int, end_year: int):
    print(f"Loading {bet_type} payoffs...")
    t0 = time.time()
    with get_conn() as conn:
        ex = pd.read_sql("""
            SELECT e.race_id,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   e.pool::float AS pool
            FROM handycapper.exotics e
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = %(bt)s
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) BETWEEN %(start)s AND %(end)s
        """, conn, params={"bt": bet_type, "start": start_year, "end": end_year})
    print(f"  {len(ex):,} {bet_type} payoffs in {time.time()-t0:.1f}s")
    return ex


# ===== Vertical streamer =====

def run_vertical(bet_type: str, n_pos: int, ma: pd.DataFrame,
                 ex: pd.DataFrame, project_fn) -> pd.DataFrame:
    """Stream race-by-race, accumulate per (model, threshold, year)."""
    pay_lookup = {int(r.race_id): (float(r.pay_per_1), float(r.pool))
                  for r in ex.itertuples()}

    races = defaultdict(lambda: {"combined": [], "odds_p": [], "decimal_odds": [],
                                   "choice": [], "finish": [],
                                   "field_size": 0, "year": None})
    for rid, c, op, do, ch, fp, fs, yr in zip(
        ma["race_id"], ma["combined_prob"], ma["odds_prob"],
        ma["decimal_odds"], ma["choice"], ma["finish_pos"],
        ma["field_size"], ma["year"],
    ):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["odds_p"].append(float(op))
        d["decimal_odds"].append(float(do))
        d["choice"].append(int(ch) if pd.notna(ch) else 99)
        d["finish"].append(int(fp))
        d["field_size"] = int(fs)
        d["year"] = int(yr)

    agg = defaultdict(lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                                "n_winners": 0, "race_nets": [], "race_invested": []})

    print(f"Streaming {bet_type} ({len(races):,} races)...")
    t0 = time.time()
    n_processed = 0

    for race_id, d in races.items():
        n = len(d["combined"])
        if n < n_pos:
            continue
        info = pay_lookup.get(race_id)
        if info is None:
            continue
        chart_payoff, pool = info
        try:
            winner_idx = tuple(d["finish"].index(p) for p in range(1, n_pos + 1))
        except ValueError:
            continue

        combined = np.asarray(d["combined"])
        odds_raw = np.asarray(d["odds_p"])
        odds_norm = odds_raw / odds_raw.sum() if odds_raw.sum() > 0 else odds_raw
        decimal_odds = d["decimal_odds"]
        choice = d["choice"]
        field_size = d["field_size"]
        year = d["year"]
        hhi = float((combined ** 2).sum())

        fav_idx = choice.index(1) if 1 in choice else None

        race_buckets = {
            "combined": defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
            "odds":     defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
        }

        # Enumerate ordered combos of size n_pos
        from itertools import permutations
        for combo in permutations(range(n), n_pos):
            # fav_position: 1-indexed slot of fav_idx in combo, None if absent
            if fav_idx is None or fav_idx not in combo:
                fav_pos = None
            else:
                fav_pos = combo.index(fav_idx) + 1

            combo_odds = [decimal_odds[i] for i in combo]
            if bet_type == "EXACTA":
                proj_pay = project_fn(combo_odds[0], combo_odds[1],
                                      pool, field_size, hhi, 1, fav_pos)
            elif bet_type == "TRIFECTA":
                proj_pay = project_fn(combo_odds[0], combo_odds[1], combo_odds[2],
                                      pool, field_size, hhi, 1, fav_pos)
            elif bet_type == "SUPERFECTA":
                proj_pay = project_fn(combo_odds[0], combo_odds[1], combo_odds[2], combo_odds[3],
                                      pool, field_size, hhi, 1, fav_pos)
            else:
                continue
            if proj_pay is None or proj_pay <= 0:
                continue

            is_winner = combo == winner_idx
            for model_name, p_arr in [("combined", combined), ("odds", odds_norm)]:
                proj_p = stern_n_pos(p_arr, combo, STERN_K)
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
                key = (model_name, thr, year)
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

    print(f"  Done in {time.time()-t0:.0f}s")
    return aggregate_to_df(agg, bet_type)


def aggregate_to_df(agg: dict, bet_type: str) -> pd.DataFrame:
    rows = []
    for (model, thr, year), a in agg.items():
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
        else:
            ci_lo = ci_hi = None
        rows.append({
            "bet_type": bet_type, "model": model, "threshold": thr, "year": year,
            "n_races": n_races, "n_combos": int(a["n_combos"]),
            "n_winners": int(a["n_winners"]),
            "invested": round(a["invested"], 2),
            "returned": round(a["returned"], 2),
            "roi_pct": round(100 * roi, 2),
            "ci_lo_pct": None if ci_lo is None else round(100 * ci_lo, 2),
            "ci_hi_pct": None if ci_hi is None else round(100 * ci_hi, 2),
        })
    return pd.DataFrame(rows).sort_values(["bet_type", "model", "threshold", "year"]).reset_index(drop=True)


# ===== Horizontal exotics (PICK_3, PICK_4, ...) =====

def run_horizontal(bet_type: str, n_legs: int, project_fn,
                    start_year: int, end_year: int) -> pd.DataFrame:
    """Generic N-leg horizontal: combo = (winner of leg 1, ..., winner of leg N).
    Probability = product of per-leg Stern win probs. Projected payoff via
    the supplied project_fn (e.g. project_pick3_payoff or project_pick4_payoff).

    PICK_5 / PICK_6 would work in principle but combinatorial explosion makes
    them infeasible to enumerate exhaustively at this stage; defer to a
    sub-step that uses a tighter combo space.
    """
    print(f"Loading {bet_type} data for {start_year}-{end_year}...")
    t0 = time.time()
    with get_conn() as conn:
        legs = pd.read_sql("""
            SELECT e.id AS exotic_id, e.race_id AS leg1_race_id,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   e.pool::float AS pool,
                   erl.race_id AS leg_race_id, erl.leg_number,
                   EXTRACT(YEAR FROM r.date)::int AS year
            FROM handycapper.exotics e
            JOIN handycapper.exotic_race_legs erl ON erl.exotic_id = e.id
            JOIN handycapper.races r ON r.id = e.race_id
            JOIN handycapper.sim_candidates sc
              ON sc.track = r.track AND sc.date = r.date
            WHERE e.bet_type = %(bt)s
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND EXTRACT(YEAR FROM r.date) BETWEEN %(start)s AND %(end)s
        """, conn, params={"bt": bet_type, "start": start_year, "end": end_year})
    print(f"  {len(legs):,} leg rows in {time.time()-t0:.1f}s")

    by_id = defaultdict(lambda: {"legs": {}, "pay_per_1": None, "pool": None, "year": None})
    for row in legs.itertuples():
        eid = int(row.exotic_id)
        d = by_id[eid]
        d["pay_per_1"] = float(row.pay_per_1)
        d["pool"] = float(row.pool)
        d["year"] = int(row.year)
        d["legs"][int(row.leg_number)] = int(row.leg_race_id)
    complete = {eid: d for eid, d in by_id.items() if len(d["legs"]) == n_legs}
    print(f"  {len(complete):,} {bet_type} results with {n_legs} legs")

    # Pull per-starter probs/odds for all relevant races
    leg_race_ids = sorted({rid for d in complete.values() for rid in d["legs"].values()})
    print(f"  Loading per-starter data for {len(leg_race_ids):,} legs...")
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
            WHERE ma.race_id = ANY(%(ids)s)
              AND ma.combined_prob IS NOT NULL AND ma.odds_prob IS NOT NULL
              AND s.odds IS NOT NULL AND s.official_position IS NOT NULL
        """, conn, params={"ids": leg_race_ids})
    print(f"  {len(ma):,} starter rows in {time.time()-t0:.1f}s")

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

    agg = defaultdict(lambda: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                                "n_winners": 0, "race_nets": [], "race_invested": []})

    print(f"Streaming {bet_type}...")
    t0 = time.time()
    n_processed = 0
    from itertools import product
    for eid, d in complete.items():
        leg_data = []
        skip = False
        for lnum in range(1, n_legs + 1):
            lrid = d["legs"][lnum]
            ld = races.get(lrid)
            if ld is None or len(ld["combined"]) < 2:
                skip = True
                break
            leg_data.append(ld)
        if skip:
            continue

        try:
            winner_idx = tuple(ld["finish"].index(1) for ld in leg_data)
        except ValueError:
            continue

        avg_field_size = float(np.mean([ld["field_size"] for ld in leg_data]))
        avg_hhi = float(np.mean([
            (np.asarray(ld["combined"]) ** 2).sum() for ld in leg_data
        ]))

        # Per-leg favorite indices (None if no favorite identifiable)
        fav_idx_per_leg = []
        for ld in leg_data:
            if 1 in ld["choice"]:
                fav_idx_per_leg.append(ld["choice"].index(1))
            else:
                fav_idx_per_leg.append(None)

        # Pre-compute Stern-power arrays per leg per model (avoid recomputing inside the loop)
        leg_p_k = {"combined": [], "odds": []}
        for ld in leg_data:
            comb = np.asarray(ld["combined"])
            leg_p_k["combined"].append(comb ** STERN_K)
            odds = np.asarray(ld["odds_p"])
            odds_norm = odds / odds.sum() if odds.sum() > 0 else odds
            leg_p_k["odds"].append(odds_norm ** STERN_K)
        leg_totals = {model: [arr.sum() for arr in leg_p_k[model]] for model in leg_p_k}

        race_buckets = {
            "combined": defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
            "odds":     defaultdict(lambda: {"invested": 0.0, "returned": 0.0,
                                              "n_combos": 0, "n_winners": 0}),
        }

        leg_sizes = [len(ld["combined"]) for ld in leg_data]
        for combo in product(*[range(s) for s in leg_sizes]):
            is_winner = combo == winner_idx

            bad = sum(1 for k in range(n_legs)
                      if fav_idx_per_leg[k] is not None and combo[k] != fav_idx_per_leg[k])
            leg_winner_odds = [leg_data[k]["decimal_odds"][combo[k]] for k in range(n_legs)]

            proj_pay = project_fn(leg_winner_odds, d["pool"], avg_hhi,
                                   avg_field_size, bad)
            if proj_pay is None or proj_pay <= 0:
                continue

            for model_name in ("combined", "odds"):
                proj_p = 1.0
                ok = True
                for k in range(n_legs):
                    total = leg_totals[model_name][k]
                    if total <= 0:
                        ok = False
                        break
                    proj_p *= leg_p_k[model_name][k][combo[k]] / total
                if not ok or proj_p <= 0:
                    continue
                er = proj_p * proj_pay
                for thr in ER_THRESHOLDS:
                    if er < thr:
                        continue
                    rb = race_buckets[model_name][thr]
                    rb["n_combos"] += 1
                    rb["invested"] += 1.0
                    if is_winner:
                        rb["returned"] += d["pay_per_1"]
                        rb["n_winners"] += 1

        for model_name, by_thr in race_buckets.items():
            for thr, rb in by_thr.items():
                key = (model_name, thr, d["year"])
                a = agg[key]
                a["n_combos"] += rb["n_combos"]
                a["invested"] += rb["invested"]
                a["returned"] += rb["returned"]
                a["n_winners"] += rb["n_winners"]
                a["race_nets"].append(rb["returned"] - rb["invested"])
                a["race_invested"].append(rb["invested"])

        n_processed += 1
        if n_processed % 10000 == 0:
            print(f"  {n_processed:,}/{len(complete):,} {bet_type}s, "
                  f"{time.time()-t0:.0f}s elapsed")

    print(f"  Done in {time.time()-t0:.0f}s")
    return aggregate_to_df(agg, bet_type)


def print_per_year_table(df: pd.DataFrame, bet_type: str):
    sub = df[df["bet_type"] == bet_type]
    if sub.empty:
        return
    years = sorted(sub["year"].dropna().unique().astype(int))
    HIGHLIGHT = [1.00, 1.10, 1.30, 2.00]
    print(f"\n=== {bet_type} per-year ROI (PROJECTED-payoff filter) ===")
    print(f"{'model':<10}{'thr':>5} ", end="")
    for y in years:
        print(f"{y:>9}", end="")
    print(f" {'mean':>8} {'+yrs':>6}")
    for model in ["combined", "odds"]:
        for thr in HIGHLIGHT:
            row = f"{model:<10}{thr:>5.2f} "
            yr_rois = []
            for y in years:
                m = sub[(sub["model"] == model) & (sub["threshold"] == thr) & (sub["year"] == y)]
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start-year", type=int, default=2010)
    ap.add_argument("--end-year",   type=int, default=2016)
    ap.add_argument("--bet-types",  default="EXACTA,TRIFECTA,SUPERFECTA,PICK_3",
                    help="Comma-sep subset")
    args = ap.parse_args()
    bts = [b.strip().upper() for b in args.bet_types.split(",")]

    all_dfs = []
    if "EXACTA" in bts:
        ma = load_vertical_data(args.start_year, args.end_year)
        ex = load_vertical_payoffs("EXACTA", args.start_year, args.end_year)
        df = run_vertical("EXACTA", 2, ma, ex, project_exacta_payoff)
        df.to_csv(TMP / "honest_overlay_per_year_EXACTA.csv", index=False)
        all_dfs.append(df)
        print_per_year_table(df, "EXACTA")
    if "TRIFECTA" in bts:
        ma = load_vertical_data(args.start_year, args.end_year)
        ex = load_vertical_payoffs("TRIFECTA", args.start_year, args.end_year)
        df = run_vertical("TRIFECTA", 3, ma, ex, project_trifecta_payoff)
        df.to_csv(TMP / "honest_overlay_per_year_TRIFECTA.csv", index=False)
        all_dfs.append(df)
        print_per_year_table(df, "TRIFECTA")
    if "SUPERFECTA" in bts:
        ma = load_vertical_data(args.start_year, args.end_year)
        ex = load_vertical_payoffs("SUPERFECTA", args.start_year, args.end_year)
        df = run_vertical("SUPERFECTA", 4, ma, ex, project_superfecta_payoff)
        df.to_csv(TMP / "honest_overlay_per_year_SUPERFECTA.csv", index=False)
        all_dfs.append(df)
        print_per_year_table(df, "SUPERFECTA")
    if "PICK_3" in bts:
        df = run_horizontal("PICK_3", 3, project_pick3_payoff,
                             args.start_year, args.end_year)
        df.to_csv(TMP / "honest_overlay_per_year_PICK_3.csv", index=False)
        all_dfs.append(df)
        print_per_year_table(df, "PICK_3")
    if "PICK_4" in bts:
        df = run_horizontal("PICK_4", 4, project_pick4_payoff,
                             args.start_year, args.end_year)
        df.to_csv(TMP / "honest_overlay_per_year_PICK_4.csv", index=False)
        all_dfs.append(df)
        print_per_year_table(df, "PICK_4")

    print("\nDone.")


if __name__ == "__main__":
    main()
