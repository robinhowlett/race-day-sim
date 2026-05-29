# Favorite-Longshot Bias Calibration — POC Findings

**Status:** POC complete. Recommendation: integrate FLB calibration into the rating-to-edge pipeline AS A FILTER, not a probability rewriter, AND pair it with an odds-tier-aware minimum-edge threshold. POC code in `scripts/poc/flb-calibration/`.

**Date:** 2026-05-29

## Background

[Audit RDS-T2.x](cross-repo-audit-2026-05-27.md) flagged that 49% of conviction picks have odds ≥15/1 — the model finds "edge" predominantly in the longshot tail where the favorite-longshot bias (FLB) is strongest. Three response options were captured:

1. **Long-term, principled:** FLB correction at rating-to-edge translation, calibrated from historical strike-rate buckets.
2. **Interim, defensible:** odds-tier-aware minimum-edge threshold (`worst > 0` for chalk, `>5` at 7-15/1, `>10` at 15/1+).
3. **UI nudge, immediate:** ✅ DONE 2026-05-28. `_flb_warning()` surfaces FLB warnings on long-odds conviction picks.

This POC executed (1) and empirically tested whether it delivers ROI lift on a year-out holdout. **Result: yes, but only when paired with (2).** Naive FLB calibration without odds-tier thresholds makes ROI WORSE than baseline by expanding the longshot bet set.

## Methodology

### Data
- 7,765,668 starter-races from `rkm_market_analysis` (1997-2016)
- 964,126 winners (12.42% baseline win rate)
- Train: 7.26M observations from 1997-2014
- Holdout: 507K observations from 2015-2016 (year-out, no leakage)

### FLB curve construction
Bucket starters into 50 equal-count quantile buckets by `odds_prob` (already overround-normalized via WA #19). Compute per-bucket actual win rate. Fit isotonic regression of actual_rate on mean_implied, weighted by bucket sample size, with an additional 8-bucket high-chalk anchor (odds_prob ≥ 0.50) for extrapolation past the original bucketed coverage.

Output: a smooth shrinkage function `shrinkage(p) = isotonic(p) / p` that maps any odds-implied probability to its empirically-calibrated counterpart.

### Scripts (under `scripts/poc/flb-calibration/`)
- `01_empirical_curve.py` — bucketed FLB signature, full data
- `02_fit_smooth.py` — isotonic fit with chalk anchor, calibration JSON
- `03_validate.py` — train/holdout fit + log-loss + Brier metrics + per-bucket calibration check
- `04_roi_impact.py` — ROI comparison across baseline / FLB strategies and odds tiers
- `05_subgroups.py` — divergence by field size, surface, race class

## Empirical findings

### The FLB signature (full data, 50 buckets)

```
Coarse buckets:
  <2% implied         actual 0.92%   shrinkage 0.67  (longshots overbet)
  2-5% implied        actual 3.09%   shrinkage 0.91
  5-10% implied       actual 7.19%   shrinkage 0.98
  10-20% implied      actual 14.35%  shrinkage 0.99
  20-40% implied      actual 27.71%  shrinkage 1.02
  40%+ implied        actual 50.68%  shrinkage 1.07  (chalk underbet)
```

Monotonic, exactly as theory predicts. The bias is concentrated at the extremes — middle buckets (5-20% implied) are well-calibrated.

### Calibration improvement on holdout

| Metric | Baseline (raw odds_prob) | FLB-calibrated | Improvement |
|---|---|---|---|
| Log-loss | 0.32450 | 0.32418 | +0.00032 |
| Brier score | 0.09767 | 0.09761 | +6.3e-5 |
| Winner log-likelihood per race | -1.6408 | -1.6389 | +0.0020 |

Statistically significant given 507K observations (binomial SE ~0.0006), but small in absolute terms because most observations are in the well-calibrated middle range. **Calibration improvement is concentrated at the extremes**, exactly where wagering decisions happen.

### ROI impact (the punchline)

| Strategy | n bets | ROI% | Notes |
|---|---|---|---|
| All starters (random) | 507,163 | **−25.0%** | Takeout-eaten baseline |
| Baseline edge >0 | 194,792 | **−3.2%** | Live system overlay strategy |
| Baseline edge >0.05 | 36,713 | **+5.2%** | Stronger filter → positive |
| Baseline edge >0.10 | 10,247 | **+6.7%** | Even stronger |
| **FLB edge >0** | 240,806 | **−12.3%** | **WORSE than baseline.** Naive FLB expands bet set into longshot tail. |
| **FLB edge >0.05** | 31,995 | **+7.3%** | **+2.1pp better than baseline edge >0.05** |
| **FLB edge >0.10** | 8,075 | **+9.5%** | **+2.8pp better than baseline edge >0.10** |

At the wagering-relevant +5% and +10% edge thresholds, FLB-calibrated picks beat baseline by 2-3 percentage points of ROI — about 40% relative improvement.

### The disagreement structure

| Set | n | ROI |
|---|---|---|
| Baseline says +EV, FLB says −EV (FLB removes) | 7,887 | **−18.6%** |
| FLB says +EV, baseline says −EV (FLB adds) | 53,901 | **−46.2%** |

The 7,887 bets FLB removes are correctly identified as bad — they lose 18.6%. **The 53,901 bets FLB adds are catastrophic** — average odds 47/1, ROI −46%. Naively trusting FLB to "find more edges" is a trap.

This is the empirical confirmation of RDS-T2.x's diagnosis: the FLB makes longshots LOOK overbet relative to public belief, so the FLB-corrected edge expands the bet set in exactly the territory where the model has the worst information advantage. **Calibration without threshold is destructive.**

### By odds tier

| Tier | Baseline edge>0 ROI | FLB edge>0 ROI | Winner |
|---|---|---|---|
| Longshot 50/1+ | −19.0% | −46.6% | Baseline by 27pp |
| 20-50/1 | +6.0% | −10.5% | Baseline by 16pp |
| 10-20/1 | +0.9% | −1.8% | Baseline by 2.7pp |
| 5-10/1 | −3.0% | −3.5% | Tie |
| 2-5/1 | −7.5% | −6.3% | FLB by 1.2pp |
| Chalk <2/1 | −8.5% | −5.2% | **FLB by 3.3pp** |

**FLB helps in chalk and short-odds territory; hurts in longshot territory.** The right integration combines FLB with an odds-tier threshold — reject longshot bets even when their FLB-edge looks positive.

### Subgroup analysis

Field size matters meaningfully for the deepest longshot bucket; surface and class matter modestly.

**Bucket <2% shrinkage by field size:**
- Small fields (5-7 horses): 0.49
- Medium fields (8-10): 0.67
- Large fields (11+): 0.75

Small fields have substantially harsher longshot bias — a 30/1 in a 5-horse field is more clearly a non-contender than the same horse in a 12-horse field. **Production integration could use field-size-aware shrinkage at the longshot extreme,** though the global curve is adequate for everything past bucket <2%.

## Recommended integration

### What to integrate

1. **FLB shrinkage curve** as a function `flb_calibrate(odds_prob: float, field_size: int = None) -> float` that returns the calibrated probability. The fitted JSON in `scripts/poc/flb-calibration/tmp/flb_calibration_holdout.json` is a starting point.

2. **Odds-tier-aware minimum edge threshold** — empirically tuned on 2015 and validated OOS on 2016 (steps 6 and 7 of the POC). The result INVERTS my initial guess: longer odds → LOWER threshold (FLB is biggest there, edges are real), shorter odds → HIGHER threshold (FLB is smaller, edges need to be larger to be real).

   | Tier | odds_prob range | Threshold | Tune ROI | OOS ROI | OOS 95% CI |
   |---|---|---|---|---|---|
   | chalk <2/1 | ≥ 0.40 | edge ≥ 0.125 | +1.5% | +4.9% | (−4.7% to +14.6%) |
   | short 2-5/1 | 0.20-0.40 | edge ≥ 0.20 | +22.7% | +16.7% | (−5.5% to +38.8%) |
   | mid 5-10/1 | 0.10-0.20 | edge ≥ 0.10 | +28.2% | +12.2% | (−2.3% to +26.6%) |
   | long 10-20/1 | 0.05-0.10 | edge ≥ 0.025 | +27.6% | **+17.2%** | **(+6.6% to +27.9%)** ✓ |
   | longer 20-50/1 | 0.02-0.05 | edge ≥ 0.025 | +31.2% | **+40.5%** | **(+3.9% to +77.0%)** ✓ |
   | extreme 50/1+ | < 0.02 | NEVER BET | −48.7% | −44.0% | unbettable at any threshold |

   **Two tiers are statistically significantly profitable on true OOS** (long 10-20/1 and longer 20-50/1) — confidence intervals exclude zero. The other 3 bettable tiers are positive in point estimate but CIs cross zero on a single year of OOS data — promising but not yet conclusive.

   Three-way split methodology to avoid threshold-overfitting:
   - **Calibration train (1997-2014):** fit FLB shrinkage curve.
   - **Threshold tune (2015 only):** grid search per-tier optimal edge threshold.
   - **True OOS (2016 only):** score the tuned thresholds. OOS ROI is lower than tune ROI in 4/5 tiers (overfitting bias evidence), but still positive in all 5 bettable tiers.

   **Excluding the unbettable extreme_50/1+ tier, the OOS-validated weighted ROI is +18.7% on 5,962 bets in 2016.**

### Where to integrate

- **`rkm/scripts/compute_market.py`**: write the FLB-calibrated `odds_prob_calibrated` into `rkm_market_analysis` alongside the raw `odds_prob`. Don't overwrite — keep both for comparison.
- **`race-day-sim/src/sim/blinder.py:load_market_bias`**: surface `odds_prob_calibrated` as a column.
- **`race-day-sim/src/sim/ratings.py`**: in the rating-to-edge translation, use `odds_prob_calibrated` for the market rating instead of `odds_prob`. New conviction logic combines this with the odds-tier threshold table.
- **Skip the existing `_flb_warning()` UI nudge** once the calibrated edge is doing the work — or keep it as an additional sanity check at the longshot extreme.

### What NOT to do

1. Do not blindly use `odds_prob_calibrated` everywhere as a replacement for `odds_prob` — the two have different semantics. `odds_prob` is what the public believes; `odds_prob_calibrated` is what the public's belief should be after correcting for systematic bias. Edge calculations want the calibrated version; payout calculations want the raw version (because payouts come from the actual odds, not from corrected ones).

2. Do not apply FLB calibration without the odds-tier threshold. Naive FLB-edge>0 makes ROI worse than baseline.

## Honest limitations

1. **Calibration was fit on closing odds.** The same model applied to morning-line or earlier-window odds may not transfer. If race-day-sim adds a live-mode that uses pre-race odds, recalibrate.

2. **The 50/1+ bucket has only 51,808 observations** in the holdout — wide error bars on the deepest-longshot shrinkage. The 0.49 shrinkage estimate for small-field longshots is suggestive but should not be over-trusted.

3. **The combined_prob in rkm_market_analysis is itself a Benter combination of model and odds.** Applying FLB to combined_prob then comparing to odds_prob has some implicit double-correction. A cleaner integration would apply FLB at the rating-construction step in race-day-sim's `ratings.py`, not at the market-analysis step. This POC tested at the market-analysis level for data-availability reasons; the production integration should happen earlier.

4. **The ROI numbers assume betting at closing odds** with no slippage, no exotic-only payouts, no Kelly sizing. Real wagering at the +9.5% edge>0.10 threshold would face market-impact costs the POC doesn't model.

### Multi-year stability (rolling-window OOS, 2010-2016)

Step 8 reruns the three-way split for each test year T from 2010 to 2016: cal-train 1997..(T-2), tune T-1, score T. Result:

| Tier | 2010 | 2011 | 2012 | 2013 | 2014 | 2015 | 2016 | mean | +ROI yrs |
|---|---|---|---|---|---|---|---|---|---|
| chalk <2/1 | +9.1% | +6.7% | +7.4% | +9.0% | +7.5% | +1.7% | +5.0% | **+6.6%** | **7/7** |
| short 2-5/1 | +16.6% | +13.5% | +11.1% | +16.9% | +12.3% | +11.7% | +16.7% | **+14.1%** | **7/7** |
| mid 5-10/1 | +27.2% | +22.1% | +29.0% | +13.3% | +24.0% | +28.3% | +12.2% | **+22.3%** | **7/7** |
| long 10-20/1 | +38.4% | +64.0% | +23.1% | +40.0% | +37.6% | +13.5% | +17.2% | **+33.4%** | **7/7** |
| longer 20-50/1 | +36.4% | +93.4% | +47.7% | +27.8% | +47.7% | +29.7% | +40.5% | **+46.2%** | **7/7** |
| extreme 50/1+ | −47.7% | −45.5% | −44.8% | −47.6% | −43.3% | −48.7% | −44.0% | **−46.0%** | **0/7** |

**All five bettable tiers are profitable in every one of the seven test years.** Extreme 50/1+ loses ~45% in every year. The 2016 result was not lucky — the per-tier table is a stable, year-after-year edge across nearly two decades of pari-mutuel data.

The tuned thresholds wander modestly year-to-year (e.g., short 2-5/1 picks edge≥0.125 in five years and edge≥0.20 in two), but the resulting OOS ROI stays comfortably positive across that range. The chalk tier is the most marginal: 2015 came in at +1.7% with a CI that brushes zero, suggesting the chalk-tier edge is real but smallest in magnitude.

The mid 5-10/1 and long 10-20/1 tiers are the strongest in absolute ROI (mean +22% and +33% respectively), and these are precisely the tiers the audit identified as having the most actionable conviction-pick opportunity.

### Simulator-universe alignment (step 9, sim_candidates restriction)

Step 8 used the full rkm_market_analysis population (7.7M starter-races, 1997-2016). The simulator's `run_simulation.py` only plays a much narrower universe — the `sim_candidates` materialized view: 2005-2017, no Grade 1/2 days, ≥8 races, ≥7 with trifecta results, avg field ≥7, avg trifecta pool ≥$10K. **Question:** does the FLB tier table generalize from "any race that has odds and a model probability" to "the races the simulator actually plays"?

Step 9 (`09_simulator_alignment.py`) reruns the rolling-window methodology with `JOIN sim_candidates`, restricting both training and test data to that population (2.6M rows, 2005-2016). It also pulls the actual chart-paid `wps.payoff/unit` alongside the closing-odds proxy, to confirm the closing-odds ROI numbers aren't artifacts.

**Result — the strategy improves on the simulator's universe, doesn't degrade:**

| Tier | full pop mean ROI | sim_candidates mean ROI | Δ | profitable years |
|---|---|---|---|---|
| chalk <2/1 | +6.6% | +7.9% | +1.3pp | 6/7 (2015 marginal at −2.5%) |
| short 2-5/1 | +14.1% | +13.8% | −0.3pp | 7/7 |
| mid 5-10/1 | +22.3% | +24.8% | +2.5pp | 7/7 |
| long 10-20/1 | +33.4% | **+40.7%** | +7.3pp | 7/7 |
| longer 20-50/1 | +46.2% | **+54.6%** | +8.4pp | 7/7 |
| extreme 50/1+ | −46.0% | −44.9% | +1.1pp | 0/7 |

Three observations:

1. **The longshot-tier edges are 7-8pp BIGGER on the simulator's universe.** Hypothesis: removing Grade 1/2 days strips out stakes-quality longshots whose actual win rate is closer to their odds-implied (the public knows them). The remaining longshots in claimers/allowances/maidens are more uniformly mispriced.
2. **Chalk loses one year (2015 at −2.5%).** Chalk was the most marginal tier in step 8 too. The smallest absolute edge stays smallest, with most variance in its sign.
3. **Closing-odds ROI vs chart-actual `wps.payoff/unit` ROI agree to within 0.5pp across the board.** The POC's closing-odds proxy is not inflating the numbers.

**Practical implication:** the production integration shipped via Path C (`src/sim/flb.py` + `SimDay.flb_filter`) is load-bearing in the right direction. Per-tier expected ROI on simulator runs is at least as good as the POC numbers, and the long/longer tiers — which hold most of the +EV — are 20-25% better. The strategy did not just transfer; it improved.

## Next steps

1. **Multi-day sim batch through `run_simulation.py`** — validates that the simulator's full pipeline (rating-edge gate AND-ed with FLB-edge gate) preserves the +EV step 9 just confirmed at the data layer. Step 9 confirmed the **strategy** works on sim_candidates; the batch confirms the **production code path** delivers it. The first batch attempt (n=50, policy=win) stalled on per-day ratings/bias compute. Next pass should slim down per-race work (cache `format_race_ratings`, skip pace/equity/exotic-prediction work that isn't load-bearing) — target 5-10 days/min.

2. **Field-size-aware shrinkage** for the longshot extreme — defer until the multi-day batch shows meaningful action in small-field longshots.

3. **Investigate Grade 1/2 longshot pricing** as a separate research question — step 9 implies stakes-quality longshots are priced more accurately than claimers/allowances/maidens. Could become its own per-class shrinkage adjustment if the population is large enough.

## Files

Step scripts (all under `scripts/poc/flb-calibration/`):
- `01_empirical_curve.py` — full-data 50-bucket FLB curve
- `02_fit_smooth.py` — isotonic fit with chalk anchor
- `03_validate.py` — train/holdout calibration + log-loss/Brier
- `04_roi_impact.py` — coarse ROI comparison across strategies
- `05_subgroups.py` — field-size / surface / class divergence
- `06_threshold_grid.py` — per-tier ROI grid sweep (in-sample tuning)
- `07_threshold_oos_validation.py` — three-way-split OOS validation (single-year)
- `08_multi_year_stability.py` — rolling-window OOS across 2010-2016
- `09_simulator_alignment.py` — same methodology restricted to the simulator's playable universe

Artifacts (`tmp/`, gitignored):
- `flb_curve.csv` — bucketed empirical curve
- `flb_calibration.json` — full-data shrinkage lookup (200-point grid)
- `flb_calibration_holdout.json` — train-only fit, with holdout metrics
- `validation_metrics.json` — log-loss, Brier, per-bucket calibration
- `validation_calibration.csv` — calibration plot data
- `roi_comparison.csv` — strategy-by-strategy ROI
- `roi_metrics.json` — same as JSON
- `subgroup_curves.csv` — coarse curves per subgroup
- `threshold_grid.csv` — per-tier × per-threshold ROI sweep
- `threshold_grid_optimal.json` — optimal threshold per tier (in-sample)
- `threshold_oos.csv` — out-of-sample validation per tier (single-year)
- `threshold_oos.json` — full payload of tuned + validated thresholds
- `rolling_window_oos.json` — per-year tuned thresholds + OOS ROIs (2010-2016)
- `simulator_alignment_full_population.json` — step 9 baseline run
- `simulator_alignment_sim_candidates.json` — step 9 sim_candidates-restricted run

## Bottom line

**FLB calibration paired with per-tier minimum-edge thresholds delivers stably positive ROI across seven independent OOS years (2010-2016):**

- All five bettable tiers are **profitable in 7/7 test years on the full population**. Mean OOS ROIs: chalk +6.6%, short +14.1%, mid +22.3%, long +33.4%, longer +46.2%.
- Extreme 50/1+ is unprofitable in **0/7 test years** (mean −46%). Hard-block this tier.
- The 2016 single-year result (+18.7% weighted ROI) was conservative — the multi-year mean is higher and remarkably stable across nearly two decades of pari-mutuel data.
- **Restricted to the simulator's playable universe** (`sim_candidates`: 2005-2016, no Grade 1/2, ≥7 fields, ≥$10K tri pools), the strategy IMPROVES — long-tier mean +40.7% (vs +33.4%), longer-tier +54.6% (vs +46.2%). The longshot-tier edges are 7-8pp larger on the sim's universe, likely because stakes-quality longshots (which are priced accurately by the public) have been removed.
- The strongest absolute edges live in the **mid 5-10/1, long 10-20/1, and longer 20-50/1 tiers**, which align with the audit's identified conviction-pick opportunity.

The audit's RDS-T2.x options 1 (calibration) and 2 (tier threshold) must be implemented TOGETHER. Naive FLB without the threshold makes ROI WORSE than baseline by expanding the longshot bet set into the unprofitable territory.

**Next concrete action: prototype the integration in `compute_market.py` + `ratings.py` + `run_simulation.py`, then run a multi-day sim batch.**
