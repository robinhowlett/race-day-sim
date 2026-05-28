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
    race: int
    bet_type: str  # WIN, EXACTA, TRIFECTA, PICK3, etc.
    programs: list  # program numbers involved
    amount: float
    rationale: str

    def __str__(self):
        return f"R{self.race} {self.bet_type} {_format_programs(self.programs)} ${self.amount:.2f} — {self.rationale}"


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
                     *, force: bool = False):
        """Register a bet with explicit program numbers.

        Validates structure deterministically (programs in race, bet type whitelist,
        WIN minimum odds). Raises ValueError on invalid bets unless force=True.
        Also runs the equity test (simulation-protocol.md Step E.4) as a soft
        check — prints warnings for selections that lose equity, but registers
        the bet anyway. Use force=True to silence equity warnings entirely.
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
            ]
            for kind, tag, msgs in checks:
                for m in msgs:
                    print(f"  [{tag} {kind}] R{race} {bet_type}: {m}")
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
    print("-" * 60)

    conn.close()
    return sim


if __name__ == "__main__":
    main()
