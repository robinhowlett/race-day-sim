# Exotic Overlay POC — Findings

**Status:** POC steps 1-4 complete for EXACTA, TRIFECTA, PICK_3. SUPERFECTA pending an upstream fix in wagering-analytics. POC code under `scripts/poc/exotic-overlay/`.

**Date:** 2026-05-29

## Background

Following the FLB POC (closed +EV on WIN bets), the natural extension was to ask whether similar overlay-driven selection rules apply to exotic pools. Exotic takeout is higher (21-24% vs 17% on WIN), but the public's exotic-pool pricing has structurally bigger inefficiencies because (a) combinatorial pools are smaller and less competitive, (b) CAW is constrained on smaller pools, and (c) deep verticals invite tail-distribution mispricing.

The question for this POC: **for each exotic bet type, does selecting combos by `model_probability × projected_payoff > threshold` produce stably positive ROI across years?**

## Methodology

### Three-way comparison

For each (bet_type, ER threshold, year):

1. **bet-everything baseline:** $1 on every possible combination. Recovers the takeout floor.
2. **odds-only filter:** ER = `Stern-Harville(odds_prob_normalized) × projected_payoff` ≥ threshold. Tells us what the *public's own probability estimate* would deliver — the structural component.
3. **combined filter:** ER = `Stern-Harville(combined_prob) × projected_payoff` ≥ threshold. Combined-prob is rkm's Benter blend of velocity-curve model + odds. Difference vs odds-only is the **pure model edge**.

### Probability model

Stern/Harville with k=0.86 (the audit-corrected value from WA-T1.1; race-day-sim's `probability.py` still defaults to 0.81 — see follow-up item).

### Projected payoff

Critical: **NOT the chart's actual paid-this-race payoff** (which is the post-hoc-known answer for one specific outcome). Instead, `src/sim/payoff.py:project_<bettype>_payoff()` — OLS regression coefficients fit on 1.2M historical rows by wagering-analytics, predicting per-$1 payoff from `(combo_odds, pool_size, field_size, HHI, favorite_position)`. These coefficients are in `models/payoff_coefficients.json`.

This was the methodology bug that confounded our initial step-3 results. Using the chart's actual paid amount as the ER input meant the filter knew the outcome — combos involving longshot horses passed disproportionately on races where longshot horses won, producing apparent +160% ROI from post-hoc selection bias. Switching to the **OLS-projected payoff** (which uses no per-race outcome information) eliminated the bug.

### Population

`sim_candidates` materialized view: 2005-2017, no Grade 1/2, ≥7 fields, ≥$10K trifecta pool, breed=TB. The simulator's actual playable universe.

### Sample sizes (2010-2016, 7 years)

| Bet type | Distinct races / pools | Total combos evaluated |
|---|---|---|
| EXACTA | 162K | ~13M |
| TRIFECTA | 162K | ~870M |
| SUPERFECTA | 162K | ~5B |
| PICK_3 | 133K | ~700M |
| PICK_4 | 40K | ~1B |

## Headline results — 7-year per-year stability

Each bet type at ER ≥ 1.30. **Combined ROI** is what you'd realize betting $1 on every combo passing the filter using rkm's Benter-blended `combined_prob`. **Odds-only ROI** is the same procedure using overround-normalized public odds — the structural component anyone betting that pool gets without skill. **Pure model edge** = combined − odds-only, the actionable per-pp lift the model produces.

### EXACTA — ER ≥ 1.30

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +yrs |
|---|---|---|---|---|---|---|---|---|
| +22.2% | +36.0% | +36.5% | +46.2% | +20.4% | +29.5% | +22.8% | **+30.5%** | **7/7** |

Odds-only at same threshold: **−12.0%** (vertical-pool double-takeout). Pure model contribution: **+42.5pp**.

### TRIFECTA — ER ≥ 1.30

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +yrs |
|---|---|---|---|---|---|---|---|---|
| +46.3% | +24.1% | +30.9% | +26.3% | +19.0% | +22.7% | +25.1% | **+27.8%** | **7/7** |

Odds-only: **−13.4%**. Pure model contribution: **+41.2pp**.

### SUPERFECTA — ER ≥ 1.30

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +yrs |
|---|---|---|---|---|---|---|---|---|
| +43.0% | +58.2% | +60.3% | +54.1% | +62.5% | +45.3% | +32.6% | **+50.9%** | **7/7** |

Odds-only: **+9.0%** — slightly positive, not at takeout. Deeper combinatorics let the OLS payoff projection capture more structural overlay even before model skill enters. Pure model contribution: **+41.9pp**.

### PICK_3 — ER ≥ 1.30

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +yrs |
|---|---|---|---|---|---|---|---|---|
| +61.0% | +110.9% | +124.0% | +75.3% | +62.6% | +89.9% | +68.7% | **+84.6%** | **7/7** |

Odds-only: **+52.8%** (6 years; 2016 had insufficient surviving combos for stable estimate). PICK_3 has two compounding effects:
1. **Structural takeout-stacking advantage** (+52.8% in odds-only) — single-leg-takeout on the parlay pool dominates the triple-leg-takeout of a manual WIN parlay. Anyone betting Pick 3 captures this; it's not novel skill.
2. **Pure model edge** (+31.8pp on top) — comparable to the verticals.

### PICK_4 — ER ≥ 1.30

| 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +yrs |
|---|---|---|---|---|---|---|---|---|
| +77.5% | +98.5% | +97.8% | +97.0% | +86.0% | +98.4% | +73.2% | **+89.8%** | **7/7** |

Odds-only: **−1.6%** — close to takeout floor. PICK_4 doesn't free-ride on takeout-stacking the way PICK_3 does. **Pure model contribution: +91.4pp** — the highest of any bet type, more than 2× the verticals' +41-42pp. Plausible explanation: PICK_4 pools are smaller and less efficient than other pools, leaving more room for an informed model to find combos the public has mispriced. Worth investigating in a follow-up.

## Pattern across bet types

**Verticals (EXACTA, TRIFECTA, SUPERFECTA) all show ~+42pp pure model contribution at ER ≥ 1.30.** Bigger structural variance (deeper exotics) doesn't amplify or diminish the per-pp model contribution — the Benter blend's per-horse improvement translates to roughly the same per-combo selection edge regardless of combinatorial depth.

**Horizontals split into two regimes:**
- **PICK_3** has substantial structural takeout-stacking (+52.8% odds-only) on top of which the model adds +32pp. Total deployable ROI is +85% but most of it is structural.
- **PICK_4** has minimal structural advantage (−1.6% odds-only) but **+91pp of pure model contribution** — 2× larger than verticals. Likely because PICK_4 pools are smaller and the public is more mispriced.

This suggests **PICK_4 may be the most actionable bet type** — it's where the model adds the most value above what anyone betting that pool would automatically capture.

## What we caught (and learned)

### Bug: chart payoff in ER calculation

Step 3 of the POC used `chart_payoff_per_$1` as the multiplier in ER. This couples the filter to the actual race outcome. Combos involving longshots passed disproportionately on races where longshots happened to win (because the chart paid more), inflating ROI by post-hoc selection bias.

**Lesson:** The right per-combo "expected payoff" is **a model's projection** trained on historical pool/field characteristics, not the realized payout for a specific race. `src/sim/payoff.py` already implemented this six audit-sessions ago via wagering-analytics' OLS coefficients; the POC should have used it from step 1. The audit memory captures this:

> Before launching any new analytical script: (1) state the inputs the math needs, (2) check whether they exist via grep + CLAUDE.md + models/, (3) prefer existing infrastructure over reimplementation.

### Stern k = 0.81 vs 0.86 in race-day-sim

The audit (WA-T1.1, resolved 2026-05-27) recalibrated Stern's exponent from 0.81 to 0.86 via grid-search across 80K races. wagering-analytics uses 0.86. **race-day-sim's `probability.py` still hardcodes 0.81** in function defaults. The POC uses 0.86. Follow-up item to harmonize race-day-sim with the calibrated value.

### SUPERFECTA gap (fixed during this POC)

Initial run found `payoff_coefficients.json` had no SUPERFECTA model — the script silently dropped SUPERFECTA rows because of a wrong join.

Root cause: `fit_payoff_models.py` joined `race_probabilities.wagering_position = 4` to look up the 4th-place horse's odds. `wagering_position` is structurally 1-3 only — a WPS-payout-attribution column, correctly so for WPS pools (where coupled entries share a payout slot). For SUPERFECTA, each individual horse has its own pool-payout finish; the right join is `starters.official_position = 4`.

Same pattern as audit WA-T1.2 (which fixed horizontal leg-winner identification by switching from `wagering_position` to `finish_position`). Fixed in wagering-analytics commit 551b571. SUPERFECTA model fitted: R²(test) = 0.79 on 595K rows. Now in `models/payoff_coefficients.json` and exposed via `src/sim/payoff.py:project_superfecta_payoff`.

EXACTA and TRIFECTA do NOT have this bug because their positions are 1-3, all within `wagering_position` range. QUINELLA also fits within 1-2; its absence from the JSON has a different (yet-unidentified) cause but isn't in scope. HI_5 would need the same SUPERFECTA fix but the upstream `exotic_harville_ratios` table has no HI_5 entries — out of scope.

## What deployment of these would look like

| Bet type | ER thr | Expected ROI | Pure model contribution | Notes |
|---|---|---|---|---|
| EXACTA | 1.30 | +30% | +42pp | Smallest pool fraction; cleanest deployable |
| TRIFECTA | 1.30 | +28% | +41pp | Same pattern as EXACTA, deeper combinatorics |
| SUPERFECTA | 1.30 | +51% | +42pp | Higher headline because deeper combinatorics; same model edge |
| PICK_3 | 1.30 | +85% | +32pp | Heavy structural component (+53pp from takeout-stacking) |
| PICK_4 | 1.30 | +90% | **+91pp** | Biggest pure model edge; most actionable |

For all bet types, ER ≥ 1.30 is the recommended threshold — sweet spot of selectivity vs noise.

## What's NOT yet done

1. **Pool-fraction analysis at deployable thresholds across all 5 bet types** — only sampled for EXACTA at one threshold so far
2. **Multi-day batch through `run_simulation.py`** with these filters wired in (the FLB POC's analog of step 6) — required before any real-money deployment
3. **PICK_5 / PICK_6** — projection model R² is weak (0.37, 0.27) and pools are dominated by carryover/jackpot mechanics. Separate POC needed
4. **QUINELLA model** — absent from the JSON, root cause yet-unidentified, deferred since QUINELLA is small-volume
5. **PICK_4 follow-up** — the +91pp pure-model contribution is unusually large. Worth investigating whether it's a real structural advantage or whether the smaller training-set (110K rows, R²=0.56) introduces overfitting we can't see at this evaluation level

## Files

POC scripts (`scripts/poc/exotic-overlay/`):
- `01_data_inventory.py` — bet-type-by-bet-type data availability
- `02_bet_everything_baseline.py` — takeout-floor confirmation per bet type
- `03a_vertical_overlay.py` — EXACTA/QUINELLA/TRIFECTA/SUPERFECTA with chart-payoff filter (the buggy version we corrected)
- `04_honest_overlay_per_year.py` — EXACTA/TRIFECTA/SUPERFECTA/PICK_3/PICK_4 with projected-payoff filter (the honest version, full 5-type)
- `audit_superfecta_2014.py` — diagnostic that surfaced the chart-payoff bug
- `audit_top10_races.py` — per-race detail of the largest-paying SUPERFECTA results
- `diag_combined_vs_odds.py` — first run of combined-vs-odds-only comparison (still using chart payoff)
- `diag_exacta_projected.py` — single-year EXACTA with the corrected projected-payoff filter

Artifacts (`tmp/`, gitignored):
- `exotic_inventory.csv` — sample sizes by bet type and pool type
- `bet_everything_baseline.csv` — takeout-floor ROI per bet type
- `vertical_overlay_grid.csv` — per-year per-threshold from step 3 (buggy version)
- `audit_superfecta_2014.csv` — race-by-race details
- `honest_overlay_per_year_EXACTA.csv` — corrected per-year EXACTA results
- `honest_overlay_per_year_TRIFECTA.csv` — corrected per-year TRIFECTA results
- `honest_overlay_per_year_PICK_3.csv` — corrected per-year PICK_3 results

## Bottom line

**The combined-prob model adds stable +30-90pp ROI above the takeout floor across all 5 tested exotic bet types, in 7/7 years from 2010-2016 on the simulator's playable universe.**

- Verticals (EXACTA, TRIFECTA, SUPERFECTA): +41-42pp pure model contribution at ER ≥ 1.30
- PICK_3: +32pp on top of +53pp structural takeout-stacking
- PICK_4: **+91pp pure model contribution** — the standout deployable target

This is comparable in magnitude to the FLB WIN-bet POC's +30-50pp tier-edge contributions and is similarly deployable subject to:

1. End-to-end multi-day sim batch validation (the FLB POC's step 6 analog)
2. Pool-fraction analysis at deployment thresholds across all 5 bet types
3. PICK_4 follow-up to confirm the unusually-large pure-model edge isn't an artifact of the smaller training set

The POC is ready to advance to integration once those three items close.
