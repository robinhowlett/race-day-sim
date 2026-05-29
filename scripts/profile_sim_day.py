"""Profile SimDay.load() and per-race work to find hot spots.

Times each component of a single sim-day load to figure out where
to spend optimization effort. Picks 3 deterministic days from
sim_candidates so the result is stable across runs.
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim.blinder import load_market_bias, load_pool_sizes, load_pre_race_card
from sim.db import get_connection
from sim.ratings import format_race_ratings
from run_simulation import SimDay


DAYS = [
    ("FL", "2013-08-10"),
    ("DED", "2009-11-13"),
    ("TUP", "2011-02-09"),
]


def profile_one_day(conn, track: str, date: str) -> dict:
    """Time individual sim_day operations."""
    timings = {}

    t0 = time.time()
    card = load_pre_race_card(conn, track, date)
    timings["load_pre_race_card"] = time.time() - t0

    t0 = time.time()
    pools = load_pool_sizes(conn, track, date)
    timings["load_pool_sizes"] = time.time() - t0

    t0 = time.time()
    bias = load_market_bias(conn, track, date)
    timings["load_market_bias"] = time.time() - t0

    timings["per_race_ratings"] = []
    timings["per_race_combined_probs"] = []
    timings["per_race_protocol_check"] = []
    timings["per_race_classify_opinion"] = []
    timings["per_race_flb_filter"] = []

    sim = SimDay(track, date)
    sim.card = card
    sim.pools = pools
    sim.bias = bias

    for rn in sorted(card["race_number"].unique()):
        rn = int(rn)

        t0 = time.time()
        sim.ratings[rn] = format_race_ratings(card, bias, rn)
        timings["per_race_ratings"].append(time.time() - t0)

        t0 = time.time()
        sim.combined_probs(rn)
        timings["per_race_combined_probs"].append(time.time() - t0)

        t0 = time.time()
        sim.flb_filter(rn)
        timings["per_race_flb_filter"].append(time.time() - t0)

        t0 = time.time()
        sim.protocol_check(rn)
        timings["per_race_protocol_check"].append(time.time() - t0)

        t0 = time.time()
        sim.classify_opinion(rn)
        timings["per_race_classify_opinion"].append(time.time() - t0)

    return {
        "track": track, "date": date,
        "n_races": int(card["race_number"].nunique()),
        "n_starters": len(card),
        "timings": timings,
    }


def fmt_ms(seconds: float) -> str:
    return f"{seconds*1000:>7.0f}ms"


def main():
    conn = get_connection()

    print(f"\n{'Day':<18} {'#R':>3}  {'card':>10} {'pools':>10} {'bias':>10} "
          f"{'rate (sum)':>11} {'cprob (sum)':>11} {'flb (sum)':>11} "
          f"{'check (sum)':>11} {'opin (sum)':>11} {'TOTAL':>9}")
    print("-" * 145)

    grand = {}
    for track, date in DAYS:
        r = profile_one_day(conn, track, date)
        t = r["timings"]
        per_race_sums = {
            k: sum(t[k]) for k in
            ("per_race_ratings", "per_race_combined_probs",
             "per_race_flb_filter", "per_race_protocol_check",
             "per_race_classify_opinion")
        }
        total = (t["load_pre_race_card"] + t["load_pool_sizes"] + t["load_market_bias"]
                 + sum(per_race_sums.values()))
        print(f"{r['track']+' '+r['date']:<18} {r['n_races']:>3}  "
              f"{fmt_ms(t['load_pre_race_card']):>10} "
              f"{fmt_ms(t['load_pool_sizes']):>10} "
              f"{fmt_ms(t['load_market_bias']):>10} "
              f"{fmt_ms(per_race_sums['per_race_ratings']):>11} "
              f"{fmt_ms(per_race_sums['per_race_combined_probs']):>11} "
              f"{fmt_ms(per_race_sums['per_race_flb_filter']):>11} "
              f"{fmt_ms(per_race_sums['per_race_protocol_check']):>11} "
              f"{fmt_ms(per_race_sums['per_race_classify_opinion']):>11} "
              f"{total:>7.2f}s")

        grand.setdefault("load_card", 0); grand["load_card"] += t["load_pre_race_card"]
        grand.setdefault("load_pools", 0); grand["load_pools"] += t["load_pool_sizes"]
        grand.setdefault("load_bias", 0); grand["load_bias"] += t["load_market_bias"]
        for k, v in per_race_sums.items():
            grand.setdefault(k, 0)
            grand[k] += v

    print()
    print(f"{'Component':<28} {'avg/day':>10} {'% of total':>12}")
    print("-" * 55)
    total = sum(grand.values())
    n = len(DAYS)
    for k, v in sorted(grand.items(), key=lambda kv: -kv[1]):
        avg = v / n
        pct = 100 * v / total if total else 0
        print(f"{k:<28} {fmt_ms(avg):>10} {pct:>10.1f}%")
    print(f"{'TOTAL avg/day':<28} {fmt_ms(total/n):>10}")


if __name__ == "__main__":
    main()
