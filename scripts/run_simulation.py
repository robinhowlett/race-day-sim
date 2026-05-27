"""Deterministic simulation scaffold.

Loads data, computes ratings, applies protocol rules mechanically,
and presents structured output for handicapping judgment. Registers
bets and evaluates results without ambiguity.

Usage:
    python scripts/run_simulation.py --track TAM --date 2010-01-31
    python scripts/run_simulation.py --seed "any text here"
"""

import argparse
import json
import os
import sys
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from sim.blinder import load_market_bias, load_pool_sizes, load_pre_race_card
from sim.db import get_connection
from sim.pace import predict_pace
from sim.ratings import format_race_ratings


# --- Protocol thresholds (deterministic) ---

MIN_EDGE_CONVICTION = 0  # edge - band must be > 0 for conviction
MIN_ODDS_WIN_BET = 3.0  # don't win bet below this price
MIN_HORIZONTAL_CONVICTION_LEGS = 2  # need opinions in 2+ legs
MIN_FIELD_SIZE_VERTICALS = 7  # skip verticals in tiny fields
MIN_RATED_FRACTION = 0.4  # at least 40% of field must be rated


@dataclass
class Bet:
    race: int
    bet_type: str  # WIN, EXACTA, TRIFECTA, PICK3, etc.
    programs: list  # program numbers involved
    amount: float
    rationale: str

    def __str__(self):
        progs = "/".join(str(p) for p in self.programs) if len(self.programs) <= 3 else (
            " × ".join("{" + ",".join(str(x) for x in pos) + "}" for pos in self.programs)
        )
        return f"R{self.race} {self.bet_type} #{progs} ${self.amount:.2f} — {self.rationale}"


@dataclass
class SimDay:
    track: str
    date: str
    card: pd.DataFrame = None
    pools: pd.DataFrame = None
    bias: pd.DataFrame = None
    ratings: dict = field(default_factory=dict)  # race_number -> DataFrame
    bets: list = field(default_factory=list)
    results: pd.DataFrame = None

    def load(self, conn):
        self.card = load_pre_race_card(conn, self.track, self.date)
        self.pools = load_pool_sizes(conn, self.track, self.date)
        self.bias = load_market_bias(conn, self.track, self.date)
        for rn in sorted(self.card["race_number"].unique()):
            self.ratings[rn] = format_race_ratings(self.card, self.bias, rn)

    def race_summary(self, rn: int) -> dict:
        """Compute deterministic race metrics."""
        race = self.card[self.card["race_number"] == rn]
        ratings = self.ratings[rn]
        r = race.iloc[0]
        furlongs = float(r["furlongs"])

        rated = ratings[ratings["tier"] == "RATED"]
        n_rated = len(rated)
        n_total = len(ratings)

        # Pace
        v0s = race["adj_v0"].dropna().tolist()
        decays = race["adj_decay"].dropna().tolist()
        pace = predict_pace(v0s, decays, furlongs) if len(v0s) >= 3 else None

        # Edge analysis
        top_edge = rated["edge"].max() if n_rated > 0 else None
        top_band = rated.loc[rated["edge"].idxmax(), "band"] if top_edge and not np.isnan(top_edge) else None
        conviction = (top_edge - top_band > MIN_EDGE_CONVICTION) if top_edge and top_band else False

        # Favorite
        fav_row = rated[rated["odds"] == rated["odds"].min()] if n_rated > 0 and rated["odds"].notna().any() else None
        fav_edge = float(fav_row["edge"].iloc[0]) if fav_row is not None and not fav_row.empty and pd.notna(fav_row["edge"].iloc[0]) else None

        # Pool
        race_pools = self.pools[self.pools["race_number"] == rn] if self.pools is not None else None
        tri_pool = None
        if race_pools is not None and not race_pools.empty:
            tri = race_pools[race_pools["bet_type"] == "TRIFECTA"]
            tri_pool = int(tri["pool"].iloc[0]) if not tri.empty else None

        return {
            "race_number": rn,
            "surface": r["surface"],
            "furlongs": furlongs,
            "race_type": r["race_type"],
            "purse": int(r["purse"]) if pd.notna(r["purse"]) else 0,
            "field_size": n_total,
            "n_rated": n_rated,
            "rated_fraction": n_rated / n_total if n_total > 0 else 0,
            "pace_scenario": pace["scenario"] if pace else "UNKNOWN",
            "top_edge": round(top_edge, 1) if top_edge and not np.isnan(top_edge) else None,
            "top_band": top_band,
            "has_conviction": conviction,
            "fav_edge": round(fav_edge, 1) if fav_edge else None,
            "tri_pool": tri_pool,
        }

    def protocol_check(self, rn: int) -> dict:
        """Apply protocol rules deterministically. Returns playability assessment."""
        summary = self.race_summary(rn)
        ratings = self.ratings[rn]
        rated = ratings[ratings["tier"] == "RATED"]

        checks = {
            "race": rn,
            "playable": True,
            "reasons": [],
            "candidates": [],
        }

        # Check: enough rated horses
        if summary["rated_fraction"] < MIN_RATED_FRACTION:
            checks["playable"] = False
            checks["reasons"].append(f"Low rating coverage ({summary['n_rated']}/{summary['field_size']})")

        # Check: meaningful edge exists
        if not summary["has_conviction"]:
            checks["playable"] = False
            checks["reasons"].append(f"No conviction edge (top: {summary['top_edge']} ±{summary['top_band']})")

        # Check: field size for verticals
        if summary["field_size"] < MIN_FIELD_SIZE_VERTICALS:
            checks["reasons"].append(f"Small field ({summary['field_size']}) — verticals less attractive")

        # Identify conviction candidates (edge - band > 0)
        if not rated.empty:
            for _, row in rated.iterrows():
                if pd.notna(row["edge"]) and row["band"] and (row["edge"] - row["band"] > 0):
                    checks["candidates"].append({
                        "program": row["program"],
                        "horse": row["horse"],
                        "rating": row["rating"],
                        "edge": row["edge"],
                        "band": row["band"],
                        "worst_case": round(row["edge"] - row["band"], 1),
                        "odds": row["odds"],
                        "form": row["form"],
                    })

        return checks

    def register_bet(self, race: int, bet_type: str, programs, amount: float, rationale: str):
        """Register a bet with explicit program numbers."""
        bet = Bet(race=race, bet_type=bet_type, programs=programs,
                  amount=amount, rationale=rationale)
        self.bets.append(bet)
        return bet

    def print_bet_register(self):
        """Print all registered bets in unambiguous format."""
        print("\n" + "=" * 60)
        print("BET REGISTER")
        print("=" * 60)
        total = 0
        for bet in self.bets:
            print(f"  {bet}")
            total += bet.amount
        print(f"\n  TOTAL: ${total:.2f}")
        print("=" * 60)

    def reveal_and_evaluate(self, conn):
        """Load results and mechanically match against registered bets."""
        from sim.blinder import load_race_results
        self.results = load_race_results(conn, self.track, self.date)

        print("\n" + "=" * 60)
        print("RESULTS & EVALUATION")
        print("=" * 60)

        total_invested = sum(b.amount for b in self.bets)
        total_returned = 0.0

        for rn in sorted(self.results["race_number"].unique()):
            race_results = self.results[self.results["race_number"] == rn].sort_values("official_position")
            top3 = race_results.head(3)
            winner_pgm = str(top3.iloc[0]["program"])
            winner_name = top3.iloc[0]["horse_name"]
            winner_odds = top3.iloc[0]["odds"]

            # Get payoffs
            tri_pay = race_results["trifecta_payoff"].dropna().iloc[0] if race_results["trifecta_payoff"].notna().any() else None
            ex_pay = race_results["exacta_payoff"].dropna().iloc[0] if race_results["exacta_payoff"].notna().any() else None

            top3_str = " → ".join(f"{r['horse_name']} (#{r['program']}, {r['odds']:.1f}/1)" for _, r in top3.iterrows())
            print(f"\n  R{rn}: {top3_str}")
            if tri_pay:
                print(f"       Exacta: ${ex_pay:.2f}  Trifecta: ${tri_pay:.2f}")

            # Match bets
            race_bets = [b for b in self.bets if b.race == rn]
            for bet in race_bets:
                hit = False
                payout = 0.0

                if bet.bet_type == "WIN":
                    if str(bet.programs[0]) == winner_pgm:
                        hit = True
                        payout = bet.amount * (winner_odds + 1)

                elif bet.bet_type == "EXACTA":
                    first_pgm = str(top3.iloc[0]["program"])
                    second_pgm = str(top3.iloc[1]["program"])
                    # Check if any combo in the bet matches
                    if isinstance(bet.programs[0], list):
                        for top in bet.programs[0]:
                            for bot in bet.programs[1]:
                                if str(top) == first_pgm and str(bot) == second_pgm:
                                    hit = True
                                    payout = ex_pay * (bet.amount / len(bet.programs[0]) / len(bet.programs[1]))
                    else:
                        if str(bet.programs[0]) == first_pgm and str(bet.programs[1]) == second_pgm:
                            hit = True
                            payout = ex_pay * bet.amount

                status = "✓ HIT" if hit else "✗ MISS"
                payout_str = f" → ${payout:.2f}" if hit else ""
                print(f"       {status}: {bet.bet_type} #{bet.programs} ${bet.amount:.2f}{payout_str}")
                if hit:
                    total_returned += payout

        print(f"\n  {'=' * 40}")
        print(f"  Total invested: ${total_invested:.2f}")
        print(f"  Total returned: ${total_returned:.2f}")
        print(f"  P&L: ${total_returned - total_invested:.2f}")
        print(f"  ROI: {((total_returned / total_invested) - 1) * 100:.1f}%" if total_invested > 0 else "  ROI: N/A")
        print(f"  {'=' * 40}")


def main():
    parser = argparse.ArgumentParser(description="Run a blinded race day simulation")
    parser.add_argument("--track", help="Track code")
    parser.add_argument("--date", help="Race date (YYYY-MM-DD)")
    parser.add_argument("--seed", help="Hash seed to pick a random day")
    args = parser.parse_args()

    if args.seed:
        from scripts.pick_sim_day import pick_day
        result = pick_day(args.seed)
        track, date = result["track"], result["date"]
        print(f"Seed: \"{args.seed}\" → {track} {date}")
    elif args.track and args.date:
        track, date = args.track, args.date
    else:
        parser.error("Provide --track + --date, or --seed")
        return

    conn = get_connection()
    sim = SimDay(track=track, date=date)

    print(f"\nLoading {track} {date}...")
    sim.load(conn)
    print(f"Loaded: {len(sim.card)} starters, {sim.card['race_number'].nunique()} races")

    print("\n" + "=" * 60)
    print("CARD OVERVIEW")
    print("=" * 60)

    for rn in sorted(sim.card["race_number"].unique()):
        s = sim.race_summary(rn)
        check = sim.protocol_check(rn)
        status = "PLAY" if check["playable"] else "PASS"
        edge_str = f"+{s['top_edge']}" if s["top_edge"] and s["top_edge"] > 0 else "—"
        cands = len(check["candidates"])
        print(f"  R{rn:2d}: {s['surface']:5s} {s['furlongs']:.1f}f | {s['race_type'][:25]:25s} | "
              f"{s['n_rated']}/{s['field_size']} rated | edge: {edge_str:>4s} | "
              f"{status} ({cands} candidates) | {', '.join(check['reasons'][:2]) if not check['playable'] else s['pace_scenario']}")

    print("\n" + "=" * 60)
    print("CONVICTION PLAYS (edge - band > 0)")
    print("=" * 60)

    for rn in sorted(sim.card["race_number"].unique()):
        check = sim.protocol_check(rn)
        if check["candidates"]:
            s = sim.race_summary(rn)
            print(f"\n  R{rn}: {s['surface']} {s['furlongs']}f {s['race_type']} (fav edge: {s['fav_edge']})")
            for c in check["candidates"]:
                odds_check = "✓ win-bettable" if c["odds"] and c["odds"] >= MIN_ODDS_WIN_BET else "✗ too short for win"
                print(f"    #{c['program']:>3s} {c['horse'][:20]:20s} "
                      f"Edge +{c['edge']:.0f} (±{c['band']}) worst: +{c['worst_case']:.0f} "
                      f"odds: {c['odds']:.1f}/1 form: {c['form']} {odds_check}")

    print("\n" + "-" * 60)
    print("HANDICAPPING JUDGMENT REQUIRED:")
    print("  For each candidate above, decide:")
    print("  1. Do I believe this edge? (form trend, class context, pace fit)")
    print("  2. What's the purest expression? (win / exotic / horizontal leg)")
    print("  3. Register bets with sim.register_bet(race, type, programs, amount, rationale)")
    print("-" * 60)

    conn.close()
    return sim


if __name__ == "__main__":
    main()
