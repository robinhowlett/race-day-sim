# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Race Day Simulation — a blinded backtesting system for pari-mutuel wagering. Given a historical race day, the system loads pre-race data (velocity curves, current form, market bias signals), computes ratings and edge, applies protocol rules to identify conviction plays, and evaluates against actual results.

The system combines three layers:
1. **Physics** (RKM velocity curves → projected ability)
2. **Market Bias** (AN2 trainer/jockey/equipment signals → group-level mispricing)
3. **Wagering Structure** (pool selection, equity tests, ticket construction)

## How To Run a Simulation

### Deterministic scaffold (recommended):
```bash
python scripts/run_simulation.py --seed "any text"    # hash-picks from 44K candidates
python scripts/run_simulation.py --track GP --date 2014-09-06  # specific date
```
Loads data, computes ratings, applies protocol rules mechanically, surfaces conviction candidates for handicapping judgment. Registers bets by program number and evaluates results without ambiguity.

### Pick a race day:
```bash
python scripts/pick_sim_day.py "any random text"
```
Hashes input text to deterministically select from 44K pre-filtered candidate days (no Grade 1/2, min field ≥7, min tri pool ≥$10K).

### Key protocol (for conversational sim):
Follow `docs/simulation-protocol.md` and `docs/wagering-framework.md`:

1. Load card via `blinder.load_pre_race_card()` + `load_pool_sizes()` + `load_market_bias()`
2. Compute ratings via `ratings.format_race_ratings()` — produces RATED/UNRATED tiers
3. Apply protocol checks: edge - band > 0? rated fraction ≥ 40%? field ≥ 7?
4. For conviction candidates: judge form, pace, class context (this is the handicapping)
5. Express opinion in purest pool type (win bet for specific, exotic for structural)
6. Register bets with explicit program numbers
7. Reveal and evaluate mechanically

## Key Modules

| Module | Purpose |
|---|---|
| `blinder.py` | Information firewall — pre-race card + pool sizes + market bias signals + post-race reveal |
| `ratings.py` | Confidence-weighted ratings: physics (curves) blended with prior (class + bias). Computes Rating, Market, Edge with ±band. Separates RATED/UNRATED tiers. |
| `probability.py` | Benter logit (α=1.89), Stern-Harville (k=0.81), model probs from curves |
| `pace.py` | Prospective pace prediction from field v0/decay distribution |
| `payoff.py` | Projects expected exotic payoffs using OLS models from wagering-analytics |
| `horizontal.py` | Per-leg equity assessment for Pick 3/4/5/6 |
| `kelly.py` | Quarter-Kelly staking, exposure caps, race-context modifiers (fav-edge tier, WCMI, carryover) |

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
| `docs/wagering-framework.md` | Quantitative wagering system (edge-driven, equity tests, press mechanics) |
| `docs/research-findings.md` | Empirical results from 13 research items (A/E tables, market bias) |
| `docs/rating-calibration-plan.md` | Rating scale, canonical race, display format spec |
| `docs/confidence-weighted-rating.md` | Blending spec: w_physics × physics + (1-w) × prior |
| `docs/edge-calibration-issue.md` | Known issue: rank-mapping inflates edges (fix pending) |
| `docs/itp-wagering-framework.md` | ITP source material (historical reference) |
| `docs/itp-principles.md` | ITP quick reference (historical) |

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
- `rkm_current_form` — time-weighted current form snapshots (PRIMARY physics input, point-in-time safe)
- `rkm_velocity_curves` — career curves (fallback only, filtered by `first_race < sim_date` to prevent future leakage)
- `rs_trainer_ae_daily`, `rs_jockey_career_daily`, `rs_jockey_track_weekly`, `rs_race_overround_static` — point-in-time biographical stats (from [racing-stats](https://github.com/robinhowlett/racing-stats); load_market_bias consumes these via O(1) index lookups)
- `race_wcmi` — market informativeness score per race (from wagering-analytics AN2)
- `races`, `starters`, `exotics`, `meds`, `equip` — base tables (from pdf-importer)

Note: `trainer_ae_profiles` (the static wagering-analytics table) is no longer used —
its responsibility moved to `rs_trainer_ae_daily` in racing-stats. WA-T1.4
recommended dropping the static table; that recommendation now stands.

## Calibration Constants

In `probability.py` and `kelly.py`:
- `ALPHA = 1.89` (Benter model weight — from rkm Phase 4 logit fit)
- `BETA = 1.0` (odds weight)
- `STERN_K = 0.81` (empirically calibrated from wagering-analytics AN1)
- `TEMPERATURE = 1000.0` (softmax temperature in ms; was 6500 — too flat, fixed 2026-05-27 via RDS-T1.1)
- `KELLY_FRACTION = 0.25` (quarter-Kelly)
- `MAX_EXPOSURE = 0.05` (5% of bankroll per race)
