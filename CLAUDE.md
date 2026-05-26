# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Race Day Simulation — a blinded backtesting system where Claude acts as the bettor. Given a historical race day, Claude sees only pre-race information (velocity curves, odds, form trends), makes probability estimates and bet commitments, then evaluates against actual results.

The simulation tests whether RKM velocity curves + Benter probability combination + ITP-informed wagering principles produce genuine prospective edge.

## How To Run a Simulation

### Runner script (structured output):
```bash
python scripts/simulate_race_day.py --track GP --date 2014-09-06
```
Outputs pre-race card, pool sizes, pace predictions, probabilities, and top overlay combos per race. Pauses for bet commitment before revealing results.

### Conversational (with Claude):
Follow `docs/simulation-protocol.md` exactly:

1. Select track + date (avoid marquee days where training data contamination is likely)
2. Load pre-race card via `blinder.load_pre_race_card()` + pool sizes via `blinder.load_pool_sizes()`
3. Assess pools (compute density per pool type, identify where the largest/thinnest pools are)
4. Handicap each race: pace prediction from adj_v0/decay, assess favorite vulnerability, identify contenders
5. Use `payoff.estimate_combo_value()` to quantify overlay on candidate combinations
6. Use `horizontal.evaluate_leg_selections()` to check equity per leg of any Pick 3/4/5/6
7. Construct bets driven by value opinions (where does model disagree with crowd? which pool type best exploits it?)
8. Commit ALL bets before requesting reveal
9. Call `blinder.load_race_results()` for the reveal
10. Run `evaluate.evaluate_race()` + `day_summary()` for P&L

## Key Modules

| Module | Purpose |
|---|---|
| `blinder.py` | Information firewall — pre-race extraction + pool sizes + post-race reveal |
| `probability.py` | Benter logit (α=1.89), Stern-Harville (k=0.81), model probs from curves |
| `pace.py` | Prospective pace prediction from field v0/decay distribution with running style profiles |
| `payoff.py` | Projects expected exotic payoffs using OLS models (trifecta R²=0.88) from wagering-analytics. Computes overlay ratio vs Stern fair value for quantitative edge estimates. |
| `horizontal.py` | Per-leg equity assessment for Pick 3/4/5/6. Detects ITP "flashing stop sign" legs, compares horizontal takeout vs synthetic parlay. |
| `kelly.py` | Quarter-Kelly staking, exposure caps |
| `evaluate.py` | P&L computation, ROI, day summary |

## Models (from wagering-analytics)

```
models/
├── payoff_coefficients.json  — OLS coefficients for exacta/tri/super/pick 3-6 payoff prediction
└── jitter_calibration.json   — per-leg log-normal σ for horizontal odds uncertainty
```

These are static calibration outputs from [wagering-analytics](https://github.com/robinhowlett/wagering-analytics). They don't change unless the underlying AN1 analysis is re-run.

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
