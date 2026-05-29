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
from sim.payoff import estimate_combo_value
from sim.probability import (
    benter_combine,
    harville_ordered_prob,
    model_probs_from_curves,
    odds_to_probs,
)
from sim.ratings import format_race_ratings


# --- Protocol thresholds (deterministic) ---

# Conviction = (edge - band) > MIN_EDGE_CONVICTION_MARGIN.
# The "edge - band > 0" form (margin = 0) means "even at the lower end of the
# model's confidence band, the edge is still positive." That's a defensible
# floor: the margin doesn't say "edge above some arbitrary number," it says
# "the band is clear of zero."
#
# Bumping this to 1-3 would tighten the gate further but is empirically
# unjustified without measuring the actual worst-case distribution after the
# RDS-T1.1/T1.2/T1.3/T1.4 fixes. With those fixes reducing edge inflation,
# the band-clear-of-zero gate already captures the real-conviction set.
# Future calibration: sample worst-case distribution across a multi-day batch
# and pick a margin that produces a reasonable conviction-pick density.
MIN_EDGE_CONVICTION_MARGIN = 0.0
MIN_EDGE_CONVICTION = MIN_EDGE_CONVICTION_MARGIN  # legacy alias

MIN_ODDS_WIN_BET = 3.0  # don't win bet below this price
MIN_HORIZONTAL_CONVICTION_LEGS = 2  # need opinions in 2+ legs (informational, not gating — see PROTO-T3.13)
MIN_FIELD_SIZE_VERTICALS = 7  # skip verticals in tiny fields
MIN_RATED_FRACTION = 0.4  # at least 40% of field must be rated

# Bet type structure: number of finish positions required, plus whether the
# programs argument is interpreted as positional lists (e.g., TRIFECTA passes
# a list-of-3-lists for "horse in 1st, horse in 2nd, horse in 3rd").
_BET_TYPE_POSITIONS = {
    "WIN":        1,
    "PLACE":      1,  # ITP forbids; validation will reject
    "SHOW":       1,
    "EXACTA":     2,
    "QUINELLA":   2,  # order-independent — handle separately at evaluate
    "TRIFECTA":   3,
    "SUPERFECTA": 4,
    "HI_5":       5,
}
_HORIZONTAL_LEGS = {
    "DAILY_DOUBLE": 2,
    "PICK_3":       3,
    "PICK_4":       4,
    "PICK_5":       5,
    "PICK_6":       6,
}
_FORBIDDEN_BET_TYPES = {"PLACE", "SHOW"}  # ITP framework: never use these

# Stake-as-fraction-of-pool thresholds. Above this, your own bet meaningfully
# distorts the payoff for itself (you're competing with your own money for the
# pool). Empirical convention from professional play.
_MAX_STAKE_PCT_OF_POOL = {
    "WIN":          0.005,
    "EXACTA":       0.005,
    "QUINELLA":     0.005,
    "TRIFECTA":     0.010,
    "SUPERFECTA":   0.010,
    "HI_5":         0.010,
    "DAILY_DOUBLE": 0.010,
    "PICK_3":       0.010,
    "PICK_4":       0.010,
    "PICK_5":       0.005,
    "PICK_6":       0.005,
}
# Hard absolute floor below which a pool is effectively dead.
_DEAD_POOL_FLOOR = 1_000.0


def _format_programs(programs) -> str:
    """Format a bet's programs in standard track notation.

    Examples:
        WIN [7]                              → "7"
        EXACTA [[1], [2,3]]                  → "1/2,3"
        TRIFECTA [[7], [3,1,2], [3,1,2]]     → "7/1,2,3/1,2,3"
        PICK_4 [[1,2,4,6],[1,2,4,6],[1-6],[1-6]] → "1,2,4,6/1,2,4,6/1,2,3,4,5,6/1,2,3,4,5,6"

    Within each position/leg, programs are sorted by integer value (or
    lexically when non-numeric), comma-separated. Position/leg groups are
    separated by '/'.
    """
    if not isinstance(programs, (list, tuple)) or not programs:
        return str(programs)

    # Single-program list (WIN/PLACE/SHOW): just the program
    if len(programs) == 1 and not isinstance(programs[0], (list, tuple)):
        return str(programs[0])

    # Flat list (legacy single-combo input like ['7','3','2']): treat as one
    # program per position
    if not isinstance(programs[0], (list, tuple)):
        return "/".join(str(p) for p in programs)

    # List of position/leg lists
    parts = []
    for pos in programs:
        # Sort numerically when possible; dedupe; comma-join
        seen = []
        for p in pos:
            if p not in seen:
                seen.append(p)
        try:
            seen.sort(key=lambda x: int(str(x)))
        except (ValueError, TypeError):
            seen.sort(key=lambda x: str(x))
        parts.append(",".join(str(p) for p in seen))
    return "/".join(parts)


@dataclass
class Bet:
    """A single registered bet with one flat per-combo cost.

    Press pattern (PROTO-T3.4): the protocol's "press the strong combos"
    mechanic is expressed as MULTIPLE Bet objects on the same (race,
    bet_type) where the narrow press is a subset of the wide spread.

    Example — $24 trifecta with 4 strong combos at $3 + 12 spread combos
    at $1 (per simulation-protocol.md Step E.4 worked example):
        # The wide spread (16 combos at $1 each = $16)
        sim.register_bet(R, 'TRIFECTA',
            [['7'], ['1','2','3','4'], ['1','2','3','4']],
            16.0, 'spread coverage')
        # The narrow press on the strong combos (4 combos at $2 extra each = $8)
        sim.register_bet(R, 'TRIFECTA',
            [['7'], ['1','2'], ['1','2']],
            8.0, 'press strongest 4 combos')
        # Total: $24, with $3 effective on the 4 strong combos (1 + 2)
        # and $1 on the other 12. Press detection in register_bet surfaces
        # this as a [press note] when it fires.

    Basket pattern (PROTO-T3.9): when a single strategic opinion is expressed
    across multiple bets (e.g. WIN + EXACTA + TRIFECTA all keyed on #7), tag
    each bet with the same `basket_id`. An aggregate-exposure note fires when
    the cumulative basket stake gets thick relative to single-race exposure
    (the over-investment trap). At evaluation time, P&L rolls up per basket
    so retrospective questions like "did this strategic opinion clear?" are
    answerable. Untagged bets behave exactly as before.
    """
    race: int
    bet_type: str  # WIN, EXACTA, TRIFECTA, PICK3, etc.
    programs: list  # program numbers involved
    amount: float
    rationale: str
    basket_id: str | None = None

    def __str__(self):
        suffix = f" [{self.basket_id}]" if self.basket_id else ""
        return f"R{self.race} {self.bet_type} {_format_programs(self.programs)} ${self.amount:.2f} — {self.rationale}{suffix}"


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

        # Pace (surface-aware so gap thresholds use the right fraction)
        surface = str(r["surface"]) if "surface" in race.columns else None
        v0s = race["adj_v0"].dropna().tolist()
        decays = race["adj_decay"].dropna().tolist()
        pace = predict_pace(v0s, decays, furlongs, surface=surface) if len(v0s) >= 3 else None

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

    def combined_probs(self, rn: int) -> dict:
        """Compute model + odds + Benter-combined win probabilities per starter.

        Read-only race-level numbers for exploratory analysis. Returns a dict
        with `programs`, `odds`, `model`, `odds_probs`, `benter` arrays
        aligned to the race's starter order. `model` and `benter` are None
        when fewer than 3 horses have curve data.
        """
        race = self.card[self.card["race_number"] == rn].copy().reset_index(drop=True)
        if race.empty:
            return {"programs": [], "odds": [], "model": None,
                    "odds_probs": None, "benter": None}

        furlongs = float(race["furlongs"].iloc[0])
        race_distance_ft = furlongs * 660.0

        odds_list = race["closing_odds"].fillna(99).tolist()
        programs = race["program"].astype(str).tolist()
        odds_probs = odds_to_probs(odds_list)

        has_curves = race["adj_v0"].notna() & race["adj_decay"].notna()
        if has_curves.sum() < 3:
            return {"programs": programs, "odds": odds_list,
                    "model": None, "odds_probs": odds_probs, "benter": None}

        adj_v0s = race["adj_v0"].fillna(race["adj_v0"].median()).tolist()
        decays = race["adj_decay"].fillna(race["adj_decay"].median()).tolist()
        model_probs = model_probs_from_curves(adj_v0s, decays, race_distance_ft)
        benter = benter_combine(model_probs, odds_probs)

        return {"programs": programs, "odds": odds_list,
                "model": model_probs, "odds_probs": odds_probs, "benter": benter}

    def top_value_combos(self, rn: int, top_n: int = 10,
                         bet_type: str = "TRIFECTA") -> list[dict]:
        """Enumerate trifecta (or exacta) combos with highest projected
        overlay vs Stern/Harville fair value.

        Limits the search to the top 6 horses by Benter probability for
        the first two positions (third position takes any horse). Returns
        a list of dicts sorted by overlay descending: [{combo, odds,
        harv_prob, projected, fair, overlay, overlay_pct, fav_pos}].
        Returns [] when probabilities can't be computed or the relevant
        pool is missing.
        """
        if bet_type not in ("EXACTA", "TRIFECTA"):
            return []
        n_pos = 2 if bet_type == "EXACTA" else 3

        cp = self.combined_probs(rn)
        if cp["benter"] is None:
            return []
        benter = cp["benter"]
        n = len(benter)
        if n < n_pos + 1:
            return []

        race_pools = self.pools[self.pools["race_number"] == rn] if self.pools is not None else None
        if race_pools is None or race_pools.empty:
            return []
        pool_match = race_pools[race_pools["bet_type"] == bet_type]
        if pool_match.empty:
            return []
        pool_size = float(pool_match["pool"].iloc[0])
        if pool_size <= 0:
            return []

        odds_list = cp["odds"]
        programs = cp["programs"]
        field_size = len(odds_list)
        # Herfindahl on odds-implied probs (concentration of win-pool money)
        op = cp["odds_probs"]
        hhi = float((op ** 2).sum())

        # Identify the favorite by minimum positive odds
        positive_odds = [(i, o) for i, o in enumerate(odds_list) if o and o > 0]
        if not positive_odds:
            return []
        fav_idx = min(positive_odds, key=lambda x: x[1])[0]

        # Search the top 6 by Benter probability for the leading positions
        top_by_prob = sorted(range(n), key=lambda i: benter[i], reverse=True)[:6]

        combos = []
        if bet_type == "EXACTA":
            for i in top_by_prob:
                for j in range(n):
                    if j == i:
                        continue
                    harv_prob = harville_ordered_prob(benter, [i, j])
                    if harv_prob < 1e-8:
                        continue
                    fav_pos = 1 if i == fav_idx else (2 if j == fav_idx else None)
                    value = estimate_combo_value(
                        [odds_list[i], odds_list[j]],
                        harv_prob, pool_size, field_size, hhi, fav_pos,
                        bet_type="EXACTA",
                    )
                    if value["overlay_ratio"] is not None:
                        combos.append({
                            "combo": f"{programs[i]}/{programs[j]}",
                            "odds":  f"{odds_list[i]:.0f}-{odds_list[j]:.0f}",
                            "harv_prob": harv_prob,
                            "projected": value["projected_payoff"],
                            "fair": value["harville_fair"],
                            "overlay": value["overlay_ratio"],
                            "overlay_pct": value["overlay_pct"],
                            "fav_pos": fav_pos,
                        })
        else:  # TRIFECTA
            for i in top_by_prob:
                for j in top_by_prob:
                    if j == i:
                        continue
                    for k in range(n):
                        if k == i or k == j:
                            continue
                        harv_prob = harville_ordered_prob(benter, [i, j, k])
                        if harv_prob < 1e-8:
                            continue
                        fav_pos = (1 if i == fav_idx else
                                   2 if j == fav_idx else
                                   3 if k == fav_idx else None)
                        value = estimate_combo_value(
                            [odds_list[i], odds_list[j], odds_list[k]],
                            harv_prob, pool_size, field_size, hhi, fav_pos,
                            bet_type="TRIFECTA",
                        )
                        if value["overlay_ratio"] is not None:
                            combos.append({
                                "combo": f"{programs[i]}/{programs[j]}/{programs[k]}",
                                "odds":  f"{odds_list[i]:.0f}-{odds_list[j]:.0f}-{odds_list[k]:.0f}",
                                "harv_prob": harv_prob,
                                "projected": value["projected_payoff"],
                                "fair": value["harville_fair"],
                                "overlay": value["overlay_ratio"],
                                "overlay_pct": value["overlay_pct"],
                                "fav_pos": fav_pos,
                            })

        combos.sort(key=lambda c: c["overlay"], reverse=True)
        return combos[:top_n]

    def propose_ticket_structures(self, rn: int, opinion: dict) -> dict:
        """For STRONG_SPECIFIC / STRONG_NEGATIVE / STRUCTURAL opinions, build
        an equity table for the rated field and class-specific basket
        suggestions (primary + defensive ticket shapes).

        Returns:
            {
              "equity_table": [
                  {program, horse, odds, edge, role, ratios: {2: r2, 3: r3, 4: r4, 5: r5}}
              ],
              "baskets": [
                  {"label": str, "shape": str, "rationale": str}
              ],
            }
        Or {} if the class is not vertical-actionable (MODERATE_SPECIFIC,
        SPREAD, NO_OPINION).
        """
        cls = opinion.get("opinion")
        if cls not in ("STRONG_SPECIFIC", "STRONG_NEGATIVE", "STRUCTURAL"):
            return {}

        ratings = self.ratings[rn]
        rated = ratings[ratings["tier"] == "RATED"].copy()
        if rated.empty:
            return {}
        rated = rated[rated["edge"].notna() & rated["odds"].notna() & (rated["odds"] > 0)].copy()
        if rated.empty:
            return {}

        # Optional per-horse pace role for STRUCTURAL
        race = self.card[self.card["race_number"] == rn]
        pace_role = {}  # program -> "CLOSER" | "SPEED" | "MID"
        if cls == "STRUCTURAL" and "adj_decay" in race.columns and "adj_v0" in race.columns:
            valid = race[race["adj_decay"].notna() & race["adj_v0"].notna()]
            if not valid.empty:
                median_decay = valid["adj_decay"].median()
                median_v0 = valid["adj_v0"].median()
                for _, h in valid.iterrows():
                    pgm = str(h["program"])
                    is_speed = h["adj_v0"] >= median_v0
                    is_low_decay = h["adj_decay"] <= median_decay
                    if is_speed and not is_low_decay:
                        pace_role[pgm] = "SPEED"
                    elif is_low_decay and not is_speed:
                        pace_role[pgm] = "CLOSER"
                    elif is_low_decay and is_speed:
                        pace_role[pgm] = "CLOSER"  # sustained-speed, treat as closer
                    else:
                        pace_role[pgm] = "MID"

        fav_pgm = self._favorite_in_race(rn)

        # Build equity table — one row per rated horse, ratios at widths 2..5
        equity_table = []
        for _, h in rated.sort_values("edge", ascending=False).iterrows():
            pgm = str(h["program"])
            o = float(h["odds"])
            ratios = {N: round((o + 1.0) / N, 2) for N in (2, 3, 4, 5)}
            role_parts = []
            if pgm == fav_pgm:
                role_parts.append("FAV")
            if pgm in pace_role:
                role_parts.append(pace_role[pgm])
            equity_table.append({
                "program": pgm,
                "horse":   h["horse"],
                "odds":    o,
                "edge":    float(h["edge"]),
                "role":    "/".join(role_parts) if role_parts else "",
                "ratios":  ratios,
            })

        # Class-specific basket suggestions
        baskets = []
        if cls == "STRONG_SPECIFIC":
            key_pgm = opinion["details"]["key_program"]
            # Eligible underneath: any rated horse not the key, whose ratio
            # at the chosen width is > 1.0. We display widths in the table —
            # bettor picks. Suggest a generic shape with N referring to "all
            # horses underneath that pass at your chosen width".
            non_keys = [r for r in equity_table if r["program"] != key_pgm]
            best_under = sorted(non_keys, key=lambda r: -r["edge"])[:5]
            under_strs = [
                f"#{r['program']} (odds {r['odds']:.1f}, ratios "
                f"{r['ratios'][2]:.1f}/{r['ratios'][3]:.1f}/{r['ratios'][4]:.1f}/{r['ratios'][5]:.1f})"
                for r in best_under
            ]
            baskets.append({
                "label":     "primary",
                "shape":     f"TRIFECTA #{key_pgm}/AB/AB",
                "rationale": (f"key #{key_pgm} on top — the strongest opinion. "
                              f"Underneath, include any of: {'; '.join(under_strs[:3])}. "
                              f"Pick a width where chosen horses' ratio at that width is ≥ 1.0."),
            })
            baskets.append({
                "label":     "defensive",
                "shape":     f"TRIFECTA AB/#{key_pgm}/AB",
                "rationale": (f"defensive: assumes #{key_pgm} runs 2nd to an underused winner. "
                              f"Same underneath set as primary; #{key_pgm} pinned in 2nd. "
                              f"Smaller allocation than primary — only when you suspect the model's value horse "
                              f"is the right horse but a longshot grabs the win."),
            })

        elif cls == "STRONG_NEGATIVE":
            non_fav = [r for r in equity_table if r["program"] != fav_pgm]
            # Order by edge descending
            non_fav_sorted = sorted(non_fav, key=lambda r: -r["edge"])
            top_strs = [
                f"#{r['program']} (odds {r['odds']:.1f}, edge {r['edge']:+.1f}, ratios "
                f"{r['ratios'][2]:.1f}/{r['ratios'][3]:.1f}/{r['ratios'][4]:.1f}/{r['ratios'][5]:.1f})"
                for r in non_fav_sorted[:6]
            ]
            baskets.append({
                "label":     "primary",
                "shape":     "TRIFECTA non-fav/non-fav/non-fav (fav excluded entirely)",
                "rationale": (f"favorite #{fav_pgm} excluded everywhere — the structural call. "
                              f"Eligible non-fav contenders by edge: {'; '.join(top_strs[:4])}. "
                              f"Pick top, 2nd, 3rd from this set; depth is your call based on "
                              f"how far down the equity ratios stay > 1.0 at your chosen width."),
            })
            baskets.append({
                "label":     "defensive",
                "shape":     f"TRIFECTA non-fav/non-fav/#{fav_pgm} (fav allowed in 3rd only)",
                "rationale": (f"defensive: allow #{fav_pgm} to grab a placing in 3rd if they hold on. "
                              f"Saves you from the case where the fav fades to 3rd but doesn't completely "
                              f"collapse. Smaller allocation than primary — only if you expect a partial fade, "
                              f"not a total collapse."),
            })

        elif cls == "STRUCTURAL":
            closers = [r for r in equity_table if "CLOSER" in r["role"]]
            speed   = [r for r in equity_table if "SPEED" in r["role"]]
            mids    = [r for r in equity_table if r["role"] == "MID" or r["role"] == ""]
            closer_strs = [
                f"#{r['program']} (odds {r['odds']:.1f}, edge {r['edge']:+.1f})"
                for r in sorted(closers, key=lambda r: -r["edge"])[:4]
            ]
            speed_strs = [
                f"#{r['program']} (odds {r['odds']:.1f})"
                for r in sorted(speed, key=lambda r: -r["edge"])[:4]
            ]
            baskets.append({
                "label":     "primary",
                "shape":     "TRIFECTA closers/closers/speed-or-mid",
                "rationale": (f"pace will collapse. Closers (low decay) belong on top: {'; '.join(closer_strs[:3])}. "
                              f"Speed types ({'; '.join(speed_strs[:3])}) and mid-types belong in 3rd — "
                              f"they may grab a placing even after fading. Choose width per equity table."),
            })
            baskets.append({
                "label":     "defensive",
                "shape":     "TRIFECTA closers/closers/closers (closers all three positions)",
                "rationale": (f"defensive: bet the pace shape entirely without expecting any speed to hold on. "
                              f"Use if you have strong conviction the speed types will completely fade — "
                              f"all three positions go to closers. Smaller allocation."),
            })
            # Note: speed-on-top is deliberately NOT a defensive shape because
            # speed types in CONTESTED_HIGH_DECAY tend to fade entirely.

        return {"equity_table": equity_table, "baskets": baskets}

    @staticmethod
    def _flb_warning(opinion_class: str, odds: float, edge: float, worst: float) -> str | None:
        """Generate a favorite-longshot-bias warning when a conviction pick
        sits in the longshot tail.

        Empirically (handycapper TB), longshots win LESS than odds-implied
        probability — the public over-bets longshots. A model finding "edge"
        in this tier is fighting the population bias, so the conviction
        needs to come from specific information the model genuinely has,
        not from generic edge calculation. See RDS-T2.x in audit doc.

        Returns None for chalk picks (FLB is in the model's favor there).
        """
        if odds is None or odds <= 0:
            return None
        # CHALK: odds < 7 — public under-bets favorites; FLB works WITH us
        if odds < 7:
            return None
        # MID: 7 ≤ odds < 15 — neutral zone, no FLB warning
        if odds < 15:
            return None
        # LONGSHOT: odds ≥ 15 — public over-bets longshots; FLB runs AGAINST us
        if opinion_class == "STRONG_SPECIFIC":
            return (
                f"longshot conviction (odds {odds:.1f}, worst-case +{worst:.1f}): "
                f"model is highly confident, but empirical favorite-longshot bias "
                f"runs against longshots winning. Verify the model's edge against "
                f"trip notes / equipment / recent works the simulator can't see."
            )
        # MODERATE_SPECIFIC at longshot odds: thin conviction, FLB risk
        return (
            f"longshot pick on thin conviction (odds {odds:.1f}, worst-case +{worst:.1f}): "
            f"empirical favorite-longshot bias runs against longshots winning, AND "
            f"the model's worst-case is just barely positive. High FLB risk — "
            f"prefer a horizontal leg over a standalone bet, and treat as suggestive "
            f"unless you have specific information confirming the edge."
        )

    def classify_opinion(self, rn: int) -> dict:
        """PROTO-T3.6: Classify the model's opinion in this race per
        simulation-protocol.md Step E.1.

        Returns one of six classes:
            STRONG_SPECIFIC, MODERATE_SPECIFIC, STRONG_NEGATIVE,
            STRUCTURAL, SPREAD, NO_OPINION
        Plus a rationale string with concrete numbers and a recommended
        primary bet expression per Step E.2.
        """
        summary = self.race_summary(rn)
        ratings = self.ratings[rn]
        rated = ratings[ratings["tier"] == "RATED"].copy()

        result = {
            "race": rn,
            "opinion": "NO_OPINION",
            "rationale": "",
            "recommended": "PASS",
            "hint": None,
            "flb_warning": None,
            "details": {},
        }

        if rated.empty:
            result["rationale"] = (
                f"no rated horses in field ({summary['n_rated']}/{summary['field_size']}); "
                f"no model opinion possible"
            )
            return result

        # Filter to horses with valid edge + band
        rated = rated[rated["edge"].notna() & rated["band"].notna()].copy()
        if rated.empty:
            result["rationale"] = "no horses with computed edge/band; no model opinion possible"
            return result
        rated["worst_case"] = rated["edge"] - rated["band"]

        # Pre-compute "favorite is unrated" hint so it can propagate to any
        # opinion class (the bettor wants to know this regardless of which
        # branch fires). Set early; the STRONG_NEGATIVE branch reads it.
        actual_fav_pgm = self._favorite_in_race(rn)
        if actual_fav_pgm is not None:
            fav_match = rated[rated["program"].astype(str) == str(actual_fav_pgm)]
            if fav_match.empty and rated["odds"].notna().any():
                rated_lowest = rated.loc[rated["odds"].idxmin()]
                if (float(rated_lowest["edge"]) < -10 and
                        float(rated_lowest["edge"]) + float(rated_lowest["band"]) < 0):
                    fav_card_row = self.card[
                        (self.card["race_number"] == rn) &
                        (self.card["program"].astype(str) == str(actual_fav_pgm))
                    ]
                    fav_odds_str = (f"{fav_card_row['closing_odds'].iloc[0]:.1f}"
                                    if not fav_card_row.empty else "?")
                    result["hint"] = (
                        f"actual favorite #{actual_fav_pgm} (at {fav_odds_str}) has no model rating "
                        f"(insufficient curve coverage); rated-lowest-odds "
                        f"#{rated_lowest['program']} {rated_lowest['horse']} "
                        f"(at {rated_lowest['odds']:.1f}) edge {rated_lowest['edge']:+.1f} — "
                        f"model is bearish on the chalkiest rated horse but can't evaluate the actual chalk"
                    )

        # 1. STRONG_SPECIFIC — clear single-horse bet (worst_case > 5)
        strong = rated[rated["worst_case"] > 5].sort_values("worst_case", ascending=False)
        if not strong.empty:
            top = strong.iloc[0]
            result["opinion"] = "STRONG_SPECIFIC"
            result["rationale"] = (
                f"#{top['program']} {top['horse']} edge {top['edge']:+.1f} ±{top['band']:.0f} "
                f"(worst {top['worst_case']:+.1f}) at {top['odds']:.1f} — "
                f"band clear of zero with margin"
            )
            # Win bet only if odds ≥ MIN_ODDS_WIN_BET; below that, recommend exotic key
            if top["odds"] >= MIN_ODDS_WIN_BET:
                result["recommended"] = f"WIN #{top['program']}"
            else:
                result["recommended"] = (
                    f"EXACTA/TRIFECTA key #{top['program']} (odds {top['odds']:.1f} below "
                    f"{MIN_ODDS_WIN_BET} WIN floor — express via exotic)"
                )
            result["flb_warning"] = self._flb_warning(
                "STRONG_SPECIFIC",
                float(top["odds"]) if pd.notna(top["odds"]) else 0,
                float(top["edge"]),
                float(top["worst_case"]),
            )
            result["details"] = {"key_program": str(top["program"]), "edge": float(top["edge"])}
            return result

        # 2. STRUCTURAL — pace collapse with closers underneath
        if summary.get("pace_scenario") == "CONTESTED_HIGH_DECAY":
            # Closers = horses with low decay AND positive worst-case edge
            race = self.card[self.card["race_number"] == rn]
            closers = []
            if "adj_decay" in race.columns:
                median_decay = race["adj_decay"].median(skipna=True)
                if pd.notna(median_decay):
                    for _, h in rated.iterrows():
                        h_decay_row = race[race["program"] == h["program"]]
                        if h_decay_row.empty:
                            continue
                        h_decay = h_decay_row["adj_decay"].iloc[0]
                        if pd.notna(h_decay) and h_decay <= median_decay and h["worst_case"] > 0:
                            closers.append(h)
            if len(closers) >= 2:
                result["opinion"] = "STRUCTURAL"
                closer_strs = [
                    f"#{c['program']} (edge {c['edge']:+.0f}, low decay)" for c in closers[:3]
                ]
                result["rationale"] = (
                    f"CONTESTED_HIGH_DECAY pace AND {len(closers)} low-decay horses with "
                    f"positive edge: {', '.join(closer_strs)} — pace will collapse, key closers on top"
                )
                result["recommended"] = (
                    f"TRIFECTA closers/{','.join(str(c['program']) for c in closers[:3])} on top, "
                    f"speed types underneath"
                )
                result["details"] = {"closers": [str(c["program"]) for c in closers]}
                return result

        # 3. STRONG_NEGATIVE — actual public favorite is overbet (rated only).
        # If favorite is unrated, the hint set above carries the partial info.
        if actual_fav_pgm is not None:
            fav_match = rated[rated["program"].astype(str) == str(actual_fav_pgm)]
            if not fav_match.empty:
                fav_row = fav_match.iloc[0]
                fav_edge = float(fav_row["edge"])
                fav_band = float(fav_row["band"])
                if fav_edge < -10 and (fav_edge + fav_band) < 0:
                    result["opinion"] = "STRONG_NEGATIVE"
                    result["rationale"] = (
                        f"favorite #{fav_row['program']} {fav_row['horse']} (at {fav_row['odds']:.1f}) "
                        f"edge {fav_edge:+.1f} ±{fav_band:.0f} "
                        f"(worst {fav_edge + fav_band:+.1f}) — model says fav is overbet; "
                        f"every non-fav exotic combo is structurally overlaid"
                    )
                    result["recommended"] = (
                        f"TRIFECTA/SUPERFECTA excluding #{fav_row['program']} on top "
                        f"(see equity table to choose depth)"
                    )
                    result["details"] = {
                        "fav_program": str(fav_row["program"]),
                        "fav_edge": fav_edge,
                    }
                    return result

        # 4. MODERATE_SPECIFIC — single-horse opinion but band crosses or is close to zero
        moderate = rated[(rated["worst_case"] > 0) & (rated["worst_case"] <= 5)] \
            .sort_values("worst_case", ascending=False)
        if not moderate.empty:
            top = moderate.iloc[0]
            result["opinion"] = "MODERATE_SPECIFIC"
            result["rationale"] = (
                f"#{top['program']} {top['horse']} edge {top['edge']:+.1f} ±{top['band']:.0f} "
                f"(worst {top['worst_case']:+.1f}) at {top['odds']:.1f} — "
                f"positive worst-case but thin margin; not strong enough for standalone WIN"
            )
            result["recommended"] = (
                f"horizontal leg (single or A/B with #{top['program']}) — "
                f"sequence context can add value the standalone bet lacks"
            )
            result["flb_warning"] = self._flb_warning(
                "MODERATE_SPECIFIC",
                float(top["odds"]) if pd.notna(top["odds"]) else 0,
                float(top["edge"]),
                float(top["worst_case"]),
            )
            result["details"] = {"key_program": str(top["program"]), "edge": float(top["edge"])}
            return result

        # 5. SPREAD — 3+ horses bunched within ±3 edge near the top
        rated_sorted = rated.sort_values("edge", ascending=False)
        top_edge = rated_sorted["edge"].iloc[0]
        bunched = rated_sorted[rated_sorted["edge"] >= top_edge - 6]  # ±3 from top means ~6 spread
        if len(bunched) >= 3:
            result["opinion"] = "SPREAD"
            top3 = bunched.head(3)
            top3_str = ", ".join(
                f"#{r['program']} (edge {r['edge']:+.0f})" for _, r in top3.iterrows()
            )
            result["rationale"] = (
                f"{len(bunched)} horses within 6 edge points of the top: {top3_str} — "
                f"competitive race, no clear leader; standalone vertical not justified"
            )
            result["recommended"] = (
                "horizontal leg (use top 2-3 candidates as A/B/C) — "
                "race adds value to a sequence but not as a standalone bet"
            )
            result["details"] = {"contenders": [str(r["program"]) for _, r in bunched.iterrows()]}
            return result

        # 6. NO_OPINION fallback — top edge in the noise
        top = rated_sorted.iloc[0]
        result["rationale"] = (
            f"top edge #{top['program']} {top['edge']:+.1f} ±{top['band']:.0f} "
            f"(worst {top['edge'] - top['band']:+.1f}) within band of zero — "
            f"model has no meaningful opinion"
        )
        return result

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

    def _validate_bet(self, race: int, bet_type: str, programs, amount: float) -> None:
        """Raise ValueError if the bet is structurally invalid.

        Checks performed:
          - bet_type is in the supported whitelist (and not ITP-forbidden)
          - race exists in the loaded card
          - all programs referenced exist in that race's field
          - structure matches the bet type (e.g., TRIFECTA needs 3 positions)
          - WIN bets meet MIN_ODDS_WIN_BET
          - amount is positive
        """
        if amount <= 0:
            raise ValueError(f"amount must be positive (got {amount})")
        if bet_type in _FORBIDDEN_BET_TYPES:
            raise ValueError(f"{bet_type} is forbidden by ITP framework — use WIN or exotics")
        if bet_type not in _BET_TYPE_POSITIONS and bet_type not in _HORIZONTAL_LEGS:
            raise ValueError(f"unknown bet_type {bet_type!r}")

        if bet_type in _BET_TYPE_POSITIONS:
            # Vertical bet: programs is a single race's structure
            race_df = self.card[self.card["race_number"] == race]
            if race_df.empty:
                raise ValueError(f"race {race} not in card")
            valid_pgms = {str(p) for p in race_df["program"].astype(str).tolist()}
            n_pos = _BET_TYPE_POSITIONS[bet_type]

            if n_pos == 1:
                # WIN/PLACE/SHOW: programs is a single program (in a list of length 1)
                if not isinstance(programs, (list, tuple)) or len(programs) != 1:
                    raise ValueError(f"{bet_type} requires a single program in a 1-element list")
                pgm = str(programs[0])
                if pgm not in valid_pgms:
                    raise ValueError(f"program {pgm} not in race {race}")
                if bet_type == "WIN":
                    horse_row = race_df[race_df["program"].astype(str) == pgm]
                    odds = float(horse_row["closing_odds"].iloc[0]) if not horse_row.empty else None
                    if odds is not None and odds < MIN_ODDS_WIN_BET:
                        raise ValueError(
                            f"WIN bet on #{pgm} at {odds:.1f}/1 below MIN_ODDS_WIN_BET={MIN_ODDS_WIN_BET}"
                        )
            else:
                # Multi-position vertical: programs is a list of N position-lists
                if not isinstance(programs, (list, tuple)) or len(programs) != n_pos:
                    raise ValueError(
                        f"{bet_type} requires {n_pos} position lists (got {len(programs) if hasattr(programs, '__len__') else '?'})"
                    )
                for pos_idx, pos_list in enumerate(programs, start=1):
                    if not isinstance(pos_list, (list, tuple)) or not pos_list:
                        raise ValueError(f"{bet_type} position {pos_idx} must be a non-empty list of programs")
                    for pgm in pos_list:
                        if str(pgm) not in valid_pgms:
                            raise ValueError(f"program {pgm} (pos {pos_idx}) not in race {race}")
        else:
            # Horizontal bet: programs is list-of-leg-lists, race arg = first leg
            n_legs = _HORIZONTAL_LEGS[bet_type]
            if not isinstance(programs, (list, tuple)) or len(programs) != n_legs:
                raise ValueError(f"{bet_type} requires {n_legs} leg lists")
            for leg_idx, leg_list in enumerate(programs):
                if not isinstance(leg_list, (list, tuple)) or not leg_list:
                    raise ValueError(f"{bet_type} leg {leg_idx + 1} must be a non-empty list of programs")
                leg_race_num = race + leg_idx
                race_df = self.card[self.card["race_number"] == leg_race_num]
                if race_df.empty:
                    raise ValueError(f"{bet_type} leg {leg_idx + 1}: race {leg_race_num} not in card")
                valid_pgms = {str(p) for p in race_df["program"].astype(str).tolist()}
                for pgm in leg_list:
                    if str(pgm) not in valid_pgms:
                        raise ValueError(f"program {pgm} (leg {leg_idx + 1}) not in race {leg_race_num}")

    def _favorite_in_race(self, race: int):
        """Return the program of the post-time favorite, or None."""
        race_df = self.card[self.card["race_number"] == race]
        if race_df.empty:
            return None
        with_odds = race_df[race_df["closing_odds"].notna() & (race_df["closing_odds"] > 0)]
        if with_odds.empty:
            return None
        fav_row = with_odds.loc[with_odds["closing_odds"].idxmin()]
        return str(fav_row["program"])

    def _pool_for(self, race: int, bet_type: str):
        """Return the pool size for this race × bet_type, or None if not loaded."""
        if self.pools is None or self.pools.empty:
            return None
        match = self.pools[(self.pools["race_number"] == race) &
                           (self.pools["bet_type"] == bet_type)]
        if match.empty:
            return None
        return float(match["pool"].iloc[0])

    def _favorite_exclusion_notes(self, race: int, bet_type: str, programs) -> list[str]:
        """PROTO-T3.5: informational note if TRIFECTA/SUPERFECTA excludes the
        favorite from 2nd AND 3rd positions. Explains WHY this is worth
        noting: the favorite has a baseline ~30-45% chance of finishing on
        the board; excluding them entirely means the ticket dies in those
        outcomes. Surfaces the favorite's odds and approximate P(top 3) so
        the bettor can decide if their conviction beats the structural cost.
        """
        if bet_type not in ("TRIFECTA", "SUPERFECTA"):
            return []
        if not isinstance(programs[0], (list, tuple)):
            return []
        fav = self._favorite_in_race(race)
        if fav is None:
            return []
        if len(programs) < 3:
            return []
        in_2nd = fav in [str(p) for p in programs[1]]
        in_3rd = fav in [str(p) for p in programs[2]]
        if in_2nd or in_3rd:
            return []

        # Look up the favorite's odds and approximate their P(top 3)
        race_df = self.card[self.card["race_number"] == race]
        fav_row = race_df[race_df["program"].astype(str) == fav]
        if fav_row.empty:
            return ["favorite excluded from 2nd and 3rd"]
        fav_odds = float(fav_row["closing_odds"].iloc[0]) if pd.notna(fav_row["closing_odds"].iloc[0]) else None
        if fav_odds is None or fav_odds <= 0:
            return [f"favorite #{fav} excluded from 2nd and 3rd"]

        # Crude P(win) from odds; P(top 3) ≈ 2.6× P(win) empirically (varies by
        # field size; 2.5× is a reasonable rule of thumb for 8-10 horse fields)
        p_win = 1.0 / (fav_odds + 1)
        p_top3 = min(p_win * 2.6, 0.85)
        return [(
            f"favorite #{fav} (at {fav_odds:.1f}, ~{p_win*100:.0f}% to win, "
            f"~{p_top3*100:.0f}% to hit board) excluded from 2nd and 3rd — "
            f"ticket forfeits ~{p_top3*100:.0f}% of outcomes; verify pace/form supports the exclusion"
        )]

    def _pool_notes(self, race: int, bet_type: str, amount: float) -> list[str]:
        """PROTO-T3.12: stake-vs-pool sizing.

        Two notes possible:
          - dead pool (below absolute floor): payoffs unreliable because
            published $/unit can swing wildly with even modest late money
          - oversized stake (>X% of pool): your bet visibly moves the
            payoff against itself — you'd be competing with yourself
        """
        notes = []
        pool = self._pool_for(race, bet_type)
        if pool is None:
            return notes
        if pool < _DEAD_POOL_FLOOR:
            notes.append(
                f"{bet_type} pool ${pool:,.0f} (below ${_DEAD_POOL_FLOOR:,.0f} floor) — "
                f"payoff/$1 is volatile in tiny pools; chart-published price may not survive late money"
            )
            return notes
        max_pct = _MAX_STAKE_PCT_OF_POOL.get(bet_type)
        if max_pct is not None:
            stake_pct = amount / pool
            if stake_pct > max_pct:
                # Estimate: if the bet hits, it adds your stake to the pool but
                # also subtracts from the per-combo payoff. Self-impact ≈ stake_pct
                # for a single winning combo on a per-$1 basis.
                approx_self_impact = stake_pct * 100
                notes.append(
                    f"stake ${amount:.2f} is {stake_pct*100:.2f}% of ${pool:,.0f} {bet_type} pool "
                    f"(rule-of-thumb max {max_pct*100:.1f}%) — if you hit, your own stake compresses the "
                    f"published per-$1 payoff by roughly {approx_self_impact:.1f}%"
                )
        return notes

    def _horizontal_conviction_notes(self, race: int, bet_type: str, programs) -> list[str]:
        """PROTO-T3.13: leg-coverage summary.

        Not a gate. A horizontal can be +EV with one strong opinion if the
        leg's equity carries the parlay. Surfaces which legs lack a model
        opinion and total combo count, so the bettor can sanity-check the
        ticket geometry.
        """
        if bet_type not in _HORIZONTAL_LEGS:
            return []
        n_legs = len(programs)
        n_conviction = 0
        no_conviction_legs = []
        for leg_idx in range(n_legs):
            leg_race = race + leg_idx
            try:
                check = self.protocol_check(leg_race)
                if check["candidates"]:
                    n_conviction += 1
                else:
                    no_conviction_legs.append(f"R{leg_race}")
            except Exception:
                no_conviction_legs.append(f"R{leg_race}")
        n_combos = 1
        for leg_list in programs:
            n_combos *= max(len(leg_list), 1)
        if n_conviction == 0:
            return [(
                f"no model conviction in any of {n_legs} legs ({', '.join(no_conviction_legs)}); "
                f"{n_combos} combos — ticket is pure spread play, equity rests entirely on closing-odds value"
            )]
        if n_conviction < n_legs:
            return [(
                f"{n_conviction}/{n_legs} legs have a conviction candidate; "
                f"non-conviction legs: {', '.join(no_conviction_legs)}; "
                f"{n_combos} combos — confirm the conviction leg(s) carry enough equity for the spread"
            )]
        return []

    @staticmethod
    def _normalize_programs_for_press(programs) -> list[set] | None:
        """Convert programs into a list of frozensets (one per position/leg)
        for press subset comparison. Returns None for shapes that don't make
        sense for press detection (single-program WIN, flat lists).
        """
        if not isinstance(programs, (list, tuple)) or not programs:
            return None
        # WIN-style single program: ['7']
        if not isinstance(programs[0], (list, tuple)):
            # Could be flat single-combo list ['7','3','2'] for tri — treat
            # each position as a 1-element set
            return [{str(p)} for p in programs]
        return [{str(p) for p in pos} for pos in programs]

    def _basket_exposure_notes(self, basket_id: str | None, race: int,
                                bet_type: str, programs, amount: float) -> list[str]:
        """PROTO-T3.9 (basket): surface cumulative exposure when multiple bets
        share the same `basket_id`.

        A basket is one strategic opinion expressed across multiple bets
        (WIN + EXACTA + TRIFECTA on same key, or multi-race horizontal that
        the bettor wants to track as one play). Without aggregation, the
        per-bet display hides the over-investment trap of registering 5
        small bets on a +3-edge conviction. This note quantifies the trap.

        Fires only when basket_id is provided and at least one prior bet
        already carries the same basket_id. Reports total stake, race count,
        and bet-type mix across the basket.
        """
        if not basket_id:
            return []
        prior = [b for b in self.bets if b.basket_id == basket_id]
        if not prior:
            return []
        prior_total = sum(b.amount for b in prior)
        new_total = prior_total + amount
        races = sorted({b.race for b in prior} | {race})
        type_counts: dict[str, int] = {}
        for b in prior:
            type_counts[b.bet_type] = type_counts.get(b.bet_type, 0) + 1
        type_counts[bet_type] = type_counts.get(bet_type, 0) + 1
        type_str = ", ".join(f"{n}× {t}" for t, n in sorted(type_counts.items()))
        race_str = ",".join(f"R{r}" for r in races)
        return [
            f"basket '{basket_id}' now {len(prior) + 1} bets "
            f"({type_str}) across {race_str}; cumulative stake "
            f"${new_total:.2f} (this bet ${amount:.2f} + prior ${prior_total:.2f}). "
            f"Verify the cumulative stake matches conviction strength — "
            f"multiple small bets on one opinion can compound into thick exposure."
        ]

    def _press_notes(self, race: int, bet_type: str, programs, amount: float) -> list[str]:
        """PROTO-T3.4: detect press patterns across multiple Bets on the same
        (race, bet_type).

        A press ticket is conceptually one bet but registered as N separate
        Bet objects with overlapping `programs` and different `amount`. This
        check fires when the new bet's programs is a SUBSET (per-position) of
        a prior bet's programs — meaning the new bet is "pressing" a subset
        of the prior bet's combos at a different unit cost.

        Surfaces total stake across the press group, total combo count, and
        per-combo cost so the bettor sees the unified economic picture.
        """
        new_sets = self._normalize_programs_for_press(programs)
        if new_sets is None:
            return []
        prior_matches = []
        for prev in self.bets:
            if prev.race != race or prev.bet_type != bet_type:
                continue
            prev_sets = self._normalize_programs_for_press(prev.programs)
            if prev_sets is None or len(prev_sets) != len(new_sets):
                continue
            # Subset check (either direction): new ⊆ prev OR prev ⊆ new
            new_in_prev = all(ns <= ps for ns, ps in zip(new_sets, prev_sets))
            prev_in_new = all(ps <= ns for ns, ps in zip(prev_sets, new_sets))
            if new_in_prev or prev_in_new:
                prior_matches.append(prev)
        if not prior_matches:
            return []

        # Build the press summary: total stake, total combo count, effective
        # per-combo costs at each level
        n_combos_new = 1
        for ns in new_sets:
            n_combos_new *= max(len(ns), 1)
        per_combo_new = amount / n_combos_new if n_combos_new > 0 else 0

        notes = []
        for prev in prior_matches:
            prev_sets = self._normalize_programs_for_press(prev.programs)
            n_combos_prev = 1
            for ps in prev_sets:
                n_combos_prev *= max(len(ps), 1)
            per_combo_prev = prev.amount / n_combos_prev if n_combos_prev > 0 else 0
            total_stake = prev.amount + amount
            # Identify which is the "narrow press" and which is the "wide spread"
            new_in_prev = all(ns <= ps for ns, ps in zip(new_sets, prev_sets))
            if new_in_prev:
                narrow_pgms = _format_programs(programs)
                narrow_cost = per_combo_new
                narrow_n = n_combos_new
                wide_pgms = _format_programs(prev.programs)
                wide_cost = per_combo_prev
                wide_n = n_combos_prev
            else:
                narrow_pgms = _format_programs(prev.programs)
                narrow_cost = per_combo_prev
                narrow_n = n_combos_prev
                wide_pgms = _format_programs(programs)
                wide_cost = per_combo_new
                wide_n = n_combos_new
            notes.append(
                f"press detected with prior R{race} {bet_type} bet: "
                f"narrow {narrow_pgms} ({narrow_n} combos at ${narrow_cost:.2f}/combo) "
                f"+ wide {wide_pgms} ({wide_n} combos at ${wide_cost:.2f}/combo); "
                f"total stake ${total_stake:.2f}. Effective cost on the narrow combos: "
                f"${narrow_cost + wide_cost:.2f}/combo."
            )
        return notes

    def _classify_leg_strategy(self, race: int, leg_programs: list) -> dict:
        """Classify a leg's selection structure into a strategy mode label
        and report the favorite's market-vs-model mispricing magnitude.

        Pure description. The mode label names what the bettor constructed,
        based on the FRACTION OF MARKET EQUITY their selection set captures.
        Equity-based thresholds auto-adjust to field size and field shape:
          - 12-horse race with a 4/5 favorite: 4 mid-priced selections might
            still capture only 30 pct of public equity → SPREAD-EQUITY narrow
          - 6-horse race with no clear chalk: 3 selections might already
            capture 80 pct of public equity → SURVIVE territory

        Mode thresholds (public_equity = sum of overround-normalized
        implied probabilities across the leg's selections):
          SINGLE              — k=1
          SURVIVE             — public_equity >= 0.90 (using "almost all
                                 the value," regardless of horse count)
          WIDE-WITH-FAV       — public_equity >= 0.75 AND includes favorite
                                 (expensive coverage)
          SPREAD-EQUITY-WIDE  — public_equity >= 0.50 AND excludes favorite
                                 (wide contrarian)
          NORMAL              — includes favorite, equity below WIDE-WITH-FAV
                                 threshold (A/B with chalk)
          SPREAD-EQUITY       — excludes favorite, equity below
                                 SPREAD-EQUITY-WIDE threshold (narrow contrarian)

        Returns dict with:
            mode: one of the labels above
            n_selected:     count of selections in this leg
            n_field:        field size of this leg's race
            public_equity:  fraction of market-implied (overround-normalized)
                            win probability captured by the selection set.
                            None when odds data is missing.
            model_equity:   fraction of model-implied true win probability
                            captured by the selection set. None when curves
                            are missing for the race.
            includes_fav:   whether the favorite is among the selections
            mispricing:     market P(fav) − model P(fav), or None.
                            Positive = favorite overbet (market gives them
                            more pool weight than the model thinks).
                            Negative = favorite underbet.
        """
        race_df = self.card[self.card["race_number"] == race]
        n_field = len(race_df) if not race_df.empty else 0
        k = len(leg_programs)
        fav_pgm = self._favorite_in_race(race)
        includes_fav = fav_pgm is not None and str(fav_pgm) in [str(p) for p in leg_programs]

        # Compute equity captured by the selection set, both market-side
        # and model-side. combined_probs returns overround-normalized
        # odds_probs (sum to 1) and Benter-blended model probs (sum to 1).
        public_equity = None
        model_equity = None
        try:
            cp = self.combined_probs(race)
            programs_list = cp.get("programs") or []
            sel_set = {str(p) for p in leg_programs}
            sel_idxs = [i for i, p in enumerate(programs_list) if p in sel_set]
            if sel_idxs and cp.get("odds_probs") is not None:
                op = cp["odds_probs"]
                public_equity = float(sum(op[i] for i in sel_idxs))
            if sel_idxs and cp.get("model") is not None:
                mp = cp["model"]
                model_equity = float(sum(mp[i] for i in sel_idxs))
        except Exception:
            public_equity = None
            model_equity = None

        # Mode by equity fraction (falls back to count-based when equity
        # is unavailable, e.g., missing odds data).
        if k == 1:
            mode = "SINGLE"
        elif public_equity is None:
            # No equity data — fall back to count thresholds (best effort)
            if n_field > 0 and k >= n_field - 1:
                mode = "SURVIVE"
            elif k <= 3:
                mode = "NORMAL" if includes_fav else "SPREAD-EQUITY"
            else:
                mode = "WIDE-WITH-FAV" if includes_fav else "SPREAD-EQUITY-WIDE"
        else:
            if public_equity >= 0.90:
                mode = "SURVIVE"
            elif includes_fav and public_equity >= 0.75:
                mode = "WIDE-WITH-FAV"
            elif (not includes_fav) and public_equity >= 0.50:
                mode = "SPREAD-EQUITY-WIDE"
            elif includes_fav:
                mode = "NORMAL"
            else:
                mode = "SPREAD-EQUITY"

        # Favorite mispricing (market_P_fav − model_P_fav)
        mispricing = None
        try:
            cp = self.combined_probs(race)
            if cp.get("model") is not None and fav_pgm is not None:
                programs_list = cp["programs"]
                if str(fav_pgm) in programs_list:
                    fav_idx = programs_list.index(str(fav_pgm))
                    market_p = float(cp["odds_probs"][fav_idx])
                    model_p  = float(cp["model"][fav_idx])
                    mispricing = market_p - model_p
        except Exception:
            mispricing = None

        return {
            "mode": mode,
            "n_selected": k,
            "n_field": n_field,
            "public_equity": public_equity,
            "model_equity": model_equity,
            "includes_fav": includes_fav,
            "mispricing": mispricing,
        }

    def _hurdle_notes(self, race: int, bet_type: str, programs) -> list[str]:
        """PROTO-T3.9 hurdle: surface the leg-strategy structure of a
        registered horizontal so the bettor can see what they actually
        constructed.

        Pure description. For each leg, reports the structural mode
        (SINGLE / NORMAL / SPREAD-EQUITY / SURVIVE / NORMAL-WIDE) and the
        favorite's market-vs-model mispricing magnitude when meaningful.
        No verdicts, no prescriptions — the bettor has the math; the
        bettor decides.
        """
        if bet_type not in _HORIZONTAL_LEGS:
            return []
        if not isinstance(programs, (list, tuple)) or not programs:
            return []

        leg_classifications = []
        for leg_idx, leg_pgms in enumerate(programs):
            leg_race = race + leg_idx
            cls = self._classify_leg_strategy(leg_race, leg_pgms)
            cls["leg_idx"] = leg_idx
            cls["leg_race"] = leg_race
            leg_classifications.append(cls)

        # Strategy mix summary at the ticket level
        mode_counts = {}
        for c in leg_classifications:
            mode_counts[c["mode"]] = mode_counts.get(c["mode"], 0) + 1
        mix_str = ", ".join(f"{n} {m}" for m, n in sorted(mode_counts.items(), key=lambda x: -x[1]))
        notes = [f"strategy mix: {mix_str}"]

        # Per-leg detail when there's a meaningful equity gap or mispricing
        # to surface beyond the mode label. Equity gap is "model captures
        # markedly different fraction than market does" → tells the bettor
        # whether their selection set is over- or under-covered relative to
        # what the model thinks the true win probability is.
        for c in leg_classifications:
            parts = []

            # Equity numbers as a pair when both available
            pub = c.get("public_equity")
            mod = c.get("model_equity")
            if pub is not None and mod is not None:
                parts.append(f"public_eq {pub*100:.0f} pct / model_eq {mod*100:.0f} pct")
            elif pub is not None:
                parts.append(f"public_eq {pub*100:.0f} pct")

            # Favorite mispricing when meaningful (>= 1.5pp)
            if c["mispricing"] is not None and abs(c["mispricing"]) >= 0.015:
                pct = c["mispricing"] * 100
                direction = "overbet" if pct > 0 else "underbet"
                parts.append(f"fav {direction} by {abs(pct):.0f}pp")

            if parts:
                notes.append(
                    f"leg {c['leg_idx'] + 1} (R{c['leg_race']}): {c['mode']} "
                    f"({c['n_selected']}/{c['n_field']} selected) — "
                    f"{'; '.join(parts)}"
                )

        return notes

    def _win_only_notes(self, race: int, bet_type: str, programs) -> list[str]:
        """PROTO-T3.9 (win-only): the ITP rule "some horses are win-only —
        either they win or they're no good. Don't use them underneath in
        exotics."

        Empirically validated (handycapper TB 2010-2017, ~217K speed-fade
        starters in 8+-horse fields). A "speed_fade" type — top quintile of
        the field by both adj_v0 (high early speed) AND adj_decay (high
        fade) — shows the win-only finishing pattern ONLY in sprint races.
        Route-race speed_fade horses finish 2nd/3rd at the same rate as 1st.

        | surface×zone | n | under_to_win ratio |
        |---|---|---|
        | Dirt sprint     | 117K | 0.901 |
        | Synthetic sprint|  12K | 0.867 |
        | Turf sprint     |   5K | 0.748 |
        | Dirt route      |  65K | 0.999 (NOT win-only) |
        | Synthetic route |  10K | 1.022 (NOT win-only) |
        | Turf route      |  35K | 1.057 (NOT win-only) |

        Pace scenario does not discriminate further — the asymmetry is
        purely sprint-vs-route. Field size does not discriminate either.

        The note fires when an EXACTA or TRIFECTA registers a speed_fade
        program in the under leg of a SPRINT race. Informational only —
        the bettor may have specific reasons to expect this horse to hold
        on (front-end pace dynamics, lone speed in a slow field, etc).
        Suppressed in route races by design.
        """
        if bet_type not in ("EXACTA", "TRIFECTA"):
            return []
        if not isinstance(programs[0], (list, tuple)):
            return []
        if len(programs) < 2:
            return []

        race_df = self.card[self.card["race_number"] == race]
        if race_df.empty:
            return []
        # Sprint vs route — RKM convention: furlongs > 6.5 = route
        furlongs = race_df["furlongs"].iloc[0]
        if pd.isna(furlongs) or float(furlongs) > 6.5:
            return []

        # Identify speed_fade horses: top quintile of field by BOTH adj_v0
        # AND adj_decay. Use rank-based quintile (so a 5-horse field gets
        # exactly 1 horse per quintile).
        valid = race_df[race_df["adj_v0"].notna() & race_df["adj_decay"].notna()].copy()
        if len(valid) < 5:
            return []
        n = len(valid)
        # Top 20% rank cutoff
        cutoff = max(1, int(n * 0.2))
        v0_top = set(valid.nlargest(cutoff, "adj_v0")["program"].astype(str))
        decay_top = set(valid.nlargest(cutoff, "adj_decay")["program"].astype(str))
        speed_fade = v0_top & decay_top
        if not speed_fade:
            return []

        # Walk under legs (positions 2 onward) and flag any speed_fade
        # programs found there
        flagged: list[tuple[str, int]] = []  # (program, position_idx 2..N)
        for pos_idx in range(1, len(programs)):
            for p in programs[pos_idx]:
                if str(p) in speed_fade and (str(p), pos_idx + 1) not in flagged:
                    flagged.append((str(p), pos_idx + 1))
        if not flagged:
            return []

        flagged_strs = [f"#{p} (slot {pos})" for p, pos in flagged]
        return [(
            f"under leg includes speed-fade type(s) {', '.join(flagged_strs)} "
            f"— top quintile of this field in BOTH adj_v0 and adj_decay. "
            f"Empirical (TB 2010-2017 sprints): horses fitting this profile "
            f"finish 2nd/3rd ~10% LESS often than 1st (under_to_win ratio ~0.90 "
            f"on dirt, 0.87 synthetic, 0.75 turf). The ITP heuristic — \"they "
            f"either win or they're no good\" — empirically holds in sprint "
            f"races. Verify pace context supports including them under; "
            f"consider keying them on top instead, or excluding from under leg."
        )]

    def _kill_shot_notes(self, race: int, bet_type: str, programs) -> list[str]:
        """PROTO-T3.9: kill-shot warning on exactas keying the favorite on top.

        Empirical finding (handycapper TB 2010-2017, ~56K exactas where 1st-
        and 2nd-choice finished 1-2 in some order):
          - Favorite-on-top exactas pay 1.121× Harville fair on average
          - Upset direction (2nd-choice tops 1st-choice) pays 1.225×
          - The differential of ~9 percentage points is structural, not noise

        The kill-shot rule encodes this: "take the price on top, never the
        chalk." When register_bet receives an EXACTA where the favorite is in
        position 1 and a longer-priced horse in position 2, surface the empirical
        finding as a note — the bettor likely should flip the direction.

        Only fires for exactas keying the actual public favorite on top.
        QUINELLA is order-independent and therefore exempt.
        """
        if bet_type != "EXACTA":
            return []
        if not isinstance(programs[0], (list, tuple)):
            return []
        if len(programs) < 2:
            return []
        fav_pgm = self._favorite_in_race(race)
        if fav_pgm is None:
            return []

        # Position 1 (top) contains the favorite, position 2 (under) has any
        # horse with strictly higher odds → kill-shot pattern triggered
        top_set = [str(p) for p in programs[0]]
        if fav_pgm not in top_set:
            return []

        race_df = self.card[self.card["race_number"] == race]
        fav_row = race_df[race_df["program"].astype(str) == fav_pgm]
        if fav_row.empty:
            return []
        fav_odds = float(fav_row["closing_odds"].iloc[0]) if pd.notna(fav_row["closing_odds"].iloc[0]) else None
        if fav_odds is None:
            return []

        under_set = [str(p) for p in programs[1]]
        longer_under = []
        for pgm in under_set:
            row = race_df[race_df["program"].astype(str) == str(pgm)]
            if row.empty:
                continue
            o = row["closing_odds"].iloc[0]
            if pd.notna(o) and float(o) > fav_odds:
                longer_under.append((str(pgm), float(o)))
        if not longer_under:
            return []

        under_strs = [f"#{p} ({o:.1f})" for p, o in longer_under[:3]]
        return [(
            f"keying favorite #{fav_pgm} ({fav_odds:.1f}) on top with "
            f"{', '.join(under_strs)} underneath. Empirical (TB 2010-2017): "
            f"this direction pays ~1.12× Harville fair, while the inverse "
            f"(longer horse on top, fav 2nd) pays ~1.22× — about 9 percentage "
            f"points more overlay. Consider flipping the direction unless you "
            f"have specific information that the favorite WILL win this race."
        )]

    def _equity_warnings(self, race: int, bet_type: str, programs) -> list[str]:
        """Soft check: flag horses in the ticket that LOSE per-leg equity.

        Per simulation-protocol.md Step E.4: a horse "loses equity" when
        (odds + 1) × surviving_combos / total_combos < 1. Returns a list of
        human-readable warnings (empty if all selections gain equity).
        """
        warnings: list[str] = []

        # Determine the legs to check
        if bet_type in _HORIZONTAL_LEGS:
            legs = list(programs)  # one entry per leg
            leg_races = [race + i for i in range(len(legs))]
        elif bet_type in ("EXACTA", "QUINELLA", "TRIFECTA", "SUPERFECTA", "HI_5"):
            # Vertical: each "leg" is one finishing position in the same race
            if not isinstance(programs[0], (list, tuple)):
                return warnings  # flat-list inputs already strict
            legs = list(programs)
            leg_races = [race] * len(legs)
        else:
            return warnings

        for leg_idx, (leg_list, leg_race) in enumerate(zip(legs, leg_races)):
            n_used = len(leg_list)
            if n_used <= 1:
                continue  # singles can't lose equity by being "wide"
            race_df = self.card[self.card["race_number"] == leg_race]
            if race_df.empty:
                continue
            for pgm in leg_list:
                row = race_df[race_df["program"].astype(str) == str(pgm)]
                if row.empty:
                    continue
                odds_val = row["closing_odds"].iloc[0]
                if pd.isna(odds_val) or odds_val <= 0:
                    continue
                ratio = (float(odds_val) + 1.0) / n_used
                if ratio < 1.0:
                    leg_label = (f"leg {leg_idx + 1} (R{leg_race})"
                                 if bet_type in _HORIZONTAL_LEGS
                                 else f"position {leg_idx + 1}")
                    warnings.append(
                        f"#{pgm} at {float(odds_val):.1f} in {leg_label} "
                        f"loses equity ({ratio:.2f} < 1.0): {n_used}-deep too wide for this price"
                    )
        return warnings

    def register_bet(self, race: int, bet_type: str, programs, amount: float, rationale: str,
                     *, force: bool = False, basket_id: str | None = None):
        """Register a bet with explicit program numbers.

        Validates structure deterministically (programs in race, bet type whitelist,
        WIN minimum odds). Raises ValueError on invalid bets unless force=True.
        Also runs the equity test (simulation-protocol.md Step E.4) as a soft
        check — prints warnings for selections that lose equity, but registers
        the bet anyway. Use force=True to silence equity warnings entirely.

        `basket_id` (optional) tags this bet as part of a named strategic
        opinion. When set, an aggregate-exposure note fires once a second bet
        joins the basket, and reveal_and_evaluate prints per-basket P&L
        rollups. Untagged bets behave exactly as before.
        """
        if not force:
            self._validate_bet(race, bet_type, programs, amount)
            # `equity` is a warning (math says you've lost EV in this slot).
            # The rest are informational notes — the bettor may have a reason
            # the simulator can't see (vulnerable-fav structural play, single
            # high-equity leg carrying a horizontal, etc).
            checks = [
                ("warning", "equity",     self._equity_warnings(race, bet_type, programs)),
                ("note",    "fav-excl",   self._favorite_exclusion_notes(race, bet_type, programs)),
                ("note",    "pool",       self._pool_notes(race, bet_type, amount)),
                ("note",    "horiz-conv", self._horizontal_conviction_notes(race, bet_type, programs)),
                ("note",    "hurdle",     self._hurdle_notes(race, bet_type, programs)),
                ("note",    "kill-shot",  self._kill_shot_notes(race, bet_type, programs)),
                ("note",    "win-only",   self._win_only_notes(race, bet_type, programs)),
                ("note",    "press",      self._press_notes(race, bet_type, programs, amount)),
                ("note",    "basket",     self._basket_exposure_notes(basket_id, race, bet_type, programs, amount)),
            ]
            for kind, tag, msgs in checks:
                for m in msgs:
                    print(f"  [{tag} {kind}] R{race} {bet_type}: {m}")
        bet = Bet(race=race, bet_type=bet_type, programs=programs,
                  amount=amount, rationale=rationale, basket_id=basket_id)
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
        """Load results and mechanically match against registered bets.

        Deterministic — given (registered bets, race results), produces an
        unambiguous (hit, payout) for every bet. Supports WIN, EXACTA,
        QUINELLA, TRIFECTA, SUPERFECTA, DAILY_DOUBLE, PICK_3/4/5/6. Uses
        `official_position` for the finishing order so DQs are honored.
        """
        from sim.blinder import load_race_results
        self.results = load_race_results(conn, self.track, self.date)

        print("\n" + "=" * 60)
        print("RESULTS & EVALUATION")
        print("=" * 60)

        # Pre-compute per-race finish data and exotic payoffs by bet_type
        race_data = self._build_race_data(conn)

        total_invested = sum(b.amount for b in self.bets)
        total_returned = 0.0

        for rn in sorted(race_data.keys()):
            d = race_data[rn]
            top4 = d["finish_order"][:4]

            # Identify favorite (lowest closing odds) for asterisk marker
            full_finish = d["finish_order"]
            fav_pgm = None
            if full_finish:
                with_odds = [f for f in full_finish if f.get("odds") and f["odds"] > 0]
                if with_odds:
                    fav_pgm = min(with_odds, key=lambda f: f["odds"])["program"]

            def _fmt_horse(r):
                star = "*" if r["program"] == fav_pgm else ""
                return f"#{r['program']} {r['horse']} ({r['odds']:.1f}{star})"

            top4_str = " → ".join(_fmt_horse(r) for r in top4)
            print(f"\n  R{rn}: {top4_str}")

            # Winner WPS — show per-$1 and base-unit payout
            if top4:
                w = top4[0]
                wps_parts = []
                if w.get("win_per_1") is not None:
                    wps_parts.append(f"WIN {w['win_per_1']:.2f} (${w['win_unit']:g} base → ${w['win_per_1']*w['win_unit']:.2f})")
                if len(top4) > 1 and top4[1].get("place_per_1") is not None:
                    p = top4[1]
                    wps_parts.append(f"PLACE {p['place_per_1']:.2f} (${p['place_unit']:g} base → ${p['place_per_1']*p['place_unit']:.2f})")
                if wps_parts:
                    print(f"       {'  '.join(wps_parts)}")

            # Vertical exotics
            if d["payoffs"]:
                base_units = d.get("base_units", {})
                payoff_strs = []
                for bt, p in d["payoffs"].items():
                    bu = base_units.get(bt)
                    if bu is not None:
                        payoff_strs.append(f"{bt} {p:.2f} (${bu:g} base → ${p*bu:.2f})")
                    else:
                        payoff_strs.append(f"{bt} {p:.2f}")
                for i in range(0, len(payoff_strs), 2):
                    print(f"       {'  '.join(payoff_strs[i:i+2])}")

            for bet in [b for b in self.bets if b.race == rn]:
                hit, payout, breakdown = self._evaluate_bet(bet, race_data)
                if hit:
                    per_1 = breakdown.get("per_1", 0.0)
                    n_combos = breakdown.get("n_combos", 1)
                    bu = breakdown.get("base_unit")
                    bu_str = f"${bu:g} base" if bu else ""
                    if n_combos > 1:
                        stake_per = breakdown.get("stake_per_combo", 0.0)
                        math_str = (f"  → ${payout:.2f}  "
                                    f"[{per_1:.2f} × ${stake_per:.2f}/combo × {n_combos} combos; {bu_str}]".rstrip("; "))
                    else:
                        math_str = f"  → ${payout:.2f}  [{per_1:.2f} × ${bet.amount:.2f}; {bu_str}]".rstrip("; ")
                    print(f"       ✓ HIT: {bet.bet_type} {_format_programs(bet.programs)} ${bet.amount:.2f}{math_str}")
                    total_returned += payout
                else:
                    reason = breakdown.get("reason", "")
                    reason_str = f"  ({reason})" if reason and reason != "lost" else ""
                    print(f"       ✗ MISS: {bet.bet_type} {_format_programs(bet.programs)} ${bet.amount:.2f}{reason_str}")

        baskets: dict[str, dict] = {}
        for bet in self.bets:
            if not bet.basket_id:
                continue
            hit, payout, _ = self._evaluate_bet(bet, race_data)
            entry = baskets.setdefault(bet.basket_id, {"invested": 0.0, "returned": 0.0, "hits": 0, "n": 0})
            entry["invested"] += bet.amount
            entry["returned"] += payout if hit else 0.0
            entry["hits"]     += 1 if hit else 0
            entry["n"]        += 1

        if baskets:
            print(f"\n  {'-' * 40}")
            print("  BASKET ROLLUPS")
            for bid, e in sorted(baskets.items()):
                pnl = e["returned"] - e["invested"]
                roi = ((e["returned"] / e["invested"]) - 1) * 100 if e["invested"] > 0 else 0.0
                print(f"    {bid}: {e['hits']}/{e['n']} hits, "
                      f"${e['invested']:.2f} in, ${e['returned']:.2f} out, "
                      f"P&L ${pnl:+.2f} (ROI {roi:+.1f}%)")

        print(f"\n  {'=' * 40}")
        print(f"  Total invested: ${total_invested:.2f}")
        print(f"  Total returned: ${total_returned:.2f}")
        print(f"  P&L: ${total_returned - total_invested:.2f}")
        if total_invested > 0:
            print(f"  ROI: {((total_returned / total_invested) - 1) * 100:.1f}%")
        print(f"  {'=' * 40}")

    def _build_race_data(self, conn) -> dict:
        """Pre-compute per-race finish + payoff data keyed by race_number.

        For each race, returns:
          - finish_order: list of {program, horse, odds, official, win/place/show payoff} dicts
          - payoffs: {bet_type: per_dollar_payoff} for all exotic types
          - base_units: {bet_type: chart's base bet unit} for transparency

        All payoffs are normalized per $1, so multiplying by stake yields the
        gross payout regardless of whether the chart published per-$2 or per-$0.50.
        """
        race_data = {}
        for rn in sorted(self.results["race_number"].unique()):
            race_results = self.results[self.results["race_number"] == rn] \
                .sort_values("official_position")
            finish = []
            for _, row in race_results.iterrows():
                pgm = str(row["program"])
                # Per-$1 WPS payoffs from the wps table (base unit is typically $2,
                # so payoff_raw / unit normalizes to per-$1).
                win_per_1 = (float(row["win_payoff_raw"]) / float(row["win_unit"])
                             if pd.notna(row.get("win_payoff_raw")) and row.get("win_unit") else None)
                place_per_1 = (float(row["place_payoff_raw"]) / float(row["place_unit"])
                               if pd.notna(row.get("place_payoff_raw")) and row.get("place_unit") else None)
                show_per_1 = (float(row["show_payoff_raw"]) / float(row["show_unit"])
                              if pd.notna(row.get("show_payoff_raw")) and row.get("show_unit") else None)
                finish.append({
                    "program":  pgm,
                    "horse":    row["horse_name"],
                    "odds":     float(row["odds"]) if pd.notna(row["odds"]) else 0.0,
                    "official": int(row["official_position"]) if pd.notna(row["official_position"]) else None,
                    "win_per_1":   win_per_1,
                    "place_per_1": place_per_1,
                    "show_per_1":  show_per_1,
                    "win_unit":    float(row["win_unit"])   if pd.notna(row.get("win_unit"))   else None,
                    "place_unit":  float(row["place_unit"]) if pd.notna(row.get("place_unit")) else None,
                    "show_unit":   float(row["show_unit"])  if pd.notna(row.get("show_unit"))  else None,
                })

            payoffs = {}
            base_units = {}
            for bt, col, unit_col in [
                ("EXACTA",     "exacta_payoff",   "exacta_unit"),
                ("QUINELLA",   "quinella_payoff", "quinella_unit"),
                ("TRIFECTA",   "trifecta_payoff", "trifecta_unit"),
                ("SUPERFECTA", "super_payoff",    "super_unit"),
                ("HI_5",       "hi5_payoff",      "hi5_unit"),
            ]:
                if col in race_results.columns and race_results[col].notna().any():
                    payoffs[bt] = float(race_results[col].dropna().iloc[0])
                    if unit_col in race_results.columns and race_results[unit_col].notna().any():
                        base_units[bt] = float(race_results[unit_col].dropna().iloc[0])
            race_data[rn] = {"finish_order": finish, "payoffs": payoffs, "base_units": base_units}

        # Horizontal + DD payoffs come from the exotics table directly (not in
        # load_race_results' per-starter join shape).
        with conn.cursor() as cur:
            cur.execute("""
                SELECT r.number AS race_number,
                       e.bet_type, e.payoff, e.unit
                FROM handycapper.exotics e
                JOIN handycapper.exotic_race_legs erl
                    ON erl.exotic_id = e.id AND erl.leg_number = 1
                JOIN handycapper.races r ON r.id = erl.race_id
                WHERE r.id IN (
                    SELECT id FROM handycapper.races
                    WHERE track = %(track)s AND date = %(date)s
                )
                AND e.payoff > 0 AND e.unit > 0
                AND e.pool_type = 'STANDARD'
                AND e.bet_type IN ('DAILY_DOUBLE','PICK_3','PICK_4','PICK_5','PICK_6')
            """, {"track": self.track, "date": self.date})
            for race_number, bet_type, payoff, unit in cur.fetchall():
                if race_number in race_data:
                    race_data[race_number]["payoffs"][bet_type] = float(payoff) / float(unit)
                    race_data[race_number]["base_units"][bet_type] = float(unit)
        return race_data

    def _evaluate_bet(self, bet, race_data: dict) -> tuple:
        """Return (hit, payout, breakdown) for a registered bet.

        breakdown is a dict describing the payoff math (base unit, per-$1 payoff,
        n_combos, hit_count) so callers can render transparent output.
        """
        rn = bet.race
        if rn not in race_data:
            return False, 0.0, {"reason": "race not in card"}
        d = race_data[rn]
        finish = d["finish_order"]
        payoffs = d["payoffs"]
        base_units = d.get("base_units", {})

        bt = bet.bet_type
        progs = bet.programs

        # Vertical bets — match official top finishers
        if bt == "WIN":
            if not finish:
                return False, 0.0, {"reason": "no finish data"}
            winner = finish[0]
            if str(progs[0]) != winner["program"]:
                return False, 0.0, {"reason": "lost"}
            # Prefer the actual WIN payoff from the wps table; fall back to
            # (odds + 1) × stake only if WPS data is missing (older charts or
            # races where the chart was thin).
            if winner.get("win_per_1") is not None:
                per_1 = winner["win_per_1"]
                payout = per_1 * bet.amount
                breakdown = {
                    "source": "wps table",
                    "base_unit": winner.get("win_unit") or 2.0,
                    "per_1": per_1,
                    "n_combos": 1,
                    "stake": bet.amount,
                }
            else:
                per_1 = (winner["odds"] + 1) / 2.0  # equiv per-$1 from per-$2 odds quote
                payout = bet.amount * (winner["odds"] + 1)
                breakdown = {
                    "source": "fallback from odds",
                    "base_unit": 2.0,
                    "per_1": per_1,
                    "n_combos": 1,
                    "stake": bet.amount,
                }
            return True, payout, breakdown

        if bt in ("EXACTA", "QUINELLA", "TRIFECTA", "SUPERFECTA"):
            n_pos = _BET_TYPE_POSITIONS[bt]
            if len(finish) < n_pos:
                return False, 0.0, {"reason": "incomplete finish"}
            top_n = [f["program"] for f in finish[:n_pos]]
            base_pay = payoffs.get(bt)
            if base_pay is None:
                return False, 0.0, {"reason": f"no {bt} payoff in chart"}

            if not isinstance(progs[0], (list, tuple)):
                progs_list = [[p] for p in progs]
            else:
                progs_list = [list(p) for p in progs]

            if bt == "QUINELLA":
                hit = (str(top_n[0]) in [str(p) for p in progs_list[0]] and
                       str(top_n[1]) in [str(p) for p in progs_list[1]]) or \
                      (str(top_n[1]) in [str(p) for p in progs_list[0]] and
                       str(top_n[0]) in [str(p) for p in progs_list[1]])
            else:
                hit = all(str(top_n[i]) in [str(p) for p in progs_list[i]] for i in range(n_pos))

            n_combos = 1
            for pos_list in progs_list:
                n_combos *= len(pos_list)

            if hit:
                stake_per_combo = bet.amount / n_combos
                payout = base_pay * stake_per_combo
                return True, payout, {
                    "source": "exotics table",
                    "base_unit": base_units.get(bt),
                    "per_1": base_pay,
                    "n_combos": n_combos,
                    "stake_per_combo": stake_per_combo,
                    "stake": bet.amount,
                }
            return False, 0.0, {"reason": "lost"}

        # Horizontal bets — walk leg-by-leg
        if bt in _HORIZONTAL_LEGS:
            base_pay = payoffs.get(bt)
            if base_pay is None:
                return False, 0.0, {"reason": f"no {bt} payoff in chart"}
            for leg_idx, leg_list in enumerate(progs):
                leg_rn = rn + leg_idx
                if leg_rn not in race_data or not race_data[leg_rn]["finish_order"]:
                    return False, 0.0, {"reason": f"leg {leg_idx + 1} race not in card"}
                leg_winner = race_data[leg_rn]["finish_order"][0]["program"]
                if leg_winner not in [str(p) for p in leg_list]:
                    return False, 0.0, {"reason": f"lost leg {leg_idx + 1}"}
            n_combos = 1
            for leg_list in progs:
                n_combos *= len(leg_list)
            stake_per_combo = bet.amount / n_combos
            return True, base_pay * stake_per_combo, {
                "source": "exotics table",
                "base_unit": base_units.get(bt),
                "per_1": base_pay,
                "n_combos": n_combos,
                "stake_per_combo": stake_per_combo,
                "stake": bet.amount,
            }

        return False, 0.0, {"reason": f"unsupported bet_type {bt}"}


def main():
    parser = argparse.ArgumentParser(description="Run a blinded race day simulation")
    parser.add_argument("--track", help="Track code")
    parser.add_argument("--date", help="Race date (YYYY-MM-DD)")
    parser.add_argument("--seed", help="Hash seed to pick a random day")
    args = parser.parse_args()

    if args.seed:
        from pick_sim_day import pick_day
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
        opinion = sim.classify_opinion(rn)
        edge_str = f"+{s['top_edge']}" if s["top_edge"] and s["top_edge"] > 0 else "—"
        print(f"  R{rn:2d}: {s['surface']:5s} {s['furlongs']:.1f}f | {s['race_type'][:25]:25s} | "
              f"{s['n_rated']}/{s['field_size']} rated | edge: {edge_str:>4s} | "
              f"{opinion['opinion']:17s} | {s['pace_scenario']}")

    print("\n" + "=" * 60)
    print("OPINIONS BY RACE")
    print("=" * 60)

    for rn in sorted(sim.card["race_number"].unique()):
        opinion = sim.classify_opinion(rn)
        if opinion["opinion"] == "NO_OPINION":
            continue
        s = sim.race_summary(rn)
        print(f"\n  R{rn}: {s['surface']} {s['furlongs']}f {s['race_type']}")
        print(f"    Opinion:     {opinion['opinion']}")
        print(f"    Rationale:   {opinion['rationale']}")
        print(f"    Recommended: {opinion['recommended']}")
        if opinion.get("hint"):
            print(f"    Hint:        {opinion['hint']}")
        if opinion.get("flb_warning"):
            print(f"    FLB warning: {opinion['flb_warning']}")

        proposals = sim.propose_ticket_structures(rn, opinion)
        if not proposals:
            continue

        print(f"\n    Equity table (ratios at depth N):")
        print(f"      {'#':>3s}  {'horse':<22s} {'odds':>5s}  {'edge':>6s}  {'role':<12s}  "
              f"{'N=2':>5s}  {'N=3':>5s}  {'N=4':>5s}  {'N=5':>5s}")
        for r in proposals["equity_table"][:8]:
            print(f"      {r['program']:>3s}  {r['horse'][:22]:<22s} {r['odds']:>5.1f}  "
                  f"{r['edge']:>+6.1f}  {r['role']:<12s}  "
                  f"{r['ratios'][2]:>5.2f}  {r['ratios'][3]:>5.2f}  "
                  f"{r['ratios'][4]:>5.2f}  {r['ratios'][5]:>5.2f}")

        for b in proposals["baskets"]:
            print(f"\n    {b['label'].upper()} ticket: {b['shape']}")
            print(f"      {b['rationale']}")

    print("\n" + "-" * 60)
    print("REGISTER BETS:")
    print("  sim.register_bet(race, bet_type, programs, amount, rationale)")
    print("  e.g. sim.register_bet(2, 'WIN', ['7'], 10, 'STRONG specific #7')")
    print("       sim.register_bet(3, 'TRIFECTA', [['1'],['2','5'],['2','5']], 4, 'key/AB/AB')")
    print("  optional basket_id= tags multi-bet strategic plays for aggregate P&L:")
    print("       sim.register_bet(2, 'WIN', ['7'], 10, 'key', basket_id='r2-key-7')")
    print("       sim.register_bet(2, 'EXACTA', [['7'],['1','2']], 4, 'cover', basket_id='r2-key-7')")
    print("-" * 60)

    conn.close()
    return sim


if __name__ == "__main__":
    main()
