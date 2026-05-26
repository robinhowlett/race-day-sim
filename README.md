# Race Day Simulation

Blinded backtesting system for horse racing wagering. Given a historical race day, the system enforces an information firewall — only pre-race data (velocity curves, closing odds, form trends) is visible before bets are committed. After commitment, results are revealed and performance is evaluated.

## Purpose

Tests whether a combination of:
- **RKM velocity curves** (measuring horse performance via deceleration models)
- **Benter conditional logit** (combining model probabilities with market odds)
- **ITP wagering principles** (professional bet structuring — equity over survival, exploiting overbet favorites, pool selection)

...produces genuine prospective edge when applied to historical race days without hindsight.

## The Information Firewall

The hard constraint: **no result is visible before bets are committed.**

The blinder layer extracts only information knowable before post time — velocity curves, closing odds, pool sizes, career records, form trends. Finish positions, payoffs, and running lines are withheld until after commitment. See `docs/simulation-protocol.md` for the complete list of allowed vs forbidden data.

## Architecture

```
src/sim/
├── blinder.py      — Pre-race card + pool sizes (blinded) + post-race reveal
├── probability.py  — Benter combination + Stern-Harville matrix + fair value
├── pace.py         — Pace prediction from v0/decay profiles with running style classification
├── kelly.py        — Fractional Kelly staking
└── evaluate.py     — Post-race P&L + day summary

docs/
├── simulation-protocol.md  — Step-by-step protocol for valid simulations
└── itp-principles.md       — Wagering principles (verified against source transcripts)
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

## Database

Requires PostgreSQL with the `handycapper` schema populated by [pdf-importer](https://github.com/robinhowlett/pdf-importer), plus derived tables from [rkm](https://github.com/robinhowlett/rkm) (velocity curves, current form).

Configure via environment variables:

```bash
export SIM_DB_HOST=localhost
export SIM_DB_PORT=5432
export SIM_DB_NAME=handycapper
export SIM_DB_USER=handycapper
export SIM_DB_PASSWORD=handycapper
```

## Calibration Inputs

| Parameter | Value | Source |
|---|---|---|
| Stern k | 0.81 | wagering-analytics AN1 |
| Benter α | 1.89 | rkm Phase 4 |
| Softmax temperature | 6500ms | rkm Phase 4 |
| Kelly fraction | 0.25 (quarter Kelly) | Conservative default |
| Max single-race exposure | 5% of bankroll | Conservative default |

## Key Empirical Findings (from wagering-analytics AN1)

These are used as **priors** (not rigid rules) during simulation:

| Finding | Implication |
|---|---|
| "Price on top, fav 2nd/3rd" = 15-21% overlay | Trifectas structured this way are systematically underbet |
| "Fav excluded" = 25% underlay in aggregate | Doesn't apply when model projects fav off the board |
| Fastest horse by adj_v0 wins only 12-16% | adj_v0 predicts who leads, not who wins — decay determines outcomes |

## Related Projects

- [rkm](https://github.com/robinhowlett/rkm) — velocity curve fitting + market analysis + current form
- [wagering-analytics](https://github.com/robinhowlett/wagering-analytics) — Harville/Stern calibration + payoff models
- [pdf-importer](https://github.com/robinhowlett/pdf-importer) — data ingestion from Equibase PDFs
