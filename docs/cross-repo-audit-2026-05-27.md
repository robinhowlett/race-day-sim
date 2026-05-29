# Cross-Repo Audit & Remediation Plan

**Date:** 2026-05-27
**Scope:** rkm, wagering-analytics, race-day-sim
**Trigger:** 0/15 record across 7 simulation days. Investigation found systematic flaws beyond the documented edge calibration issue.

**Status:** All findings are based on code review, not DB verification. Each finding requires DB-side validation before applying any fix. Fixes should be verified individually — running multiple fixes in parallel risks confusion about which one moved which metric.

---

## Tier 1: System-Breaking Bugs

These directly cause inflated edges, future-data leakage, or incorrect probability computation. Likely responsible for the bulk of the 0/15 record.

### RKM-T1.1 — Cross-track normalization is structurally inert  **[PARTIALLY DISPROVEN — 2026-05-27]**

**VERIFICATION RESULT (2026-05-27):** The audit's claim that the offsets are inert is WRONG. `rkm_track_offsets` has 104 rows (one per track) with a healthy distribution: range -2.80 to +1.28, std=0.83, with 59 tracks in the moderate (0.50-1.50) bucket and 20 in the large (1.50-3.00) bucket. Reference track is BEL (offset = 0.00 by design). 96.6% of `rkm_velocity_curves` rows have `adj_v0 ≠ v0`, average adjustment +0.87 ft/s. Confidence values vary (1.00 for high-shipping tracks down to <0.10 for thin ones).

**What the audit got right:** `rkm_velocity_curves` has NO `track` column — curves ARE aggregated per (horse, surface, distance_zone). The "shipping horse pairs" approach as literally described in the spec cannot be the implementation.

**What the audit got wrong:** The system DOES produce sensible offsets via some other path (possibly the orphaned `compute_group_priors` mentioned in audit RKM #10, or the simpler track-mean approach at lines 130-138). The track normalization is functional, not inert.

**Statistical validity remains an open question.** Whether the offsets correctly capture track speed differences (vs being confounded with horse-quality bias, per RKM #12 — top barns shipping good horses to flagship tracks) requires a deeper read of the actual code path, which the audit did via code review only.

**Updated severity:** Downgrade from HIGH to "investigation-needed" — the system isn't broken in the way the audit claimed, but its statistical correctness vs the original spec is unverified.

---

### RKM-T1.1 (original audit text below)

**Files:** `rkm/src/rkm/adjustments.py:144-220`, `rkm/scripts/compute_adjustments.py:33-50`

**Problem:** `compute_track_offsets` uses a "shipping horse" approach but the inputs make it impossible:
- Curves are aggregated per-(horse, surface), so each horse has only one v0 per surface — no per-track v0
- `compute_adjustments.py:33-50` assigns each (horse_key, surface) exactly one `primary_track`
- The pairs filter (`track_a < track_b`) returns empty results because each horse has only one track per surface
- The "track offset" being computed is essentially noise

**Verification approach:** Query `rkm_track_offsets` directly. If the values are all near-zero or only populated for a tiny subset, the bug is confirmed. Also: count how many distinct tracks each (horse_key, surface) pair has in the curves table — should be exactly 1.

**Fix:** Requires fitting curves per (horse_key, surface, track, distance_zone) so one horse has multiple v0s — one per track they ran at. Then compute pairwise differences across rows for the same (horse_key, surface). This is a significant RKM pipeline change.

**Severity:** HIGH. Without working track normalization, `adj_v0` isn't actually cross-track comparable, which means rating comparisons across tracks are meaningless.

### RKM-T1.2 — Bare horse name join ignores identity disambiguation  **[CONFIRMED — 2026-05-27]**

**VERIFICATION RESULT (2026-05-27):** Confirmed. 2.25% of horse names have multiple birth-year disambiguations in `rkm_velocity_curves` (7,438 names representing 14,876+ distinct horses). Concrete demonstration: "Capo" has 3 distinct horses (1991, 2001, 2011 birth years) with 5 separate curves. Bare-name join produces 80 matched starter rows when only 63 actual Capo starts exist — a 17-row (27%) inflation for that one name. The bug exists and propagates.

**Severity confirmed at HIGH.** Fix is straightforward: either (a) add `horse_key` to derived tables that consumers join on, or (b) require `vc.first_race ≤ r.date ≤ vc.last_race` in the join condition to date-disambiguate.

---

### RKM-T1.2 (original audit text below)

**Files:** `rkm/scripts/compute_adjustments.py:36`, `rkm/scripts/compute_situations.py:40`

**Problem:** `JOIN handycapper.starters s ON s.horse = SPLIT_PART(vc.horse_key, '|', 1)` — joins on bare horse name, ignoring the `|YYYY` birth-year disambiguation. Different horses sharing a name get merged.

**Verification approach:** Find a horse name with multiple birth years in `rkm_velocity_curves`, then trace through `compute_adjustments.py` and `compute_situations.py` to see if both share the same starts.

**Fix:** Attach canonical `horse_key` to `starters` (or use a horse-key-aware join). Search across both repos for any other instances of `SPLIT_PART(...horse_key...)` joins.

**Severity:** HIGH. Reused horse names contaminate the data.

### RKM-T1.3 — `compute_form.py` loop bound out of sync with `MIN_PRIOR_RACES`

**File:** `rkm/scripts/compute_form.py:136`

**Status (2026-05-27):** RESOLVED in code (recompute pending).

**Original framing:** Loop starts at index 2, "skipping first 2 races." User pushback was correct — the original `range(2, ...)` was BY DESIGN under the original `MIN_PRIOR_RACES = 2`: needing 2 prior races to make a snapshot meant starting at i=2 so `prior = race_obs[:2]` gives 2 races. The original code was self-consistent.

**Actual issue:** Our recent change of `MIN_PRIOR_RACES` from 2 → 1 in `form.py` was incompletely propagated. The script loop bound still hardcoded `range(2, ...)`, so 2nd-start horses (1 prior race) never got a snapshot — the broader coverage we intended did not exist in practice.

**Decision (2026-05-27):** Option B — honor `MIN_PRIOR_RACES = 1`. 2nd-start horses get a snapshot from their single prior race. Adds coverage; those snapshots will be noisy (single-race basis) but downstream consumers can weight by `n_recent_races`.

**Fix applied:** Loop changed to `range(1, len(race_obs))` at `compute_form.py:138` with comment explaining the dependence on `MIN_PRIOR_RACES`.

**Pending:** Full recompute of `rkm_current_form` on robinpc to materialize 2nd-start snapshots.

**Severity:** HIGH. Until recompute, 2nd-start horses (a key market situation, especially for FTS-following debuts) remain silently excluded from `current_form`.

### RKM-T1.4 — Career baseline leaks future data into v0_trend [FIXED 2026-05-29]

**Files:** `rkm/scripts/compute_form.py:53-62, 105-106, 140`

**Status (2026-05-27):** CONFIRMED by code review.

**Problem:** `career_v0` is loaded once from `rkm_velocity_curves` (compute_form.py:53-57), which `compute_curves.py` fits over each horse's *entire* career with no date bound. That single full-career value is passed unchanged into every `compute_form_at_date` invocation regardless of snapshot date (lines 105-106, 140). `v0_trend = current_v0 - career_v0` therefore compares prior-only `current_v0` against a future-aware `career_v0`. The `rkm_current_form` table is marked "pre-race safe" in CLAUDE.md but isn't strictly so for `v0_trend` or `career_v0`.

There is no path in the code by which the stored `career_v0` could be as-of-date — it is a static lookup.

**Fix:** Compute career baseline as a trailing aggregate (only races before the snapshot date), not the full-career curve. This means `career_v0` becomes a per-snapshot computation, not a join from `rkm_velocity_curves`.

**Fix applied 2026-05-29:** `compute_form_at_date` now derives both fits from the same `prior_observations` set (which the caller already constructs as strictly-prior races): the **recent** values come from a recency-weighted polyfit (DECAY_FACTOR ** days/30), and the **career-to-date** values come from an *unweighted* polyfit on the same data. `v0_trend = current_v0 - career_v0` now means "recent vs career-to-date" rather than the old "recent vs full-career-with-future-leak."

Implementation:
- `compute_form_at_date` signature changed: dropped the `career_v0` / `career_decay` parameters. Both quantities now come out of the function rather than going into it. Old callers that passed those args fail with a `TypeError` (covered by a regression test).
- `compute_form.py` no longer joins `rkm_velocity_curves` for the career parameters. It still loads the `(horse_key, surface, distance_zone)` set from that table as a quality gate (filters out horses with too-few observations to fit a meaningful curve at all) — same eligibility behavior as before, just without the leaky values.
- Schema unchanged: `rkm_current_form.career_v0` / `career_decay` keep the same column names and types but now hold trailing-career values.
- 9 new pytest cases in `tests/test_form.py` cover: point-in-time invariance, robustness to a future race accidentally appearing in the input, recent-higher-than-career → positive trend, recent-lower → negative trend, single-prior-race → zero trend, sub-threshold inputs returning None, and the dropped-parameters contract.

CLAUDE.md updated: `rkm_current_form` is now strictly pre-race-safe across **all** columns (was previously "partially" because `career_v0` / `career_decay` came from a static full-career fit).

**Operator action:** the `compute_form.py` recompute that's already pending for RKM-T1.3 (2nd-start coverage) will also materialize the trailing-career baseline. Run once to refresh both.

**Severity:** HIGH (broke the pre-race firewall for `v0_trend` and `career_v0`) → FIXED.

### WA-T1.1 — Stern k = 0.81 was never empirically calibrated

**Files:** `wagering-analytics/scripts/populate_stern_fair.py:29`, `wagering-analytics/docs/exotic-payoff-analysis.md:201-227`

**Status (2026-05-27):** PARTIALLY CONFIRMED — no calibration code existed; calibrated MLE differs modestly from 0.81.

**Problem:** Spec promised a grid-search calibration segmented by field size / surface / race_type. No such code existed in the repo. The constant 0.81 was imported from Stern (1992) — a different dataset and era. CLAUDE.md called it "empirically confirmed" without a calibration record.

**Verification (2026-05-27):** Built `wagering-analytics/scripts/calibrate_stern_k.py`. Ran grid search of k ∈ [0.50, 1.20] step 0.02 against top-3 ordering log-likelihood. Used 80,042 clean races (excluded coupled entries, DH/DQ in top 3, incomplete top-3, and field size <5).

**Findings:**
- **Global MLE k = 0.86** (corrected 2026-05-27 to use `official_position` instead of `finish_position`; original `finish_position` run gave 0.88).
- LL surface very flat near peak: k=0.81 is ~150 nats worse than peak across 80K races (~0.002 nats/race) — practically negligible per-race, but a clear ~0.05 offset in MLE.
- Segmentation by field size **does NOT earn its keep**: small (5-7) → 0.86, mid (8-10) → 0.86, large (11+) → 0.88. CLAUDE.md's "minimal variation by field size; single parameter sufficient" is empirically supported.
- The chosen single parameter (0.81) is off by ~0.05 from the MLE.

**Fix:** Updated `populate_stern_fair.py:29` to `STERN_K = 0.86`. Run `populate_stern_fair.py --recompute-all` to refresh `exotic_harville_ratios.stern_fair`. Document calibration provenance and keep `calibrate_stern_k.py` checked in for reproducibility. Skip surface/distance segmentation (gain is minimal vs added complexity); revisit only if odds-implied prob calibration changes.

**Note on official_position vs finish_position:** Exotic payoffs are settled against `official_position` (post-DQ, post-objection), not `finish_position` (where the horse crossed the line). 213K starters differ between the two columns; 50K were DQ'd; ~37K races had a top-3 DQ. Calibration scripts must use `official_position`.

**Caveat:** Calibration uses tote-implied win probabilities (per `race_probabilities.win_prob`), so the calibrated k inherits any favorite-longshot bias in the closing odds. Document this.

**Severity reclassified:** MEDIUM. Every `stern_fair` value is biased, but the bias is small (0.81 vs 0.87 on a flat LL surface). The "15-21% trifecta overlay" headline shifts modestly but doesn't invert.

### WA-T1.2 — Payoff model R² is largely tautological

**Files:** `wagering-analytics/scripts/fit_payoff_models.py:80-95, 152, 210-226, 306-308`

**Status (2026-05-27):** PARTIALLY DISPROVEN — model adds real skill above naive baseline; tautology framing was too harsh.

**Problem (original framing):** Random train/test split (not year-stratified). Model fits `log(payoff) ~ log_odds_1 + log_odds_2 + log_odds_3` which is "inverse of joint probability identity" — high R² mostly reflects identity, not learned skill. No naive baseline reported.

**Verification (2026-05-27):** Built `wagering-analytics/scripts/verify_payoff_skill.py`. Year-stratified split (train < 2016, test 2016-2017). Tested against naive Stern baseline (k=0.87, after refresh). Excluded SUPERFECTA (race_probabilities.wagering_position only goes 1-3 in dataset).

**Findings:**

| Model | EXACTA R²_test (n=97K) | TRIFECTA R²_test (n=96K) |
|---|---|---|
| Naive Harville (1 predictor) | 0.867 | 0.808 |
| Naive Stern k=0.87 (1 predictor) | 0.874 | 0.819 |
| Pre-race full (no fav_*) | 0.915 | 0.893 |
| Original full (with post-race fav_*) | 0.916 | 0.894 |

**Deltas:**
- Stern over Harville: +0.007 / +0.011 (small but real)
- **Pre-race full over Stern: +0.041 / +0.074 — this is the actual learned skill.** Pool size, field size, HHI, surface effects do add genuine predictive value above naive Stern fair value.
- Post-race fav_* features: +0.001 / +0.001 — see WA-T1.3 below.

**Conclusion:** The model adds 4-7 R² points of real skill above the naive Stern baseline. The original "R²=0.88" headline is somewhat misleading (most variance comes from the joint-odds identity), but the model is not mostly tautological — it captures real pool-size, field-size, and surface effects. Year-stratified test R² ≈ train R², so generalizes.

**Fix:** (1) Update `fit_payoff_models.py` to year-stratified split. (2) Report skill-above-naive (ΔR²) alongside raw R² in the coefficient JSON. (3) Use refreshed `stern_fair` (k=0.87) as input.

**Severity reclassified:** MEDIUM. Headline misleading but model has real skill. Audit's "mostly tautology" framing was wrong.

### WA-T1.3 — Payoff model uses post-race features

**File:** `wagering-analytics/scripts/fit_payoff_models.py:111, 154-159`

**Status (2026-05-27):** DISPROVEN for verticals (EXACTA/TRIFECTA); horizontals (Pick 3/4/5/6) NOT YET VERIFIED.

**Problem (original framing):** `bad_fav_legs`, `fav_won`, `fav_second`, `fav_third`, `fav_fourth`, `log_odds1_x_fav_*` interactions are post-race outcomes. Model is sold as pre-race projection but fit on race outcomes. Audit cited Pick 3/4 `bad_fav_legs` coefficient at 0.088 / 0.122 as evidence of feature dominance.

**Verification (2026-05-27 — verticals only):** `verify_payoff_skill.py` compared pre-race full model (no `fav_*` features) vs original full model on year-stratified holdout.

**Findings (verticals):**

| Bet type | Pre-race R²_test | + fav_* features R²_test | ΔR² |
|---|---|---|---|
| EXACTA | 0.915 | 0.916 | **+0.001** |
| TRIFECTA | 0.893 | 0.894 | **+0.001** |

**Conclusion (verticals):** The post-race `fav_*` features contribute essentially zero out-of-sample R² for EXACTA and TRIFECTA. They are present in the spec and statistically non-zero in coefficient terms, but predictively inert. The audit's concern that these features were doing real work was **wrong for verticals**.

**Recommendation:** Drop the `fav_*` features from vertical models for cleanliness (they're dead weight, and they're post-race so violate the contract on principle), but accept that doing so won't materially change predictions.

**Horizontal verification (2026-05-27):** `verify_payoff_skill_horizontal.py` ran on Pick 3 (608K) and Pick 4 (137K), excluding carryover. Year-stratified split.

| Bet | Pre-race R²_test | + bad_fav_legs R²_test | ΔR² |
|---|---|---|---|
| PICK_3 | 0.7498 | 0.7502 | **+0.0003** |
| PICK_4 | 0.6858 | 0.6862 | **+0.0004** |

`bad_fav_legs` coefficient was +0.078 (Pick 3) / +0.111 (Pick 4) — non-zero but predictively inert. The audit confused coefficient magnitude with predictive contribution; in correlated feature sets, a non-zero coefficient just shifts mass without adding skill.

**Bonus finding (not in original audit):** Pick 3 OLS model is near-useless above a one-line parlay formula. Naive parlay R²=0.747 vs full pre-race R²=0.750. Pick 4 marginally better at +0.027 above naive. Could replace the Pick 3/4 OLS models with `expected_payoff = (1 - takeout) × Π(odds_i + 1)` and lose almost nothing.

**Pick 5 / Pick 6 verified 2026-05-27 (carryover-aware):**

Distinction:
- **Standard pari-mutuel carryover** is +EV — yesterday's stranded pool gets added to today's, effectively a takeout reduction. Pros wait for carryover days.
- **Jackpot pool_type** is a separate product (Rainbow 6, Single 6 Jackpot) with single-unique-winner rules; only +EV on mandatory-payout days. Excluded from this verification.

`verify_payoff_skill_pick56.py` ran on Pick 5 (20K rows, 10% carryover) and Pick 6 standard (44K rows, 54% carryover). Year-stratified split (train < 2016, test 2016-2017).

| Model | Pick 5 R²_test | Pick 6 R²_test |
|---|---|---|
| Naive parlay only | 0.261 | **−0.001** (worse than mean) |
| + log_carryover | 0.575 | 0.326 |
| Pre-race full (legs+pool+carry+hhi+field) | 0.626 | 0.469 |
| + bad_fav_legs (post-race) | 0.627 | 0.468 |

**Findings:**

1. **Naive parlay is useless for Pick 5/6.** R² = 0.26 (Pick 5) and ~0.00 (Pick 6) — naive multiplication of leg odds explains essentially nothing of payoff variance. This contrasts sharply with Pick 3 (0.75) and Pick 4 (0.66). At Pick 5/6 depth, pool and carryover dynamics dominate.

2. **Carryover is the single largest predictor.** Adding `log_carryover` to naive parlay: +0.31 R² (Pick 5), +0.33 R² (Pick 6). Coefficient is NEGATIVE (-0.45 / -0.32) — when controlling for pool size, high-carryover days attract a flood of syndicate money producing more winners, so per-ticket payoff is lower than an equal-sized non-carryover pool.

3. **bad_fav_legs predictively inert across all four horizontals** (Pick 3 +0.0003, Pick 4 +0.0004, Pick 5 +0.001, Pick 6 −0.001). Audit's WA-T1.3 concern is fully disproven.

4. **The current `fit_payoff_models.py:183-184` actively excludes carryover for Pick 6:** `if bet_type == "PICK_6": extra = " AND (e.carryover IS NULL OR e.carryover = 0)"`. Since 54% of Pick 6 standard rows have carryover, the model is fit on the minority of pool dynamics, missing the variation that would teach it pool behavior. Pick 5 doesn't have this exclusion but also doesn't include `log_carryover` as a feature, so it can't distinguish carryover-vs-not within its sample.

**Note:** Pick 6 is +EV in two distinct cases — (a) carryover days where pool dynamics lower effective takeout, and (b) any day where the bettor's structural edge (vulnerable-favorite plays, contrarian leg construction) creates a payoff differential vs the public's chalk ticket. Race-day-sim should surface both. The existing model handles neither correctly; it predicts non-carryover days but without a model of how pool dynamics shape payoffs, even non-carryover predictions are noisy.

**Severity reclassified:**
- WA-T1.3 (bad_fav_legs): LOW across all bet types — disproven
- Existing Pick 5/6 OLS models: HIGH urgency to rebuild — include all `pool_type = STANDARD` rows (carryover and non-carryover both), add `log_carryover` as a feature, exclude only `pool_type = JACKPOT`. Race-day-sim needs working payoff projections for both carryover-EV plays and structural-edge plays.

### WA-T1.4 — Trainer profiles are aggregate, not point-in-time

**Files:** `wagering-analytics/scripts/compute_trainer_profiles.py:14-16, 33`, `wagering-analytics/docs/market-bias-analysis.md:83-84`

**Status (2026-05-27):** STRUCTURAL PROBLEM CONFIRMED; NO CURRENT LEAKAGE in race-day-sim — table is unused there.

**Problem:** `compute_trainer_profiles.py:33` sets `DATE_RANGE = ("2005-01-01", "2017-12-31")` and groups all queries within it. The resulting `trainer_ae_profiles` table contains a single career-aggregate A/E per trainer per dimension, computed across the full 13-year window. From any pre-2017 race's perspective this contains future information.

**Verification (2026-05-27):**
1. Confirmed by code review and DB sample. For Calhoun, full-career A/E = 0.870 vs as-of-2010-06-01 A/E = 0.916 (delta = -0.046). Smaller deltas (0.003-0.010) for other top trainers, but sign and magnitude unpredictable per trainer.
2. **Searched all of race-day-sim/src/, scripts/, notebooks/ for `trainer_ae_profiles` references — zero hits.** Race-day-sim never queries the static table during simulation.
3. `blinder.py:load_market_bias` (lines 156-216) computes trainer A/E point-in-time via CTEs (`date < race_date`) across all 5 dimensions. This is the actual live input — already correct.

**Conclusion:** The static table is research scratch space, not a live simulation input. The structural leakage exists but is currently benign. The risk is anticipatory: someone could later query the static table as a "shortcut," at which point blinded simulations would silently leak future data.

**Recommended fix (in order of preference):**
1. **Drop the table entirely.** No code path reads it; existence is the only risk. Adjust CLAUDE.md (currently lists `trainer_ae_profiles` as a race-day-sim dependency, which is incorrect).
2. Or rename to `trainer_ae_profiles_research_only` or move to a research schema, making misuse self-evident.
3. Or keep + document with prominent "DO NOT USE FOR LIVE SIMULATION" header in the script and CLAUDE.md.

**Severity reclassified:** LOW (no live leakage). The audit's anticipatory framing was right — this is a footgun — but not a current bug. Recommend Option 1 as a one-line fix.

### WA-T1.5 — Jitter calibration measures wrong quantity

**File:** `wagering-analytics/scripts/compute_jitter_calibration.py:35-86, 117`

**Status (2026-05-27):** STRUCTURAL PROBLEM CONFIRMED; NO LIVE LEAKAGE — calibration is unused.

**Problem:** Script's stated intent is to capture within-race odds drift between bet placement and post-time. Actual SQL computes `STDDEV(log_winner_odds - leg1_log_odds)` across exotic_ids — i.e., the variance of where leg-2 winners closed relative to leg-1 winners across all sequences. That's just twice the within-leg odds-distribution variance (winners are independent draws), not a measure of drift.

Output sigma ≈ 1.0 across all leg positions (1.01, 1.01, 1.02, 0.99, 0.98). Within-leg stddev_log_odds is ~0.73, so √2 × 0.73 ≈ 1.03 — exactly what you'd predict if the script measures inter-sequence spread, not drift.

A σ=1.0 log-normal jitter means each odds projection is multiplied by `e^N(0,1)`, spanning ~2.7× per std dev. 95% CI = 0.14× to 7.4×. Signal is drowned.

**Verification (2026-05-27):**
1. Inspected `models/jitter_calibration.json`: σ values are 1.0102, 1.0127, 1.0157, 0.9938, 0.9759 across legs 2-6. Flat, not monotonically increasing — consistent with measuring the wrong quantity.
2. Searched race-day-sim for jitter consumers: `horizontal.py:29` defines `get_leg_sigma`, but **no other code calls it.** `estimate_horizontal_value` (the function that actually computes parlay probability) ignores jitter entirely — uses leg odds directly.
3. The intra-race odds time series needed for a real jitter calibration is **not in the database** — `race_probabilities.win_prob` is closing-only.

**Conclusion:** Calibration is structurally broken AND unused. Race-day-sim's horizontal projections currently rely on closing-odds parlay multiplication, not jitter-perturbed odds.

**Architectural reframe — jitter is a non-problem for blinded backtests:** Jitter exists to model uncertainty about future closing odds when constructing a multi-leg ticket live (you have leg-1 closing odds but legs 2/3/4 are still 30-90 min from post). Race-day-sim is a blinded backtest — `blinder.load_pre_race_card()` loads closing odds for every race on the card before any bet is constructed. The simulator already knows every leg's closing odds with zero uncertainty. There is nothing to jitter.

Jitter would matter only in: (1) live wagering mode, (2) explicit what-if sensitivity analysis, (3) adversarial "could a real bettor have built this?" testing. None are on the current roadmap.

**Recommended fix:**
1. Delete `get_leg_sigma` from `race-day-sim/src/sim/horizontal.py`.
2. Delete `race-day-sim/models/jitter_calibration.json`.
3. Delete `wagering-analytics/scripts/compute_jitter_calibration.py`.
4. Document: "Jitter applies to live betting, not blinded backtests; defer until live mode is roadmapped."

**Severity reclassified:** LOW. Same pattern as WA-T1.4 — broken file with no consumers, but the underlying premise (future-leg uncertainty) doesn't apply to backtest-mode race-day-sim, so this isn't even worth a heuristic placeholder. Drop entirely.

### RDS-T1.1 — TEMPERATURE = 6500ms produces nearly-uniform probabilities

**Files:** `race-day-sim/src/sim/probability.py:17, 28`

**Status (2026-05-27):** CONFIRMED — high-confidence finding, likely a major contributor to -42% ROI.

**Problem:** `model_probs_from_curves` softmax uses `temperature = 6500.0` (in ms). With T = 6500, the time gap that halves probability is `T × ln(2) ≈ 4500ms` — about 22 lengths. Real within-race predicted-time spreads are nowhere near that.

**Verification (2026-05-27):**

1. **Empirical within-race time spreads** (581 races, sample week of June 2014):
   - Mean max-min spread: **2,805 ms** (≈14 lengths)
   - Mean IQR (P75 − P25): **1,055 ms** (≈5.3 lengths)
   - Mean stddev: **972 ms** (≈4.9 lengths)
   - Max spread: 14,704 ms; min spread: 588 ms

2. **Probability distributions for an 8-horse field with even spacing across 2,800ms:**

| T | p₁ | p₂ | p₃ | p₄ | p₅ | p₆ | p₇ | p₈ | p₁/p₈ ratio |
|---|---|---|---|---|---|---|---|---|---|
| 200 | 87% | 12% | 2% | <1% | 0 | 0 | 0 | 0 | 1.2M:1 |
| 500 | 55% | 25% | 11% | 5% | 2% | 1% | <1% | <1% | 270:1 |
| 1000 | 34% | 23% | 15% | 10% | 7% | 5% | 3% | 2% | 16:1 |
| 2000 | 23% | 19% | 15% | 13% | 10% | 8% | 7% | 6% | 4:1 |
| **6500** | **15%** | **14%** | **14%** | **13%** | **12%** | **11%** | **11%** | **10%** | **1.5:1** |

At T=6500, **the fastest horse has only a 1.5× edge over the slowest in a 14-length-spread field**. The model layer is producing nearly-uniform probabilities.

**Cascade effect through Benter:** `benter_combine` does `α × log(model_prob) + β × log(odds_prob)` with α=1.89, β=1.0. When model_prob is near-uniform, log(model_prob) is near-zero across all horses, so the α term contributes nothing. **The Benter output becomes a near-pure echo of the market.** That means:
- Edge-vs-market is mostly noise
- The system is "betting based on a model that has no opinion"
- Likely a major contributor to the documented -42% ROI

**Fix:** Recalibrate TEMPERATURE. Based on observed time-spreads, **T ≈ 1000ms** produces realistic dispersion (favorite ~25-35%, longshot 2-7%). A more rigorous approach: fit T jointly with α via MLE on historical race outcomes (logistic regression on actual finish vs predicted-time differences). For now, use T = 1000ms as a defensible default and document the calibration improvement as a follow-up.

**Severity:** HIGH (confirmed). One of the single most impactful issues identified in this audit. Affects every Benter-combined probability and every downstream edge calculation. Almost certainly contributing to negative ROI.

### RDS-T1.2 — Off-turf credit applied to entire field, not just favorite

**File:** `race-day-sim/src/sim/ratings.py:267-268` (pre-fix)

**Status (2026-05-27):** CONFIRMED and FIXED.

**Problem (verified):**
```python
if _flag("off_turf"):
    multiplier *= 1.050
```

Research finding 9 (`research-findings.md:78, 134`): off-turf favorite gets +7.5% lift (A/E = 0.884 lift); the recommendation is "use the favorite strongly, fade turf-only horses underneath." The original code applied a +5% lift to every starter in off-turf races, inverting the research — turf-only horses (which research says to fade) got the same boost as the favorite (which research says to lean on).

**Fix applied:**
- `bias_multiplier()` now accepts `is_favorite: bool = False` parameter.
- Off-turf credit raised from 1.050 → 1.075 (matching the research +7.5% number) and gated to favorites only.
- Caller (`format_race_ratings`) identifies favorite by lowest closing odds and passes through.
- Complementary "fade turf-only horses underneath" remains TODO — needs each horse's recent surface history (separate access path).

**Fix:** Only apply when the horse is the favorite (from `s.choice == 1`). Add a separate negative multiplier for turf-only horses on dirt.

**Severity:** HIGH. Inverts a core research finding.

### RDS-T1.3 — Turf rating prior double-counts surface offset

**File:** `race-day-sim/src/sim/ratings.py:134-136` (pre-fix)

**Status (2026-05-27):** CONFIRMED and FIXED.

**Problem:** Original code did `if surface == "Turf": base += 5`, applied on top of a dirt-scale class ladder. But `_get_anchor` already returns `anchor_rating = 112` for turf (vs 100 for dirt) — a +12 universal-scale offset. The +5 fudge was inconsistent with the anchor: a $25K turf claimer's physics rating anchored to ~112 while the prior anchored to ~110. The misalignment grew with purse band.

This affected **every turf rating**, not just prior-only horses, because `format_race_ratings` blends `w × physics + (1-w) × prior`.

**Fix applied:**
- Replaced single `_CLASS_RATINGS` dict with `_CLASS_RATINGS_MAIN` (Dirt + Synthetic, anchor 100) and `_CLASS_RATINGS_TURF` (= main + 12, anchor 112). Constant `_TURF_CLASS_OFFSET = 12`. The naming reflects the canonical-anchor table grouping: Dirt and Synthetic both anchor to 100; only Turf elevates.
- `compute_prior_rating()` picks the turf ladder when `surface == "Turf"`, main ladder otherwise (Dirt, Synthetic, or unknown).
- Removed the `if surface == "Turf": base += 5` line. The +12 also propagates through the claiming-purse refinement branch.

**Verified:** Synthetic spot-check confirms it anchors to 100 (matches `_get_anchor("Synthetic", 6.0)[1] = 100`): `compute_prior_rating("CLAIMING", 25000, "Synthetic") = 105`, `compute_prior_rating("ALLOWANCE", 0, "Synthetic") = 114`. Turf spot-check: `compute_prior_rating("CLAIMING", 25000, "Turf") = 117` (= 105 + 12), `compute_prior_rating("ALLOWANCE", 0, "Turf") = 126` (= 114 + 12). All priors now align with physics anchor.

**Severity reclassified:** HIGH (was misrating every turf horse, even those with strong physics) → FIXED.

### RDS-T1.4 — Surface-switch trainer A/E double-counts

**File:** `race-day-sim/src/sim/ratings.py:300-309, 341-345` (pre-fix)

**Status (2026-05-27):** CONFIRMED and FIXED.

**Problem:** Generic surface-switch multiplier (Synthetic→Turf = 1.075, etc.) was applied unconditionally. Then later, if the trainer had a `trainer_switch_ae` record with ≥10 switches, the trainer-specific multiplier (`trainer_switch_ae / 0.80`) was applied multiplicatively on top. The trainer's A/E was measured on their actual surface switches — so the population effect is **already baked in**. A trainer matching the population average had their effect counted twice (1.075 × 1.075 = 1.156).

The class-drop block immediately below correctly undoes the generic before applying trainer-specific (`multiplier /= 1.029` then apply `trainer_drop_ae / 0.80`). Surface-switch did not.

**Fix applied:**
- Captured the generic surface-switch multiplier in a local `surface_switch_mult` variable when applied.
- Trainer-specific block now divides by `surface_switch_mult` (undo generic) before multiplying by `trainer_switch_ae / BASELINE_AE`. Mirrors the class-drop pattern.
- Generic remains the fallback when trainer has no record / insufficient sample.

**Verified with 4-case sanity test:**
- Generic-only (no trainer record): 1.075 ✅
- Trainer A/E = 1.0 (strong): 1.250, not 1.344 ✅
- Trainer matches pop avg (A/E ≈ 0.86): 1.075, not 1.156 ✅ (was the double-count case)
- No switch: 1.000 ✅

**Severity reclassified:** HIGH (compounded artificially across all surface-switching horses) → FIXED.

### RDS-T1.5 — Horizontal parlay_prob unnormalized for overround

**File:** `race-day-sim/src/sim/horizontal.py:135` (pre-fix)

**Status (2026-05-27):** CONFIRMED with corrected direction; FIXED.

**Problem:** `leg_prob = sum(1.0 / (s.get("odds", 99) + 1) for s in selections)` summed raw `1/(odds+1)` per leg without overround correction. Across a full field with 17% takeout, raw `1/(odds+1)` sums to ~1.17, not 1.0 — so each horse's raw value **over-estimates** true probability.

**Direction correction:** The audit said "leg_prob is under-estimated, payoff is over-estimated, tickets look more attractive than they are." That was backwards. Raw `1/(odds+1)` over-estimates probability, so leg_prob is over-estimated, parlay_prob is over-estimated, and `fair_payoff = (1-takeout)/parlay_prob` is **under**-estimated. Tickets looked **less** attractive than they are. The system was too pessimistic, not too optimistic.

**Verified with sanity test:**
- Pick 3 with 3/1 favorites in each leg, 8-horse fields (overround 1.16):
  - Without normalization: parlay_prob = 0.0156, fair_payoff = $48
  - With normalization: parlay_prob = 0.0100, fair_payoff = $75
- The system was ~36% too pessimistic about fair payoff per Pick 3.

**Fix applied:**
- Added `all_odds` field to each leg dict in the API.
- `estimate_horizontal_value` now normalizes by the field's overround when `all_odds` is supplied, falls back to raw sum (with a docstring warning that raw values are biased) when not.

**Note:** `estimate_horizontal_value` is currently imported but unused in `simulate_race_day.py` — same pattern as several other findings today. The fix is preventive: when this is wired into actual ticket-construction logic, it'll produce honest fair values out of the gate.

**Severity reclassified:** HIGH (would distort every horizontal evaluation if wired up; system would systematically underbet horizontal value) → FIXED.

---

## Tier 2: Significant but localized issues

### RKM coverage / discontinuity findings (added 2026-05-27 from sim run on GP 2014-09-06)

**RKM-T2.1 — Sprint/route binary cutoff produces hard discontinuity at 6.5f**

Surfaced when investigating low coverage on R10/R11 (2yo FSS stakes at 7f Dirt, only 1/8 and 1/13 horses RATED). Root cause: the field is dominated by 2yos with rich sprint data but no route data. Current `(horse_key, surface, distance_zone)` partition with `zone = "sprint" if furlongs <= 6.5 else "route"` drops them to UNRATED for any 7f+ race.

Stop-gap fix applied 2026-05-27: cross-zone fallback in `blinder.py`. When primary-zone curve is missing, use opposite-zone curve with surface-specific shift and confidence haircut.

Empirical cross-zone correlations (117K paired starters, route − sprint, same horse, same surface):
- Dirt: r(v0) = 0.38, Δv0 = -2.81 ft/s, Δdecay = -1.17
- Synthetic: r(v0) = 0.51, Δv0 = -3.33 ft/s, Δdecay = -1.21
- Turf: r(v0) = 0.25, Δv0 = -4.09 ft/s, Δdecay = -0.78

Original spec used 0.34 as a single global cross-zone factor — that was actually the cross-*surface* correlation (dirt ↔ turf, route only). Different dimension. Replaced with surface-specific cross-zone numbers from direct measurement.

**Remaining gaps:** the binary cutoff is itself too coarse. Distance distribution shows 7 distances cover ~84% of races (5f, 5.5f, 6f, 6.5f, 7f, 8f, 8.3f, 8.5f). 7f is a "tweener" — currently classified as route but physiologically closer to a long sprint. A 4-band partition (SHORT_SPRINT < 6f, MID_SPRINT 6-6.5f, TWEENER 7-7.5f, ROUTE ≥ 8f) would capture more of the gradient, at the cost of splitting curve data and requiring a recompute of `rkm_velocity_curves`.

**RKM-T2.2 — `rkm_current_form` is single-dimensional (no surface/zone partition)**

Schema is `(starter_id, race_id, current_v0, current_decay, ...)`. One row per starter, no surface or distance_zone. This means a horse's "current form" mixes recent sprint and recent route observations, and recent dirt and recent turf. A horse with strong sprint form and rusty route form has a single muddled `current_v0`.

Should be `(starter_id, surface, distance_zone)` or finer (matching whatever zone scheme is settled in T2.1). Requires rebuilding the form-snapshot computation in `rkm/scripts/compute_form.py` with surface- and zone-aware partitioning.

**Severity:** MEDIUM — affects accuracy of `current_v0`/`current_decay` for any horse who's raced multiple zones recently, especially zone-switchers or surface-switchers. Same kind of confounding as RKM-T2.1 but for the recent-form layer.

**RKM-T2.3 — RKM-T1.2 prerequisite for cross-zone fallback safety**

The bare-name `SPLIT_PART(vc.horse_key, '|', 1) = s.horse` join in `blinder.py` was already known (RKM-T1.2). The cross-zone fallback adds a second LEFT JOIN against `rkm_velocity_curves` on bare name, which **doubles the row-multiplication risk** when a starter's name collides with another horse_key. Confirmed on GP 2014-09-06 R10/R11 — "Of Course" matched 3 horse_keys (1994/2002/2012) and "Leap Year Luck" matched 2 (1996/2012), inflating R10 from 8 → 9 rows and R11 from 13 → 14.

Cross-zone fallback is structurally sound but relies on RKM-T1.2 being fixed first to avoid attaching wrong-horse data. Treat T1.2 as a hard prerequisite for safe T2.1 deployment.

### Cross-cutting

- **Date range chaos:** RKM scripts use 1997-2016, form computation 1991-2017, WCMI 1999-2017, trainer profiles 2005-2017, payoff models all data. CLAUDE.md inconsistencies. Audit each script and align to a documented standard (likely 1991-2017 with caveats for exotic data starting 1999).
- **A/E denominators not normalized for overround** (WA #19): ✅ FIXED 2026-05-28. `blinder.py:load_market_bias` now joins each trainer-bias subquery to a `race_overround` CTE and normalizes raw `1/(odds+1)` by the per-race overround. Each starter's `true_prob = (1/(odds+1)) / race_overround` sums to 1.0 per race instead of 1.17.

  **Impact:**
  - Population A/E shifts from 0.802 → 1.002 (verified empirically on 2014 TB, 350K starters). A neutral trainer now displays as A/E ≈ 1.0 instead of 0.8.
  - `BASELINE_AE` in `ratings.py` updated 0.800 → 1.000 in lockstep so bias multipliers (`trainer_ae / BASELINE_AE`) are unchanged numerically.
  - Five trainer dimensions affected: FTS, claim, drop, layoff, surface_switch. Sim sanity-tested on GP 2014-09-06 — output unchanged as expected.

  Displayed A/E values are now directly interpretable: 1.20 = 20% better than expected, not "0.96 vs population 0.80 = slightly above neutral."
- **Coupled entries treated as independent everywhere** (WA #11): V003, Stern, payoff, WCMI, trainer A/E all ignore coupling. Affects ~3-5% of US races.
- **"Edge" defined three different ways across modules** (RDS C1): ✅ FIXED 2026-05-28. `payoff.py:edge_pct` renamed to `overlay_pct` (it's an overlay-percentage metric, not "edge in rating space"); `kelly.py` internal `edge` renamed to `ev_per_dollar` (per-dollar EV); `ratings.py:edge` retained as the canonical "edge" (rating-point distance from market in rating-point space); `horizontal.py:horizontal_advantage_pct` already unambiguous (takeout savings, not "edge"). Caller in `simulate_race_day.py` updated to read `overlay_pct`. Module docstrings now distinguish the three concepts explicitly.

### Race-Day-Sim specific

- **`evaluate.py` exotic payoff math** (RDS H1): ✅ FIXED 2026-05-28 (deprecated, then deleted). Was marked deprecated with `DeprecationWarning` and bugs documented in docstring; canonical evaluator is `SimDay._evaluate_bet` in `run_simulation.py`. Module fully removed when PROTO-T3.7 (scaffold consolidation) deleted its only consumer (`simulate_race_day.py`).
- **`kelly_exotic` formula** (RDS H5): ✅ FIXED 2026-05-28 by deletion. Function was dead code (never called); the formula was off by `b/(b+1)` and didn't match its docstring. Removing it eliminates the foot-gun. `size_bets` in `kelly.py` is the canonical exotic sizer.
- **Pace thresholds are unit-naive across surfaces** (RDS M1): ✅ FIXED 2026-05-28 (re-refined with empirical surface×zone fractions). `predict_pace` takes a `surface` parameter and looks up surface×zone-specific gap-threshold fractions (P50 = contested, P85 = lone-speed) calibrated from handycapper TB 2014 data (~34K races):

  | Surface×Zone | Contested frac | Lone-speed frac | n_races |
  |---|---|---|---|
  | Dirt route | 0.0125 | 0.0320 | 10,342 |
  | Dirt sprint | 0.0134 | 0.0334 | 18,095 |
  | Synthetic route | 0.0155 | 0.0387 | 1,128 |
  | Synthetic sprint | 0.0139 | 0.0340 | 1,151 |
  | Turf route | 0.0137 | 0.0354 | 3,613 |
  | Turf sprint | 0.0264 | 0.0722 | 158 (small-sample caution) |

  Most surface×zone combos cluster tightly (~0.013), confirming the auto-scaling intuition. Synthetic route is meaningfully looser (0.0155); turf sprint is markedly looser still (0.0264) but with a small-n caveat. Both `run_simulation.py` and `simulate_race_day.py` updated to pass `surface=` to `predict_pace`. Default fallback uses dirt (largest, most stable sample).
- **Pace second-clause is unreachable** (RDS M2): ✅ FIXED 2026-05-28 (dead-code branch removed in same edit).
- **MIN_EDGE_CONVICTION = 0** (RDS L1): ✅ FIXED 2026-05-28 (clarified, kept at 0; empirically validated). Renamed to `MIN_EDGE_CONVICTION_MARGIN` with explanatory comment. The constant is NOT a no-op — `worst_case > 0` is "band clear of zero," a defensible floor.

  **Empirical validation (2026-05-28, 10 random days, 440 rated horses):**
  - 8.9% of rated horses pass `worst > 0` → ~4 conviction picks/day (mean), max 9
  - Threshold sensitivity is gradual, not cliff-like: `>0`→8.9%, `>2`→6.6%, `>5`→4.5%
  - Edge distribution is heavily skewed negative (mean -7.6, median -5.3) — the conviction set is the right tail
  - MIN_ODDS_WIN_BET = 3.0 binds 1 of 39 conviction picks (a 2/1 with worst +0.6 — correctly blocked since edge that thin at 2/1 is well inside takeout). Threshold validated.

### RDS-T2.x — Longshot skew in conviction picks runs against favorite-longshot bias  **[NEW FINDING — 2026-05-28]**

**Surfaced from MIN_EDGE_CONVICTION validation data:** 49% of conviction picks have odds ≥15/1, median 14.8/1. The model's "edge" picks cluster heavily in the longshot tail.

**Why this is concerning:** The empirical favorite-longshot bias (FLB) is well-documented across pari-mutuel data:
- Favorites win MORE than their odds-implied probability (public slightly underbets chalk)
- Longshots win LESS than their odds-implied probability (public overbets longshots)

A model that finds "edge" predominantly in the longshot tail is fighting that bias. Possible interpretations:
1. The model correctly identifies specific mispriced longshots (and the bettor wins despite the population-level bias)
2. The model has its own bias that overrates longshots — and is catching the same illusion the public falls for

The latter is the more likely explanation given what the model can't see: workouts, trip notes, equipment changes, trainer intent, recent context. Longshots at 20/1+ often have those soft signals the public is using to discount the horse — signals the physics/form layer can't access.

**This is a strong candidate for explaining part of the documented -42% ROI.** The model is calling "edge" where the market has soft information advantage.

**Three responses, in priority order:**
1. **(Long-term, principled)** Apply FLB correction at the rating-to-edge translation step. Multiply edge by an odds-dependent shrinkage factor calibrated from historical strike-rate bucketing.
2. **(Interim, defensible)** Tighten the conviction threshold for longshots: require worst-case edge to scale with odds (e.g., `worst > 0` for chalk, `worst > 5` at 7-15/1, `worst > 10` at 15/1+).
3. **(UI nudge, immediate)** ✅ DONE 2026-05-28. `_flb_warning()` helper on `SimDay` emits per-opinion FLB warnings when conviction-pick odds ≥15/1. STRONG_SPECIFIC longshots get a "verify against trip notes / equipment / recent works" prompt; MODERATE_SPECIFIC longshots get a stronger "thin conviction in danger zone" warning recommending horizontal-leg use over standalone bets. Surfaced as `FLB warning:` line in the OPINIONS BY RACE display.

**Status:** UI nudge done. Long-term FLB correction (option 1) remains the substantive ROI-moving work — captured in completion-plan.md Sprint 4.

**Severity:** HIGH (likely ROI-driving). The UI nudge surfaces the issue every run; the empirical correction needs separate focused work.
- **Jockey upgrade only detected for jockeys with ≥50 starts** (RDS L6): ✅ FIXED 2026-05-28. Lowered to ≥20 starts in `blinder.py:jockey_career` CTE. Empirical: 1,567 / 3,779 apprentices (41%) had ≥50 career starts; lowering to 20 captures 1,861 (49%). The 20-start floor balances statistical meaningfulness (below ~20 starts, win rate is dominated by 0-vs-1-win jitter) against apprentice coverage. Documented inline.

### Wagering-Analytics specific

- **Default takeout 0.20** (WA #12): ✅ FIXED 2026-05-28. `populate_stern_fair.py` and `payoff.py:estimate_combo_value` and `horizontal.py:estimate_horizontal_value` all now use bet-type-specific defaults (WPS 0.17, EX/QU/DD 0.21, TRI/SUP/HI5 0.24, P3 0.20, P4 0.18, P5 0.15, P6 0.20). Caller can still override with explicit value. Lookup priority in `populate_stern_fair`: (track, bet_type) → (any, bet_type) → bet-type default → 0.21 fallback. Precision note added in code at all three sites: takeout is used only for informational fair-value displays — realized P&L reads actual paid amounts from `wps` + `exotics` tables, so fallback precision doesn't affect bet outcomes. A ±3 percentage point error in fallback rates moves fair-value estimates ~3-4%, dwarfed by other modeling uncertainties.

- **Future enhancement (deferred):** parse Christopher Larmey's @derby1592 takeout PDF (`Takeout info 5-16-2026.pdf`) to add a 2026 effective-date snapshot. Would unlock: (a) time-versioned takeout records (~1% precision instead of ±3% with the 2009 snapshot alone), (b) **CAW-limited flags** as a per-(track, pool) market-structure signal — when CAWs are excluded from a pool, late-money composition differs, surfacing as a distinct overlay landscape, (c) jackpot/carryover type flags and specialty-wager attribution. The CAW flag is the most novel signal — not in the current data at all. Half-day of work; deferred because takeout precision isn't currently a binding constraint.
- **Coupled entries / dead heats / late scratches not handled** (WA #13)
- **Surface dummies all-zero in EXACTA/TRIFECTA models** (WA #14): `models/payoff_coefficients.json` shows `surface_T = surface_S = 0.0` with `p_value = NaN`. Surface effect silently dropped.
- **Outliers** (WA #15): ✅ FIXED 2026-05-28. `fit_payoff_models.py` now winsorizes `actual_payoff` at the 99.5th percentile before log-transform in both `load_vertical` and `load_horizontal`. New module constant `WINSOR_PCT = 0.995`. The cap-then-log order matters — capping after log compresses the signal-bearing right tail. Logs the count + threshold per bet type for transparency. Fitted models will need re-running to materialize the change (no automatic invalidation).
- **`jock_upgrade` claimed as 6th dimension but never computed** (WA #16): Placeholder zeros.
- **Claim query double-counts horses claimed multiple times** (WA #7): ✅ FIXED 2026-05-28. `compute_trainer_profiles.py:SQL_CLAIM` now adds a `post_claim_per_start` CTE that picks the most-recent prior `claim_date` per starter (`ROW_NUMBER() OVER (PARTITION BY s.id ORDER BY c.claim_date DESC)` — keep `claim_rank=1`). Empirical impact on 2014 TB: removes 3,767 / 33,160 (11.4%) duplicated rows where a horse was claimed twice within 180 days and both windows captured the same later start. Trainer A/E counts now treat each post-claim start as belonging to one and only one claim event — the most recent.
- **Drop/layoff filtered to dirt/fast only** (WA #8): ✅ FIXED 2026-05-28. `compute_trainer_profiles.py:SQL_CLAIM` now also constrains to `surface='Dirt' AND track_condition='Fast'` (both at the inner `claims` CTE that identifies claim events and at the outer join that pulls subsequent starts). DROP/LAYOFF/CLAIM now share the same population baseline. SQL_SWITCH stays unfiltered by design — a switch event spans two surfaces, so a per-surface filter would discard the signal — and the rationale is documented inline above the CTE. Consumers comparing dimension A/Es should treat SWITCH as measured against a broader population. Note: a fully principled fix would compute a per-dimension `BASELINE_AE` rather than reuse the all-trainer 1.000 baseline; that's research work, not a quick fix.
- **Dimensions are not independent** (WA #9): layoff×drop, layoff×switch overlap. Composite scoring misuses them.
- **Velocity range filter inconsistent** (RKM #6): ✅ FIXED 2026-05-28. `form.py:MAX_VELOCITY` lowered 85.0 → 70.0 to match `curves.py:MAX_VELOCITY`. Empirical: 0.016% of `indiv_fractionals` exceed 70 ft/s (1,632 of 10M sampled), max observed 2,671 ft/s (clearly bad data — that's ~1,800 mph). 70 was already the correct value; form.py's 85 was the inconsistency. Note: 85 was used in `curves.py` as a separate sanity ceiling on the FITTED v0 (regression intercept can exceed observed velocity); that 85 ceiling stays put.
- **v0 extrapolated from midpoint velocities** (RKM #7): No near-zero anchor. Conflates start speed with stamina.
- **`slope > 0.001` clamp inconsistency between `curves.py` and `form.py`** (RKM #8): ✅ FIXED 2026-05-28 by clarification. Same threshold in both modules but different actions: `curves.py` rejects (returns None), `form.py` clamps to flat. The asymmetry is intentional — career curves on many races with positive slope strongly indicate bad data, while recent-form fits on few weighted observations are more likely to show spurious positive slope from noise (rejecting would discard usable form coverage). Extracted as `POSITIVE_SLOPE_CLAMP_THRESHOLD = 0.001` in `curves.py`; `form.py` imports it. Both call sites now have docstrings explaining the rationale. No data change; this prevents future "harmonization" from accidentally regressing the trade-off.

---

## Tier 3: Protocol/Code Alignment Issues

A second-pass audit examined `simulation-protocol.md`, `wagering-framework.md`, `itp-principles.md`, `itp-wagering-framework.md`, and `research-plan.md` against the actual code. Findings: **the wagering protocol is mostly aspirational with respect to the code.** The code implements rating computation and a single conviction filter; nearly every wagering rule documented in the protocol is unenforced.

### PROTO-T3.1 — `register_bet()` performs zero validation

**File:** `scripts/run_simulation.py:164-169` (pre-fix)

**Status (2026-05-27):** CONFIRMED and FIXED.

**Problem:** Original `register_bet()` performed NO checks. Original `reveal_and_evaluate()` only handled WIN and EXACTA. Any TRIFECTA/SUPERFECTA/PICK_N registered would silently fall through to "✗ MISS" regardless of outcome. **Material consequence:** prior sim days that registered trifectas (e.g., GP 2014-09-06's documented "TRIFECTA 12-5-10: $292.90 per dollar" play) had hits silently miscounted as losses. The cumulative -42% ROI is therefore a **lower bound** on actual realized performance — true ROI may be less negative.

**Principle (added 2026-05-27 from user feedback):** Bet registration and result evaluation must be deterministic — same registered bet + same race result must always produce the same (hit, payout) result. Structural validation at the registration boundary; pure-function evaluation downstream.

**Fix applied:**
- New `_validate_bet()` enforces:
  - bet_type whitelist (rejects ITP-forbidden PLACE/SHOW; rejects unknown types)
  - race exists in card; programs exist in race's field
  - structural validation per bet type (vertical: N position lists; horizontal: N leg lists)
  - WIN bets meet `MIN_ODDS_WIN_BET = 3.0`
  - amount > 0
- `register_bet(..., force=False)` calls validation; raises ValueError on invalid input. `force=True` available for testing/override.
- New `_build_race_data()` pre-computes deterministic per-race finish + payoff dict (loads ALL exotic types: EXACTA, TRIFECTA, SUPERFECTA, QUINELLA, DAILY_DOUBLE, PICK_3/4/5/6).
- New `_evaluate_bet()` pure function: bet + race_data → (hit, payout). Handles all supported bet types including horizontals (walks leg-by-leg). Uses `official_position` (DQ-honoring).
- Top-4 finish display (was top-3) so superfecta combos are inspectable.

**Verified:** Tested validation rejects all 5 invalid input cases (program not in race, sub-3.0 WIN, PLACE bet, malformed TRIFECTA structure, etc.). Tested evaluator correctly grades a TRIFECTA bet that previously would have silently MISSED. WIN payout math `(odds + 1) × stake` confirmed at $44 for #7 at 3.4/1 with $10 stake.

**Severity reclassified:** HIGH (was material ROI miscount + structural fragility) → FIXED.

### PROTO-T3.2 — Equity test computed but never enforced as a gate

**Files:** `src/sim/horizontal.py:39-101`, `src/sim/payoff.py:168-209`, `scripts/run_simulation.py`

**Status (2026-05-28):** CONFIRMED, FIXED for horizontal/vertical legs as soft gate.

**Problem:** `evaluate_leg_selections()` and `estimate_combo_value()` compute equity ratios. But none of those values are consulted by `register_bet()`. `flashing_stop_sign` was set but never read. The single most-emphasized protocol rule ("every combination must pass the equity test before inclusion") was advisory only.

**Fix applied:**
- New `_equity_warnings(race, bet_type, programs)` method on `SimDay` checks each selected horse's equity ratio per leg against the spec's threshold.
- `register_bet(..., force=False)` calls it after structural validation; prints `[equity warning]` lines for any horse that loses equity per Step E.4. Bet is still registered (soft gate); use `force=True` to silence warnings.
- Logic applies to all multi-position bets (TRIFECTA/SUPERFECTA/HI_5/EXACTA underneath positions, PICK_3/4/5/6 leg positions).

**Verified:**
- TRIFECTA 4-deep over a 2.3 chalk: warnings fire correctly (ratio 0.82 < 1.0).
- TRIFECTA 2-deep over a 0.4 chalk: warnings fire (ratio 0.70).
- TRIFECTA 3-deep over 2.3 chalk: no warning (ratio 1.10 — barely gains).
- 1-deep selections never warn.
- `force=True` suppresses warnings entirely.

**Severity reclassified:** HIGH → FIXED (soft gate). Per-combo vertical equity (full overlay × prob × cost) deferred — see notes in PROTO-T3.3.

### PROTO-T3.3 — Horizontal equity formula

**File:** `src/sim/horizontal.py:39-58` (pre-fix)

**Status (2026-05-28):** Audit re-evaluated; AUDIT CLAIM PARTIALLY DISPROVEN — formula was numerically correct, docstring was misleading.

**Problem (original framing):** Audit said `estimate_leg_equity()` used a "cheap shortcut" that disagrees with the protocol's spec formula. Worked example was: "two formulas can disagree — a horse 'loses in one but gains in the other'."

**Re-analysis (2026-05-28):** Worked the spec example through both formulas:
- Spec: `(odds + 1) × surviving_combos / total_combos`. For a 3×2×2 Pick 3 with leg 1 width 3, this is `(odds + 1) × 4 / 12`.
- Code: `(odds + 1) / n_horses_used = (odds + 1) / 3`.
- These collapse to the same number whenever `n_horses_used == total_combos / surviving_combos`, which is true for any single leg of a horizontal ticket and any single position of a vertical exotic.

For the spec's 3/1 horse in 3×2×2: spec = 1.33, code = 1.33. For the spec's 6/5 horse: spec = 0.73, code = 0.73. **They never disagree** for the per-leg/per-position case the function handles.

**What was actually wrong:**
- Docstring described a "per-horse stake = ticket_cost / N" model that isn't the spec's framing — confusing for readers.
- `ticket_cost_per_combo` parameter accepted but unused.
- Math was right; the explanation wasn't.

**Fix applied:**
- Rewrote docstring to explicitly show the spec equivalence and worked example.
- Kept the implementation (math is correct).
- `ticket_cost_per_combo` retained as default-1 for API compatibility; documented as unused (dimensionless ratio).

**Per-combo vertical equity** (the `combo_equity = projected_payoff × prob / cost_per_combo` form from spec line 253) is approximately equal to overlay_ratio for $1-unit combos. A more rigorous per-combo gate inside register_bet would require enumerating cartesian products and looking up `harville_prob` + projected payoff for each — infrastructure exists in `payoff.py:estimate_combo_value` but orchestration deferred. Soft per-leg equity gate (PROTO-T3.2) is the practical version for now.

**Severity reclassified:** HIGH → LOW (formula was right; docstring fixed). Future: add per-combo vertical equity computation, gate ticket-level "average equity" if useful.

### PROTO-T3.4 — Press mechanic is doc-only, no code support

**Status (2026-05-28):** FIXED via decomposition pattern + detection note (light approach).

**Decision rationale:** Pressing is "same total stake, weighted toward high-conviction combos." Mathematically a press ticket equals N flat tickets with overlapping `programs` and different `amount` — no new datatype needed for evaluation. The unity is conceptual (bettor's view of "one strategic plan"), not structural. Adding a `Basket` class would touch the Bet dataclass, register_bet validation, evaluation breakdown, and display formatting for a rarely-used construct. YAGNI applies until press is empirically common in real sim runs.

**Fix applied:**
- `Bet` docstring updated with the press decomposition pattern as a worked example (the protocol's $24 trifecta with 4 strong combos at $3 + 12 spread combos at $1 → register as wide ticket + narrow press, with totals reconstructed at evaluation).
- New `_press_notes()` helper detects press patterns at registration: when a new bet's programs is a subset (per-position) of a prior bet on the same (race, bet_type), surfaces a `[press note]` showing both tickets' combo counts, per-combo costs, total stake, and the effective per-combo cost on the narrow combos (sum of both layers).

**Verified directly:**
- Wide spread `TRIFECTA 7/1,2,3,4/1,2,3,4 ($16)` followed by narrow press `TRIFECTA 7/1,2/1,2 ($8)` → press note fires reporting "narrow 4 combos at $2/combo + wide 16 combos at $1/combo; total stake $24. Effective cost on the narrow combos: $3/combo."
- False-positive checks pass: different bet_type, different race, or non-subset programs all produce no note.

**Basket tagging (added 2026-05-28):** Per Cat-B decision, opted for "tag, don't wrap" instead of a full `Basket` class. `Bet` gained an optional `basket_id: str | None = None` field; `register_bet` accepts a matching kwarg. When ≥2 bets share a `basket_id`, `_basket_exposure_notes` fires reporting cumulative stake, bet-type mix, and race coverage so the bettor sees the over-investment trap before adding more. `reveal_and_evaluate` prints per-basket P&L rollups when any bets are tagged. Untagged bets behave exactly as before. ~50 LOC; no breaking changes. A full `Basket` class is no longer planned — tagging covers both the press-vs-flat retrospective question and the T3.9 aggregate-exposure detection.

**Severity:** HIGH → FIXED (informational detection; press structure preserved via decomposition).

### PROTO-T3.5 — "Never exclude favorite from 2nd/3rd unless total collapse" — unenforced

**Status (2026-05-28):** FIXED as informational NOTE (not warning).

**Reframe:** Excluding the favorite from 2nd/3rd is a legitimate ITP play (kill-shot, vulnerable-favorite-misses-board, structural underlay). The bettor may explicitly want this. Original audit framing as a "warning" was too strong.

**Fix applied:** `_favorite_exclusion_notes()` emits `[fav-excl note]` informationally when the favorite is excluded from both 2nd and 3rd in a TRIFECTA/SUPERFECTA. The bet registers regardless; the note surfaces awareness, not rejection.

**Verified:** TRIFECTA in R2 excluding #3 (favorite at 2.3 odds) emits a note showing the math: "favorite #3 (at 2.3, ~30% to win, ~79% to hit board) excluded from 2nd and 3rd — ticket forfeits ~79% of outcomes; verify pace/form supports the exclusion." Counter-case emits nothing.

**Severity reclassified:** HIGH → FIXED (informational with concrete math).

### PROTO-T3.6 — Decision tree (E.1 opinion classification) not implemented

**Status (2026-05-28):** FIXED.

**Fix applied:** New `classify_opinion(rn)` method on `SimDay` returns one of six classes per Step E.1, with concrete rationale and recommended bet expression per Step E.2:

| Class | Trigger | Recommended |
|---|---|---|
| `STRONG_SPECIFIC` | candidate worst-case > 5 | WIN (or exotic key if odds < 3.0) |
| `STRUCTURAL` | `CONTESTED_HIGH_DECAY` AND ≥2 closers with positive edge | TRIFECTA closers on top, speed under |
| `STRONG_NEGATIVE` | fav edge < -10 AND band clear of zero | TRIFECTA/SUPERFECTA excluding fav on top |
| `MODERATE_SPECIFIC` | candidate worst-case ∈ (0, 5] | horizontal leg (single/A-B) |
| `SPREAD` | 3+ horses within 6 edge points of top | horizontal leg with A/B/C |
| `NO_OPINION` | none of above | PASS |

The card-overview display now shows the opinion class per race; a separate "OPINIONS BY RACE" section gives full rationale + recommendation for any race with an opinion.

**Verified on GP 2014-09-06:**
- R2/R3 MODERATE_SPECIFIC (Tricky Call worst +2.9, J.M.'s Parade worst +0.5 → horizontal leg)
- R4/R6 SPREAD with hints (actual favorite is unrated; rated-lowest-odds horse has strongly negative edge — surfaced as `Hint:` so bettor sees partial information)
- R9 NO_OPINION (2/12 rated coverage too thin)
- Stakes races correctly NO_OPINION on coverage

Notes interact cleanly with downstream soft checks: a STRONG_NEGATIVE recommendation that excludes the favorite triggers the fav-excl note (which quantifies the structural cost) — the two views reinforce each other.

**Equity table + basket structures (added 2026-05-28):** For STRONG_SPECIFIC / STRONG_NEGATIVE / STRUCTURAL classes, `propose_ticket_structures()` returns:

1. **Per-horse equity table** showing each rated horse's odds, edge, role (FAV / CLOSER / SPEED), and equity ratio at depths N=2,3,4,5. Bettor reads off who survives at what spread depth without doing math in their head.
2. **Class-specific basket suggestions** (primary + defensive), e.g.:
   - STRONG_SPECIFIC: primary `KEY/AB/AB`, defensive `AB/KEY/AB`
   - STRONG_NEGATIVE: primary `non-fav/non-fav/non-fav`, defensive `non-fav/non-fav/fav` (allows fav in 3rd if partial fade)
   - STRUCTURAL: primary `closers/closers/speed-or-mid`, defensive `closers/closers/closers` (full pace-collapse play)

**Bug fixed in this work:** Earlier STRONG_NEGATIVE used `rated.odds.idxmin()` to identify the favorite — that's the model's lowest-odds rated horse, not the public favorite. When the actual favorite is unrated (~30% of stakes-style races), the classifier was lying about which horse is the chalk. Fixed: STRONG_NEGATIVE only fires when the actual public favorite is rated AND has strongly-negative edge. Otherwise a `Hint:` surfaces the partial information honestly.

**Severity reclassified:** HIGH → FIXED.

### PROTO-T3.7 — Two scaffolds, fragmented capabilities

**Status (2026-05-28):** FIXED via consolidation (option A — delete + port).

**Decision rationale:** `simulate_race_day.py` had two unique features (per-horse Benter combined probabilities; top-N value combos by overlay), the rest was duplication. Two scaffolds were taxing every fix at 2× (yesterday's pace fix had to land in both files; edge naming had to land in both). Maintaining the split going forward would have required ongoing diligence to keep them aligned.

**Fix applied:**
- Ported the two unique features into `SimDay` as new methods on `run_simulation.py`:
  - `combined_probs(rn)` — returns model + odds + Benter-combined win probabilities per starter
  - `top_value_combos(rn, top_n=10, bet_type='TRIFECTA')` — enumerates EXACTA or TRIFECTA combos with highest projected overlay vs Stern/Harville fair (search bounded to top 6 by Benter prob for first two positions; uses the new bet-type default takeouts via `estimate_combo_value`).
- Deleted `scripts/simulate_race_day.py`.
- Deleted `src/sim/evaluate.py` (was deprecated yesterday; only consumer was `simulate_race_day.py`).

**Verified by code review.** Functional smoke-test deferred to next sim run when DB tunnel is back up.

**Severity:** MEDIUM → FIXED.

### PROTO-T3.8 — Flat Kelly sizing ignores Fav-Edge tier modifiers

**File:** `src/sim/kelly.py:56-97`

**Status (2026-05-28):** FIXED.

**Fix applied:**
- New `RaceContext` dataclass holding `fav_edge`, `wcmi`, `band_crosses_zero`, `n_independent_edges`, `carryover_active`, `pool_density_per_combo`. All optional.
- New `context_multiplier(ctx)` composes the six modifiers per spec (simulation-protocol.md:345-350 Fav-Edge tiers; wagering-framework.md:241-248 WCMI/band/biases/carryover/pool-density).
- `size_bets(..., context=...)` accepts the context, applies the modifier to base Kelly per horse and to the exotic budget, then caps via win_cap / max_exposure / exotic_cap.
- Stack of modifiers clamped to [0.05×, 4.0×] so composed cases (e.g., overbet fav + carryover + low WCMI) can't blow past sane bounds.

**Verified:**
- Fav-edge tiers: +8 → 0.25× (small play), -3 to +5 → 1.0×, -5 → 1.5×, -15 → 2.0×
- WCMI: 0.05 → 1.5×, 0.30 → 0.5×
- Band crosses zero: 0.25×
- 2 confirming biases: 1.25² = 1.5625
- Carryover: 2.0×
- Thin pool (<$5/combo): 0.75×
- Composed extreme (fav=-15 + carryover + 2 biases + low WCMI = math 9.375×) clamps to 4.0×

**Note:** `RaceContext.band_crosses_zero` is interpreted at the race level — caller decides whether the basket as a whole is speculative. Per-horse band logic remains in `format_race_ratings`. `size_bets` returns `context_mult` so callers can render it transparently.

**Severity reclassified:** MEDIUM → FIXED.

### PROTO-T3.9 — ITP concepts referenced as rules but not coded

**Status (2026-05-28):** FIXED — kill-shot, hurdle, basket tagging, and win-only all empirically validated and encoded as soft notes.

**Empirical validation of kill-shot (TB 2010-2017, 56K exactas with top-2-choice 1-2 finishes):**

| Pair | Direction | n | Mean overlay (vs Harville fair) |
|---|---|---|---|
| 1,2 | favorite-on-top | 32,891 | **1.121** |
| 1,2 | upset (2nd-choice tops 1st-choice) | 22,942 | **1.225** (+9pp) |
| 2,3 | lower-on-top | 12,042 | 0.959 |
| 2,3 | upset (3rd-choice tops 2nd-choice) | 10,011 | 1.039 (+8pp) |
| 3,4 | lower-on-top | 6,212 | 0.838 |
| 3,4 | upset (4th-choice tops 3rd-choice) | 5,467 | 0.919 (+8pp) |

**Findings:**
1. Kill-shot premise is empirically right: upset-direction exactas pay ~8-9pp more overlay than chalk-on-top across all three choice pairs. The differential is structural, not noise.
2. Pair (1,2) chalk-on-top still has positive overlay (1.121× fair) — both directions are profitable, but upset is meaningfully better.
3. Pairs (2,3) and (3,4) chalk-on-top have NEGATIVE overlay (<1.0) — losing money even at theoretical fair value. The kill-shot direction is barely profitable for (2,3), still loses for (3,4).

**Fix applied:** New `_kill_shot_notes()` method on `SimDay`. Fires when an EXACTA is registered with the actual public favorite in position 1 (top) and any longer-priced horse in position 2 (under). Note explains the empirical numbers and suggests flipping the direction unless the bettor has specific information the favorite WILL win.

**Verified:** Tested on R2 of GP 2014-09-06 (fav #3 at 2.3). Kill-shot pattern (`EXACTA 3 / 7,1,2`) triggers the note with all three empirical numbers; correct-direction pattern (`EXACTA 7 / 3,1`) does not.

**Hurdle (added 2026-05-28, refined for description-only and equity-based thresholds):** Implemented as `_hurdle_notes()` for horizontal bets — purely descriptive. Each leg gets a strategy mode label and reports the favorite's market-vs-model mispricing magnitude. No fit assessment, no prescriptions, no rule-language.

  Mode thresholds use the **fraction of public equity** (overround-normalized odds-implied probability) covered by the leg's selections, not raw counts. This auto-adjusts to field size and field shape — a 4-horse selection in a 12-horse race with a 4/5 chalk captures only ~37% of equity (SPREAD-EQUITY narrow), while the same count in a 6-horse race with no clear chalk might capture 53% (SPREAD-EQUITY-WIDE).

  | Mode | Trigger |
  |---|---|
  | SINGLE | k=1 |
  | SURVIVE | public_equity ≥ 0.90 |
  | WIDE-WITH-FAV | public_equity ≥ 0.75 AND includes favorite |
  | SPREAD-EQUITY-WIDE | public_equity ≥ 0.50 AND excludes favorite |
  | NORMAL | includes favorite, below WIDE-WITH-FAV threshold |
  | SPREAD-EQUITY | excludes favorite, below SPREAD-EQUITY-WIDE threshold |

  Per-leg display surfaces both equity numbers (public_eq / model_eq pair when available) and the favorite's market-vs-model mispricing in pp when meaningful (≥1.5pp). Ticket-level mix summary shows mode distribution. The bettor sees the structural shape and the underlying market signal; the bettor decides.

  Falls back to count-based mode classification when odds data is missing for the race.

**Win-only (validated and encoded 2026-05-28):** Multi-dimensional empirical validation completed against handycapper TB 2010-2017 (n>1.6M starter-races in fields ≥5). Findings:

- "Speed-fade" type = top quintile of field by BOTH `adj_v0` AND `adj_decay`.
- The ITP win-only finishing pattern (under_to_win ratio < 1.0) holds **only in sprint races** (furlongs ≤ 6.5), and across all surfaces:

| surface | zone | n | under_to_win |
|---|---|---|---|
| Dirt sprint | 117K | **0.901** |
| Synthetic sprint | 12K | **0.867** |
| Turf sprint | 5K | **0.748** |
| Dirt route | 65K | 0.999 (NOT win-only) |
| Synthetic route | 10K | 1.022 (NOT win-only) |
| Turf route | 35K | 1.057 (NOT win-only) |

- Pace scenario (CONTESTED/PRESSURED/CONTROLLED) does NOT discriminate further within sprints — the asymmetry is purely sprint-vs-route. The audit's deferral guess that pace_scenario × distance × surface × age would be the right axis was partly wrong: pace and age don't add signal once you've conditioned on sprint vs route.
- Field size doesn't discriminate either (5-7, 8-10, 11+ all show the same pattern).

**Fix applied:** New `_win_only_notes()` method on `SimDay`. Fires for EXACTA/TRIFECTA when an under-leg position contains a speed-fade horse (top 20% of field's adj_v0 AND adj_decay) AND the race is a sprint. Suppressed in route races by design. Note reports the empirical numbers and suggests keying the horse on top or excluding from under leg.

**Verified on ARP 2016-07-24:**
- R1 sprint (6f): EXACTA #6/#2 fires (speed-fade #2 under) ✓
- R1 sprint: EXACTA #2/#6 does not fire (speed-fade on top) ✓
- R5 sprint: TRIFECTA #X/#Y/#1 fires (speed-fade in 3rd slot) ✓
- R7 route (8.5f): no fire even with same horse types ✓
- WIN bets: no fire (irrelevant bet type) ✓

**Basket (added 2026-05-28; tagging implemented):** Different concept from press (T3.4). Basket = one strategic opinion expressed across multiple pool types (WIN + EXACTA + TRIFECTA all keyed on same horse). Press = same total stake weighted across combos within one bet. Implemented via the optional `basket_id` tag on `Bet` / `register_bet` (see T3.4 for the cross-cutting design call). The aggregate-exposure note fires once a second bet joins the basket and reports cumulative stake + bet-type mix; `reveal_and_evaluate` prints per-basket P&L rollups. Catches the over-investment trap of registering 5 small bets on a +3-edge conviction.

**Severity:** MEDIUM → FIXED. All four ITP concepts now encoded as soft notes: kill-shot, hurdle, basket tagging, and win-only (Cat-B 2026-05-28).

### PROTO-T3.10 — FTS rule contradiction across docs

**Status (2026-05-28):** FIXED.

**Fix applied:** Updated `itp-principles.md:123-141` with a clear "this rule is superseded" note pointing to `wagering-framework.md`. The historical ITP guidance is preserved below the note for source-material accuracy. The note also clarifies that `itp-principles.md` is a historical reference, not the operational protocol — `wagering-framework.md` wins where they conflict.

**Severity reclassified:** MEDIUM → FIXED (doc-only change).

### PROTO-T3.11 — Place betting forbidden by ITP, not blocked by code

**Status (2026-05-28):** FIXED via PROTO-T3.1.

**Fix applied:** `_FORBIDDEN_BET_TYPES = {"PLACE", "SHOW"}` constant rejects these at `_validate_bet`. Verified: `register_bet(2, 'PLACE', ['7'], 10, ...)` raises `ValueError: PLACE is forbidden by ITP framework — use WIN or exotics`.

**Severity reclassified:** MEDIUM → FIXED.

### PROTO-T3.12 — Pool minimum thresholds never checked

**Status (2026-05-28):** FIXED with stake-relative check (replaced absolute thresholds).

**Re-evaluation:** The original `wagering-framework.md:105` line ("$20K+ for trifectas, $50K+ for Pick 3/4") was a single uncited claim. Coverage check on actual data showed those numbers reject most candidate races: only 7.7% of Pick 3 races have ≥$50K pools (median $6.5K), only 23% of Pick 4 ≥$75K. The thresholds were calibrated to big-track conditions, not the broad market.

**Reframe:** The real concern isn't "pool too small" — it's "stake too large relative to pool" (your bet competing with itself for the pool). That's stake-relative, not absolute.

**Fix applied:**
- `_MAX_STAKE_PCT_OF_POOL` per bet type (0.5% for WPS/EX/QU/P5/P6; 1.0% for tri/super/DD/P3/P4).
- `_DEAD_POOL_FLOOR = $1,000` absolute minimum (anything less is essentially a dead pool, payoffs unreliable regardless of stake).
- `_pool_notes(race, bet_type, amount)` emits `[pool note]` if either condition fails. Informational, not gating.

**Verified:** $10 stake into $19K Pick 3 pool (0.05%) → no note. $300 stake into same pool (1.55%) → note explains the concrete impact: "your own stake compresses the published per-$1 payoff by roughly 1.5%."

**Severity reclassified:** MEDIUM → FIXED (informational, stake-aware).

### PROTO-T3.13 — Horizontal qualification (2+ conviction legs) unenforced

**Status (2026-05-28):** FIXED as informational NOTE (not gate).

**Reframe:** A rigid "≥2 conviction legs required" rule is wrong. A single high-equity leg can carry an entire horizontal — e.g., a 1-in-3 chance of a 50/1 winner in one leg pays a parlay multiplier large enough to justify spread plays in the other legs. **Conviction count isn't the right gate; conviction value is.**

**Fix applied:** `_horizontal_conviction_notes()` emits an informational `[horiz-conv note]` describing leg coverage. Two cases:
- 0/N legs have conviction → "pure speculation?" prompt
- 1..N-1 legs have conviction → simple "X/N legs have a conviction candidate" report

Full coverage (all legs have a candidate) emits nothing — that's normal. The bettor judges whether the structure makes sense.

**Verified:** P3 in R3 (1/3 conviction) → note lists which legs lack conviction (R4, R5), combo count (8), and prompt to verify the conviction leg's equity carries the spread. P3 in R4 (0/3) → note explicitly states "ticket is pure spread play, equity rests entirely on closing-odds value." P3 in R1 (2/3) → identifies R1 as the non-conviction leg.

**Severity reclassified:** MEDIUM → FIXED (informational with leg-level detail).

### Dead code findings

- ~~`kelly_exotic` (kelly.py:26-53) — not called from any script~~ ✅ DELETED 2026-05-28 (Tier 2 quick win — never wired up, formula was off by `b/(b+1)`, `size_bets` is the canonical exotic sizer).
- `evaluate_race` (evaluate.py:6) — imported in simulate_race_day.py but never called
- `MIN_EDGE_CONVICTION = 0` — misleadingly named (the check is `worst_case > 0`; constant is a no-op)

### Phase D: Protocol/Code Alignment Fixes

After verifying Tier 1/2 findings, address:

1. **Make `register_bet` validate** (programs in race, structural validity, bet type whitelist, pool minimums, win-bet odds floor) — HIGH priority because invalid bets silently mis-grade
2. **Extend evaluator** to handle TRIFECTA, SUPERFECTA, PICK_N, DAILY_DOUBLE, QUINELLA — currently anything past EXACTA silently MISSES
3. **Decide on ITP rules:** either delete from docs (acknowledge they're judgment) or implement enforcement
4. **Fix horizontal equity formula** to use full ticket geometry, then make it a registration gate
5. **Resolve FTS contradiction** between itp-principles.md and wagering-framework.md
6. **Implement opinion classification** in `protocol_check` (six types from Step E.1)
7. **Decide whether press/basket structure** is worth coding, or document as judgment-only

---

## Tier 4: Shared Docs (`/github/docs/`)

The shared specs directory contains stale copies of specs now living in repo-specific dirs, plus cross-cutting documents (`GUIDE.md`, `intents.md`, `handycapper-schema.md`).

### DOC-T4.1 — `handycapper-schema.md` "Unmapped Columns" section is fully stale

**File:** `/github/docs/specs/handycapper-schema.md:56,59,73,260-267`

Spec claims `age_code`, `female_only`, `off_turf` are NOT mapped. They are mapped in `pdf-importer/src/main/java/.../RaceWriter.java:219, 222, 233`.

**Verification:** Code confirms. No DB needed.

**Fix:** Update the spec's "Unmapped Columns" section to remove these three fields. Note that historical data may still need backfill (which we did manually).

**Severity:** HIGH

### DOC-T4.2 — `chart-parser.md` vs `handycapper-schema.md` disagree on `favorite` provenance

**Files:** `chart-parser.md:52` (PDF asterisk) vs `handycapper-schema.md:139` (computed from `choice = 1`)

Actual code (`Starter.java:110`): extracted from PDF asterisk. Schema doc is wrong.

**Fix:** Update `handycapper-schema.md:139` to reflect that `favorite` is parsed directly, not computed from `choice`.

**Severity:** HIGH

### DOC-T4.3 — `intents.md` AN1 status row contradicts itself

**File:** `/github/docs/intents.md`

AN1 row marked `done` but Notes column says "Pending: stern_fair population, Phase 5 jitter, Phase 6 model fit." These are now actually done in wagering-analytics, so the Notes is the stale part.

**Fix:** Update AN1 row Notes to reflect actual completion. AN2 row similarly references archived `race-day-simulation.md` spec but the actual AN2 is `market-bias-analysis.md` (a different concept).

**Severity:** HIGH (but limited blast radius — internal planning doc)

### DOC-T4.4 — Schema spec attributes V002/V003 migrations to wrong repo

**File:** `handycapper-schema.md:11`

Lists `race_probabilities`, `race_metrics`, `exotic_race_legs` as "wagering-analytics (migrations)". Actual migrations live at `redboarders/db/migrations/V002__analysis_schema.sql` and `V003__race_analysis_views.sql`. wagering-analytics has no migrations directory.

**Fix:** Update ownership attribution. Or move the migrations to wagering-analytics if that's the intended owner.

**Severity:** MEDIUM

### DOC-T4.5 — Schema spec missing `race_wcmi` and `trainer_ae_profiles` tables

**File:** `handycapper-schema.md`

These tables exist (created by AN2 scripts) and `race-day-sim/CLAUDE.md` declares dependencies on them, but the schema spec doesn't list them.

**Fix:** Add table descriptions for both.

**Severity:** MEDIUM

### DOC-T4.6 — `predict_payoff()` surface format mismatch

**File:** `exotic-payoff-analysis.md:419`

Spec says `surface: str # 'D', 'T', 'S'`. Actual DB values: `'Dirt', 'Turf', 'Synthetic'`. AN1 `predict_payoff` would silently fail or all-default if passed `'D'`.

**Fix:** Update spec. If the implementation accepts long form, update spec; if it accepts short form, update implementation OR add normalization at the API boundary.

**Severity:** MEDIUM

### DOC-T4.7 — Three different AN1 row count claims

`intents.md`: 2.16M rows. `exotic-payoff-analysis.md`: 1.07M trifecta rows. `wagering-analytics/CLAUDE.md`: 2.9M rows.

**Verification:** Query `SELECT COUNT(*) FROM exotic_harville_ratios`. Update all three to match.

**Severity:** MEDIUM

### DOC-T4.8 — `rkm-v3.md` phase numbers don't align with implementation

Spec describes 5 phases (curve fitting, hierarchical pooling, track adjustment, pace interaction, market combination). Implementation has 6 phases (compute_curves, compute_adjustments, compute_race_performance, compute_market, compute_form, compute_situations). Phase numbers don't line up — spec Phase 5 = Benter, impl Phase 5 = current form.

**Fix:** Add a phase-alignment table to rkm-v3.md showing spec phase → impl phase mapping. Or rewrite the spec to match implementation.

**Severity:** MEDIUM

### DOC-T4.9 — Promised but unbuilt features

Several specs promise features that don't exist:
- Hi-5 and Quinella vertical models in `exotic-payoff-analysis.md`
- Segmented `stern_calibration` table in `exotic-payoff-analysis.md`
- "Composite Edge Score" in `market-bias-analysis.md` Phase 4
- "FDS forecast" / Field Dispersion Score in `rkm-v3.md`
- Hierarchical pooling in `rkm-v3.md` Phase 2

**Fix:** Either build them or mark them as "deferred" in the specs. Don't leave specs in an aspirational state.

**Severity:** LOW (specs are aspirational by nature, but inconsistent with reality)

### DOC-T4.10 — Archived `race-day-simulation.md` has multiple SQL bugs

**File:** `archive/race-day-simulation.md`

If anyone resurrects this archived spec, they hit: `s.name` (wrong, it's `s.horse`), undefined param names (`p_track` vs `p_target_track`), references `model_prob` from wrong table, hardcodes `r.breed = 'Thoroughbred'` (actual value is `'TB'`), uses `wagering_position = 1` (NULL for ~7.5% of races). Multiple bugs.

**Fix:** Mark the file with a prominent "ARCHIVED — DO NOT USE" header. Or delete it entirely.

**Severity:** LOW (archived, low resurrection probability)

### DOC-T4.11 — `rkm_market_analysis`, `rkm_current_form`, `rkm_race_situations` schema specs underspecified

The schema spec lists 3 of ~13 columns for `rkm_race_situations`, misses `career_v0/career_decay/n_recent_races/race_id/horse_key` for `rkm_current_form`, and misses `combined_prob` for `rkm_market_analysis`.

**Verification:** `\d+ rkm_*` in psql to get actual column lists.

**Fix:** Update schema spec to reflect actual columns.

**Severity:** MEDIUM

### DOC-T4.12 — `GUIDE.md` repo inventory misses active repos

Doesn't list `rkm`, `wagering-analytics`, `race-day-sim` — the three repos doing the current AN1/AN2/AN3 work.

**Fix:** Update GUIDE.md.

**Severity:** LOW

---

## Tier 5: pdf-importer

pdf-importer drives `chart-parser` (a private library) and writes to the `handycapper` schema. Three HIGH-severity findings affect downstream data quality.

### IMP-T5.1 — `upsertRace` only updates 6 of 50+ columns on conflict [FIXED 2026-05-28]

**File:** `pdf-importer/src/main/java/.../pipeline/RaceWriter.java:154-166`

The `doUpdate()` clause sets only `track_name, final_time, final_millis, dead_heat, number_of_runners, footnotes`. Every other race-level column silently retains the original value.

**Why this matters:** This is the root cause of the `off_turf`/`female_only`/`age_code` hole. Those fields ARE coded into RaceWriter (lines 219, 222, 233) but never made it into rows imported BEFORE that code was added — because re-runs don't update the missing columns.

**Verification:** Query rows with `imported_at` predating the RaceWriter change vs after. Compare `off_turf` populated rate. Pre-change rows should be NULL/false; post-change should match the spec's expected distribution.

**Fix:** Either (a) expand `doUpdate()` to set every column with `EXCLUDED.col` for each field, or (b) change strategy to delete-and-reinsert per race_id (matching how starters are handled). Option (b) is simpler and matches existing pattern.

**Fix applied 2026-05-28 (option b):** `writeRace` now does `deleteRace(...)` then a fresh insert. The race-level FK CASCADE clears all child tables (starters and grandchildren, scratches, fractionals, splits, exotics, ratings) so the explicit per-table delete block is no longer needed and was removed.

Discussion that shaped the choice:
- The `ImportTracker` SQLite progress log already prevents incidental re-imports — `PdfScanner` filters out anything marked SUCCESS/UNIMPORTABLE before parsing. So a re-import is always intentional (fresh tracker DB or explicit re-run after a chart-parser bug fix), which means full replace is the right semantic.
- The downstream `rkm_*` FKs use `ON DELETE NO ACTION`. If RKM analytics have already been computed for this race, the delete will FK-block loudly. That's the correct signal: silently overwriting values RKM has aggregated against would leave derived analytics stale; the FK error forces an explicit recomputation decision.
- An earlier proposal to keep `doUpdate` and just expand to all non-key columns was rejected because it would silently break RKM consistency in exactly the case the FK constraint exists to protect against.

New integration test `write_ReimportPropagatesChangedNonKeyColumn` exercises 6 different non-key columns (`surface`, `conditions`, `track_record_holder`, `post_time`, `weather`, `age_code`) — all of which would have stayed stale under the old 6-column doUpdate. 13/13 pdf-importer tests pass.

**Severity:** HIGH → FIXED.

### IMP-T5.2 — `cancelled` and `races` rows can co-exist for the same (date, track, number) [FIXED 2026-05-28]

**File:** `RaceWriter.java:126-152, 292-316`

`writeRace` returns early if cancelled, writing only to `cancelled`. Never deletes from `races` (or vice versa). The two tables have separate unique constraints. If a race flips classification between runs, both tables hold rows.

**Verification:**
```sql
SELECT r.track, r.date, r.number FROM handycapper.races r
JOIN handycapper.cancelled c ON c.track = r.track AND c.date = r.date AND c.number = r.number;
```
Any rows returned = at least one race exists in both tables.

**Fix:** In `writeRace`, when classification flips: delete from the OTHER table on conflict. Add a sanity-check view that asserts the two tables are disjoint.

**Severity:** HIGH

### IMP-T5.3 — `UNIMPORTABLE` status defined but never set [FIXED 2026-05-28]

**Files:** `model/ImportResult.java:11`, `pipeline/ImportTracker.java:62`, `PdfImporter.java:88-115`

The enum value exists and the tracker treats it as "done", but no code path actually sets it. The 1,738 known unparseable PDFs (per `docs/zero-race-files.md`) were classified manually via direct SQLite UPDATE. On a fresh re-import, every PDF gets re-classified `PARSE_FAILED` and retried indefinitely.

**Verification:** Inspect `ImportTracker` SQLite DB for any rows with status `UNIMPORTABLE`. They came from manual UPDATE statements, not code.

**Fix:** Categorize known unparseable exception classes (encrypted PDFs, malformed structure, non-Equibase format) and have `recordFailure(...UNIMPORTABLE...)` flip them. Also: persist this categorization in source control somehow so other hosts inherit it.

**Fix applied 2026-05-28:** New `UnimportableClassifier` (~150 LOC) inspects the parse-failure exception type and the raw file (size + magic bytes) to recognize the 6 unimportable shapes catalogued in `docs/zero-race-files.md`:

| Shape | Detection signal |
|---|---|
| HtmlStub | file size matches one of the known stub sizes (3253, 8280) OR small file with `<html` / `<!doctype` magic |
| EmptyPdf | file < 600 bytes with `%PDF-` magic |
| OldChartFormat | `MalformedRaceException` in cause chain |
| UnsupportedRaceFormat | `NoRaceDistanceFound` in cause chain |
| UnparsableRunningLines | `MissingHorseJockeyException` in cause chain |
| UnknownRaceType | `RaceTypeNameOrBreedNotIdentifiable` in cause chain |

Class-name matching (rather than direct typed catch) keeps the classifier from needing a hard dependency on every chart-parser exception type. Anything not confidently classified stays `PARSE_FAILED` so the file is retried on the next run — the safe direction is "retry one extra time" rather than "permanently mark a recoverable file unimportable."

`PdfImporter` now also catches the **zero-race success** case — chart-parser silently swallows per-race exceptions and returns an empty list when every race fails. Those PDFs now flip to `UNIMPORTABLE` rather than `SUCCESS` with 0 races written. `ImportTracker.recordFailure` made null-safe so the zero-race path can pass `null` for the cause.

`ImportResult.unimportable(Path, Exception)` factory added for symmetry with `parseFailed` / `writeFailed`. New `UnimportableClassifierTest` exercises 11 cases (all 6 unimportable shapes, transient-IOException-stays-PARSE_FAILED, wrapped-cause-chain, zero-race-success). 24/24 pdf-importer tests pass.

**Severity:** HIGH (operational — wastes CPU on every re-run) → FIXED.

### IMP-T5.4 — `dead_heat` flag only marks WIN dead heats, not 2nd/3rd [FIXED 2026-05-28]

**File:** `chart-parser/.../RaceResult.java:804-815`

`detectDeadHeat()` counts starters with `officialPosition == 1`. A dead heat for second/third is recorded on `starters.position_dead_heat = true` but invisible at the race level. Also has a hard-coded carve-out for the "2016 Parx Oaks debacle" that suppresses dead_heat even when data says co-winners.

**Verification:**
```sql
SELECT race_id, COUNT(*) FROM handycapper.starters WHERE position_dead_heat = true
GROUP BY race_id HAVING COUNT(*) > 0
EXCEPT
SELECT id, 1 FROM handycapper.races WHERE dead_heat = true;
```

**Fix:** Either rename `races.dead_heat` to `races.win_dead_heat` (truthful) or extend the detection to flag any-position dead heats. Document the Parx carve-out as a comment in the code.

**Fix applied 2026-05-28 (clarify, don't change semantics):** The audit's two named alternatives both involve invasive changes — a column rename touches schema, jOOQ codegen, JSON property order, and every consumer; an extended detection silently changes the meaning of an existing column for downstream readers who've been treating `dead_heat=true` as WIN-only. A third path is honest and least-risky: keep the WIN-only semantic by design, document it clearly, and rely on the existing per-position field on `Starter` for the more general case.

Per-position dead heats are already captured on each `Starter.positionDeadHeat`. The race-level flag is a convenience for WIN payoff splits — sub-1 dead heats don't change race-level payoff math, so the field's intent matches its current implementation. Anyone needing "any-position dead heat at race granularity" can `EXISTS (SELECT 1 FROM starters WHERE race_id=r.id AND position_dead_heat=true)`.

Edits:
- `RaceResult.deadHeat` field — JavaDoc explains WIN-only semantic and points readers to `Starter.positionDeadHeat`.
- `RaceResult.detectDeadHeat` — JavaDoc explains the WIN-only detection rationale and the per-position alternative.
- The Parx-carve-out call site (line 510 area) — comment explains *what* the carve-out does (preserves a single official winner for that race) and *why* (the 2016 settlement was a paper anomaly, not a real on-track dead heat).
- `ChartParser.is2016ParxOaksDebacle` — JavaDoc covers the historical context of the settlement and clarifies the carve-out's narrow applicability.

No behavior change. 211/211 chart-parser tests pass.

**Severity:** MEDIUM → FIXED (clarify; behavior unchanged by design).

### IMP-T5.5 — `number_of_runners` counts coupled entries as separate [FIXED 2026-05-28]

**File:** `chart-parser/.../RaceResult.java:217-219`

`getNumberOfRunners() = starters.size()`. A 1/1A coupled entry counts as 2.

**Verification:**
```sql
SELECT r.id, r.number_of_runners, COUNT(*) FILTER (WHERE s.entry IS NOT NULL) as coupled_starters
FROM races r JOIN starters s ON s.race_id = r.id
WHERE s.entry IS NOT NULL
GROUP BY r.id, r.number_of_runners;
```

**Fix:** Add a `number_of_wagering_interests` column = `COUNT(DISTINCT entry_program)`. Don't change `number_of_runners` (downstream depends on it as physical-horse count).

**Fix applied 2026-05-28:** Followed the audit's recommendation literally — added a parallel `number_of_wagering_interests smallint` column rather than changing `number_of_runners` semantics.

- `pdf-importer/db/schema.sql` updated; new Flyway migration `V7__add-races-number-of-wagering-interests.sql` for test deployments. Production backfill SQL is documented in the migration as a comment for the operator.
- `RaceWriter.countWageringInterests(List<Starter>)` collapses coupled (1+1A) and field (1+1X+1Y) entries to one interest by counting distinct `entryProgram` values. Computed in pdf-importer, not in chart-parser, so no chart-parser version bump was required.
- The new column is referenced via `DSL.field("number_of_wagering_interests", Short.class)` rather than a regenerated jOOQ static — avoids the codegen step (which would require ALTER on the live primary DB) until the next planned regen pass.
- Empirical: on TB 2014 (~46K races), 2,560 (5.5%) have at least one coupled entry — that's the fraction of races where the two columns now legitimately disagree.
- New `CountWageringInterestsTest` (7 cases) covers uncoupled fields, classic 1/1A coupling, three-horse field entries, multiple coupled groups in one race, empty/null lists, and null-entryProgram handling. New `RaceWriterTest` assertion verifies the column is populated on every race in the ARP sample card and equals `number_of_runners` (no coupled entries on that card). 31/31 pdf-importer tests pass.

**Severity:** MEDIUM → FIXED.

### IMP-T5.6 — Scratched horses split on comma; commas in payouts mangle records [FIXED 2026-05-28]

**File:** `chart-parser/.../Scratch.java:55`

`text.split(",")` will mangle `(Earned $1,234.00)` style annotations.

**Fix:** Use a regex-based extractor that respects `(...)` grouping, OR pre-process to remove commas from amounts before splitting.

**Fix applied 2026-05-28:** Replaced `text.split(",")` in `Scratch.parseScratchedHorses` with a paren-depth-aware splitter `splitTopLevelCommas` — a single linear scan that emits a part on every comma at depth 0 and accumulates everything else, including commas inside `(Earned $X,XXX.00)` annotations.

The bug was latent because all existing test fixtures used 3-digit earned amounts (no internal commas). On 874K+ races with $10K+ purses any 4+-digit Earned would have manifested. ScratchTest extended with two new cases: a single 4-digit-earned scratch, and two 4-digit-earned scratches in one chart line — both would have produced 4 malformed fragments under the old splitter. 213/213 chart-parser tests pass.

**Severity:** MEDIUM (rare, but produces silent scratch losses) → FIXED.

### IMP-T5.7 — Trainer/jockey suffix handling [DEFERRED 2026-05-28]

**File:** `chart-parser/.../Trainer.java:77-94`

PDF format is "Last, First". For "Smith, John Jr." the suffix lands in firstName field. Inconsistent first/last splits cause downstream code joining on `(first, last)` to see two distinct entities for the same person.

**Verification:** Query for trainer_first values containing 'Jr.', 'Sr.', 'II', 'III'. Inspect for the same trainer_last with and without the suffix in trainer_first.

**Fix:** Post-process trainer/jockey first names to strip and re-attach known suffixes to last name. Or: introduce a separate `trainer_suffix` column. Or: build an alias mapping table for known same-person variants.

**Status (2026-05-28):** DEFERRED. Empirical investigation against the live DB revised both the audit's diagnosis and its severity:

1. **The audit's directional claim was wrong.** The chart format embeds the suffix in `trainer_last` (as `"Plesa, Jr."`), not `trainer_first`. The greedy `(.+),( (.+))?` regex captures everything up to the LAST comma into lastName, leaving only the actual first name in firstName.
2. **The "two distinct entities" failure mode is bounded.** ~224 trainer (last_bare, first) collisions across the entire dataset where the same person appears with and without the suffix — real but a small fraction of the trainer population. ~55 jockey collisions. Mostly the same individual; sometimes different father/son pairs that share an initial.
3. **The "merge real father/son" failure mode is the bigger risk.** Confirmed cases include Plesa Edward Sr. vs Edward Jr., Cormier Donald Sr. vs Jr. vs III, Hess Robert Sr. vs Jr., Tammaro John Sr. vs III vs IV, etc. — these are genuinely different trainers identified only by the suffix. Naively stripping the suffix would silently merge them.

So the right fix is the schema-column approach (separate `trainer_suffix`, `jockey_suffix`, `new_trainer_suffix`), which preserves the disambiguation while letting downstream code group by `(bare_last, first)` when appropriate. That's a multi-layer change:
- chart-parser: strip suffix from `lastName` during parse, expose `getSuffix()` on `Trainer`/`Jockey`. Requires a chart-parser release bump.
- pdf-importer: schema migration for the new columns; `RaceWriter` populates them.
- downstream: queries can opt into either grouping.

Total work ~1 hour but spans three layers. Deferred because: (a) real impact is bounded relative to the spend, (b) the downstream consumers most affected (trainer A/E aggregation) have other open issues that should be addressed together (WA #11/#13 coupled entries, WA #14 surface dummies, WA #16 jock_upgrade), and (c) any caller that needs to dedupe today can do so in SQL via `split_part(trainer_last, ',', 1)` — the existing data is salvageable without schema change.

**Severity:** MEDIUM (data-quality cleanliness, bounded blast radius) → DEFERRED. Reopen when trainer A/E work warrants it.

### IMP-T5.8 — Schema spec lists `exotics.bet_type` and `exotics.pool_type` columns that don't exist [FIXED 2026-05-28]

**Files:** `handycapper-schema.md:196-197` vs actual `db/schema.sql:228-247`

**Fix:** Either add the columns or remove from spec.

**Fix applied 2026-05-28:** Reality won — the live DB has these columns and AN1 / race-day-sim both query them. Brought the canonical schema in sync: added the columns to `pdf-importer/db/schema.sql` (`varchar(30)` and `varchar(20)`) with matching indexes, and added Flyway migration `V6__add-exotics-bet-type-pool-type.sql` so test deployments now match production. RaceWriter does NOT populate the columns (they're filled by a one-time SQL backfill from the parsed `name` column); spec at `docs/specs/handycapper-schema.md` updated to reflect that re-imports leave them NULL until backfill. Backfill logic itself is a separate concern, not covered here.

**Severity:** LOW → FIXED.

### IMP-T5.9 — `breeding` table is winners-only by design (NOT a bug)

The PDF only prints sire/dam/breeder/foaling info in the Winner block. Non-winning starters have nothing to write. This is by source-data design, not pdf-importer's choice.

**Implication for downstream:** Sire/dam analysis can only reference winners. Cannot compute "sire's progeny win rate" using just this table — would need an external data source for the denominator (all progeny). This was discovered earlier during Item 12 research; documented as permanent limitation.

**Severity:** N/A — not a bug.

---

## Tier 6: chart-parser

chart-parser is the upstream parsing library. ACTIVE (recent 2026 commits). Not deprecated.

### CP-T6.1 — Disqualification cascade is incorrect for multiple DQs [FIXED 2026-05-28]

**File:** `chart-parser/.../ChartParser.java:580-599`

`updateStartersAffectedByDisqualifications` reads `getOfficialPosition()` which has already been mutated by prior DQs in the loop. With multiple simultaneous DQs (real, per `DisqualificationTest.java`), the second iteration sees adjusted positions and applies wrong predicate. Starters can be missed (under-promoted) when 2+ DQs simultaneously demote past them. **No test** of the cascade itself.

**Verification:** Find races with 2+ DQs in the data. Manually verify official positions match what the chart printed.
```sql
SELECT race_id, COUNT(*) FROM handycapper.starters
WHERE disqualified = true
GROUP BY race_id HAVING COUNT(*) >= 2;
```

**Fix:** Snapshot original `finishPosition` per starter, then compute adjusted position = original − count(DQs whose `originalPosition < finishPos AND newPosition >= finishPos`). Add tests covering 2-DQ, 3-DQ, 4-DQ scenarios.

**Fix applied 2026-05-28:** Rewrote `updateStartersAffectedByDisqualifications` to two passes — first mark all DQ'd starters with their stated `newPosition`, then for each non-DQ'd starter compute `officialPosition = finishPosition − count(DQs where DQ.originalPosition < finishPosition)`. The audit's suggested predicate (`originalPosition < finishPos AND newPosition >= finishPos`) was actually wrong: a DQ'd horse vacates one slot ahead of any starter with a higher chart-finish position regardless of where the DQ'd horse ultimately lands, so the `newPosition >= finishPos` clause incorrectly excludes valid promotions. Method made `static` since it has no instance dependencies; companion `matchesStarter(Disqualification, Starter)` overload also made static. New `ChartParserDqCascadeTest` covers single-DQ, 2-DQ no-overlap, 3-DQ skip pattern, the 4-DQ fixture from `DisqualificationTest`, and the no-DQs degenerate case. All 223 chart-parser tests pass.

**Severity:** HIGH (silently wrong official positions) → FIXED.

### CP-T6.2 — Trainer/Owner program-less fallback breaks outer loop after one assignment [FIXED 2026-05-28]

**File:** `ChartParser.java:505-510, 526-531`

`break` exits the entire trainers loop. If a chart has multiple program-less entries, only the first gets assigned.

**Fix:** Replace `break` with `continue` (or remove if loop continues naturally). Trivial code change.

**Severity:** HIGH (silent data loss for older chart formats)

### CP-T6.3 — Time-format regex tolerates malformed times (`.` matches `:`) [FIXED 2026-05-28]

**File:** `FractionalTimes.java:21`

The `.` in `\d\d.\d\d` is unescaped — matches any character. Spurious matches produce strings that downstream `FractionalService.calculateMillisecondsForFraction` rejects, returning empty Optional. Fractions silently dropped.

**Fix:** Escape the dot: `\d\d\.\d\d`.

**Severity:** HIGH

### CP-T6.4 — `IndividualTime.parse` rejects times ≥ 60 seconds with minutes [FIXED 2026-05-28]

**File:** `running_line/IndividualTime.java:14`

Regex `\d{1,3}\.\d{1,3}` rejects format `1:11.45`. QH races for longer distances can produce these. Returns null → speed-index Rating null → fractional written with null millis.

**Fix:** Update regex to accept `\d{1,2}:\d{1,2}\.\d{1,3}` as alternative.

**Severity:** HIGH (affects QH long-distance races)

### CP-T6.5 — Fractional fewer-than-expected fallback ignores QUARTER_HORSE/MIXED breeds [FIXED 2026-05-28]

**File:** `fractionals/FractionalService.java:67-90`

Fallback uses TB-baseline speed constants (0.045 / 0.0647). Breed-aware adjustment only checks `Breed.ARABIAN`, not `QUARTER_HORSE` or `MIXED`. QH chart with one missing fractional silently assigns times to wrong points of call.

**Fix:** Add QH and MIXED-breed speed constants. Or document that breeds outside TB/AR are unsupported.

**Fix applied 2026-05-28:** Took the documented option — `FractionalService.getFractionalPointsForDistance` now logs a WARN and skips the fallback for non-TB/non-AR breeds rather than silently using TB constants. Adding empirically-calibrated QH/MIXED constants is deferred until someone has the data to fit them. The skip is safer than wrong-bucket assignment.

**Severity:** MEDIUM (affects non-TB races; small fraction of dataset but contaminates them silently) → FIXED.

### CP-T6.6 — `feetBehind = lengths * 8.75` magic number [FIXED 2026-05-28]

**File:** `RaceResult.java:698`

8.75 ft/length is plausible (a horse length ≈ ~9 ft) but undocumented. Now used for split-speed regression that writes to `indiv_fractionals`.

**Fix:** Extract as a named constant (`FEET_PER_LENGTH = 8.75`) with sourcing comment. Make it overrideable for future research.

**Severity:** MEDIUM

### CP-T6.7 — `daysSince` uses `LocalDate.now()` instead of race date [FIXED 2026-05-28]

**File:** `running_line/LastRaced.java:96`

The 2-digit year reducer uses `LocalDate.now().minusYears(80)`. Parsing the same PDF in different calendar years produces different `lastRaced` values for ambiguous 2-digit years.

**Fix:** Pass the race date through and use it as the base for year disambiguation.

**Severity:** MEDIUM

### CP-T6.8 — Owner regex has no end anchor [FIXED 2026-05-28]

**File:** `Owner.java:20`

`(\w+)?\s?-\s?(.+)` is greedy on `(.+)$`. If the `;` separator is ever missing/changed, the entire remainder collapses into one owner name with no warning.

**Fix:** Add end anchor or stricter delimiter handling.

**Severity:** LOW (separator format stable in practice)

### CP-T6.9 — `isWinner()` uses `==` on boxed Integer [FIXED 2026-05-28]

**Files:** `Starter.java:623, 633`, `RaceResult.java:810`

Works today via Integer cache for value 1, but latent footgun if refactor returns unboxed `int`.

**Fix:** Use `.equals()` or `.intValue() == 1`. Trivial.

**Severity:** LOW

### CP-T6.10 — Per-starter trip notes from footnotes are NOT structured [DEFERRED to Tier 7]

**File:** `Footnotes.java`

Footnotes contain rich trip-note information ("rallied four wide", "checked early") tied to specific horse names but the parser flattens to a single text blob. Per-starter trip notes would feed RKM trip-trouble adjustments and are currently lost.

**Fix:** Build a per-starter trip-note extractor that segments the footnotes by horse name and attaches phrases to `starters.trip_notes` (new column). This is a feature add, not a bug fix.

**Status (2026-05-28):** Deferred to Tier 7 (Trip Classification spec). The audit itself flagged this as "feature add, not bug fix"; Tier 7 covers the same problem area at length and prescribes a phased approach starting with DB analysis of footnote vocabulary. Doing CP-T6.10 in isolation now would build the wrong tool — the right shape of `trip_notes` depends on what Tier 7 decides about per-POC `wide` extraction, deterministic vs LLM, etc.

**Severity:** LOW (data exists, just not structured) → DEFERRED.

### CP-T6.11 — Single sample PDF, 28% complexity coverage [PARTIALLY ADDRESSED 2026-05-28]

**File:** `pom.xml`, two test fixtures

Two test PDFs cover one TB raceday + one multi-page race. No QH-only, Arabian, walkover, cancellation, real DQ-cascade, broken-font edge cases.

**Fix:** Expand fixture set. Add property-based tests for the regex parsers. Raise coverage threshold gradually as fixtures grow.

**Status (2026-05-28):** Coverage ratchet applied — JaCoCo BUNDLE-level COMPLEXITY minimum raised 0.28 → 0.32 in `pom.xml` (actual at the time of ratchet was 0.3228 over 119 classes after this week's chart-parser work added 5 new tests for the DQ cascade and 10 for IndividualTime, plus minor regression tests in FractionalTimes). The threshold sits just below actual so trivial changes don't break the build, but the floor now climbs whenever real coverage does. Fixture expansion (QH-only, Arabian, walkover, broken-font) still pending — needs representative real PDFs and verification of expected output, deferred until those PDFs are sourced.

**Severity:** LOW (testing gap, not a runtime bug) → ratcheted; fixture expansion deferred.

### CP-T6.12 — `convertToCsv` swallows IO errors [FIXED 2026-05-28]

**File:** `ChartParser.java:134-136`

Catches `IOException`, logs, returns whatever was accumulated. Truncated multi-page PDFs return partial lists; caller can't distinguish "no charts" from "errored mid-stream".

**Fix:** Wrap return value with a status indicator or rethrow. Caller (pdf-importer) can decide to retry or fail.

**Severity:** LOW

---

## Tier 7: Data Extraction Enhancement — Trip Classification from Footnotes

This is a future-enhancement proposal, not a bug. Captures the design decisions discussed for extending chart-parser to extract structured trip information from `races.footnotes` and `starters.comments`. Related to CP-T6.10 (per-starter trip notes not structured) and Item 13 of the research plan.

### Context

Race-replay already implements a deterministic regex-based footnote parser at `race-replay/public/replay.js:218-271` (`parseLateral` + `buildLateralHints`). It segments footnotes by uppercase horse name and extracts spatial hints (early/mid/late wide values, inside/outside qualifiers) for visual lane offsets in the 3D replay. This is a starting point — but the question is whether and how to lift this into chart-parser/pdf-importer so the structured data is available to all consumers (rkm, wagering-analytics, race-day-sim).

### Decision 1 — Should chart-parser/pdf-importer derive a `wide` value at each point of call?

**No.** Reasons:

1. **The data is structurally incomplete.** Chartwriters describe what's memorable, not what's exhaustive. They'll mention "5 wide on the turn" for the horse that lost ground but say nothing about the horse that saved ground. Populating per-call `wide` for some horses and leaving NULL for others creates a misleading "absence of note = horse was on the rail" inference that isn't supported by the data.

2. **It's a lossy one-way conversion.** If the parsing is wrong (uppercase-name segmentation mis-attributing a sentence), the DB now contains corrupt structured data. Consumers can't reverse-engineer back to the source. Leaving the footnote intact lets consumers re-parse with better logic later.

3. **Different consumers want different precision.** race-replay wants numeric offsets (0.2 / 2.5 / 4.0) for visual lanes. rkm wants categorical labels (WIDE_TRIP) for v0-residual analysis. race-day-sim wants per-call trouble flags for form-context discounting. Forcing one canonical numeric representation in the DB constrains all consumers to the lowest-common interpretation.

4. **Equibase already provides a structured `wide` field on points_of_call.** That's the authoritative source. Footnote-derived wide values would compete with it and create ambiguity about which to trust.

5. **chart-parser already has silent-corruption issues** (DQ cascade, time regex, scratched-horse comma split — see Tier 6). Adding more interpretive parsing to the source-of-truth project before fixing existing bugs adds risk without retiring any.

### Decision 2 — What chart-parser SHOULD extract (lower risk)

Two new columns on `starters` (subject to validation in the analysis phase):

- **`starters.trip_phrase`** (text) — the footnote sentence attributed to this starter via uppercase-name segmentation. Verbatim text, no interpretation. Lets downstream consumers parse however they want.
- **`starters.trip_label`** (enum: `TROUBLE / WIDE / DREW_OFF / PRESSED / EASY / NORMAL / UNCLASSIFIED`) — categorical classification. Useful for joins to `rkm_race_performance.surprise` and for race-day-sim's form-context layer.

The numeric-spatial derivation (race-replay's per-phase wide values) stays in race-replay as a visualization concern.

### Decision 3 — Deterministic vs LLM?

**Deterministic for structural extraction.** Equibase chartwriters use formulaic vocabulary ("rallied", "stalked", "tracked", "set pressured fractions"). A regex/lexicon-based extractor handles 95%+ of cases. LLMs are slow, expensive, non-reproducible, and overkill for structural classification. chart-parser is regex-based throughout — staying consistent matters.

**LLM only at the synthesis layer, optional and downstream.** Generating a pre-race "Edge Call narrative" from the model's outputs (pace prediction, decay profiles, expected positions) is something an LLM does well. After the race, comparing the projected narrative to the actual chartwriter footnote becomes interesting validation. But that's a race-day-sim feature, not a chart-parser feature.

### Decision 4 — DB analysis BEFORE writing the spec

**Required.** Risk of writing the spec without analysis: encoding our mental model of what footnotes look like instead of what they actually contain.

Specific analysis questions:

1. **Vocabulary distribution.** Top 100 bigrams/trigrams in `races.footnotes` and `starters.comments`. Identify phrases not anticipated by the proposed enum.

2. **Phrase coverage per category.** For the proposed enum {TROUBLE, WIDE, DREW_OFF, PRESSED, EASY, NORMAL, UNCLASSIFIED}: what fraction of starters get matched? How many fall to UNCLASSIFIED?

3. **Inter-class overlap.** Phrases that legitimately fit two categories (e.g., "steadied wide" — TROUBLE or WIDE?). Disambiguation rules.

4. **Era drift.** Does chartwriter language differ between 1995 and 2015? Regional or per-track variations? If so, the extractor needs era-awareness.

5. **Sentence segmentation accuracy.** Random sample of 200 races: manually verify each sentence is correctly attributed. The uppercase-name approach can fail when one horse's name is contained in another's (e.g., "KING" inside "KING'S BISHOP"). Quantify the error rate.

6. **Validation against `surprise`.** Research-findings.md Item 13 measured: PRESSED → -0.25 ft/s surprise; DREW_OFF → +0.73; TROUBLE → -0.04. After the new classification, reproduce these averages? If yes, the new classifier is at least as good as the research-time prototype. If not, the new vocabulary is worse than what we already have.

### Recommended phased approach

| Phase | Purpose | Output | DB needed? |
|---|---|---|---|
| 1. Analysis | Understand actual footnote language | Vocabulary report, phrase distribution, era drift assessment | Yes |
| 2. Spec | Define `trip_label` enum, segmentation rules, classifier logic | `docs/specs/trip-classification.md` | Optional |
| 3. Prototype | Python extractor, run against historical data, validate vs surprise | Working classifier with documented coverage and accuracy | Yes |
| 4. Implementation | Port validated logic into chart-parser | New columns + extractor, schema migration | No (until deploy) |

Without phases 1 and 3, anything that goes into chart-parser is guesswork dressed up as engineering. Worth a couple of focused sessions when DB access is available.

### Severity / priority

**MEDIUM** — this is a feature gap, not a bug. The current state (footnotes as text blob, no per-starter structuring) means trip information is lost to all downstream consumers except via ad-hoc parsing. But the system functions without it. Should be tackled AFTER Tier 1 system bugs are fixed and the more impactful data-leakage issues resolved.

---

## Tier 8: race-replay

race-replay is a visualization tool (not a model/calibration system). Some "issues" might be acceptable design choices for a UI tool, but several affect data accuracy and user trust. No CLAUDE.md exists; design.md is significantly stale relative to README and current code.

### RR-T8.1 — POC `feet` exact-match silently breaks 9f+ races

**File:** `server.js:332-341, 373-378`

Win-prob endpoint maps POC rows to call points via `parseInt(row.feet) === callFeet`. For 2-lap races (1¼ mile / 9f+ on a 1m track), POC `feet` resets per call and may not match `callFeet` exactly due to importer rounding inconsistencies. Horses fall through to the `-6.0` logit DNF penalty path. The replay reader (`replay.js:382-385`) maps by integer `point` instead, which is more robust — there's an inconsistency between the two readers.

**Verification:** Pick a 1¼m race, query its POC rows. Compare `feet` values to the call grid the win-prob endpoint generates. If any horse is missing a row at any expected `feet`, win-prob will spike to ~0% and back as the SVG progresses.

**Fix:** Use `point` (the integer call number) consistently across both endpoints, OR add a tolerance to the `feet` match.

**Severity:** HIGH

### RR-T8.2 — `parseLateral` mis-attribution and missing word boundaries

**File:** `replay.js:218-271`

Multiple bugs in footnote-to-horse attribution:
1. **Substring matching without `\b`:** `up.includes(upper)` matches `BISHOP` inside `BISHOPRIC`. Longest-first sort (L262) helps but doesn't solve cases where the longer name isn't in the field.
2. **Single attribution per sentence:** "BAYERN and SHARED BELIEF battled to the wire" — only one horse gets the sentence (the `break;` at L264). Both horses' lateral hints should be derived.
3. **Sentence splitting on `\.\s+` doesn't account for periods in titles** (`MR. PROSPECTOR`).
4. **"rail" pattern over-triggers:** "off the rail" / "well off the rail" both incorrectly trigger an INSIDE hint (L243).
5. **Apostrophe handling in legend regex** (`replay.js:919`) — apostrophes aren't in the regex-escape character class, so `BISHOP'S WIFE` colors only on `BISHOP`.

**Verification:** Pick races with closely-named horses (e.g., one whose name is a substring of another, names with apostrophes, sentences that mention multiple horses). Check the replay's lane offsets visually against the actual footnote.

**Fix:**
- Use `\b` word-boundary matching in `buildLateralHints` (`replay.js:262-264`)
- Loop through ALL horse name matches in a sentence rather than `break;` after first
- Refine "rail" pattern to require it's not preceded by "off the" / "well off"
- Improve sentence splitting to handle abbreviation periods
- Properly escape apostrophes in the legend regex

**Severity:** HIGH (affects every race with a footnote — visualization shows the wrong horse going wide)

### RR-T8.3 — Trip-quality thresholds are uniform across distance/surface

**File:** `server.js:417-453`

`earlyZ` and `lateZ` thresholds (e.g., Pace Collapse needs `earlyZ > 0.8 AND lateZ < -0.5`) are applied uniformly. A 0.8 fps SD difference at the 1/4 of a sprint is a much bigger relative effect than at the 1/4 of a 1¼m route. Labels skew sprint-heavy.

Additional `else if` ordering quirks: `Even Pace` (L435) catches `earlyZ=-1.0, lateZ=-1.0` even though Outclassed semantics fit better — Outclassed wins by ordering, but the order-dependence makes the rules brittle.

`earlyZ`/`lateZ` derived from first/last splits without normalization to actual race phase — for routes with some splits filtered out, `earlyZ` might be the 3/4 split.

**Verification:** Run the trip-quality classifier across the dataset, inspect distribution of labels by distance bucket. If sprint races have substantially more "extreme" labels (Pace Collapse, Dominant) than routes, the thresholds need normalization.

**Fix:** Compute thresholds dynamically based on the section's typical fps SD by distance × surface. Or convert to percentile-based thresholds (e.g., earlyZ in top 20% of sprint distribution → fast early pace).

**Severity:** HIGH (user-visible labels influence handicapping interpretation)

### RR-T8.4 — "Live" win probability is a misnomer (uses post-race position data)

**Files:** `server.js:363-371, 386-391`; README.md:13, 207; `app.js:522`

The win-prob model reads `tot_len_bhd` at every call point. That's chart-derived post-race data. The model is a REPLAY (showing how a chart-aware observer would update beliefs given known position at each call), not a predictive model. README copy and the UI panel title both say "live" — misleading.

**Verification:** Code review confirms.

**Fix:** Rename the panel and README copy to "Replay win probability" or "Probability evolution by call." Document explicitly that this uses position observations from the chart (post-race).

**Severity:** HIGH (user trust — implies predictive capability that doesn't exist)

### RR-T8.5 — Scratched horses pollute win-prob endpoint

**File:** `server.js:255-265`

Win-prob query selects all `starters` for the race. Scratches are stored separately in `handycapper.scratches` (joined only by horse name) but not used to filter the starters list. Scratched horses with no POC rows get `-6.0` logit penalty at every call, appearing on the SVG at ~0% (visual clutter) and in trip-quality cards as `Normal` with no z-bars.

**Verification:** Find a race with a scratch. Inspect the win-prob endpoint output. Confirm the scratched horse appears.

**Fix:** Filter scratched horses before processing. Match by name (with disambiguation if duplicate names — though `scratches.horse` is just a name field).

**Severity:** MEDIUM

### RR-T8.6 — `loadPar` doesn't cache empty results

**File:** `server.js:170-224`

When a track/furlongs/surface/condition combo has no historical par data and no within-race fallback, the function returns an empty Map but doesn't cache it for the original key. Subsequent requests re-run the expensive query against `indiv_splits ⋈ starters ⋈ races`.

**Verification:** Hit the same condition-specific race repeatedly, observe DB query count.

**Fix:** Always cache the result for the requested key, including empty Maps.

**Severity:** MEDIUM (performance — busy track with many race-detail loads is impacted)

### RR-T8.7 — Front-end race tab race condition

**File:** `app.js:140-159`

`loadRace` fires two parallel fetches and awaits both. If the user clicks a different race tab while the first is in flight, the older request can resolve and overwrite `currentRaceData` and append to `chartArea`. No `currentRaceId` re-check after the await.

**Verification:** Click rapidly between race tabs while a slow connection is loading the first.

**Fix:** Capture `raceId` at function entry, compare to `currentRaceId` after the await, return early if they differ.

**Severity:** MEDIUM (rapid tab clicking shows wrong race)

### RR-T8.8 — Date range hard-coded to 2000-2017 in `loadPar`, undocumented

**File:** `server.js:199-200`

```sql
AND r.date >= '2000-01-01'
AND r.date <  '2017-01-01'
```

Per `notes/2026-04-11.md` this is by design (training/holdout split). But:
- Not documented in README
- Races from 2017+ get pace z-scores against pre-2017 par (silently fine)
- Tracks that didn't exist before 2017 get within-race fallback (z-scores collapse to ~0)

**Fix:** Document in README. Consider extending the par window now that 1991-2017 is the standard data range across the ecosystem.

**Severity:** MEDIUM

### RR-T8.9 — Coupled entries: `entry`/`entry_program` columns ignored

**File:** `server.js:97-107`

The starters query never reads `entry` or `entry_program`. Coupled entries appear as separate horses in the chart UI (correct for chart fidelity) but if `odds = NULL` on the second coupled horse, the `meanP` fallback at L325-329 silently inflates that horse's prior.

**Verification:** Find a race with coupled entries. Check whether both rows have odds populated. Inspect the win-prob output for the coupled entry.

**Fix:** When odds are NULL on a coupled entry, copy from the primary entry (same betting interest).

**Severity:** MEDIUM

### RR-T8.10 — `start_comments` regex misses common gate-trouble phrases

**File:** `server.js:317`

```js
const gateRe = /slow\s*start|bobbled|bumped|squeez|stumbl|reared|fractious|dwelt/i;
```

Misses: "broke awkwardly", "left at the start", "reluctant to load", "hopped", "slammed". False-negative on gate trouble.

**Fix:** Extend regex. (This is the same kind of vocabulary issue Tier 7 calls out for trip classification — DB analysis would surface the right vocabulary.)

**Severity:** LOW

### RR-T8.11 — `docs/design.md` is significantly stale

**File:** `docs/design.md`

- L344-388: claims credentials are hardcoded in server.js — moved to env vars
- L405: "PostgreSQL Server (direct TCP connection on localhost:5432)" — ignores .env-driven port (5433 default for Docker)
- L412: claims "Input: PostgreSQL database dump format" — there's no pg_dump in the project
- L113-115: lists only 2 race indexes; schema.sql creates 35+
- L156-167: describes points_of_call columns but omits `len_ahead_text`, `tot_len_bhd_text`
- L12 vs README: "PostgreSQL 11+" vs "12+"

**Fix:** Either delete design.md or rewrite from scratch. README is canonical.

**Severity:** LOW (but actively misinforms readers)

### RR-T8.12 — Other LOW findings (consolidated)

- **Apostrophe regex escape** in legend (L919) — names with apostrophes color partially
- **Replay frame interpolation** (L491-493) — `df < -0.3` wrap heuristic has minor edge cases
- **`replayRaceFeet`** derived only from fractionals (L328-330) — falls back to 1-mile if race has no fractionals; should use `races.feet`
- **Canvas re-context per frame** — `canvas.getContext('2d')` called inside drawFrame loop; cache once
- **`parCache` unbounded** — bounded in practice (~33K max keys) but no LRU eviction
- **CI only syntax-checks `server.js`** — public/*.js files not checked
- **No CLAUDE.md** for race-replay (other repos have one)

### RR-T8.13 — What's CORRECT in race-replay

- Credentials properly moved to env vars
- No N+1 SQL — uses `starter_id = ANY($1)` batching
- `softmaxProbs` is numerically stable (subtracts max before exp)
- Z-score clamping at ±4 prevents outlier dominance
- Within-race par fallback for low-data sections
- Track geometry math in replay.js well-commented
- All README screenshot references exist
- Joins use `starter_id` (not horse name) — avoids the cross-repo "join on name" trap

### RR-T8 — Recommended priorities

1. **RR-T8.4** (rename "live" → "replay") — pure copy fix, high user-trust impact
2. **RR-T8.2** (parseLateral fixes) — affects every race's replay accuracy
3. **RR-T8.1** (POC feet match) — silently breaks 9f+ races
4. **RR-T8.3** (trip-quality thresholds) — user-visible labels systematically biased
5. **RR-T8.5** (scratched horses) — visible artifacts on ~5-10% of races
6. **RR-T8.7** (tab race condition) — easy fix, easy reproduction
7. **RR-T8.11** (design.md staleness) — delete or rewrite

---

## Coverage notes

This audit covered code logic, statistical calibration, pre-race firewall integrity, and protocol/code alignment. NOT covered:
- Performance / scaling (heavy queries over the SSH tunnel, etc.)
- Wagering psychology / discipline (whether the protocol is correct in principle, only whether the code enforces it)
- Long-run statistical validity (whether 100+ sim days would actually show edge)

A second pass added: shared docs (`/github/docs/`), pdf-importer, and chart-parser.

---

## What's NOT a problem (verified during audit)

- Linear deceleration model is empirically defensible
- Huber reweighting in curves.py is standard
- WCMI computation is mathematically correct
- The point-in-time CTEs in `load_market_bias` are properly date-bounded (no new leaks beyond known curve issue)
- Most class-rating multipliers in `bias_multiplier` are correctly derived from research (1.022 first-Lasix, 1.101 blinkers-off, 0.970 first-blinkers, 0.961 class-rise, 1.029 class-drop all check out)
- Identity disambiguation logic is mostly right (small leap-year edge case)
- The previously-known `odds_to_rating` rank-mapping issue is documented in `edge-calibration-issue.md`

---

## Updated Phase Priority

After all four audit passes:

**Tier 1 (system-breaking) and PROTO-T3.1/T3.2 (validation+evaluator)** remain top priority.

**New high-priority additions:**
- IMP-T5.1 (race-row update misses most columns) — root cause of off_turf-style data holes
- IMP-T5.3 (UNIMPORTABLE never set) — wastes CPU every re-run
- CP-T6.1 (DQ cascade with multi-DQ) — silently wrong official positions
- CP-T6.2 (loop break bug) — silent data loss
- CP-T6.3/T6.4 (time-format regex bugs) — silent fractional drops

**Medium additions:**
- DOC-T4.1 (schema spec stale on backfilled columns) — doc cleanup, prevents future confusion
- DOC-T4.2 (favorite provenance) — schema doc accuracy
- IMP-T5.4/T5.5 (dead_heat semantics, coupled entries in count)
- CP-T6.5/T6.6/T6.7

The pdf-importer/chart-parser issues are mostly silent corruption at the source. They affect every downstream calculation but are individually small. Worth running batch verification queries when DB access returns to estimate the actual data corruption rate before deciding which to fix first.
## Verification & Remediation Plan

When robinpc DB access is restored:

### Phase A: Verify Tier 1 findings (DB queries, no code changes yet)

1. **RKM-T1.1:** Query `rkm_track_offsets` distribution. Count distinct tracks per (horse_key, surface) in curves table.
2. **RKM-T1.2:** Find duplicate horse names in `rkm_velocity_curves` and trace through joins.
3. **RKM-T1.3:** ✅ Code fix applied 2026-05-27 (loop bound now `range(1, ...)` matching `MIN_PRIOR_RACES = 1`). Pending recompute will materialize `n_recent_races = 1` snapshots for 2nd-start horses.
4. **RKM-T1.4:** ✅ CONFIRMED 2026-05-27 by code review (`career_v0` is a static full-career lookup from `rkm_velocity_curves`, no date bound). No DB query needed.
5. **WA-T1.1:** ✅ DONE 2026-05-27. Grid search via `calibrate_stern_k.py` on 81K clean races (using `official_position`): global MLE k = 0.86. Field-size segmentation flat (0.86/0.86/0.88). 0.81 is ~0.05 below MLE on a flat LL surface.
6. **WA-T1.2:** ✅ DONE 2026-05-27. Verticals only. `verify_payoff_skill.py` shows pre-race full model adds +0.04 (exacta) / +0.07 (trifecta) R² above naive Stern — real skill, not just tautology. Severity dropped to MEDIUM.
7. **WA-T1.3:** ✅ FULLY DONE 2026-05-27 across verticals + Pick 3/4/5/6. bad_fav_legs is predictively inert in all (≤0.001 ΔR²). Separate finding: existing Pick 5/6 OLS models lack a `log_carryover` feature (and Pick 6 actively excludes carryover rows). Carryover explains +0.31-0.33 R² above naive parlay — without it, the models can't capture pool dynamics that shape payoffs on carryover AND non-carryover days. Rebuild needed for both carryover-EV plays and structural-edge plays.
8. **WA-T1.4:** ✅ DONE 2026-05-27. Static table contains future info (confirmed by Calhoun example, delta 0.046), but race-day-sim never reads it (`load_market_bias` uses point-in-time CTEs). Severity LOW; recommend dropping the table to remove the footgun.
9. **WA-T1.5:** ✅ DONE 2026-05-27. Calibration is structurally broken AND unused. More importantly, jitter is a non-problem for blinded backtests — the blinder already provides closing odds for every leg, so there's no future-leg uncertainty to model. Recommend deleting the function, JSON, and compute script entirely; revisit only if live-betting mode is added later.
10. **RDS-T1.1:** ✅ DONE 2026-05-27. T=6500ms confirmed catastrophically over-flat — produces ~15%/15%/.../10% probabilities on a 14-length-spread 8-horse field (ratio 1.5:1). Empirical time spreads (581 races) average 2,805 ms max-min, 972 ms stddev. Recommended T ≈ 1000ms; cascade effect through Benter explains why edge-vs-market is mostly noise. Likely a major ROI driver.
11. **RDS-T1.2:** ✅ DONE 2026-05-27. Confirmed by code review; fix applied — `bias_multiplier(is_favorite=...)` gates off-turf credit to favorites only and raises lift to research-finding +7.5%.
12. **RDS-T1.3:** ✅ DONE 2026-05-27. Confirmed and fixed — surface-specific class ladders (`_CLASS_RATINGS_MAIN/TURF`, +12 universal offset) replace the inconsistent `base += 5` fudge. Priors now align with `_get_anchor`'s anchor ratings on both surfaces.
13. **RDS-T1.4:** ✅ DONE 2026-05-27. Confirmed and fixed — generic surface-switch multiplier now undone before trainer-specific is applied (mirrors class-drop pattern at lines 347-353). Verified with 4-case sanity test.
14. **RDS-T1.5:** ✅ DONE 2026-05-27. Confirmed (with corrected direction — system was pessimistic, not optimistic, by ~36% on Pick 3 fair value) and fixed via `all_odds` API param + field overround normalization. Currently no live caller, but fix is preventive.

### Phase B: Apply Tier 1 fixes one at a time, verify each

Order by likely impact and ease of fix:

1. **RDS-T1.2** (off-turf favorite-only) — ✅ FIXED 2026-05-27
2. **RDS-T1.3** (turf prior offset) — ✅ FIXED 2026-05-27
3. **RDS-T1.4** (surface-switch double-count) — ✅ FIXED 2026-05-27
4. **RDS-T1.5** (parlay_prob normalization) — ✅ FIXED 2026-05-27
5. **RDS-T1.1** (TEMPERATURE) — ✅ verified 2026-05-27 as one of the highest-impact bugs. Pending: change TEMPERATURE from 6500 → 1000 in `probability.py:17, 28` as immediate fix; calibrate properly via MLE as follow-up
6. **RKM-T1.3** (form loop bound) — ✅ code fix applied 2026-05-27 (Option B: `MIN_PRIOR_RACES = 1`); recompute pending
7. **RKM-T1.4** (career baseline leakage) — significant rework + recompute
8. **WA-T1.4** (trainer profiles point-in-time) — ✅ verified 2026-05-27; no live leakage (race-day-sim doesn't query the static table). Pending: drop the table to remove footgun + correct CLAUDE.md dependency list
9. **WA-T1.3** (payoff post-race features) — ✅ FULLY DONE 2026-05-27 across all horizontals + verticals (post-race features predictively inert; ΔR² ≤ 0.001 everywhere). Distinct follow-up: rebuild Pick 5/6 OLS with carryover_amount as feature (current Pick 6 model excludes carryover rows entirely, making it useless for the actual play-decision use case)
10. **WA-T1.2** (payoff R² inflation) — ✅ verticals done 2026-05-27 (real skill +0.04-0.07 above naive Stern); pending: switch fit_payoff_models.py to year-stratified split + report ΔR²
11. **WA-T1.1** (Stern k calibration) — ✅ calibration done; constant 0.81 → 0.86 in `populate_stern_fair.py`; pending: re-run with `--recompute-all` (DB tunnel was dropped mid-refresh) to refresh `exotic_harville_ratios.stern_fair`
12. **RKM-T1.1 + T1.2** (track normalization + identity joins) — major RKM rework

After EACH fix:
- Re-run a sim day or two with the same seed as before-fix
- Compare ratings/edges/conviction candidates side by side
- If the fix moved metrics in the expected direction, keep it. If not, investigate.

### Phase C: Tier 2 cleanup

After Tier 1 fixes are validated, address the cross-cutting issues (date ranges, A/E normalization, coupled entries) and the smaller localized bugs.

---

## Notes for Future Self

- These findings came from agent-based analysis. Some may be incorrect interpretations of the code. Verify before fixing.
- Many of the "HIGH severity" findings interact. Fixing them one at a time is the only way to know which contributed what.
- The known `odds_to_rating` rank-mapping issue (in `edge-calibration-issue.md`) is separate from these findings but compounds with them.
- Phase 3 RKM recompute (lower `MIN_PRIOR_RACES` to 1) is still pending. Loop-bound fix (RKM-T1.3) was applied 2026-05-27, so the recompute will now actually pick up 2nd-start horses.

---

