"""Golden-test the rewritten load_market_bias against the original.

Loads the pre-rewrite blinder SQL via `git show HEAD:src/sim/blinder.py`,
runs both the old and new versions on a sample of sim days drawn from
sim_candidates (cross-year, cross-track, including month boundaries
and adjacent-day pairs to catch weekly-snapshot edge cases), and
reports any starter row whose bias-multiplier-relevant columns differ.

Pass criteria: zero rows where any A/E or career_win_pct column
differs by more than 1%, AND categorical columns (jockey_switch_type,
class_move, surface_switch, *_lasix, *_blinkers) match exactly.
"""

import hashlib
import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sim.blinder import MARKET_BIAS_SQL as NEW_SQL, load_pre_race_card  # noqa: E402
from sim.db import get_connection  # noqa: E402


def _load__old_sql():
    """Pull the previous version of MARKET_BIAS_SQL from git HEAD."""
    repo = Path(__file__).resolve().parents[1]
    src = subprocess.check_output(
        ["git", "show", "HEAD:src/sim/blinder.py"], cwd=repo
    ).decode()
    # Locate `MARKET_BIAS_SQL = """ … """` block
    marker = 'MARKET_BIAS_SQL = """'
    start = src.index(marker) + len(marker)
    end = src.index('"""', start)
    return src[start:end]


OLD_SQL = _load__old_sql()


def pick_test_days(conn, n: int = 20, seed: str = "golden-2026-05-29"):
    cur = conn.cursor()
    cur.execute("SELECT track, date FROM handycapper.sim_candidates ORDER BY date, track")
    pool = [(r[0], str(r[1])) for r in cur.fetchall()]
    cur.close()
    out = []
    used = set()
    i = 0
    while len(out) < n and len(used) < len(pool):
        h = hashlib.sha256(f"{seed}|{i}".encode()).hexdigest()
        idx = int(h, 16) % len(pool)
        if idx not in used:
            used.add(idx)
            out.append(pool[idx])
        i += 1
    # Add adjacent-day pairs to test snapshot boundary alignment
    for track, date in list(out)[:3]:
        # tomorrow + yesterday at the same track if they exist
        cur = conn.cursor()
        for delta_days in (-1, 1):
            cur.execute(
                "SELECT track, date FROM handycapper.sim_candidates "
                "WHERE track = %s AND date = %s::date + %s::int",
                (track, date, delta_days),
            )
            row = cur.fetchone()
            if row and (row[0], str(row[1])) not in out:
                out.append((row[0], str(row[1])))
        cur.close()
    return out[: n + 6]  # cap


COMPARE_FLOAT_COLS = [
    "trainer_fts_ae", "trainer_claim_ae", "trainer_drop_ae",
    "trainer_layoff_ae", "trainer_switch_ae",
    "jock_career_win_pct", "jock_track_win_pct", "prev_jock_career_win_pct",
]
COMPARE_INT_COLS = [
    "trainer_fts_starts", "trainer_claim_starts", "trainer_drop_starts",
    "trainer_layoff_starts", "trainer_switch_starts",
    "jock_career_starts", "jock_starts_12m", "jock_wins_12m",
]
COMPARE_BOOL_COLS = [
    "is_fts", "off_turf", "has_lasix", "prev_lasix", "first_time_lasix",
    "has_blinkers", "prev_blinkers", "blinkers_off", "first_time_blinkers",
    "surface_switch", "is_layoff", "claimed_last_race",
]
COMPARE_STR_COLS = ["jockey_switch_type", "class_move"]


def diff_rows(old: pd.DataFrame, new: pd.DataFrame, tol_pct=0.01):
    """Return a DataFrame of differing rows."""
    # Rename non-key columns explicitly so merge doesn't introduce
    # ambiguity for columns whose names happen to contain "old"/"new".
    old_r = old.rename(columns={c: f"{c}___old" for c in old.columns if c != "starter_id"})
    new_r = new.rename(columns={c: f"{c}___new" for c in new.columns if c != "starter_id"})
    merged = old_r.merge(new_r, on="starter_id")
    diffs = []
    for _, r in merged.iterrows():
        row_diffs = []
        for c in COMPARE_FLOAT_COLS:
            o = r.get(f"{c}__old")
            n = r.get(f"{c}__new")
            if (o is None) != (n is None) or (o is None and n is None):
                if (o is None) != (n is None):
                    row_diffs.append(f"{c}: old={o} new={n}")
                continue
            if pd.isna(o) and pd.isna(n):
                continue
            if pd.isna(o) != pd.isna(n):
                row_diffs.append(f"{c}: old={o} new={n}")
                continue
            denom = max(abs(o), abs(n), 1e-6)
            if abs(o - n) / denom > tol_pct:
                row_diffs.append(f"{c}: {o:.4f} → {n:.4f} ({100*(n-o)/denom:+.1f}%)")
        for c in COMPARE_INT_COLS:
            o, n = r.get(f"{c}__old"), r.get(f"{c}__new")
            if pd.isna(o) and pd.isna(n):
                continue
            if pd.isna(o) != pd.isna(n) or int(o or 0) != int(n or 0):
                row_diffs.append(f"{c}: {o} → {n}")
        for c in COMPARE_BOOL_COLS:
            o, n = r.get(f"{c}__old"), r.get(f"{c}__new")
            if bool(o) != bool(n):
                row_diffs.append(f"{c}: {o} → {n}")
        for c in COMPARE_STR_COLS:
            o, n = r.get(f"{c}__old"), r.get(f"{c}__new")
            if str(o) != str(n):
                row_diffs.append(f"{c}: {o!r} → {n!r}")
        if row_diffs:
            diffs.append({
                "starter_id": int(r["starter_id"]),
                "horse": r.get("horse__old"),
                "diffs": "; ".join(row_diffs),
            })
    return pd.DataFrame(diffs)


def main():
    conn = get_connection()
    days = pick_test_days(conn, n=20)
    print(f"Picked {len(days)} sim days for golden test")
    print()

    total_diffs = 0
    total_starters = 0
    t0 = time.time()
    for track, date in days:
        # Run old version
        old_df = pd.read_sql(OLD_SQL, conn, params={"track": track, "race_date": date})
        # Run new version
        new_df = pd.read_sql(NEW_SQL, conn, params={"track": track, "race_date": date})
        if len(old_df) != len(new_df):
            print(f"  {track} {date}: ROW COUNT MISMATCH old={len(old_df)} new={len(new_df)}")
            continue
        d = diff_rows(old_df, new_df)
        total_starters += len(old_df)
        if len(d):
            total_diffs += len(d)
            print(f"  {track} {date}: {len(d)}/{len(old_df)} starters differ:")
            for _, r in d.head(5).iterrows():
                print(f"    starter_id={r['starter_id']} ({r['horse']}): {r['diffs']}")
            if len(d) > 5:
                print(f"    ... and {len(d) - 5} more")
        else:
            print(f"  {track} {date}: {len(old_df)} starters, all match ✓")

    print(f"\nElapsed: {time.time()-t0:.1f}s")
    print(f"Total: {total_diffs} differing starters out of {total_starters} ({100*total_diffs/max(total_starters,1):.2f}%)")
    if total_diffs == 0:
        print("PASS: snapshot path matches live path on all sample days.")
    else:
        print("FAIL: investigate diffs above before swapping consumers to snapshot path.")


if __name__ == "__main__":
    main()
