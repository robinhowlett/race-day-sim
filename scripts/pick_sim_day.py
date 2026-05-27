"""Pick a simulation race day using a hash of arbitrary user input.

The user provides any string (a word, phrase, random text). The script
hashes it and maps deterministically to a candidate race day from the
database. This ensures:
  - Blind selection (no human bias toward interesting/known days)
  - Reproducible (same input always picks same day)
  - Fun (user can type whatever they want)

Usage:
    python scripts/pick_sim_day.py "any random text here"
    python scripts/pick_sim_day.py  # prompts for input
"""

import hashlib
import os
import sys

import psycopg2


def get_conn():
    return psycopg2.connect(
        host=os.environ.get("SIM_DB_HOST", "localhost"),
        port=os.environ.get("SIM_DB_PORT", "5432"),
        dbname=os.environ.get("SIM_DB_NAME", "handycapper"),
        user=os.environ.get("SIM_DB_USER", "handycapper"),
        password=os.environ.get("SIM_DB_PASSWORD", "handycapper"),
    )


def pick_day(seed_text: str) -> dict:
    """Hash seed text and select a race day from candidates."""
    h = hashlib.sha256(seed_text.encode()).hexdigest()
    index = int(h, 16)

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM handycapper.sim_candidates")
    n_candidates = cur.fetchone()[0]

    if n_candidates == 0:
        raise RuntimeError("No sim_candidates found. Run the materialized view creation first.")

    pick_idx = index % n_candidates

    cur.execute("""
        SELECT track, date, n_races, avg_field, races_w_tri
        FROM handycapper.sim_candidates
        ORDER BY date, track
        OFFSET %s LIMIT 1
    """, (pick_idx,))

    row = cur.fetchone()
    cur.close()
    conn.close()

    return {
        "track": row[0],
        "date": str(row[1]),
        "n_races": row[2],
        "avg_field": float(row[3]),
        "races_w_tri": row[4],
        "seed": seed_text,
        "hash": h[:16],
        "index": pick_idx,
        "of": n_candidates,
    }


def main():
    if len(sys.argv) > 1:
        seed = " ".join(sys.argv[1:])
    else:
        seed = input("Enter any text (will be hashed to pick a race day): ")

    result = pick_day(seed)

    print(f"\n{'='*50}")
    print(f"  Seed:    \"{result['seed']}\"")
    print(f"  Hash:    {result['hash']}...")
    print(f"  Pick:    #{result['index']} of {result['of']} candidates")
    print(f"{'='*50}")
    print(f"  Track:   {result['track']}")
    print(f"  Date:    {result['date']}")
    print(f"  Races:   {result['n_races']}")
    print(f"  Avg field: {result['avg_field']}")
    print(f"  Tri results: {result['races_w_tri']}")
    print(f"{'='*50}\n")


if __name__ == "__main__":
    main()
