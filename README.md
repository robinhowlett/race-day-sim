# Race Day Simulation

Blinded race day simulation — Claude reads the form without seeing results, constructs probability estimates, sizes bets using Kelly criterion, then reveals results and evaluates performance.

## What It Does

Simulates a full historical race day with an information firewall:

1. **Blinder** loads pre-race data only (velocity curves, odds, form — no results)
2. **Probability engine** combines model estimates with market odds (Benter logit, α=1.89)
3. **Pace prediction** classifies each race from field v0 distribution
4. **Kelly staking** sizes bets proportional to estimated edge (quarter-Kelly, capped)
5. **Reveal** shows actual results, payoffs, and P&L
6. **Evaluation** compares predicted edge to actual outcomes

## The Information Firewall

The hard constraint: **no result is visible before bets are committed.** The blinder SQL extracts only information knowable before post time — velocity curves, closing odds, career records, form trends. Finish positions, payoffs, and running lines are withheld until after the commitment step.

## Architecture

```
src/sim/
├── blinder.py      — Pre-race card extraction + post-race reveal
├── probability.py  — Benter combination + Stern-Harville matrix + fair value
├── pace.py         — Prospective pace prediction from v0 distribution
├── kelly.py        — Fractional Kelly for win bets + exotic budget
└── evaluate.py     — Post-race P&L + day summary
```

## Usage

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

# Requires SSH tunnel to handycapper DB
ssh -f -N -L 5434:127.0.0.1:5432 robinpc

# Run simulation for a specific track + date
python scripts/simulate_race_day.py --track GP --date 2014-03-15
```

## Calibration Inputs

| Parameter | Value | Source |
|---|---|---|
| Stern k | 0.81 | wagering-analytics AN1 |
| Benter α | 1.89 | rkm Phase 4 |
| Softmax temperature | 6500ms | rkm Phase 4 |
| "Price over fav" overlay | +15-21% | wagering-analytics AN1 |
| "Fav excluded" underlay | -25% | wagering-analytics AN1 |
| Kelly fraction | 0.25 (quarter Kelly) | ITP framework |
| Max single-race exposure | 5% of bankroll | Conservative default |

## Related Projects

- [rkm](https://github.com/robinhowlett/rkm) — velocity curves (performance measurement)
- [wagering-analytics](https://github.com/robinhowlett/wagering-analytics) — Harville/Stern calibration (market structure)
- [pdf-importer](https://github.com/robinhowlett/pdf-importer) — data ingestion
