#!/usr/bin/env python3
"""Run a blinded race day simulation.

Usage:
    python scripts/simulate_race_day.py --track GP --date 2014-09-06

Outputs pre-race card, pool sizes, pace predictions, and model probabilities
for each race. Pauses for bet commitment (interactive) before revealing results.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sim.blinder import load_pool_sizes, load_pre_race_card, load_race_results
from sim.db import get_connection
from sim.evaluate import day_summary, evaluate_race
from sim.pace import predict_pace
from sim.probability import benter_combine, model_probs_from_curves, odds_to_probs


def print_header(track: str, date: str, n_races: int):
    print(f"\n{'='*70}")
    print(f"  RACE DAY SIMULATION: {track} — {date} ({n_races} races)")
    print(f"{'='*70}\n")


def print_pools(pools_df):
    print("\n--- POOL SIZES ---")
    for race_num in sorted(pools_df["race_number"].unique()):
        race_pools = pools_df[pools_df["race_number"] == race_num]
        parts = [f"R{race_num}:"]
        for _, row in race_pools.iterrows():
            parts.append(f"  {row['bet_type']:<11} ${row['pool']:>10,.0f}")
        print("  ".join(parts[:1]))
        for p in parts[1:]:
            print(f"       {p}")
    print()


def print_race(race_num: int, race_df, pace_result: dict, probs: dict):
    starters = race_df.sort_values("choice")
    meta = starters.iloc[0]
    print(f"\n{'─'*70}")
    print(f"RACE {race_num} | {meta['furlongs']}f {meta['surface']} | "
          f"{meta['race_type']} | ${meta['purse']:,.0f} | {meta['field_size']} starters")
    print(f"{'─'*70}")

    print(f"\n  Pace: {pace_result['scenario']} — {pace_result['narrative']}")
    print(f"  Speed count: {pace_result['speed_count']} | "
          f"Leader decay: {pace_result['leader_decay']:.2f} | "
          f"Field median decay: {pace_result['median_decay']:.2f}")

    print(f"\n  {'Pgm':<5}{'Horse':<24}{'Odds':>5} {'Ch':>3} | "
          f"{'cV0':>5} {'cDec':>6} {'Trend':>6} | "
          f"{'adjV0':>5} {'adjDec':>6} | "
          f"{'ModelP':>6} {'OddsP':>6} {'BentP':>6}")
    print(f"  {'-'*95}")

    for _, s in starters.iterrows():
        pgm = str(s["program"])
        horse = str(s["horse_name"])[:23]
        odds = f"{s['closing_odds']:.1f}" if s["closing_odds"] else "  -"
        ch = str(int(s["choice"])) if s["choice"] else "-"
        cv0 = f"{s['current_v0']:.1f}" if s["current_v0"] else "  -"
        cdec = f"{s['current_decay']:.2f}" if s["current_decay"] else "  -"
        trend = f"{s['v0_trend']:+.1f}" if s["v0_trend"] else "  -"
        av0 = f"{s['adj_v0']:.1f}" if s["adj_v0"] else "  -"
        adec = f"{s['adj_decay']:.2f}" if s.get("adj_decay") else "  -"

        idx = int(s.name) if hasattr(s, "name") else 0
        mp = f"{probs['model'][idx]*100:.1f}" if probs["model"] is not None and idx < len(probs["model"]) else "  -"
        op = f"{probs['odds'][idx]*100:.1f}" if probs["odds"] is not None and idx < len(probs["odds"]) else "  -"
        bp = f"{probs['benter'][idx]*100:.1f}" if probs["benter"] is not None and idx < len(probs["benter"]) else "  -"

        print(f"  {pgm:<5}{horse:<24}{odds:>5} {ch:>3} | "
              f"{cv0:>5} {cdec:>6} {trend:>6} | "
              f"{av0:>5} {adec:>6} | "
              f"{mp:>6} {op:>6} {bp:>6}")


def compute_race_probs(race_df, furlongs: float):
    """Compute model, odds, and Benter probabilities for a race."""
    has_curves = race_df["adj_v0"].notna() & race_df["decay_rate"].notna()

    if has_curves.sum() < 3:
        odds_probs = odds_to_probs(race_df["closing_odds"].fillna(99).tolist())
        return {"model": None, "odds": odds_probs, "benter": None}

    adj_v0s = race_df["adj_v0"].fillna(race_df["adj_v0"].median()).tolist()
    decay_rates = race_df["decay_rate"].fillna(race_df["decay_rate"].median()).tolist()
    race_distance_ft = furlongs * 660

    model_probs = model_probs_from_curves(adj_v0s, decay_rates, race_distance_ft)
    odds_probs = odds_to_probs(race_df["closing_odds"].fillna(99).tolist())
    benter_probs = benter_combine(model_probs, odds_probs)

    return {"model": model_probs, "odds": odds_probs, "benter": benter_probs}


def main():
    parser = argparse.ArgumentParser(description="Run a blinded race day simulation")
    parser.add_argument("--track", required=True, help="Track code (e.g. GP, KEE, OP)")
    parser.add_argument("--date", required=True, help="Race date (YYYY-MM-DD)")
    parser.add_argument("--reveal", action="store_true", help="Skip interactive pause, show results immediately")
    args = parser.parse_args()

    conn = get_connection()

    # Step 1: Load pre-race data
    card = load_pre_race_card(conn, args.track, args.date)
    if card.empty:
        print(f"No races found for {args.track} on {args.date}")
        return

    pools = load_pool_sizes(conn, args.track, args.date)
    race_numbers = sorted(card["race_number"].unique())

    print_header(args.track, args.date, len(race_numbers))

    # Step 2: Pool assessment
    if not pools.empty:
        print_pools(pools)

    # Step 3: Per-race handicapping data
    for race_num in race_numbers:
        race_df = card[card["race_number"] == race_num].copy().reset_index(drop=True)
        furlongs = float(race_df["furlongs"].iloc[0])

        # Pace prediction
        v0s = race_df["adj_v0"].dropna().tolist()
        decays = race_df.loc[race_df["adj_v0"].notna(), "decay_rate"].tolist()
        if len(v0s) >= 3:
            pace_result = predict_pace(v0s, decays, furlongs)
        else:
            pace_result = {"scenario": "INSUFFICIENT_DATA", "speed_count": 0,
                          "leader_decay": 0, "median_decay": 0, "gap_1_2": 0,
                          "narrative": "Fewer than 3 horses with curve data", "profiles": []}

        # Probabilities
        probs = compute_race_probs(race_df, furlongs)

        print_race(race_num, race_df, pace_result, probs)

    # Step 5: Commitment pause
    if not args.reveal:
        print(f"\n{'='*70}")
        print("  ALL PRE-RACE DATA SHOWN. COMMIT BETS BEFORE PROCEEDING.")
        print(f"{'='*70}")
        input("\n  Press Enter after bets are committed to reveal results...")

    # Step 6: Reveal
    print(f"\n{'='*70}")
    print("  RESULTS")
    print(f"{'='*70}")

    results_df = load_race_results(conn, args.track, args.date)

    for race_num in race_numbers:
        race_results = results_df[results_df["race_number"] == race_num]
        top4 = race_results[race_results["finish_position"] <= 4].sort_values("finish_position")

        parts = [f"R{race_num}:"]
        for _, r in top4.iterrows():
            marker = "★" if r["finish_position"] == 1 else " "
            parts.append(f"{marker}{r['program']}({r['odds']:.1f},ch{r['choice']})")
        print(f"  {'  '.join(parts)}")

        # Payoffs
        winner_row = race_results[race_results["finish_position"] == 1]
        if not winner_row.empty:
            row = winner_row.iloc[0]
            payoffs = []
            if row.get("exacta_payoff"):
                payoffs.append(f"EX ${row['exacta_payoff']:.1f}/dollar")
            if row.get("trifecta_payoff"):
                payoffs.append(f"TRI ${row['trifecta_payoff']:.1f}/dollar")
            if row.get("super_payoff"):
                payoffs.append(f"SUP ${row['super_payoff']:.1f}/dollar")
            if payoffs:
                print(f"       {'  |  '.join(payoffs)}")

    conn.close()
    print(f"\n{'='*70}")
    print("  SIMULATION COMPLETE")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
