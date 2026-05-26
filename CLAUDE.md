# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Race Day Simulation — a blinded backtesting system where Claude acts as the bettor. Given a historical race day, Claude sees only pre-race information (velocity curves, odds, form trends), makes probability estimates and bet commitments, then evaluates against actual results.

The simulation tests whether RKM velocity curves + Benter probability combination + ITP-informed wagering principles produce genuine prospective edge.

## How To Run a Simulation

The simulation is conversational — follow `docs/simulation-protocol.md` exactly:

1. Select track + date (avoid marquee days where training data contamination is likely)
2. Load pre-race card via `blinder.load_pre_race_card()` + pool sizes via `blinder.load_pool_sizes()`
3. Assess pools (compute density per pool type, identify where the largest/thinnest pools are)
4. Handicap each race: pace prediction from adj_v0/decay, assess favorite vulnerability, identify contenders
5. Construct bets driven by value opinions (where does model disagree with crowd? which pool type best exploits it?)
6. Commit ALL bets before requesting reveal
7. Call `blinder.load_race_results()` for the reveal
8. Run `evaluate.evaluate_race()` + `day_summary()` for P&L

## Key Modules

| Module | Purpose |
|---|---|
| `blinder.py` | Information firewall — pre-race extraction + pool sizes + post-race reveal |
| `probability.py` | Benter logit (α=1.89), Stern-Harville (k=0.81), model probs from curves |
| `pace.py` | Prospective pace prediction from field v0/decay distribution with running style profiles |
| `kelly.py` | Quarter-Kelly staking, exposure caps |
| `evaluate.py` | P&L computation, ROI, day summary |

## Critical Rules

1. **Never load results before committing bets.** The blinder enforces this — `load_race_results()` is a separate function. If code accesses `finish_position`, `payoff`, or `winner` before bet commitment, that's a bug.

2. **Never query `rkm_race_situations` during the blinded phase.** That table's `has_vulnerable_fav` flag was computed using post-race pace data. Compute your own vulnerability assessment from raw inputs (decay rates from `rkm_velocity_curves`, pace prediction from adj_v0 distribution).

3. **Never query `rkm_race_performance` during the blinded phase.** It uses actual fractional splits (post-race).

## Key Documents

| Document | Purpose |
|---|---|
| `docs/simulation-protocol.md` | Step-by-step protocol for valid blinded simulations |
| `docs/itp-principles.md` | ITP wagering principles — verified against source transcripts |

## Database

Requires PostgreSQL with the `handycapper` schema (populated by [pdf-importer](https://github.com/robinhowlett/pdf-importer)).

```bash
# Connection defaults (override via environment variables)
SIM_DB_HOST=localhost
SIM_DB_PORT=5432
SIM_DB_NAME=handycapper
SIM_DB_USER=handycapper
SIM_DB_PASSWORD=handycapper
```

Depends on:
- `rkm_velocity_curves` — career velocity curves per horse (from [rkm](https://github.com/robinhowlett/rkm) Phase 1-2)
- `rkm_current_form` — time-weighted current form snapshots (rkm Phase 5)
- `races`, `starters`, `exotics` — base tables (from pdf-importer)

## Calibration Constants

In `probability.py` and `kelly.py`:
- `ALPHA = 1.89` (Benter model weight — from rkm Phase 4 logit fit)
- `BETA = 1.0` (odds weight)
- `STERN_K = 0.81` (empirically calibrated from wagering-analytics AN1)
- `TEMPERATURE = 6500.0` (softmax temperature in ms)
- `KELLY_FRACTION = 0.25` (quarter-Kelly)
- `MAX_EXPOSURE = 0.05` (5% of bankroll per race)
