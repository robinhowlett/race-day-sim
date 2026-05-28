# Full Rebuild Plan: From Scratch to Race-Day-Sim Ready

**Goal:** Wipe the database, fix critical upstream bugs, re-ingest all PDFs, recompute all analytics, and arrive at a race-day-sim that's free of the silent-corruption issues identified in the cross-repo audit.

**Scope:** Across all 8 projects (chart-parser, pdf-importer, rkm, wagering-analytics, race-day-sim, race-replay, plus shared docs and the redboarders DB migrations).

**Estimated total time:** 2-4 weeks of focused work, depending on which optional fixes are done. Most of that is bug-fixing (Phases 1, 4, 6, 8); the actual rebuild execution (Phases 2, 5, 7) is a few hours of compute time.

---

## Phase 0: Prerequisites & Decision Points

Before starting, decide:

### Decision 0A: Scope

| Option | Effort | Outcome |
|---|---|---|
| **Minimum viable rebuild** | ~1 week | Fix Tier 1 system-breaking bugs only. Skip RKM track normalization rewrite, skip Stern calibration. Accept known limitations documented in audit. |
| **Full audit fix** | ~3-4 weeks | Fix all HIGH and MEDIUM findings across all 8 tiers. Largest payoff, longest timeline. |
| **Targeted fix** | ~2 weeks | Fix HIGH findings only. Defer MEDIUM/LOW to future passes. |

**Recommendation:** Targeted fix. The HIGH findings are where data corruption actually happens. MEDIUM findings are mostly missing-feature or doc-quality issues that don't break correctness.

### Decision 0B: Backup strategy

- Snapshot the current `handycapper` DB before wiping (`pg_dump`). If anything goes wrong in the rebuild, you can restore.
- Keep the PDFs themselves untouched (they're the source of truth).
- Snapshot the pdf-importer SQLite tracker (`tracker.db`) — has the manual UNIMPORTABLE classifications for 1,738 unparseable files. Restore after re-ingestion to avoid re-attempting them.

### Decision 0C: Date range standardization

The audit identified inconsistent date ranges across scripts. Pick one and apply everywhere:
- **Recommendation:** 1991-2017 for curve fitting and form computation. 1999-2017 for wagering analytics (where exotic data is reliable). Document explicitly in each script.
- Update CLAUDE.md across all repos to match.

### Decision 0D: Fix order — top-down or bottom-up?

**Top-down (recommended).** Fix chart-parser → pdf-importer → ingest → fix rkm → recompute → fix wagering-analytics → recompute → fix race-day-sim. Each layer depends on the previous, so fixing upstream first means downstream layers see clean data.

Bottom-up (race-day-sim first, then upstream) means each upstream fix invalidates the work done downstream. Don't do this.

---

## Phase 1: Fix chart-parser bugs (4-7 days)

### Goal
Eliminate silent data corruption at the parsing source before re-ingesting. chart-parser bugs propagate to every downstream consumer.

### Required fixes (HIGH severity)

1. **CP-T6.1 — Disqualification cascade with multiple DQs**
   - Refactor `updateStartersAffectedByDisqualifications` (`ChartParser.java:580-599`) to snapshot original positions before mutating
   - Add tests covering 2-DQ, 3-DQ, 4-DQ scenarios
   - Effort: 1 day

2. **CP-T6.2 — Trainer/Owner program-less fallback `break` bug**
   - Replace `break` with `continue` (or remove) at `ChartParser.java:505-510, 526-531`
   - Add a test fixture with multiple program-less trainer entries
   - Effort: 1 hour + test

3. **CP-T6.3 — Time regex tolerates malformed times**
   - Escape the dot in `FractionalTimes.java:21` (`\d\d.\d\d` → `\d\d\.\d\d`)
   - Effort: 1 hour

4. **CP-T6.4 — `IndividualTime.parse` rejects times ≥60s with minutes**
   - Update regex at `IndividualTime.java:14` to accept `\d{1,2}:\d{1,2}\.\d{1,3}` alternative
   - Effort: 2 hours + test

### Optional fixes (MEDIUM severity)

5. **CP-T6.5** — Breed-aware fractional fallback (handle QH, Mixed)
6. **CP-T6.6** — Document/extract `feetBehind = lengths * 8.75` constant
7. **CP-T6.7** — Use race date instead of `LocalDate.now()` for 2-digit year disambiguation
8. **CP-T6.10** — Per-starter trip phrase extraction (Tier 7 enhancement; defer to later phase)

### Verification

- Run existing test suite: `mvn verify`
- Re-test the two sample PDFs (ARP_2016-07-24, CD050226USA12.pdf) and confirm no regressions
- If you have known-bad PDFs (e.g., DQ races, QH races), parse them and verify outputs

### Output
- Tagged release of chart-parser, e.g., `v2.2.0`
- pdf-importer `pom.xml` updated to depend on this version

---

## Phase 2: Fix pdf-importer bugs (2-4 days)

### Goal
Ensure pdf-importer ingestion is idempotent and complete. Fix issues that prevent clean re-ingestion or cause silent data loss.

### Required fixes (HIGH severity)

1. **IMP-T5.1 — Race upsert misses most columns**
   - In `RaceWriter.java:154-166`, expand `doUpdate()` to set EVERY race column with `EXCLUDED.col`, OR change strategy to delete-and-reinsert per race
   - This is the root cause of the off_turf/female_only/age_code backfill issue we hit earlier
   - Effort: 1 day

2. **IMP-T5.2 — `cancelled` and `races` rows can co-exist**
   - In `writeRace`, when classification flips, delete from the OTHER table on conflict
   - Add a sanity-check view: `SELECT * FROM races r JOIN cancelled c USING (track, date, number)` should be empty
   - Effort: 4 hours

3. **IMP-T5.3 — `UNIMPORTABLE` status defined but never set**
   - Add categorization step in `PdfImporter.main()` that maps known-unparseable exception classes to `UNIMPORTABLE`
   - Document the manual SQLite UPDATEs that were used to classify the 1,738 known unparseable files; commit those SQL statements to source so they can be re-applied to a fresh tracker DB
   - Effort: 4 hours

### Optional fixes (MEDIUM severity)

4. **IMP-T5.4** — `dead_heat` flag for non-win dead heats (rename to `win_dead_heat` OR extend detection)
5. **IMP-T5.5** — Add `number_of_wagering_interests` column for coupled-entry-aware counting
6. **IMP-T5.6** — Scratched horses comma-split bug
7. **IMP-T5.7** — Trainer/jockey suffix handling

### Verification

- Build pdf-importer with new chart-parser version
- Run on a small sample (e.g., 100 PDFs) and verify outputs in DB
- Re-run on the SAME PDFs (idempotency test) and verify all columns now refresh

### Output
- pdf-importer ready to run on the full PDF corpus

---

## Phase 3: Wipe + re-ingest the database (4-12 hours of compute)

### Goal
Replace the existing `handycapper` schema with freshly-ingested data using the fixed parsers.

### Steps

1. **Backup current DB:**
   ```bash
   pg_dump -h robinpc -U handycapper handycapper > backup_2026-05-27.sql.gz
   ```

2. **Wipe the schema:**
   ```sql
   DROP SCHEMA handycapper CASCADE;
   CREATE SCHEMA handycapper;
   ```

3. **Re-create base schema** (from pdf-importer's `db/schema.sql` or equivalent migration)

4. **Reset pdf-importer tracker DB:**
   ```bash
   rm tracker.db   # or back it up first to preserve UNIMPORTABLE classifications
   ```
   Then restore the UNIMPORTABLE classifications from the SQL statements committed in Phase 2 fix #3.

5. **Run pdf-importer over the full PDF corpus:**
   ```bash
   ./run-importer.sh --pdf-root /path/to/pdfs --parallel N
   ```
   - Estimated time: 4-12 hours depending on PDF count and parallelism

6. **Verify ingestion:**
   ```sql
   SELECT EXTRACT(YEAR FROM date) AS year, COUNT(*)
   FROM handycapper.races GROUP BY 1 ORDER BY 1;
   ```
   Compare counts to the pre-wipe backup. Expect ≥99% match (a few PDFs may newly succeed or newly fail with the parser changes).

7. **Run the V002/V003 migrations** (the analysis schema and views in redboarders/db/migrations/) to create `race_probabilities`, `race_metrics`, `exotic_race_legs`.

8. **Run the column backfills we did before** (off_turf, female_only, age_code) — but verify they're no longer NEEDED, since IMP-T5.1 should have ensured pdf-importer wrote them. If still needed, the fix didn't take.

### Verification queries

- Row counts per year match backup
- `off_turf`, `female_only`, `age_code` populated where expected
- No rows in both `races` and `cancelled` for the same (track, date, number)
- pdf-importer tracker shows reasonable PARSE_FAILED / WRITE_FAILED / UNIMPORTABLE counts (compare to historical)

---

## Phase 4: Fix RKM bugs (5-10 days)

### Goal
Fix all data leakage and structural issues in RKM before recomputing curves/form.

### Required fixes (HIGH severity)

1. **RKM-T1.3 — `compute_form.py` loop bound** ✅ CODE FIX APPLIED 2026-05-27
   - Decision: Option B — honor `MIN_PRIOR_RACES = 1` so 2nd-start horses get a snapshot (single-prior-race basis; downstream can weight by `n_recent_races`).
   - Loop at `compute_form.py:138` now `range(1, len(race_obs))` with comment tying it to `MIN_PRIOR_RACES` in `form.py`.
   - Pending: full recompute of `rkm_current_form` to materialize 2nd-start snapshots. Must precede Phase 5 RKM pipeline run.

2. **RKM-T1.4 — Career baseline leaks future data**
   - Refactor `compute_form_at_date` to use a TRAILING career baseline (only races before the snapshot date), not the full-career curve
   - This means the `career_v0` column in `rkm_current_form` becomes a per-snapshot computation, not a lookup from `rkm_velocity_curves`
   - Effort: 1-2 days

3. **RKM-T1.1 — Track normalization is structurally inert** (LARGE FIX)
   - Refactor `compute_curves.py` to fit per `(horse_key, surface, track, distance_zone)` instead of `(horse_key, surface, distance_zone)`
   - This requires schema change (`rkm_velocity_curves` adds a `track` column) and downstream consumers to handle multi-row-per-horse
   - Then `compute_track_offsets` can actually compute pairwise offsets
   - Effort: 3-5 days
   - **DECIDE:** is this worth it? Without it, `adj_v0` isn't truly cross-track comparable. With it, every other RKM script needs updating.

4. **RKM-T1.2 — Bare horse name joins ignore identity disambiguation**
   - In `compute_adjustments.py:36`, `compute_situations.py:40`, `race-day-sim/blinder.py:47, 73`: join on canonical `horse_key` (with birth-year suffix) not bare name
   - May require modifying upstream tables to carry `horse_key` through, or using a more elaborate join condition
   - **Hard prerequisite for RKM-T2.1 cross-zone fallback** — without it, fallback can attach wrong-horse data (verified on GP 2014-09-06: "Of Course" matched 3 horse_keys, inflating row counts)
   - Effort: 1-2 days

5. **RKM-T2.1 — Sprint/route binary cutoff** (cross-zone fallback applied 2026-05-27 — but see RKM-T1.2 dependency)
   - Stop-gap: cross-zone fallback in `blinder.py` with surface-specific shift and confidence haircut. Empirically calibrated on 117K paired starters.
   - Constants: `CROSS_ZONE_R = {Dirt: 0.38, Synthetic: 0.51, Turf: 0.25}`, `SPRINT_TO_ROUTE_V0_SHIFT = {Dirt: -2.81, Synthetic: -3.33, Turf: -4.09}`, `SPRINT_TO_ROUTE_DECAY_SHIFT = {Dirt: -1.17, Synthetic: -1.21, Turf: -0.78}`.
   - **Medium-term improvement:** finer zone partition (e.g., SHORT_SPRINT < 6f, MID_SPRINT 6-6.5f, TWEENER 7-7.5f, ROUTE ≥ 8f) — captures the 7f tweener problem and the within-zone gradient. Requires recomputing `rkm_velocity_curves` with new zone scheme.
   - **Long-term:** continuous distance model — `(v0, decay)` as smooth functions of distance instead of binned.

6. **RKM-T2.2 — `rkm_current_form` lacks surface/zone partition**
   - Current schema is `(starter_id, race_id, current_v0, ...)` — one row per starter, no surface or distance_zone. A horse with mixed sprint/route or dirt/turf recent races has a single muddled snapshot.
   - Fix: rebuild `compute_form.py` to emit one row per `(starter_id, surface, distance_zone)` and update consumers (blinder.py join, ratings.py lookup).
   - Severity MEDIUM — affects accuracy of `current_v0`/`current_decay` for zone-switchers and surface-switchers.
   - Effort: 2-3 days including form recompute.

### Optional fixes (MEDIUM severity)

7. **RKM #6** — Reconcile velocity range filters (curves.py 30-70 vs form.py 30-85)
8. **RKM #7** — Document v0 extrapolation conflation with stamina; consider a near-zero anchor
9. **RKM #8** — Resolve `slope > 0.001` clamp inconsistency between curves.py and form.py
10. **RKM #9** — Implement remaining outlier exclusion criteria from the spec (race time > 2× field mean, etc.)

### Verification

- Run a unit test fitting curves on a known horse with prior + posterior periods. Verify the trailing baseline differs from the full-career curve only in expected ways.
- Run on a small subset (one year of data) before full recompute.

---

## Phase 5: Run the RKM pipeline (4-8 hours of compute)

### Steps

1. **Phase 1: Fit career curves**
   ```bash
   python rkm/scripts/compute_curves.py
   ```
   - Estimated: 1-2 hours

2. **Phase 2: Compute track adjustments** (if RKM-T1.1 fixed)
   ```bash
   python rkm/scripts/compute_adjustments.py
   ```
   - Estimated: 30 min - 1 hour

3. **Phase 3: Per-race performance and surprise**
   ```bash
   python rkm/scripts/compute_race_performance.py
   ```
   - Estimated: 1-2 hours

4. **Phase 4: Market analysis (Benter combination)**
   ```bash
   python rkm/scripts/compute_market.py
   ```
   - Estimated: 30 min - 1 hour

5. **Phase 5: Time-weighted current form**
   ```bash
   python rkm/scripts/compute_form.py
   ```
   - Estimated: 1-2 hours
   - This is the heaviest. Watch for the loop bound fix actually firing (count of `n_recent_races = 1` rows should be substantial).

6. **Phase 6: Race situations**
   ```bash
   python rkm/scripts/compute_situations.py
   ```
   - Estimated: 30 min

### Verification

- Each phase logs row count written. Compare to expected magnitudes.
- Spot-check: pick a known horse, query their `rkm_velocity_curves` row, verify `first_race` and `last_race` make sense and `adj_v0` is in expected range.
- For `rkm_current_form`: confirm `n_recent_races = 1` rows exist (validates RKM-T1.3 fix).

---

## Phase 6: Fix wagering-analytics bugs (4-8 days)

### Goal
Fix Stern calibration, payoff model leakage, and trainer profile point-in-time issues before recomputing.

### Required fixes (HIGH severity)

1. **WA-T1.1 — Stern k calibration** ✅ CALIBRATION DONE 2026-05-27
   - Script `wagering-analytics/scripts/calibrate_stern_k.py` checked in. Run on 81K clean races (excluded coupled entries, DH/DQ in top-3 official, fields <5).
   - **Result:** Global MLE k = 0.86 (using `official_position`; `finish_position` variant gave 0.88). Field-size segmentation does not earn its keep (5-7→0.86, 8-10→0.86, 11+→0.88).
   - Code change applied: `STERN_K = 0.81` → `0.86` in `populate_stern_fair.py:29`.
   - Pending: re-run `populate_stern_fair.py --recompute-all` to refresh `exotic_harville_ratios.stern_fair` with the corrected value (DB tunnel dropped mid-refresh — k=0.87 partial value sits in DB now).
   - Caveat: calibration uses tote-implied win probabilities; the calibrated k inherits favorite-longshot bias from closing odds.
   - **All wagering-analytics calibration scripts must use `official_position`, not `finish_position`** — exotic payoffs settle against the official order.
   - Severity reclassified MEDIUM (bias from 0.81 vs 0.86 is small on a flat LL surface).

2. **WA-T1.2 — Payoff model R² inflation** ✅ VERTICALS VERIFIED 2026-05-27
   - Verification script: `wagering-analytics/scripts/verify_payoff_skill.py` (year-stratified, train<2016, test 2016-2017).
   - Pre-race full model adds +0.041 R² (exacta) / +0.074 R² (trifecta) above naive Stern — **real learned skill**, not tautology.
   - Pending: refactor `fit_payoff_models.py` to year-stratified split; report ΔR² above naive in `payoff_coefficients.json`. Note: 2018+ has only 429 races, use 2016-2017 as test window.
   - Severity reclassified MEDIUM (audit's "mostly tautological" framing was wrong).

3. **WA-T1.3 — Payoff model uses post-race features** ✅ FULLY DISPROVEN 2026-05-27
   - Verticals: fav_* features +0.001 R² (EXACTA, TRIFECTA).
   - Pick 3/4: `verify_payoff_skill_horizontal.py`. bad_fav_legs ΔR² +0.0003 / +0.0004 — predictively inert.
   - Pick 5/6: `verify_payoff_skill_pick56.py`. bad_fav_legs ΔR² +0.001 / -0.001 — predictively inert.
   - **Bonus finding (Pick 3/4):** OLS barely beats naive parlay. Pick 3 +0.003 R²; Pick 4 +0.027 R². Could simplify to `expected_payoff = (1 - takeout) × Π(odds_i + 1)`.
   - **CRITICAL finding (Pick 5/6):** Naive parlay is useless (Pick 5 R²=0.26, Pick 6 R²≈0). `log_carryover` is the single largest predictor (+0.31-0.33 R² above naive parlay). Existing `fit_payoff_models.py:183-184` ACTIVELY EXCLUDES carryover rows for Pick 6, throwing away the variation needed to learn pool dynamics. Pick 5 includes all rows but has no carryover feature, so can't distinguish carryover-vs-not.
   - **Race-day-sim needs Pick 5/6 working in two distinct +EV cases:** (a) carryover days (effective takeout reduction); (b) structural-edge days where vulnerable-favorite plays or contrarian construction create payoff differential vs the public's chalk ticket. Both require a payoff model that understands pool dynamics — neither is served by the existing models.
   - Pending fixes:
     (a) drop fav_*/bad_fav_legs from feature sets across all bet types (cleanliness, no R² impact);
     (b) **Pick 5/6 OLS rebuild: include all `pool_type = STANDARD` rows, add `log_carryover` as a feature, exclude only `pool_type = JACKPOT`**;
     (c) optionally simplify Pick 3/4 to formulaic parlay × (1-takeout).
   - Severity LOW for bad_fav_legs concern across all types. HIGH urgency for Pick 5/6 OLS rebuild — the existing models are unfit for either +EV use case.

4. **WA-T1.4 — Trainer profiles aggregate, not point-in-time** ✅ VERIFIED 2026-05-27
   - Structural future-leakage confirmed in `trainer_ae_profiles` (Calhoun: full-career A/E 0.870 vs as-of-2010 A/E 0.916, delta 0.046).
   - **No live leakage in race-day-sim:** the static table is never queried by any code path. `blinder.py:load_market_bias` uses point-in-time CTEs (date < race_date) for all 5 dimensions — already correct.
   - Pending: (a) drop the static `trainer_ae_profiles` table (one-line fix, eliminates footgun); (b) update race-day-sim CLAUDE.md which incorrectly lists `trainer_ae_profiles` as a dependency (it isn't — `load_market_bias` computes everything fresh).
   - Effort: 30 minutes.

5. **WA-T1.5 — Jitter calibration measures wrong quantity** ✅ VERIFIED 2026-05-27
   - Confirmed broken (measures inter-sequence winner odds spread, not within-race drift). σ≈1.0 across all legs is √2 × within-leg log-odds spread, not jitter.
   - Confirmed unused: `horizontal.py:29` defines `get_leg_sigma` but no code calls it; `estimate_horizontal_value` uses leg odds directly.
   - **Architectural insight:** jitter is a non-problem for blinded backtests. The blinder loads closing odds for every leg before bets are constructed, so there's no future-leg uncertainty to model. Jitter only matters for live betting (not on roadmap).
   - Pending: delete `get_leg_sigma` from `horizontal.py`, delete `models/jitter_calibration.json`, delete `wagering-analytics/scripts/compute_jitter_calibration.py`. Revisit only if live-betting mode is added.
   - Effort: 15 minutes.
   - Real fix requires intra-race odds time series (not in the database). Defer.
   - Effort: 1 hour to add a deprecation warning

### Optional fixes (MEDIUM severity)

6. **WA-T1.6** — Default takeout fallback bet-type-aware
7. **WA-T1.7** — Coupled entries / dead heats handling
8. **WA-T1.8** — Surface dummy regression bug (silent feature drop)
9. **WA-T1.9** — Claim query double-counts horses claimed multiple times

### Verification

- Calibration: run k grid search, plot residuals
- Payoff: confirm new (post-race-feature-removed) R² is plausible
- Trainer profiles: verify race-day-sim's load_market_bias() still works without the static table

---

## Phase 7: Run the wagering-analytics pipeline (1-2 hours of compute)

### Steps

1. **AN1 Phase 1-4: Stern fair value population**
   ```bash
   python wagering-analytics/scripts/populate_stern_fair.py
   ```
   - Estimated: 30 min

2. **AN1 Phase 5: Jitter calibration**
   ```bash
   python wagering-analytics/scripts/compute_jitter_calibration.py
   ```
   - Estimated: 5 min

3. **AN1 Phase 6: Payoff model fitting**
   ```bash
   python wagering-analytics/scripts/fit_payoff_models.py
   ```
   - Estimated: 5 min

4. **AN2 Phase 1: WCMI computation**
   ```bash
   python wagering-analytics/scripts/compute_wcmi.py
   ```
   - Estimated: 1-2 min (we showed it runs in 72 seconds)

5. **AN2 Phase 2: Trainer A/E profiles**
   ```bash
   python wagering-analytics/scripts/compute_trainer_profiles.py
   ```
   - Estimated: 1 min (we showed it runs in 47 seconds)

### Verification

- Each script reports row count written
- Spot-check: pick a known trainer, verify their profile values are plausible
- WCMI: distribution should be 0.05-0.30 range, with mean ~0.13-0.15

---

## Phase 8: Fix race-day-sim bugs (3-6 days)

### Goal
Fix the rating computation, ticket construction, and protocol enforcement issues so the sim actually produces honest edges and validates bets correctly.

### Required fixes (HIGH severity)

1. **RDS-T1.1 — TEMPERATURE = 6500ms produces nearly-uniform probabilities** ✅ VERIFIED 2026-05-27
   - Empirical within-race predicted-time spreads (581 sample races): mean max-min = 2,805 ms (~14 lengths), stddev = 972 ms.
   - At T=6500, an 8-horse field with 14-length spread produces probabilities of ~15%/14%/.../10% (fastest:slowest ratio 1.5:1) — nearly uniform.
   - Cascade through Benter: model term contributes ~zero log-difference, so combined output ≈ market echo. Likely a major contributor to -42% ROI.
   - Immediate fix: change `TEMPERATURE = 6500.0` → `TEMPERATURE = 1000.0` in `probability.py:17`. With T=1000, same 14-length-spread field produces 34%/23%/15%/.../2% — realistic dispersion.
   - Follow-up: fit T jointly with Benter α via MLE on historical race outcomes for a properly calibrated value.
   - Effort: 5 minutes for the constant change; ~1 day for proper MLE calibration.

2. **`odds_to_rating` rank-mapping issue (edge-calibration-issue.md)**
   - Refactor edge computation to use probability space directly
   - Display edge as % overlay alongside rating points
   - Effort: 1-2 days

3. **RDS-T1.2 — Off-turf credit applied to entire field** ✅ FIXED 2026-05-27
   - `bias_multiplier(is_favorite=...)` parameter added; off-turf credit gated to favorite only.
   - Lift raised from 1.050 → 1.075 to match research finding 9 (+7.5% favorite lift).
   - `format_race_ratings` identifies favorite by lowest closing_odds and passes flag through.
   - Pending follow-up: "fade turf-only horses underneath" — needs each horse's recent surface history; separate signal access path required.

4. **RDS-T1.3 — Turf rating prior double-counts surface offset** ✅ FIXED 2026-05-27
   - Replaced single class-rating dict with `_CLASS_RATINGS_MAIN` (Dirt + Synthetic, anchor 100) and `_CLASS_RATINGS_TURF` (main + 12 universal-scale offset, anchor 112).
   - Naming reflects `_CANONICAL_PARAMS` grouping — Dirt and Synthetic both anchor at 100, only Turf elevates.
   - Removed `base += 5` fudge from `compute_prior_rating`. Priors now anchor correctly: 100 (dirt/synthetic) / 112 (turf), matching `_get_anchor` on each surface.
   - Verified: synthetic $25K CLM prior = 105, turf $25K CLM = 117 (= 105 + 12); synthetic ALW = 114, turf ALW = 126.
   - This was affecting every turf rating, not just prior-only horses (`format_race_ratings` blends `w × physics + (1-w) × prior`).
   - **Note on uniform +12:** The offset is uniform across distances because the canonical anchor table calibrates turf at 112 across all fitted distances (5f-9f). If turf class structure ever turns out to vary by distance (e.g., 5f turf is less elevated than 9f turf), the right place to express that is in `_CANONICAL_PARAMS` (per-distance anchor rating), not in the prior ladder. The prior follows the anchor automatically via the offset constant.

5. **RDS-T1.4 — Surface-switch trainer A/E double-counts** ✅ FIXED 2026-05-27
   - Captured generic surface-switch multiplier in `surface_switch_mult` local; trainer-specific block now divides by it before multiplying by `trainer_switch_ae / BASELINE_AE`. Mirrors class-drop pattern.
   - Verified with 4 sanity cases including the double-count case (trainer matching population average): now 1.075 instead of 1.156.

6. **RDS-T1.5 — Horizontal parlay_prob unnormalized for overround** ✅ FIXED 2026-05-27
   - Added `all_odds` API param to leg dict; `estimate_horizontal_value` now normalizes by field overround when supplied (falls back to biased raw sum with warning if not).
   - Direction correction vs audit: raw `1/(odds+1)` sums to ~1.17 with 17% takeout (over-estimates each horse's true prob), so unnormalized parlay_prob was over-estimated and fair_payoff was UNDER-estimated. System was too pessimistic by ~36% on Pick 3, not too optimistic.
   - Currently unused by `simulate_race_day.py`, so no immediate behavioral change — fix is preventive for when ticket-construction logic is wired in.

7. **PROTO-T3.1 — `register_bet()` performs zero validation**
   - Add validation: programs in race, structural validity, bet type whitelist, pool minimums, win-bet odds floor
   - Critical because: extends the evaluator to handle TRIFECTA, SUPERFECTA, PICK_N (currently anything past EXACTA is silently MISS)
   - Effort: 2 days

### Optional fixes (MEDIUM severity)

8. **PROTO-T3.2 — Equity test gating** ✅ FIXED 2026-05-28 (soft warnings in `register_bet`).
9. **PROTO-T3.3 — Horizontal equity formula** ✅ AUDIT-DISPROVEN 2026-05-28. Formula was numerically correct; docstring tightened.
10. **PROTO-T3.4 to T3.13** — Press mechanic, opinion classification, FTS rule alignment, place-bet block (already enforced via `_FORBIDDEN_BET_TYPES`), pool minimum thresholds, horizontal qualification check, etc. See audit doc Tier 3.
9. **RDS H1** — Exotic payoff dilution math
10. **RDS H5** — `kelly_exotic` formula
11. **Cross-module edge naming consistency**

### Verification

- Re-run a previously-failed sim day (e.g., one of our 7 sim days) with the same seed
- Compare ratings, edges, and bet outcomes side-by-side with pre-fix output
- The TEMPERATURE fix alone should produce noticeably different probability distributions
- Edge values should be in the realistic +3% to +15% range, not +30 to +50

---

## Phase 9: Validation Sims (ongoing)

### Goal
Run multiple sim days with the rebuilt system to verify:
1. Edges are now honest (closer to 0 than the old +30 amplitudes)
2. Conviction picks actually win at rates consistent with the model's stated probabilities
3. Bet evaluation correctly grades all bet types

### Steps

1. Run 10-20 random sim days using `scripts/run_simulation.py --seed`
2. Track aggregate stats: total invested, total returned, hit rate by bet type, edge calibration (do horses with +5% edge actually win 5% more than odds imply?)
3. Use the same seeds as before-rebuild so direct comparisons are possible

### Success criteria

- Edge magnitudes look honest (no +30 outliers from rank-mapping)
- TRIFECTA / PICK_N bets actually grade correctly (not silently MISS)
- Conviction picks (worst-case edge > 5%) win at rates ≥ implied probability
- Sim day summaries show meaningful information ("3 conviction picks, 1 hit, ROI -45%") not just "100% loss across the board"

If results suggest the model still doesn't have edge: the audit fixes weren't enough, and the root issue is calibration, not bugs. That's a different conversation.

---

## Cross-Phase Activities

### Documentation hygiene (do continuously)

- After each phase, update CLAUDE.md / README.md in the affected repo
- Update docs/cross-repo-audit-2026-05-27.md with status (mark fixes as VERIFIED or DEFERRED)
- Update handycapper-schema.md to reflect actual schema after the rebuild
- Delete or rewrite race-replay/docs/design.md (significantly stale)
- Mark archived race-day-simulation.md as archived if not already

### Dependency tracking

- Tag releases at each major milestone (chart-parser v2.2.0, pdf-importer post-Phase-2, etc.)
- Document version compatibility (which RKM version goes with which wagering-analytics version)

### Test fixture expansion

- For chart-parser, add fixtures covering edge cases discovered during the audit (multi-DQ, QH long-distance, comma-in-amount)
- For race-day-sim, capture before/after sim outputs for a few seeds as regression tests

---

## Risk Mitigation

### What could go wrong

1. **The chart-parser fixes break previously-working races.** Mitigation: keep the test fixtures, expand them, run before/after diff on a sample of 100 PDFs.

2. **Re-ingestion takes way longer than expected.** Mitigation: parallelize, use a beefier robinpc setup if possible, run during off-hours.

3. **Some "verified" findings turn out to be wrong (the audit was code review, not DB inspection).** Mitigation: each fix has a verification step. If the metric doesn't move as expected, investigate before proceeding.

4. **The RKM track normalization rewrite (RKM-T1.1) takes much longer than estimated.** It's a structural change that touches every downstream consumer. Mitigation: defer it to Phase 4b (post-rebuild). The system worked without it for years; can continue to.

5. **The model still doesn't show edge after all fixes.** Possible — the model's premise (velocity curves predict winners) may be partially correct but not strong enough to overcome takeout. The rebuild eliminates BUGS as a cause but doesn't guarantee EDGE. Acceptance criterion: honest output, not profitable output.

### Rollback plan

- Phase 3 wipe is reversible via `pg_restore` from backup
- All code changes are in git; revert via branch
- The PDF corpus is untouched throughout

---

## Summary Timeline

| Phase | Days | Cumulative |
|---|---|---|
| 0: Decisions and prep | 0.5 | 0.5 |
| 1: Fix chart-parser | 4-7 | 4.5-7.5 |
| 2: Fix pdf-importer | 2-4 | 6.5-11.5 |
| 3: Wipe + re-ingest | 0.5 (mostly compute time) | 7-12 |
| 4: Fix RKM | 5-10 | 12-22 |
| 5: Run RKM pipeline | 0.5 | 12.5-22.5 |
| 6: Fix wagering-analytics | 4-8 | 16.5-30.5 |
| 7: Run wagering-analytics | 0.25 | 16.75-30.75 |
| 8: Fix race-day-sim | 3-6 | 19.75-36.75 |
| 9: Validation sims | ongoing | — |

**Realistic total: 3-5 weeks of focused work.**

If you cut to "minimum viable rebuild" (skip RKM track normalization, skip Stern calibration, defer all MEDIUM fixes), you could compress this to ~10-14 days.

---

## What I Need From You at Each Phase

- **Phase 0:** Confirm scope decision (minimum viable / targeted / full)
- **Phase 1:** Review chart-parser test results before tagging release
- **Phase 2:** Review pdf-importer behavior on small sample before full ingestion
- **Phase 3:** Approval to wipe DB + monitor ingestion logs
- **Phase 4:** Review RKM-T1.1 (track normalization) decision — fix or defer?
- **Phase 6:** Review WA-T1.1 (Stern calibration) decision — fix or defer?
- **Phase 8:** Approval to push fixes to race-day-sim
- **Phase 9:** Decide if validation sim results are good enough to call "done"
