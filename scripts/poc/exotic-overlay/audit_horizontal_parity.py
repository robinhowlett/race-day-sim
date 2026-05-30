"""Audit horizontal (PICK_3 / PICK_4) overlay filter parity.

For a sample sim_candidates day, run the SAME race through both:
  (A) Production code: SimDay.exotic_overlay_filter(rn, "PICK_3" / "PICK_4")
  (B) POC code: the logic from 04_honest_overlay_per_year.py

Compare combo-by-combo:
  - Set of combos passing ER ≥ 1.30 — same in both?
  - Per-combo harv_prob — match?
  - Per-combo proj_pay — match?
  - Per-combo ER — match?
  - For combos that pass: how does production's choice align with chart winner?

If both produce identical combos+ER, the production logic is faithful and
the horizontal failure is variance. If they diverge, the divergence
identifies the bug.

Dumps per-combo detail to tmp/audit_horizontal_<date>_<track>.csv for
manual inspection.
"""

import os
import sys
import time
from collections import defaultdict
from itertools import product
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

POC_DIR = Path(__file__).resolve().parent
TMP = POC_DIR / "tmp"

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from sim.payoff import project_pick3_payoff, project_pick4_payoff  # noqa
from sim.probability import STERN_K  # noqa
from run_simulation import SimDay  # noqa

ER_THRESHOLD = 1.30

# Pick a sample day from the batch result that had horizontal action
SAMPLE_TRACK = "PIM"
SAMPLE_DATE = "2016-05-26"


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5434"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def poc_horizontal_combos(track: str, race_date: str, bet_type: str, n_legs: int):
    """Mirror 04_honest_overlay_per_year.py's run_horizontal logic for a single
    track-day. Returns combos passing ER ≥ 1.30.

    Distinct from production by being a from-SQL pull rather than going through
    the SimDay loaders. If the production filter and this both agree, parity holds.
    """
    project_fn = project_pick3_payoff if bet_type == "PICK_3" else project_pick4_payoff

    with get_conn() as conn:
        # Pull the Pick-N exotics from this day
        legs_df = pd.read_sql("""
            SELECT e.id AS exotic_id, e.race_id AS leg1_race_id,
                   (e.payoff / NULLIF(e.unit, 0))::float AS pay_per_1,
                   e.pool::float AS pool,
                   e.winning_numbers,
                   erl.race_id AS leg_race_id, erl.leg_number,
                   r.number AS leg_race_number
            FROM handycapper.exotics e
            JOIN handycapper.exotic_race_legs erl ON erl.exotic_id = e.id
            JOIN handycapper.races r ON r.id = e.race_id
            WHERE e.bet_type = %(bt)s
              AND e.pool_type = 'STANDARD'
              AND e.payoff > 0 AND e.unit > 0
              AND r.track = %(track)s AND r.date = %(date)s
            ORDER BY e.id, erl.leg_number
        """, conn, params={"bt": bet_type, "track": track, "date": race_date})
        if legs_df.empty:
            return []

        # Pull per-starter probs/odds for all relevant leg races
        leg_race_ids = sorted(set(legs_df["leg_race_id"].tolist()))
        ma = pd.read_sql("""
            SELECT ma.race_id, ma.starter_id,
                   ma.combined_prob::float, ma.odds_prob::float,
                   s.odds::float AS decimal_odds,
                   s.choice::int AS choice,
                   s.official_position::int AS finish_pos,
                   s.program::text AS program,
                   r.number AS race_number,
                   r.number_of_runners::int AS field_size
            FROM handycapper.rkm_market_analysis ma
            JOIN handycapper.starters s ON s.id = ma.starter_id
            JOIN handycapper.races r ON r.id = ma.race_id
            WHERE ma.race_id = ANY(%(ids)s)
              AND ma.combined_prob IS NOT NULL AND ma.odds_prob IS NOT NULL
              AND s.odds IS NOT NULL AND s.official_position IS NOT NULL
        """, conn, params={"ids": leg_race_ids})

    races = defaultdict(lambda: {"combined": [], "decimal_odds": [],
                                   "choice": [], "finish": [], "programs": [],
                                   "field_size": 0, "race_number": None})
    for rid, c, op, do, ch, fp, pgm, rn, fs in zip(
        ma["race_id"], ma["combined_prob"], ma["odds_prob"], ma["decimal_odds"],
        ma["choice"], ma["finish_pos"], ma["program"], ma["race_number"],
        ma["field_size"],
    ):
        d = races[int(rid)]
        d["combined"].append(float(c))
        d["decimal_odds"].append(float(do))
        d["choice"].append(int(ch) if pd.notna(ch) else 99)
        d["finish"].append(int(fp))
        d["programs"].append(str(pgm))
        d["field_size"] = int(fs)
        d["race_number"] = int(rn)

    # Group legs by exotic_id
    by_id = defaultdict(lambda: {"legs": {}, "pay_per_1": None, "pool": None,
                                   "winning_numbers": None, "leg1_race_number": None})
    for row in legs_df.itertuples():
        eid = int(row.exotic_id)
        d = by_id[eid]
        d["pay_per_1"] = float(row.pay_per_1)
        d["pool"] = float(row.pool)
        d["winning_numbers"] = row.winning_numbers
        d["legs"][int(row.leg_number)] = int(row.leg_race_id)
        if int(row.leg_number) == 1:
            d["leg1_race_number"] = int(row.leg_race_number)

    out_combos = []
    for eid, d in by_id.items():
        if len(d["legs"]) != n_legs:
            continue
        leg_data = []
        skip = False
        for lnum in range(1, n_legs + 1):
            ld = races.get(d["legs"][lnum])
            if ld is None or len(ld["combined"]) < 2:
                skip = True
                break
            leg_data.append(ld)
        if skip:
            continue

        avg_field = float(np.mean([ld["field_size"] for ld in leg_data]))
        avg_hhi = float(np.mean([
            (np.asarray(ld["combined"]) ** 2).sum() for ld in leg_data
        ]))

        # Per-leg fav idx
        fav_idx_per_leg = []
        for ld in leg_data:
            fav_idx_per_leg.append(ld["choice"].index(1) if 1 in ld["choice"] else None)

        # Pre-compute Stern-power
        leg_p_k = []
        for ld in leg_data:
            arr = np.asarray(ld["combined"], dtype=float)
            p_k = arr ** STERN_K
            leg_p_k.append((p_k, p_k.sum()))

        leg_sizes = [ld["field_size"] for ld in leg_data]
        winners = tuple(ld["finish"].index(1) for ld in leg_data)
        winner_pgms = tuple(leg_data[k]["programs"][winners[k]] for k in range(n_legs))

        for combo in product(*[range(s) for s in leg_sizes]):
            parlay = 1.0
            for k in range(n_legs):
                p_k, total = leg_p_k[k]
                if total <= 0:
                    parlay = 0.0
                    break
                parlay *= p_k[combo[k]] / total
            if parlay <= 0:
                continue
            bad_fav = sum(
                1 for k in range(n_legs)
                if fav_idx_per_leg[k] is not None and combo[k] != fav_idx_per_leg[k]
            )
            leg_winner_odds = [leg_data[k]["decimal_odds"][combo[k]] for k in range(n_legs)]
            proj_pay = project_fn(leg_winner_odds, d["pool"], avg_hhi, avg_field, bad_fav)
            if proj_pay is None or proj_pay <= 0:
                continue
            er = parlay * proj_pay
            if er < ER_THRESHOLD:
                continue

            programs = tuple(leg_data[k]["programs"][combo[k]] for k in range(n_legs))
            is_winner = combo == winners

            out_combos.append({
                "exotic_id": eid,
                "leg1_rn": d["leg1_race_number"],
                "programs": programs,
                "indices": combo,
                "harv_prob": parlay,
                "proj_pay": proj_pay,
                "er": er,
                "is_winner": is_winner,
                "chart_pay_per_1": d["pay_per_1"],
                "chart_winning_numbers": d["winning_numbers"],
                "actual_winners": winner_pgms,
                "pool": d["pool"],
            })
    return out_combos


def production_horizontal_combos(sim: SimDay, bet_type: str, n_legs: int):
    """Run SimDay.exotic_overlay_filter for every starting race that has N legs ahead.

    Tags each combo with the leg1 race_number for matching against POC output.
    """
    race_numbers = sorted(int(r) for r in sim.card["race_number"].unique())
    if not race_numbers:
        return []
    max_rn = race_numbers[-1]
    out = []
    for rn in race_numbers:
        if rn + n_legs - 1 > max_rn:
            continue
        combos = sim.exotic_overlay_filter(rn, bet_type, er_threshold=ER_THRESHOLD)
        for c in combos:
            out.append({
                "leg1_rn": rn,
                "programs": tuple(c["programs"]),
                "harv_prob": c["harv_prob"],
                "proj_pay": c["proj_pay"],
                "er": c["er"],
            })
    return out


def diff_combo_sets(prod, poc, bet_type):
    """Index by (leg1_rn, programs) and surface mismatches."""
    prod_idx = {(c["leg1_rn"], c["programs"]): c for c in prod}
    poc_idx = {(c["leg1_rn"], c["programs"]): c for c in poc}

    only_prod = set(prod_idx) - set(poc_idx)
    only_poc = set(poc_idx) - set(prod_idx)
    both = set(prod_idx) & set(poc_idx)

    print(f"\n=== {bet_type} parity ===")
    print(f"  Production combos: {len(prod_idx)}")
    print(f"  POC combos:        {len(poc_idx)}")
    print(f"  Both:              {len(both)}")
    print(f"  Only production:   {len(only_prod)}")
    print(f"  Only POC:          {len(only_poc)}")

    if only_prod:
        print(f"\n  First 5 ONLY in production:")
        for k in list(only_prod)[:5]:
            c = prod_idx[k]
            print(f"    leg1_rn={k[0]} programs={k[1]} ER={c['er']:.3f} "
                  f"harv={c['harv_prob']:.5f} proj=${c['proj_pay']:.0f}")
    if only_poc:
        print(f"\n  First 5 ONLY in POC:")
        for k in list(only_poc)[:5]:
            c = poc_idx[k]
            print(f"    leg1_rn={k[0]} programs={k[1]} ER={c['er']:.3f} "
                  f"harv={c['harv_prob']:.5f} proj=${c['proj_pay']:.0f}")

    # For combos in both, check numerical agreement
    n_disagree = 0
    for k in list(both)[:1000]:
        p, q = prod_idx[k], poc_idx[k]
        if (abs(p["harv_prob"] - q["harv_prob"]) > 1e-6
                or abs(p["proj_pay"] - q["proj_pay"]) > 0.01
                or abs(p["er"] - q["er"]) > 0.01):
            n_disagree += 1
    print(f"  Disagreements (numerical) in first 1000 of both: {n_disagree}")
    if n_disagree:
        for k in list(both)[:5]:
            p, q = prod_idx[k], poc_idx[k]
            print(f"    {k}: prod ER={p['er']:.3f} POC ER={q['er']:.3f}, "
                  f"prod harv={p['harv_prob']:.6f} POC harv={q['harv_prob']:.6f}, "
                  f"prod proj=${p['proj_pay']:.2f} POC proj=${q['proj_pay']:.2f}")

    return prod_idx, poc_idx


def main():
    track = SAMPLE_TRACK
    date = SAMPLE_DATE
    print(f"Auditing {track} {date}")

    print(f"\nLoading SimDay...")
    t0 = time.time()
    conn = get_conn()
    sim = SimDay(track, date)
    sim.load(conn)
    print(f"  loaded in {time.time()-t0:.1f}s, {sim.card['race_number'].nunique()} races")

    for bet_type, n_legs in [("PICK_3", 3), ("PICK_4", 4)]:
        print(f"\n--- Running {bet_type} via production filter ---")
        t0 = time.time()
        prod = production_horizontal_combos(sim, bet_type, n_legs)
        print(f"  {len(prod)} combos in {time.time()-t0:.1f}s")

        print(f"--- Running {bet_type} via POC logic ---")
        t0 = time.time()
        poc = poc_horizontal_combos(track, date, bet_type, n_legs)
        print(f"  {len(poc)} combos in {time.time()-t0:.1f}s")

        prod_idx, poc_idx = diff_combo_sets(prod, poc, bet_type)

        # Dump POC-side full detail (with hit/miss + payoff) for manual inspection
        out_rows = []
        for k, c in poc_idx.items():
            in_prod = k in prod_idx
            out_rows.append({
                "bet_type": bet_type,
                "leg1_rn": k[0],
                "programs": "/".join(k[1]),
                "actual_winners": "/".join(c["actual_winners"]),
                "is_winner": c["is_winner"],
                "harv_prob": c["harv_prob"],
                "proj_pay": c["proj_pay"],
                "er": c["er"],
                "chart_pay_per_1": c["chart_pay_per_1"],
                "in_production_filter": in_prod,
                "pool": c["pool"],
            })
        df = pd.DataFrame(out_rows)
        out_path = TMP / f"audit_horizontal_{bet_type}_{date}_{track}.csv"
        df.to_csv(out_path, index=False)

        if df.empty:
            print(f"  (no POC combos to dump)")
            continue
        winners_in_filter = df[df["is_winner"]]
        print(f"\n  Winners that passed filter: {len(winners_in_filter)}/{df['is_winner'].sum() if df['is_winner'].any() else 0}")
        if not winners_in_filter.empty:
            for _, r in winners_in_filter.iterrows():
                print(f"    leg1_rn={r['leg1_rn']} {r['programs']} → "
                      f"ER={r['er']:.2f} proj_pay=${r['proj_pay']:.2f} "
                      f"actual_pay_per_1=${r['chart_pay_per_1']:.2f}")

        print(f"\n  Wrote {out_path}")


if __name__ == "__main__":
    main()
