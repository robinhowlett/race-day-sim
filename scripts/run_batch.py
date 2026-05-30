"""Multi-day batch simulator for FLB integration validation.

Picks N deterministic sim days from sim_candidates, runs the simulator on
each, auto-bets according to the chosen policy, evaluates against actual
results, and aggregates ROI / hit rate / pick density across the batch.

Three policies, ordered narrowest → broadest:

  policy=win
    Auto-bet $1 WIN on every horse the FLB+rating-edge filter passes.
    Smallest scope, most directly comparable to the FLB POC's WIN-bet
    OOS validation. Skips horses below MIN_ODDS_WIN_BET (3.0/1).

  policy=opinion
    Auto-execute the simulator's `recommended` field when the opinion class
    is STRONG_SPECIFIC or MODERATE_SPECIFIC. WIN bets where recommended;
    EXACTA/TRIFECTA keys parsed and bet at flat $1 per combo otherwise.
    Tests whether the opinion classifier picks the right wager structure.

  policy=full
    Like opinion, plus STRUCTURAL pace plays and STRONG_NEGATIVE
    fade-the-fav trifectas. Highest variance, hardest to attribute.

Output: per-day summary table + aggregate stats. Writes a JSON dump for
post-hoc analysis.

Usage:
    python scripts/run_batch.py --n 50 --policy win --seed batch-2026-05-29
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sim.db import get_connection
from run_simulation import MIN_ODDS_WIN_BET, SimDay


def get_sim_candidates(conn) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT track, date FROM handycapper.sim_candidates
            ORDER BY date, track
        """)
        return [(r[0], str(r[1])) for r in cur.fetchall()]


def pick_days(seed: str, n: int, candidates: list[tuple]) -> list[tuple]:
    """Pick N distinct days deterministically from the candidate pool.

    Different seed text → different N-day sequence; same seed always picks
    the same sequence. Uses a hash chain so picks don't collide unless the
    pool is exhausted.
    """
    out = []
    used = set()
    i = 0
    while len(out) < n and len(used) < len(candidates):
        h = hashlib.sha256(f"{seed}|{i}".encode()).hexdigest()
        idx = int(h, 16) % len(candidates)
        if idx not in used:
            used.add(idx)
            out.append(candidates[idx])
        i += 1
    return out


def _bet_win_policy(sim: SimDay) -> list[dict]:
    """policy=win — $1 WIN on every FLB+rating-edge candidate above MIN_ODDS_WIN_BET."""
    bets = []
    for rn in sorted(sim.card["race_number"].unique()):
        rn = int(rn)
        check = sim.protocol_check(rn)
        for cand in check["candidates"]:
            odds = cand.get("odds")
            if odds is None or odds < MIN_ODDS_WIN_BET:
                continue
            flb_edge = cand.get("flb_edge")
            flb_thr = cand.get("flb_threshold")
            flb_tier = cand.get("flb_tier")
            if flb_edge is not None:
                rationale = (f"FLB-validated WIN: {flb_tier} "
                             f"flb_edge {flb_edge:+.4f} ≥ {flb_thr}, "
                             f"worst_case {cand['worst_case']:+.1f}")
            else:
                # No Benter coverage; legacy rating-edge gate alone admitted this candidate.
                rationale = (f"rating-edge WIN (no FLB coverage): "
                             f"worst_case {cand['worst_case']:+.1f}")
            bets.append({
                "race": rn,
                "bet_type": "WIN",
                "programs": [str(cand["program"])],
                "amount": 1.0,
                "rationale": rationale,
            })
    return bets


def _parse_recommended_key(rec: str) -> tuple[str, str] | None:
    """Best-effort parse of an exotic-key 'recommended' string.

    Looks for shapes like 'EXACTA key #N' / 'TRIFECTA key #N' and returns
    (bet_type, program). Returns None for anything richer (a full
    structural ticket needs explicit programs we can't infer here).
    """
    rec = rec.strip()
    for bt in ("TRIFECTA", "EXACTA", "SUPERFECTA"):
        token = f"{bt} key #"
        if token in rec or token.replace("key #", "/TRIFECTA key #") in rec:
            # Look for the # number
            after = rec.split("#", 1)[1] if "#" in rec else ""
            pgm = "".join(c for c in after if c.isalnum())
            if pgm:
                return bt, pgm
    return None


def _bet_opinion_policy(sim: SimDay) -> list[dict]:
    """policy=opinion — execute simulator's recommendation for STRONG/MODERATE only."""
    bets = []
    for rn in sorted(sim.card["race_number"].unique()):
        rn = int(rn)
        opinion = sim.classify_opinion(rn)
        opclass = opinion["opinion"]
        if opclass not in ("STRONG_SPECIFIC", "MODERATE_SPECIFIC"):
            continue
        rec = opinion.get("recommended", "PASS")
        details = opinion.get("details", {})
        key_pgm = details.get("key_program")
        if rec.startswith("WIN #"):
            pgm = rec.split("#", 1)[1].strip().split()[0]
            bets.append({
                "race": rn, "bet_type": "WIN", "programs": [pgm],
                "amount": 1.0, "rationale": f"{opclass}: {opinion['rationale']}",
            })
        elif key_pgm and ("EXACTA" in rec or "TRIFECTA" in rec):
            # Build an exacta key on top: #key over all others, $1 per combo.
            race_df = sim.card[sim.card["race_number"] == rn]
            others = [str(p) for p in race_df["program"].astype(str).tolist() if str(p) != str(key_pgm)]
            if not others:
                continue
            bets.append({
                "race": rn, "bet_type": "EXACTA",
                "programs": [[str(key_pgm)], others],
                "amount": float(len(others)),  # $1 per combo
                "rationale": f"{opclass} key: {opinion['rationale']}",
            })
        # MODERATE_SPECIFIC's "horizontal leg" recommendation is hard to
        # auto-construct without a full opinion-by-race pass; skip in this
        # policy. policy=full handles richer cases.
    return bets


def _bet_full_policy(sim: SimDay) -> list[dict]:
    """policy=full — opinion + STRUCTURAL closer-on-top tris + STRONG_NEGATIVE fade-fav tris."""
    bets = list(_bet_opinion_policy(sim))
    for rn in sorted(sim.card["race_number"].unique()):
        rn = int(rn)
        opinion = sim.classify_opinion(rn)
        opclass = opinion["opinion"]
        details = opinion.get("details", {})
        race_df = sim.card[sim.card["race_number"] == rn]
        all_pgms = [str(p) for p in race_df["program"].astype(str).tolist()]

        if opclass == "STRUCTURAL":
            closers = details.get("closers", [])
            if len(closers) >= 2:
                others = [p for p in all_pgms if p not in closers]
                pos1 = closers[:3]
                pos2 = closers[:3]
                pos3 = others or all_pgms
                if pos3:
                    n_combos = sum(
                        1 for a in pos1 for b in pos2 for c in pos3
                        if len({a, b, c}) == 3
                    )
                    if n_combos > 0:
                        bets.append({
                            "race": rn, "bet_type": "TRIFECTA",
                            "programs": [pos1, pos2, pos3],
                            "amount": float(n_combos),  # $1 per combo
                            "rationale": f"STRUCTURAL closers/closers/others: {opinion['rationale']}",
                        })

        elif opclass == "STRONG_NEGATIVE":
            fav = details.get("fav_program")
            if fav:
                non_fav = [p for p in all_pgms if p != str(fav)]
                if len(non_fav) >= 3:
                    n_combos = len(non_fav) * (len(non_fav) - 1) * (len(non_fav) - 2)
                    bets.append({
                        "race": rn, "bet_type": "TRIFECTA",
                        "programs": [non_fav, non_fav, non_fav],
                        "amount": float(n_combos),  # $1 per combo
                        "rationale": f"STRONG_NEGATIVE fade-fav: {opinion['rationale']}",
                    })
    return bets


def _bet_exotic_policy(sim: SimDay) -> list[dict]:
    """policy=exotic — projected-payoff overlay filter on all 5 bet types.

    For each race, for each supported bet type, query
    sim.exotic_overlay_filter(rn, bet_type, er_threshold=1.30) and
    register $1 on every combo passing the filter. This is the production
    code path equivalent of the POC's policy: every ER-passing combo gets
    a $1 stake.

    Note: this can produce hundreds of bets per race on a deep field.
    The POC-validated headline ROIs assume zero market impact; real-world
    deployment would need stake sizing constraints. Headline numbers tell
    you whether the signal generalizes; deployable stake sizing is a
    separate engineering problem.
    """
    VERTICAL = {"EXACTA": 2, "TRIFECTA": 3, "SUPERFECTA": 4}
    HORIZONTAL = {"PICK_3": 3, "PICK_4": 4}

    bets = []
    race_numbers = sorted(int(r) for r in sim.card["race_number"].unique())
    max_rn = race_numbers[-1] if race_numbers else 0

    for rn in race_numbers:
        # Verticals: one race
        for bt, n_pos in VERTICAL.items():
            combos = sim.exotic_overlay_filter(rn, bt, er_threshold=1.30)
            for c in combos:
                # Each combo's programs is already a list of strings;
                # convert to the position-of-lists structure register_bet expects
                position_lists = [[str(p)] for p in c["programs"]]
                bets.append({
                    "race": rn,
                    "bet_type": bt,
                    "programs": position_lists,
                    "amount": 1.0,
                    "rationale": (f"{bt} overlay: ER={c['er']:.2f} "
                                  f"(harv {c['harv_prob']:.4f} × proj ${c['proj_pay']:.0f})"),
                })

        # Horizontals: must have N legs ahead in the card
        for bt, n_legs in HORIZONTAL.items():
            if rn + n_legs - 1 > max_rn:
                continue
            combos = sim.exotic_overlay_filter(rn, bt, er_threshold=1.30)
            for c in combos:
                # Horizontal: programs is list-of-leg-lists, race = first leg
                leg_lists = [[str(p)] for p in c["programs"]]
                bets.append({
                    "race": rn,
                    "bet_type": bt,
                    "programs": leg_lists,
                    "amount": 1.0,
                    "rationale": (f"{bt} overlay: ER={c['er']:.2f} "
                                  f"(parlay {c['harv_prob']:.5f} × proj ${c['proj_pay']:.0f})"),
                })
    return bets


_POLICY_FNS = {
    "win":     _bet_win_policy,
    "opinion": _bet_opinion_policy,
    "full":    _bet_full_policy,
    "exotic":  _bet_exotic_policy,
}


def run_one_day(conn, track: str, date: str, policy: str) -> dict:
    """Load a day, auto-bet per policy, evaluate, return aggregated stats."""
    sim = SimDay(track, date)
    sim.load(conn)
    bet_specs = _POLICY_FNS[policy](sim)

    # Register silently — _validate_bet still fires, but we suppress its prints.
    n_register_failures = 0
    with contextlib.redirect_stdout(io.StringIO()):
        for spec in bet_specs:
            try:
                sim.register_bet(
                    spec["race"], spec["bet_type"], spec["programs"],
                    spec["amount"], spec["rationale"],
                )
            except ValueError:
                n_register_failures += 1

    # Evaluate silently.
    with contextlib.redirect_stdout(io.StringIO()):
        from sim.blinder import load_race_results
        sim.results = load_race_results(conn, sim.track, sim.date)
        race_data = sim._build_race_data(conn)

    invested = 0.0
    returned = 0.0
    n_hits = 0
    n_bets = len(sim.bets)
    per_bet = []
    for bet in sim.bets:
        hit, payout, breakdown = sim._evaluate_bet(bet, race_data)
        invested += bet.amount
        if hit:
            returned += payout
            n_hits += 1
        per_bet.append({
            "race": bet.race, "bet_type": bet.bet_type,
            "amount": bet.amount, "hit": hit, "payout": payout,
        })

    return {
        "track": track, "date": date, "policy": policy,
        "n_races": int(sim.card["race_number"].nunique()),
        "n_bets": n_bets, "n_hits": n_hits,
        "n_register_failures": n_register_failures,
        "invested": round(invested, 2),
        "returned": round(returned, 2),
        "pnl": round(returned - invested, 2),
        "roi": (returned / invested - 1) if invested > 0 else None,
        "bets": per_bet,
    }


def aggregate(results: list[dict]) -> dict:
    """Roll up per-day results into batch-level stats, with per-bet-type breakout."""
    invested = sum(r["invested"] for r in results)
    returned = sum(r["returned"] for r in results)
    n_bets = sum(r["n_bets"] for r in results)
    n_hits = sum(r["n_hits"] for r in results)
    n_days_played = sum(1 for r in results if r["n_bets"] > 0)

    # Per-bet-type breakout
    by_type: dict[str, dict] = {}
    for r in results:
        for bet in r.get("bets", []):
            bt = bet["bet_type"]
            entry = by_type.setdefault(bt, {"n_bets": 0, "n_hits": 0,
                                             "invested": 0.0, "returned": 0.0})
            entry["n_bets"] += 1
            entry["invested"] += bet["amount"]
            if bet["hit"]:
                entry["n_hits"] += 1
                entry["returned"] += bet["payout"]
    for bt, e in by_type.items():
        e["roi"] = (e["returned"] / e["invested"] - 1) if e["invested"] > 0 else None
        e["hit_rate"] = (e["n_hits"] / e["n_bets"]) if e["n_bets"] else None
        e["pnl"] = round(e["returned"] - e["invested"], 2)
        e["invested"] = round(e["invested"], 2)
        e["returned"] = round(e["returned"], 2)

    return {
        "n_days": len(results),
        "n_days_played": n_days_played,
        "n_bets": n_bets,
        "n_hits": n_hits,
        "hit_rate": (n_hits / n_bets) if n_bets else None,
        "invested": round(invested, 2),
        "returned": round(returned, 2),
        "pnl": round(returned - invested, 2),
        "roi": (returned / invested - 1) if invested > 0 else None,
        "by_bet_type": by_type,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--policy", choices=list(_POLICY_FNS), default="win")
    ap.add_argument("--seed", default="batch-2026-05-29")
    ap.add_argument("--out", default=None,
                    help="Path to JSON output (default: tmp/batch_<policy>_<n>.json)")
    args = ap.parse_args()

    conn = get_connection()
    candidates = get_sim_candidates(conn)
    print(f"Candidate pool: {len(candidates):,} race days")
    days = pick_days(args.seed, args.n, candidates)
    print(f"Picked {len(days)} days with seed {args.seed!r}, policy={args.policy}")
    print()

    results = []
    print(f"{'#':>3}  {'date':<10}  {'trk':<4}  {'races':>5}  {'bets':>4}  "
          f"{'hits':>4}  {'invested':>9}  {'returned':>9}  {'P&L':>9}  {'ROI':>7}")
    print("-" * 80)
    t0 = time.time()
    for i, (track, date) in enumerate(days, 1):
        try:
            r = run_one_day(conn, track, date, args.policy)
            results.append(r)
            roi_s = f"{100*r['roi']:+.1f}%" if r["roi"] is not None else "  —  "
            print(f"{i:>3}  {date:<10}  {track:<4}  {r['n_races']:>5}  "
                  f"{r['n_bets']:>4}  {r['n_hits']:>4}  "
                  f"${r['invested']:>7.2f}  ${r['returned']:>7.2f}  "
                  f"${r['pnl']:>+7.2f}  {roi_s:>7}")
        except Exception as e:
            print(f"{i:>3}  {date:<10}  {track:<4}  ERROR: {e!s}")
    elapsed = time.time() - t0

    print("-" * 80)
    agg = aggregate(results)
    print(f"\n=== AGGREGATE ({args.policy}, n={args.n} requested, {len(results)} completed) ===")
    print(f"  Days played (≥1 bet): {agg['n_days_played']} / {agg['n_days']}")
    print(f"  Total bets:           {agg['n_bets']:,}")
    print(f"  Hits:                 {agg['n_hits']:,}")
    if agg["hit_rate"] is not None:
        print(f"  Hit rate:             {100*agg['hit_rate']:.1f}%")
    print(f"  Invested:             ${agg['invested']:,.2f}")
    print(f"  Returned:             ${agg['returned']:,.2f}")
    print(f"  P&L:                  ${agg['pnl']:+,.2f}")
    if agg["roi"] is not None:
        print(f"  ROI:                  {100*agg['roi']:+.2f}%")
    print(f"  Wall time:            {elapsed:.1f}s ({elapsed/max(len(results),1):.1f}s/day)")

    # Per-bet-type breakout (matters most for policy=exotic where multiple
    # types fire concurrently; harmless on win/opinion which use one type).
    by_type = agg.get("by_bet_type", {})
    if len(by_type) > 1:
        print(f"\n  Per-bet-type:")
        print(f"  {'type':<12} {'bets':>8} {'hits':>6} {'invested':>11}"
              f" {'returned':>11} {'P&L':>10} {'ROI':>9}")
        for bt in sorted(by_type, key=lambda k: -by_type[k]["n_bets"]):
            e = by_type[bt]
            roi_s = f"{100*e['roi']:+.1f}%" if e["roi"] is not None else "  —  "
            print(f"  {bt:<12} {e['n_bets']:>8,} {e['n_hits']:>6,}"
                  f" ${e['invested']:>9,.2f} ${e['returned']:>9,.2f}"
                  f" ${e['pnl']:>+8.2f} {roi_s:>9}")

    out_path = Path(args.out) if args.out else (
        Path(__file__).resolve().parent / f"../tmp/batch_{args.policy}_{args.n}_{args.seed}.json"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"args": vars(args), "agg": agg, "per_day": results}, f, indent=2, default=str)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
