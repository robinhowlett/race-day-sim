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

## Tier 3: Protocol/Code Alignment Issues

A second-pass audit examined `simulation-protocol.md`, `wagering-framework.md`, `itp-principles.md`, `itp-wagering-framework.md`, and `research-plan.md` against the actual code. Findings: **the wagering protocol is mostly aspirational with respect to the code.** The code implements rating computation and a single conviction filter; nearly every wagering rule documented in the protocol is unenforced.

### PROTO-T3.1 — `register_bet()` performs zero validation

**File:** `scripts/run_simulation.py:164-169`

No checks for: programs in race, structural validity (TRIFECTA needs 3 positions), pool minimums, win-bet minimum odds (3.0/1 — labeled but not enforced), horizontal conviction-leg minimum (constant defined, never read), bet type whitelist. **Critically: the evaluator only handles WIN and EXACTA — registered TRIFECTA/SUPERFECTA/PICK_N silently never match → counted as MISS even if they cash.** Real bug, not just a protocol violation.

**Verification approach:** No DB needed. Re-run a previous sim day's bets through `reveal_and_evaluate()` with a registered TRIFECTA — confirm it's marked MISS regardless of outcome. Cross-reference with the actual trifecta payoff to see if cash was hidden.

**Fix:** In `register_bet()` add a validation block:
- Look up the race's program numbers; raise if any in `programs` aren't in the field
- Whitelist `bet_type` against an enum of supported types
- For each bet_type, validate structure (TRIFECTA = 3 position lists; PICK3 = 3 leg lists)
- Check `MIN_ODDS_WIN_BET` against the horse's odds for WIN bets
- Check pool size against type-specific minimum (after pool data loaded)
- Check horizontal conviction-leg count against `MIN_HORIZONTAL_CONVICTION_LEGS`

Then extend `reveal_and_evaluate()` to handle TRIFECTA, SUPERFECTA, QUINELLA, DAILY_DOUBLE, PICK_3/4/5/6. Each needs a matching helper that takes the official_position-sorted top finishers and the bet's program structure, returns (hit, payout). PICK_N needs to walk leg-by-leg matching against each race's winner.

**Severity:** HIGH

### PROTO-T3.2 — Equity test computed but never enforced as a gate

**Files:** `src/sim/horizontal.py:39-101`, `src/sim/payoff.py:168-209`, `scripts/run_simulation.py`

`evaluate_leg_selections()` and `estimate_combo_value()` compute equity ratios. But:
- `run_simulation.py` doesn't import either module
- `simulate_race_day.py` uses them only for display, never gating
- `flashing_stop_sign` flag is computed but never consulted

The single most-emphasized rule in the protocol ("Every combination must pass the equity test before inclusion") is purely advisory.

**Verification approach:** No DB needed. Grep for `flashing_stop_sign` and `equity_ratio` usages — confirm they're only in print statements, not in conditionals that reject bets.

**Fix:** Add an `equity_check()` method on `SimDay` that takes a proposed bet, calls into `horizontal.evaluate_leg_selections()` for horizontals or `payoff.estimate_combo_value()` for verticals, and returns `(passes, reasons)`. Call this from `register_bet()` BEFORE appending to the bets list. If `passes=False`, raise an exception or print a warning + require an override flag (`force=True`) to register against the protocol's recommendation.

Depends on PROTO-T3.3 being fixed first (the equity formula itself must be correct before gating on it).

**Severity:** HIGH

### PROTO-T3.3 — Horizontal equity formula is wrong (uses cheap shortcut)

**File:** `src/sim/horizontal.py:39-58`

`estimate_leg_equity()` returns `(odds + 1) / n_horses_used` — treats per-horse stake as `ticket_cost / N` per-leg, ignoring full ticket geometry. The protocol's Step E.4 worked example uses a different (correct) formula based on per-combo cost vs surviving combo value across the full ticket. The two formulas can disagree — a horse "loses equity" in one but "gains" in the other.

The `ticket_cost_per_combo` parameter is accepted but never used.

**Verification approach:** Construct the protocol's worked example as a test case ($120 Pick 3, 3×2×2 = 12 combos, $10/combo). Pass it through both formulas. Confirm cheap formula and protocol formula disagree on at least one horse's equity status.

**Fix:** Rewrite `estimate_leg_equity()` to take the full leg structure (list of selections per leg) as input, not just one leg. The signature should be:
```python
def estimate_ticket_equity(leg_selections: list[list[dict]], total_cost: float)
```
For each combination (cartesian product of leg selections), compute:
- `cost_per_combo = total_cost / n_combos`
- For each horse in each leg: if that horse wins their leg, `surviving_combos = product of OTHER legs' widths`
- `surviving_value_per_combo = parlay_payoff_at_their_odds_and_others_winning / surviving_combos` — this requires assumptions about other legs' winners (the protocol example assumes equal-prob across selections in other legs)
- Compare `surviving_value_per_combo` to `cost_per_combo` to determine GAIN/LOSE equity

Use the actually-prescribed formula from simulation-protocol.md Step E.4. Mark the old `estimate_leg_equity()` deprecated.

**Severity:** HIGH

### PROTO-T3.4 — Press mechanic is doc-only, no code support

Searched all of `src/sim/` and `scripts/` — zero hits for `press`, `basket`, `multiplier`, `tier`. The protocol's "press at 2x/3x/4x with layered baskets (Win + Exacta key + Trifecta pressed + cover)" has no datatype, no helper, no enforcement.

**Verification approach:** Grep confirms absence. No DB needed.

**Fix decision required first:** Should the press be CODE or JUDGMENT?
- If code: extend `Bet` dataclass to support per-combo multipliers (instead of flat `amount`). `Bet.combinations: list[tuple[programs, multiplier]]`. The total amount becomes `sum(base_unit × multiplier × combos_in_group)`. Add a `Basket` class that bundles related Bets (Win + Exacta + Trifecta on the same conviction).
- If judgment: delete the press section from simulation-protocol.md or move it to a "guidance" appendix. Stop claiming the scaffold "applies protocol rules deterministically" for sizing.

Recommendation: code it. The press is a mechanical decision (combo identified as high-conviction → multiply by N) that benefits from automation. A `press_combos(combos, conviction_scores, base_unit, total_budget)` function could redistribute the budget proportionally to conviction.

**Severity:** HIGH

### PROTO-T3.5 — "Never exclude favorite from 2nd/3rd unless total collapse" — unenforced

The protocol's E.5 critical rule has no code enforcement. `payoff.py` accepts `fav_position=None` silently.

**Verification approach:** No DB needed. Code-grep confirms.

**Fix:** In `register_bet()` validation block: if bet_type is TRIFECTA/SUPERFECTA and the favorite (program with lowest odds in the race, or `choice == 1`) is excluded from 2nd AND 3rd positions, require an explicit `expecting_total_collapse=True` flag in the rationale or a separate parameter. Otherwise warn or reject.

The "expecting total collapse" judgment can't be coded fully — but the SCAFFOLD can require the user to acknowledge it explicitly (preventing accidental exclusion). Cross-reference with the model's pace prediction: `pace_scenario == "CONTESTED_HIGH_DECAY"` AND fav has high decay = some justification; otherwise the exclusion is suspect.

**Severity:** HIGH

### PROTO-T3.6 — Decision tree (E.1 opinion classification) not implemented

`protocol_check()` produces a flat list of horses with positive worst-case edge. The protocol's six-class taxonomy (STRONG specific, MODERATE specific, STRONG negative, STRUCTURAL, SPREAD, NO OPINION) and its mapping to pool selection is left to user judgment. CLAUDE.md claims the scaffold "applies protocol rules deterministically" — only one rule is actually deterministic.

**Verification approach:** No DB needed. Code review of `protocol_check()` confirms — only `has_conviction` boolean flag.

**Fix:** Add a `classify_opinion()` function called per race that returns one of the six categories with rationale:
- STRONG specific: candidate exists with edge - band > 5
- MODERATE specific: candidate exists with edge - band in (0, 5]
- STRONG negative: fav_edge < -10 with band clear
- STRUCTURAL: pace_scenario == CONTESTED_HIGH_DECAY AND multiple speed types AND multiple low-decay horses with positive Edge in middle of v0 distribution
- SPREAD: 3+ candidates within ±3 Edge of each other, no clear leader
- NO OPINION: top edge - band ≤ 0

Then add `recommended_pool(opinion_type, race_summary)` returning one of WIN / EXACTA_KEY / TRIFECTA_EX_FAV / HORIZONTAL_LEG / PASS. Display these in the conviction-plays output so the user sees the protocol's recommendation BEFORE constructing tickets.

**Severity:** HIGH

### PROTO-T3.7 — Two scaffolds, fragmented capabilities

`run_simulation.py` (the "recommended" one) has registration and evaluation but no equity/payoff projection. `simulate_race_day.py` has equity displays but no registration/evaluation. The two don't share helpers, and the "recommended" entry-point lacks the very tool (`estimate_combo_value`) the protocol's equity test needs.

**Verification approach:** Compare the imports + capabilities of both scaffolds (already done in audit). No DB needed.

**Fix:** Pick one canonical entry-point and consolidate. Recommendation: keep `run_simulation.py` as the canonical, port the value-display features from `simulate_race_day.py` into it (or into `SimDay` methods), then delete `simulate_race_day.py` or make it a thin wrapper. CLAUDE.md should reference only the canonical script.

**Severity:** MEDIUM

### PROTO-T3.8 — Flat Kelly sizing ignores Fav-Edge tier modifiers

**File:** `src/sim/kelly.py:56-97`

`size_bets()` has no `fav_edge` parameter. Protocol prescribes basket weight scaling: `Fav Edge < -10` → maximum basket, `> +5` → small play / pass. WCMI sizing modifiers (1.5x for low WCMI, 0.25x for band crossing zero) also not implemented.

**Verification approach:** No DB needed. Code review confirms.

**Fix:** Extend `size_bets()` to take `race_context` (fav_edge, wcmi, band_crosses_zero, carryover_active) and apply the documented multipliers from wagering-framework.md:244. Order of operations: compute base Kelly, then apply context multipliers, then enforce MAX_EXPOSURE cap.

**Severity:** MEDIUM

### PROTO-T3.9 — ITP concepts referenced as rules but not coded

Searched — zero hits for `kill_shot`, `hurdle`, `basket`, `win_only`. The doc treats these as rules ("verified against source transcripts") but the code can't enforce them.

**Verification approach:** Grep confirms.

**Fix decision required first:** Are these enforceable rules or judgment guidance? Per ITP concept:
- **Kill shot** (price on top, never both ways): codable. Reject `EXACTA #1/#2 + EXACTA #2/#1` if both are registered with the same horse as the longer price.
- **Hurdle**: definitionally judgment — "deliberately reduce survival prob for equity gain." Can flag candidates ("this single creates a hurdle") but can't force the user to single.
- **Basket of bets**: codable as a `Basket` datatype that bundles related bets at coordinated multipliers (see PROTO-T3.4).
- **Win-only horses**: codable as a horse-level flag. Decay rate above some threshold + speed-and-fade profile = "win only" → reject placement underneath in exotics.

Recommendation: code kill-shot rejection and win-only flag (low effort, prevent specific mistakes). Move "hurdle" and basket guidance to a judgment appendix.

**Severity:** MEDIUM

### PROTO-T3.10 — FTS rule contradiction across docs

`itp-principles.md:124-126` says "FTS on top only, NEVER underneath." `wagering-framework.md:200-206` says "elite FTS trainer at 8/1 is a legitimate inclusion underneath." `ratings.py` follows the latter. Code and one doc agree; the other doc disagrees. A user reading itp-principles.md would think they're following protocol while actually breaking it.

**Verification approach:** Cross-read both docs and confirm. Already done.

**Fix:** Resolve to the research-revised position (wagering-framework.md): trainer-signal FTS can be included underneath, generic FTS overbet as a group. Update `itp-principles.md` to either remove the "never underneath" rule or add a footnote: "Original ITP guidance, superseded by research finding that elite-FTS-trainer horses are exception to this rule." `itp-principles.md` should be marked clearly as historical reference for ITP source material, not the operational protocol.

**Severity:** MEDIUM

### PROTO-T3.11 — Place betting forbidden by ITP, not blocked by code

ITP doc says "never place bet." `register_bet()` accepts any bet_type string including "PLACE" — would be invested but never matched (silent loss).

**Verification approach:** No DB needed. Code-grep confirms.

**Fix:** Part of PROTO-T3.1 bet-type whitelist. Either omit PLACE/SHOW from the whitelist (rejecting them at registration) or add a `--allow-place` flag for users who explicitly want to override. Recommendation: omit by default, document that ITP forbids them.

**Severity:** MEDIUM

### PROTO-T3.12 — Pool minimum thresholds never checked

Protocol says trifectas need $20K+ pool, Pick 3/4 need $50K+. `tri_pool` is computed for display only, never compared against any threshold.

**Verification approach:** No DB needed. Code-grep confirms.

**Fix:** Add `MIN_POOL_BY_TYPE = {"TRIFECTA": 20000, "SUPERFECTA": 25000, "PICK_3": 50000, "PICK_4": 75000, "PICK_5": 100000, "PICK_6": 100000}` constant. In `register_bet()` validation, look up pool for the race × bet_type from `sim.pools` and reject if below threshold. Allow `--ignore-pool-min` override for testing.

**Severity:** MEDIUM

### PROTO-T3.13 — Horizontal qualification (2+ conviction legs) unenforced

`MIN_HORIZONTAL_CONVICTION_LEGS = 2` is defined at `run_simulation.py:33` and never referenced again. Users can register Pick 3 with 1 conviction leg + 3 random spread legs.

**Verification approach:** Grep confirms. No DB needed.

**Fix:** In `register_bet()` validation block, if bet_type starts with `PICK_` or is `DAILY_DOUBLE`: count how many of the legs' races have at least one conviction candidate (via `protocol_check`). Reject if count < `MIN_HORIZONTAL_CONVICTION_LEGS`.

Edge case: a horizontal where you SINGLE the favorite in one leg and have a conviction longshot in another might count as 2 conviction legs even though one is a chalk single. The protocol intent is "at least one STRONG opinion" — could refine the check to require at least one leg with `worst_case > 5` (STRONG) and one more with any positive worst case.

**Severity:** MEDIUM

### Dead code findings

- `kelly_exotic` (kelly.py:26-53) — not called from any script
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

### IMP-T5.1 — `upsertRace` only updates 6 of 50+ columns on conflict

**File:** `pdf-importer/src/main/java/.../pipeline/RaceWriter.java:154-166`

The `doUpdate()` clause sets only `track_name, final_time, final_millis, dead_heat, number_of_runners, footnotes`. Every other race-level column silently retains the original value.

**Why this matters:** This is the root cause of the `off_turf`/`female_only`/`age_code` hole. Those fields ARE coded into RaceWriter (lines 219, 222, 233) but never made it into rows imported BEFORE that code was added — because re-runs don't update the missing columns.

**Verification:** Query rows with `imported_at` predating the RaceWriter change vs after. Compare `off_turf` populated rate. Pre-change rows should be NULL/false; post-change should match the spec's expected distribution.

**Fix:** Either (a) expand `doUpdate()` to set every column with `EXCLUDED.col` for each field, or (b) change strategy to delete-and-reinsert per race_id (matching how starters are handled). Option (b) is simpler and matches existing pattern.

**Severity:** HIGH

### IMP-T5.2 — `cancelled` and `races` rows can co-exist for the same (date, track, number)

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

### IMP-T5.3 — `UNIMPORTABLE` status defined but never set

**Files:** `model/ImportResult.java:11`, `pipeline/ImportTracker.java:62`, `PdfImporter.java:88-115`

The enum value exists and the tracker treats it as "done", but no code path actually sets it. The 1,738 known unparseable PDFs (per `docs/zero-race-files.md`) were classified manually via direct SQLite UPDATE. On a fresh re-import, every PDF gets re-classified `PARSE_FAILED` and retried indefinitely.

**Verification:** Inspect `ImportTracker` SQLite DB for any rows with status `UNIMPORTABLE`. They came from manual UPDATE statements, not code.

**Fix:** Categorize known unparseable exception classes (encrypted PDFs, malformed structure, non-Equibase format) and have `recordFailure(...UNIMPORTABLE...)` flip them. Also: persist this categorization in source control somehow so other hosts inherit it.

**Severity:** HIGH (operational — wastes CPU on every re-run)

### IMP-T5.4 — `dead_heat` flag only marks WIN dead heats, not 2nd/3rd

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

**Severity:** MEDIUM

### IMP-T5.5 — `number_of_runners` counts coupled entries as separate

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

**Severity:** MEDIUM

### IMP-T5.6 — Scratched horses split on comma; commas in payouts mangle records

**File:** `chart-parser/.../Scratch.java:55`

`text.split(",")` will mangle `(Earned $1,234.00)` style annotations.

**Fix:** Use a regex-based extractor that respects `(...)` grouping, OR pre-process to remove commas from amounts before splitting.

**Severity:** MEDIUM (rare, but produces silent scratch losses)

### IMP-T5.7 — Trainer/jockey suffix handling

**File:** `chart-parser/.../Trainer.java:77-94`

PDF format is "Last, First". For "Smith, John Jr." the suffix lands in firstName field. Inconsistent first/last splits cause downstream code joining on `(first, last)` to see two distinct entities for the same person.

**Verification:** Query for trainer_first values containing 'Jr.', 'Sr.', 'II', 'III'. Inspect for the same trainer_last with and without the suffix in trainer_first.

**Fix:** Post-process trainer/jockey first names to strip and re-attach known suffixes to last name. Or: introduce a separate `trainer_suffix` column. Or: build an alias mapping table for known same-person variants.

**Severity:** MEDIUM

### IMP-T5.8 — Schema spec lists `exotics.bet_type` and `exotics.pool_type` columns that don't exist

**Files:** `handycapper-schema.md:196-197` vs actual `db/schema.sql:228-247`

**Fix:** Either add the columns or remove from spec.

**Severity:** LOW

### IMP-T5.9 — `breeding` table is winners-only by design (NOT a bug)

The PDF only prints sire/dam/breeder/foaling info in the Winner block. Non-winning starters have nothing to write. This is by source-data design, not pdf-importer's choice.

**Implication for downstream:** Sire/dam analysis can only reference winners. Cannot compute "sire's progeny win rate" using just this table — would need an external data source for the denominator (all progeny). This was discovered earlier during Item 12 research; documented as permanent limitation.

**Severity:** N/A — not a bug.

---

## Tier 6: chart-parser

chart-parser is the upstream parsing library. ACTIVE (recent 2026 commits). Not deprecated.

### CP-T6.1 — Disqualification cascade is incorrect for multiple DQs

**File:** `chart-parser/.../ChartParser.java:580-599`

`updateStartersAffectedByDisqualifications` reads `getOfficialPosition()` which has already been mutated by prior DQs in the loop. With multiple simultaneous DQs (real, per `DisqualificationTest.java`), the second iteration sees adjusted positions and applies wrong predicate. Starters can be missed (under-promoted) when 2+ DQs simultaneously demote past them. **No test** of the cascade itself.

**Verification:** Find races with 2+ DQs in the data. Manually verify official positions match what the chart printed.
```sql
SELECT race_id, COUNT(*) FROM handycapper.starters
WHERE disqualified = true
GROUP BY race_id HAVING COUNT(*) >= 2;
```

**Fix:** Snapshot original `finishPosition` per starter, then compute adjusted position = original − count(DQs whose `originalPosition < finishPos AND newPosition >= finishPos`). Add tests covering 2-DQ, 3-DQ, 4-DQ scenarios.

**Severity:** HIGH (silently wrong official positions)

### CP-T6.2 — Trainer/Owner program-less fallback breaks outer loop after one assignment

**File:** `ChartParser.java:505-510, 526-531`

`break` exits the entire trainers loop. If a chart has multiple program-less entries, only the first gets assigned.

**Fix:** Replace `break` with `continue` (or remove if loop continues naturally). Trivial code change.

**Severity:** HIGH (silent data loss for older chart formats)

### CP-T6.3 — Time-format regex tolerates malformed times (`.` matches `:`)

**File:** `FractionalTimes.java:21`

The `.` in `\d\d.\d\d` is unescaped — matches any character. Spurious matches produce strings that downstream `FractionalService.calculateMillisecondsForFraction` rejects, returning empty Optional. Fractions silently dropped.

**Fix:** Escape the dot: `\d\d\.\d\d`.

**Severity:** HIGH

### CP-T6.4 — `IndividualTime.parse` rejects times ≥ 60 seconds with minutes

**File:** `running_line/IndividualTime.java:14`

Regex `\d{1,3}\.\d{1,3}` rejects format `1:11.45`. QH races for longer distances can produce these. Returns null → speed-index Rating null → fractional written with null millis.

**Fix:** Update regex to accept `\d{1,2}:\d{1,2}\.\d{1,3}` as alternative.

**Severity:** HIGH (affects QH long-distance races)

### CP-T6.5 — Fractional fewer-than-expected fallback ignores QUARTER_HORSE/MIXED breeds

**File:** `fractionals/FractionalService.java:67-90`

Fallback uses TB-baseline speed constants (0.045 / 0.0647). Breed-aware adjustment only checks `Breed.ARABIAN`, not `QUARTER_HORSE` or `MIXED`. QH chart with one missing fractional silently assigns times to wrong points of call.

**Fix:** Add QH and MIXED-breed speed constants. Or document that breeds outside TB/AR are unsupported.

**Severity:** MEDIUM (affects non-TB races; small fraction of dataset but contaminates them silently)

### CP-T6.6 — `feetBehind = lengths * 8.75` magic number

**File:** `RaceResult.java:698`

8.75 ft/length is plausible (a horse length ≈ ~9 ft) but undocumented. Now used for split-speed regression that writes to `indiv_fractionals`.

**Fix:** Extract as a named constant (`FEET_PER_LENGTH = 8.75`) with sourcing comment. Make it overrideable for future research.

**Severity:** MEDIUM

### CP-T6.7 — `daysSince` uses `LocalDate.now()` instead of race date

**File:** `running_line/LastRaced.java:96`

The 2-digit year reducer uses `LocalDate.now().minusYears(80)`. Parsing the same PDF in different calendar years produces different `lastRaced` values for ambiguous 2-digit years.

**Fix:** Pass the race date through and use it as the base for year disambiguation.

**Severity:** MEDIUM

### CP-T6.8 — Owner regex has no end anchor

**File:** `Owner.java:20`

`(\w+)?\s?-\s?(.+)` is greedy on `(.+)$`. If the `;` separator is ever missing/changed, the entire remainder collapses into one owner name with no warning.

**Fix:** Add end anchor or stricter delimiter handling.

**Severity:** LOW (separator format stable in practice)

### CP-T6.9 — `isWinner()` uses `==` on boxed Integer

**Files:** `Starter.java:623, 633`, `RaceResult.java:810`

Works today via Integer cache for value 1, but latent footgun if refactor returns unboxed `int`.

**Fix:** Use `.equals()` or `.intValue() == 1`. Trivial.

**Severity:** LOW

### CP-T6.10 — Per-starter trip notes from footnotes are NOT structured

**File:** `Footnotes.java`

Footnotes contain rich trip-note information ("rallied four wide", "checked early") tied to specific horse names but the parser flattens to a single text blob. Per-starter trip notes would feed RKM trip-trouble adjustments and are currently lost.

**Fix:** Build a per-starter trip-note extractor that segments the footnotes by horse name and attaches phrases to `starters.trip_notes` (new column). This is a feature add, not a bug fix.

**Severity:** LOW (data exists, just not structured)

### CP-T6.11 — Single sample PDF, 28% complexity coverage

**File:** `pom.xml`, two test fixtures

Two test PDFs cover one TB raceday + one multi-page race. No QH-only, Arabian, walkover, cancellation, real DQ-cascade, broken-font edge cases.

**Fix:** Expand fixture set. Add property-based tests for the regex parsers. Raise coverage threshold gradually as fixtures grow.

**Severity:** LOW (testing gap, not a runtime bug)

### CP-T6.12 — `convertToCsv` swallows IO errors

**File:** `ChartParser.java:134-136`

Catches `IOException`, logs, returns whatever was accumulated. Truncated multi-page PDFs return partial lists; caller can't distinguish "no charts" from "errored mid-stream".

**Fix:** Wrap return value with a status indicator or rethrow. Caller (pdf-importer) can decide to retry or fail.

**Severity:** LOW

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

---

