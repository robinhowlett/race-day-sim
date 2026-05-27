# Cross-Repo Audit & Remediation Plan

**Date:** 2026-05-27
**Scope:** rkm, wagering-analytics, race-day-sim
**Trigger:** 0/15 record across 7 simulation days. Investigation found systematic flaws beyond the documented edge calibration issue.

**Status:** All findings are based on code review, not DB verification. Each finding requires DB-side validation before applying any fix. Fixes should be verified individually — running multiple fixes in parallel risks confusion about which one moved which metric.

---

## Tier 1: System-Breaking Bugs

These directly cause inflated edges, future-data leakage, or incorrect probability computation. Likely responsible for the bulk of the 0/15 record.

### RKM-T1.1 — Cross-track normalization is structurally inert

**Files:** `rkm/src/rkm/adjustments.py:144-220`, `rkm/scripts/compute_adjustments.py:33-50`

**Problem:** `compute_track_offsets` uses a "shipping horse" approach but the inputs make it impossible:
- Curves are aggregated per-(horse, surface), so each horse has only one v0 per surface — no per-track v0
- `compute_adjustments.py:33-50` assigns each (horse_key, surface) exactly one `primary_track`
- The pairs filter (`track_a < track_b`) returns empty results because each horse has only one track per surface
- The "track offset" being computed is essentially noise

**Verification approach:** Query `rkm_track_offsets` directly. If the values are all near-zero or only populated for a tiny subset, the bug is confirmed. Also: count how many distinct tracks each (horse_key, surface) pair has in the curves table — should be exactly 1.

**Fix:** Requires fitting curves per (horse_key, surface, track, distance_zone) so one horse has multiple v0s — one per track they ran at. Then compute pairwise differences across rows for the same (horse_key, surface). This is a significant RKM pipeline change.

**Severity:** HIGH. Without working track normalization, `adj_v0` isn't actually cross-track comparable, which means rating comparisons across tracks are meaningless.

### RKM-T1.2 — Bare horse name join ignores identity disambiguation

**Files:** `rkm/scripts/compute_adjustments.py:36`, `rkm/scripts/compute_situations.py:40`

**Problem:** `JOIN handycapper.starters s ON s.horse = SPLIT_PART(vc.horse_key, '|', 1)` — joins on bare horse name, ignoring the `|YYYY` birth-year disambiguation. Different horses sharing a name get merged.

**Verification approach:** Find a horse name with multiple birth years in `rkm_velocity_curves`, then trace through `compute_adjustments.py` and `compute_situations.py` to see if both share the same starts.

**Fix:** Attach canonical `horse_key` to `starters` (or use a horse-key-aware join). Search across both repos for any other instances of `SPLIT_PART(...horse_key...)` joins.

**Severity:** HIGH. Reused horse names contaminate the data.

### RKM-T1.3 — `compute_form.py` loop hardcodes "skip first 2 races"

**File:** `rkm/scripts/compute_form.py:136`

**Problem:** `for i in range(2, len(race_obs))` — loop starts at index 2, meaning races 1 and 2 chronologically never produce a snapshot. Our recent `MIN_PRIOR_RACES = 1` change in `form.py` is dead code because this script-level loop bound shadows it.

**Verification approach:** Check the count of starters with `n_recent_races = 1` in `rkm_current_form`. Should be substantial after the change; will be ~zero if the loop bound is still 2.

**Fix:** Change to `for i in range(MIN_PRIOR_RACES, len(race_obs))` and re-run `compute_form.py`. Will need full recompute on robinpc.

**Severity:** HIGH. 2nd-start horses (a key market situation, especially for FTS-following debuts) are silently excluded from current_form.

### RKM-T1.4 — Career baseline leaks future data into v0_trend

**Files:** `rkm/scripts/compute_form.py:54-62, 140`

**Problem:** `career_v0` passed into `compute_form_at_date` comes from a curve fit over the horse's entire career — including races AFTER the form snapshot date. `v0_trend = current_v0 - career_v0` is comparing prior-only against future-aware. The `rkm_current_form` table is marked "pre-race safe" in CLAUDE.md but isn't strictly so for v0_trend.

**Verification approach:** For a known horse with substantial form changes mid-career, manually compute the as-of-date career baseline (from races up to that date only) and compare to the stored `career_v0` in `rkm_current_form`. If they differ for early-career snapshots, leakage confirmed.

**Fix:** Compute career baseline as a trailing aggregate (only races before the snapshot date), not the full-career curve.

**Severity:** HIGH. Breaks the pre-race firewall for v0_trend.

### WA-T1.1 — Stern k = 0.81 was never empirically calibrated

**Files:** `wagering-analytics/scripts/populate_stern_fair.py:29`, `wagering-analytics/docs/exotic-payoff-analysis.md:201-227`

**Problem:** The spec promised a grid-search calibration of k segmented by field size/surface/race_type. No such code exists. The constant 0.81 is imported from Stern (1992), a different dataset and era. README.md claims "empirically confirmed" but there's no calibration record.

**Verification approach:** Run an actual grid search of k from 0.5 to 1.2 against this dataset. Check whether 0.81 actually minimizes the residual between Stern-projected probability and observed finish frequency.

**Fix:** Build the calibration script. If 0.81 is approximately right, document the validation. If not, replace with the calibrated value (potentially segmented).

**Severity:** HIGH. Every `stern_fair` value is biased by this unverified prior. The "15-21% trifecta overlay" headline is partly a function of k.

### WA-T1.2 — Payoff model R² is largely tautological

**Files:** `wagering-analytics/scripts/fit_payoff_models.py:80-95, 152, 210-226, 306-308`

**Problem:**
- Random train/test split (not year-stratified as spec requires) — same race-day rows leak between train and test
- The model regresses `log(payoff) ~ log_winner_odds + log_second_odds + ...` which is essentially the inverse of the joint probability identity. High R² mostly reflects this near-tautology, not learned skill.
- No naive baseline (`log_payoff = -log(p1×p2×p3) + const`) reported

**Verification approach:** Re-fit with year-stratified holdout (2014-2017 as test, prior as train). Compare R² to a naive baseline. The drop from "R²=0.88" to true forward R² will quantify the inflation.

**Fix:** Year-stratified split. Report skill above naive baseline, not raw R².

**Severity:** HIGH. Headline metric is misleading.

### WA-T1.3 — Payoff model uses post-race features

**File:** `wagering-analytics/scripts/fit_payoff_models.py:111`

**Problem:** Features like `bad_fav_legs`, `fav_won`, `fav_second`, `fav_third`, `fav_fourth`, and the `log_odds1_x_fav_*` interactions are POST-race outcomes. The model is sold as pre-race projection but is fit on outcomes that aren't knowable until the race runs.

For Pick 3/4, `bad_fav_legs` has the largest non-odds coefficient (PICK_3 = 0.088, PICK_4 = 0.122).

**Verification approach:** Re-fit the model with only pre-race features (drop all `fav_*_position`, `bad_fav_legs`, etc.). Compare R². Whatever drops is the post-race contribution.

**Fix:** Either (a) drop the post-race features and accept lower R², or (b) replace with pre-race surrogates (e.g., model-predicted `P(bad_fav)`).

**Severity:** HIGH. Model is unusable for stated purpose without this fix.

### WA-T1.4 — Trainer profiles are aggregate, not point-in-time

**Files:** `wagering-analytics/scripts/compute_trainer_profiles.py:14-16`, `wagering-analytics/docs/market-bias-analysis.md:83-84`

**Problem:** AN2 spec mandates point-in-time computation. Implementation publishes career-aggregate (2005-2017) A/E. Race-day-sim using these profiles for a 2010 race gets future data.

**Verification approach:** Same class of bug as the velocity curves. Check that any consumer of `trainer_ae_profiles` is actually using race-time-bounded values, not the static table.

**Fix:** Either (a) build a materialized view computed point-in-time per starter, or (b) ensure all consumers use the in-blinder query (`load_market_bias`) which already does point-in-time computation. Currently race-day-sim's `load_market_bias` correctly uses point-in-time CTEs, but the static `trainer_ae_profiles` table exists as a tempting shortcut.

**Severity:** HIGH if anything reads the static table for live simulation. Less critical if only used for research.

### WA-T1.5 — Jitter calibration measures wrong quantity

**File:** `wagering-analytics/scripts/compute_jitter_calibration.py:35-86, 117`

**Problem:** Computes `STDDEV(log_winner_odds - leg1_log_odds)` across exotic_ids. This measures the spread of log-odds across all winners in any leg — not within-race odds drift between bet placement and leg off-time. Output sigmas are flat at ~1.0 across all legs of all bet types — the spread of the closing-odds distribution itself, not what was intended.

A sigma of 1.0 in log-space means odds projections span 2.7× per std dev — drowns the signal entirely.

**Verification approach:** Inspect `models/jitter_calibration.json`. If leg-1 sigma is non-zero (it is) and later legs are all clustered around 1.0 with no monotonic increase, the bug is confirmed.

**Fix:** Requires intra-race odds time series (not currently in the database). Document as known limitation. Race-day-sim should not use these jitter values for horizontal pool projection until methodology is fixed.

**Severity:** HIGH. Currently invalidates horizontal pool projection.

### RDS-T1.1 — TEMPERATURE = 6500ms produces nearly-uniform probabilities

**Files:** `race-day-sim/src/sim/probability.py:17, 28`

**Problem:** Sprint races have ~70K ms total time, within-race spread of 200-1000ms. `exp(-1000/6500) = 0.86` — even a 3-length gap barely affects relative probability. The fastest projected horse barely dominates the slowest in the softmax.

**Verification approach:** For a typical race, compute `model_probs_from_curves` at TEMPERATURE = 6500 and at TEMPERATURE = 300. Compare distribution. The 6500 version should be near-uniform; the 300 version should show meaningful separation.

Then calibrate properly: for fields where the model has clear strength differences, what TEMPERATURE produces a probability distribution that matches the relative win rates of those strength tiers in historical data?

**Fix:** Recalibrate TEMPERATURE. Likely needs to be 200-500ms, not 6500ms. Verify with held-out data.

**Severity:** HIGH. Affects every Benter-combined probability throughout the system.

### RDS-T1.2 — Off-turf credit applied to entire field, not just favorite

**File:** `race-day-sim/src/sim/ratings.py:268-269`

**Problem:**
```python
if _flag("off_turf"):
    multiplier *= 1.050
```

Research finding (Item 9): off-turf **favorite** A/E = 0.884. Specific to favorite. Code applies +5% to every horse in off-turf races, inverting the research conclusion which said "use favorite strongly, fade turf-only horses."

**Verification approach:** Code review confirms; no DB query needed. Just check the ratings.py logic against the research-findings.md table.

**Fix:** Only apply when the horse is the favorite (from `s.choice == 1`). Add a separate negative multiplier for turf-only horses on dirt.

**Severity:** HIGH. Inverts a core research finding.

### RDS-T1.3 — Turf rating prior double-counts surface offset

**File:** `race-day-sim/src/sim/ratings.py:134-136`

**Problem:**
```python
if surface == "Turf":
    base += 5
```

The canonical anchor in `_get_anchor` already returns `anchor_rating = 112` for turf races. The class-rating ladder used as `base` is on the dirt scale. Adding +5 on top either understates turf class (a $20K turf claimer gets 105, should be ~112) or misclassifies tiers depending on purse.

**Verification approach:** Code review against rating-calibration-plan.md. The canonical anchor logic and the prior computation should agree on what 112 means.

**Fix:** Remove the +5 offset. The canonical anchor in the rating already encodes the turf scale. The class-rating ladder used as the prior should ALSO be on the universal scale (so a turf claiming race's prior is naturally 112, not 100+5).

**Severity:** HIGH. Misrates all turf horses.

### RDS-T1.4 — Surface-switch trainer A/E double-counts

**File:** `race-day-sim/src/sim/ratings.py:284-292, 325-328`

**Problem:** Generic surface-switch multiplier (Synthetic→Turf 1.075) PLUS trainer-specific switch A/E. Trainer's A/E already incorporates the population pattern (their A/E was measured on actual surface switches).

**Verification approach:** Code review. Compare against the class-drop logic which DOES correctly undo the generic before applying trainer-specific.

**Fix:** Either undo the generic surface-switch multiplier when trainer-specific is applied (mirror the drop logic), or remove the generic since the trainer-specific should subsume it.

**Severity:** HIGH. Compounds artificially.

### RDS-T1.5 — Horizontal parlay_prob unnormalized for takeout

**File:** `race-day-sim/src/sim/horizontal.py:135`

**Problem:** `leg_prob = sum(1.0 / (s.get("odds", 99) + 1) for s in selections)` — sums raw `1/(odds+1)` per leg without overround correction. Result: leg_prob is systematically under-estimated by the takeout factor (~17%), and parlay_prob compounds the error across N legs.

`fair_payoff = (1-takeout)/parlay_prob` is then over-estimated, making horizontal tickets look more attractive than they are.

**Verification approach:** Compute leg_prob for a typical race two ways: raw sum, and normalized by total field overround. The difference is the bias.

**Fix:** Normalize by full-field overround:
```python
field_overround = sum(1.0/(o+1) for o in all_field_odds)
leg_prob = sum(1.0/(s.odds+1) for s in selections) / field_overround
```

**Severity:** HIGH. Distorts every horizontal evaluation.

---

## Tier 2: Significant but localized issues

### Cross-cutting

- **Date range chaos:** RKM scripts use 1997-2016, form computation 1991-2017, WCMI 1999-2017, trainer profiles 2005-2017, payoff models all data. CLAUDE.md inconsistencies. Audit each script and align to a documented standard (likely 1991-2017 with caveats for exotic data starting 1999).
- **A/E denominators not normalized for overround** (WA #19): Population A/E ≈ 0.83 because takeout, not because trainers underperform. Profiles store raw `1/(odds+1)` sums. Consumers can misinterpret. Fix: normalize using V003's `win_prob`, not raw implied probability.
- **Coupled entries treated as independent everywhere** (WA #11): V003, Stern, payoff, WCMI, trainer A/E all ignore coupling. Affects ~3-5% of US races.
- **"Edge" defined three different ways across modules** (RDS C1): ratings.py rating points, payoff.py % of fair value, horizontal.py takeout difference. Rename to disambiguate.

### Race-Day-Sim specific

- **`evaluate.py` exotic payoff math** (RDS H1): assumes uniform $1 per combo; breaks for asymmetric tickets. Different formula in `run_simulation.py:231` than `evaluate.py:59-63` for the same concept.
- **`kelly_exotic` formula** (RDS H5): mathematically incorrect — `edge / avg_payoff` is off by `b/(b+1)`. Under-stakes (safe direction) but doesn't match docstring.
- **Pace thresholds are unit-naive across surfaces** (RDS M1): Calibrated on dirt (mean v0=64) but applied to turf (mean v0=55) without normalization. Turf races classified as CONTESTED when actually MODERATE.
- **Pace second-clause is unreachable** (RDS M2): `pace.py:58-60` has dead code.
- **MIN_EDGE_CONVICTION = 0** (RDS L1): Effectively no gate. With known edge inflation, passes too many candidates. Raise to 2-3 pts.
- **Jockey upgrade only detected for jockeys with ≥50 starts** (RDS L6): Apprentices systematically miss the UPGRADE classification.

### Wagering-Analytics specific

- **Default takeout 0.20** (WA #12): Bet-type-agnostic fallback. CLAUDE.md promises 0.21/0.24 defaults but code doesn't implement.
- **Coupled entries / dead heats / late scratches not handled** (WA #13)
- **Surface dummies all-zero in EXACTA/TRIFECTA models** (WA #14): `models/payoff_coefficients.json` shows `surface_T = surface_S = 0.0` with `p_value = NaN`. Surface effect silently dropped.
- **Outliers** (WA #15): No winsorization for extreme payoffs in OLS bet types.
- **`jock_upgrade` claimed as 6th dimension but never computed** (WA #16): Placeholder zeros.
- **Claim query double-counts horses claimed multiple times** (WA #7): Per-claim-event ROW_NUMBER, not per-horse.
- **Drop/layoff filtered to dirt/fast only** (WA #8): Other dimensions aren't. Composites are incoherent.
- **Dimensions are not independent** (WA #9): layoff×drop, layoff×switch overlap. Composite scoring misuses them.
- **Velocity range filter inconsistent** (RKM #6): curves.py 30-70, form.py 30-85. A burst >70 admitted to current_v0 but rejected from career.
- **v0 extrapolated from midpoint velocities** (RKM #7): No near-zero anchor. Conflates start speed with stamina.

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

## Verification & Remediation Plan

When robinpc DB access is restored:

### Phase A: Verify Tier 1 findings (DB queries, no code changes yet)

1. **RKM-T1.1:** Query `rkm_track_offsets` distribution. Count distinct tracks per (horse_key, surface) in curves table.
2. **RKM-T1.2:** Find duplicate horse names in `rkm_velocity_curves` and trace through joins.
3. **RKM-T1.3:** Count starters with `n_recent_races = 1` in `rkm_current_form`. Should be substantial after Phase 3 recompute; near-zero confirms loop bound bug.
4. **RKM-T1.4:** Pick a known horse with form changes; manually compute as-of-date career baseline vs stored value.
5. **WA-T1.1:** Run grid search of k from 0.5 to 1.2 against actual finish frequencies.
6. **WA-T1.2:** Re-fit payoff with year-stratified holdout. Compare to naive baseline.
7. **WA-T1.3:** Re-fit payoff without post-race features. Compare R².
8. **WA-T1.4:** Audit consumers of `trainer_ae_profiles` table.
9. **WA-T1.5:** Inspect `jitter_calibration.json` patterns (already done — confirmed).
10. **RDS-T1.1:** Test TEMPERATURE values at 200, 500, 1000, 6500ms on representative races. Calibrate against historical strike rates.
11. **RDS-T1.2 to T1.5:** Code review only — already verified.

### Phase B: Apply Tier 1 fixes one at a time, verify each

Order by likely impact and ease of fix:

1. **RDS-T1.2** (off-turf favorite-only) — quick code fix, no DB recompute
2. **RDS-T1.3** (turf prior offset) — quick code fix
3. **RDS-T1.4** (surface-switch double-count) — quick code fix
4. **RDS-T1.5** (parlay_prob normalization) — quick code fix
5. **RDS-T1.1** (TEMPERATURE) — code fix + calibration verification
6. **RKM-T1.3** (form loop bound) — code fix + recompute
7. **RKM-T1.4** (career baseline leakage) — significant rework + recompute
8. **WA-T1.4** (trainer profiles point-in-time) — significant rework
9. **WA-T1.3** (payoff post-race features) — re-fit
10. **WA-T1.2** (payoff R² inflation) — re-fit with stratification
11. **WA-T1.1** (Stern k calibration) — new calibration script
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
- Phase 3 RKM recompute (lower MIN_PRIOR_RACES) is still pending. Should be done AFTER fixing the loop-bound bug (RKM-T1.3) so the recompute actually picks up 2nd-start horses.
