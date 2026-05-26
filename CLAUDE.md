# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Race Day Simulation — a blinded backtesting system where Claude acts as the bettor. Given a historical race day, Claude sees only pre-race information, makes probability estimates and bet commitments, then evaluates against actual results.

The simulation tests whether the RKM velocity curves + Benter probability combination + ITP-informed staking produce genuine prospective edge.

## How To Run a Simulation

The simulation is conversational — Claude is given a track + date, loads the pre-race card via the blinder, handicaps each race, commits bets, then requests the reveal. The protocol is:

1. User provides track code + date (e.g., "GP 2014-03-15")
2. Claude calls `blinder.load_pre_race_card()` — sees form, curves, odds. No results.
3. For each race: predict pace, estimate probabilities, compute Kelly stakes, commit bets
4. After ALL bets committed: call `blinder.load_race_results()` for the reveal
5. Run `evaluate.evaluate_race()` + `day_summary()` for P&L

## Key Modules

| Module | Purpose |
|---|---|
| `blinder.py` | Information firewall — pre-race extraction + post-race reveal |
| `probability.py` | Benter logit (α=1.89), Stern-Harville (k=0.81), model probs from curves |
| `pace.py` | Prospective pace prediction from field v0 distribution |
| `kelly.py` | Quarter-Kelly staking, max exposure caps, pass criteria |
| `evaluate.py` | P&L computation, ROI, day summary |

## Critical Rule

**Never load results before committing bets.** The blinder enforces this architecturally — `load_race_results()` is a separate function that must only be called after the betting step. If you're reading code that accesses `finish_position`, `payoff`, or `winner` before the bet commitment, that's a bug.

## Database

Same as RKM and wagering-analytics:
- Host: localhost:5434 (SSH tunnel to robinpc)
- Database: handycapper
- User/pass: handycapper/handycapper

Depends on:
- `rkm_velocity_curves` (Phase 1-2)
- `rkm_current_form` (Phase 5)
- `exotics` + `starters` + `races` (base tables)
- `exotic_harville_ratios` (from wagering-analytics)

## Calibration Constants

Hardcoded in `probability.py` and `kelly.py`:
- `ALPHA = 1.89` (Benter model weight)
- `BETA = 1.0` (odds weight)
- `STERN_K = 0.81`
- `TEMPERATURE = 6500.0` (softmax)
- `KELLY_FRACTION = 0.25`
- `MAX_EXPOSURE = 0.05` (5% of bankroll per race)

## Spec

Full specification at `docs/specs/race-day-simulation.md` in the parent project.
