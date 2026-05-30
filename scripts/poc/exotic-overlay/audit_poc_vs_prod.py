"""POC vs Production parity audit, n=200 same days.

Runs the POC's 04_honest_overlay_per_year.py vertical-overlay logic on the
EXACT same 200 (track, date) pairs production just ran, then compares per-
bet-type bet count, hit rate, and ROI side-by-side.

Logic:
  - Pull rkm_market_analysis + chart payoffs ONLY for the 200 days
  - Mirror 04_honest_overlay_per_year.py:run_vertical exactly:
      ER = stern_harville(combined_prob, k=0.86) × project_payoff()
      Filter combos by ER ≥ threshold; bet $1; payoff if winning combo

If POC produces the same near-zero ROI as production → POC's original
7-year headline was an artifact of the wider 162K-race sample (selection
bias somewhere). If POC produces +30%+ on the same 200 days while
production produced takeout, there's a methodology bug between them.

Output: tmp/audit_poc_vs_prod.csv
"""

import json
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

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
from sim.payoff import (project_exacta_payoff, project_trifecta_payoff,
                          project_superfecta_payoff)  # noqa

STERN_K = 0.86
ER_THRESHOLDS = [1.30, 2.00]
VERTICAL = {"EXACTA": (2, project_exacta_payoff),
            "TRIFECTA": (3, project_trifecta_payoff),
            "SUPERFECTA": (4, project_superfecta_payoff)}


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def load_batch_days(batch_json_path: str):
    """Pull (track, date) pairs from a production batch JSON."""
    with open(batch_json_path) as f:
        d = json.load(f)
    return [(p['track'], p['date']) for p in d['per_day']]


def stern_harville(p_array: np.ndarray, idx: tuple, k: float) -> float:
    p_k = p_array ** k
    remaining = p_k.sum()
    prob = 1.0
    for i in idx:
        if remaining <= 0:
            return 0.0
        prob *= p_k[i] / remaining
        remaining -= p_k[i]
    return prob


def run_vertical_for_days(bet_type: str, n_pos: int, project_fn,
                           days: list[tuple[str, str]],
                           er_thresholds: list[float]):
    """POC-style vertical overlay restricted to the supplied days.

    Mirrors the structure of 04_honest_overlay_per_year.py:run_vertical
    but with a track-date filter.
    """
    if not days:
        return {}

    # Build a parameterized IN-list
    track_dates = [{"t": t, "d": dt} for t, dt in days]
    days_df = pd.DataFrame(track_dates)

    print(f"  Loading {bet_type} starter + payoff data for {len(days)} days...")
    t0 = time.time()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TEMP TABLE _audit_days (track varchar(8), race_date date)
                ON COMMIT DROP
            """)
            for t, dt in days:
                cur.execute("INSERT INTO _audit_days VALUES (%s, %s::date)", (t, dt))

            ma = pd.read_sql("""
                SELECT ma.race_id, ma.starter_id,
                       ma.combined_prob::float, ma.odds_prob::float,
                       s.odds::float AS decimal_odds,
                       s.choice::int AS choice,
                       s.official_position::int AS finish_pos,
                       r.number_of_runners::int AS field_size,
                       r.track, r.date
                FROM handycapper.rkm_market_analysis ma
                JOIN handycapper.starters s ON s.id = ma.starter_id
                JOIN handycapper.races r ON r.id = ma.race_id
                JOIN _audit_days d ON d.track = r.track AND d.race_date = r.date
                WHERE ma.combined_prob IS NOT NULL
                  AND ma.odds_prob IS NOT NULL
                  AND s.odds IS NOT NULL
                  AND s.official_position IS NOT NULL
            """, conn)
            ex = pd.read_sql("""
                SELECT e.race_id,
                       (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                       e.pool::float AS pool
                FROM handycapper.exotics e
                JOIN handycapper.races r ON r.id = e.race_id
                JOIN _audit_days d ON d.track = r.track AND d.race_date = r.date
                WHERE e.bet_type = %(bt)s
                  AND e.pool_type = 'STANDARD'
                  AND e.payoff > 0 AND e.unit > 0
            """, conn, params={"bt": bet_type})
    print(f"    {len(ma):,} starters / {len(ex):,} payoffs in {time.time()-t0:.1f}s")

    if ma.empty or ex.empty:
        return {}

    pay_lookup = {int(r.race_id): (float(r.pay_per_1), float(r.pool))
                  for r in ex.itertuples()}

    races = defaultdict(lambda: {
        "combined": [], "odds_p": [], "decimal_odds": [],
        "choice": [], "finish": [], "field_size": 0,
    })
    for rid, c, op, do, ch, fp, fs in zip(
        ma["race_id"], ma["combined_prob"], ma["odds_prob"],
        ma["decimal_odds"], ma["choice"], ma["finish_pos"], ma["field_size"]
    ):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["odds_p"].append(float(op))
        d["decimal_odds"].append(float(do))
        d["choice"].append(int(ch) if pd.notna(ch) else 99)
        d["finish"].append(int(fp))
        d["field_size"] = int(fs)

    # Aggregate per (threshold) → totals
    agg = {thr: {"n_combos": 0, "invested": 0.0, "returned": 0.0,
                  "n_winners": 0, "race_nets": [], "race_invested": []}
           for thr in er_thresholds}

    print(f"  Streaming {bet_type} ({len(races):,} races)...")
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
        decimal_odds = d["decimal_odds"]
        choice = d["choice"]
        field_size = d["field_size"]
        hhi = float((combined ** 2).sum())
        fav_idx = choice.index(1) if 1 in choice else None

        race_buckets = {thr: {"invested": 0.0, "returned": 0.0,
                               "n_combos": 0, "n_winners": 0}
                        for thr in er_thresholds}

        for combo in permutations(range(n), n_pos):
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
                proj_pay = project_fn(combo_odds[0], combo_odds[1],
                                       combo_odds[2], combo_odds[3],
                                       pool, field_size, hhi, 1, fav_pos)
            else:
                continue
            if proj_pay is None or proj_pay <= 0:
                continue

            harv_prob = stern_harville(combined, combo, STERN_K)
            if harv_prob <= 0:
                continue
            er = harv_prob * proj_pay

            is_winner = combo == winner_idx
            for thr in er_thresholds:
                if er < thr:
                    continue
                rb = race_buckets[thr]
                rb["n_combos"] += 1
                rb["invested"] += 1.0
                if is_winner:
                    rb["returned"] += chart_payoff
                    rb["n_winners"] += 1

        for thr, rb in race_buckets.items():
            if rb["n_combos"] == 0:
                continue
            a = agg[thr]
            a["n_combos"] += rb["n_combos"]
            a["invested"] += rb["invested"]
            a["returned"] += rb["returned"]
            a["n_winners"] += rb["n_winners"]
            a["race_nets"].append(rb["returned"] - rb["invested"])
            a["race_invested"].append(rb["invested"])

        n_processed += 1
        if n_processed % 200 == 0:
            print(f"    {n_processed:,}/{len(races):,} races, {time.time()-t0:.0f}s")

    print(f"    done in {time.time()-t0:.0f}s")
    return agg


def main():
    # Batch JSONs land in race-day-sim/tmp/ (top-level), not POC tmp
    repo_tmp = Path(__file__).resolve().parents[3] / "tmp"
    batch_path = repo_tmp / "batch_exotic_200_FIXED3.json"
    if not batch_path.exists():
        batch_path = repo_tmp / "batch_exotic_200_ER2.json"
    print(f"Using batch days from: {batch_path}")
    days = load_batch_days(str(batch_path))
    print(f"Loaded {len(days)} (track, date) pairs")
    print()

    rows = []
    for bet_type, (n_pos, project_fn) in VERTICAL.items():
        print(f"=== {bet_type} ===")
        agg = run_vertical_for_days(bet_type, n_pos, project_fn, days, ER_THRESHOLDS)
        for thr, a in agg.items():
            if a["invested"] <= 0:
                rows.append({"bet_type": bet_type, "threshold": thr,
                             "n_combos": 0, "n_winners": 0,
                             "invested": 0, "returned": 0, "roi_pct": None})
                continue
            roi = a["returned"] / a["invested"] - 1
            race_nets = np.asarray(a["race_nets"])
            race_inv = np.asarray(a["race_invested"])
            n_races = len(race_nets)
            if n_races > 1 and race_inv.sum() > 0:
                per_race_roi = race_nets / np.maximum(race_inv, 1e-9)
                se = float(per_race_roi.std(ddof=1) / math.sqrt(n_races))
                ci_lo, ci_hi = roi - 1.96*se, roi + 1.96*se
            else:
                ci_lo = ci_hi = None
            rows.append({
                "bet_type": bet_type, "threshold": thr,
                "n_races_with_play": n_races,
                "n_combos": int(a["n_combos"]),
                "n_winners": int(a["n_winners"]),
                "invested": round(a["invested"], 2),
                "returned": round(a["returned"], 2),
                "roi_pct": round(100 * roi, 2),
                "ci_lo_pct": None if ci_lo is None else round(100 * ci_lo, 2),
                "ci_hi_pct": None if ci_hi is None else round(100 * ci_hi, 2),
            })

    df = pd.DataFrame(rows)
    out = TMP / "audit_poc_vs_prod.csv"
    df.to_csv(out, index=False)
    print()
    print("=== POC ROI on the same 200 days production ran ===")
    print(f"{'bet_type':<12} {'thr':>5} {'combos':>9} {'winners':>8}"
          f" {'invested':>11} {'returned':>11} {'ROI':>9} {'95% CI':>22}")
    print("-" * 92)
    for _, r in df.iterrows():
        if r['roi_pct'] is None:
            continue
        ci = (f"({r['ci_lo_pct']:+6.1f}%, {r['ci_hi_pct']:+6.1f}%)"
              if r['ci_lo_pct'] is not None else "—")
        print(f"{r['bet_type']:<12} {r['threshold']:>5.2f} {int(r['n_combos']):>9,}"
              f" {int(r['n_winners']):>8} ${r['invested']:>9,.0f} ${r['returned']:>9,.0f}"
              f" {r['roi_pct']:>+8.2f}% {ci:>22}")
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
