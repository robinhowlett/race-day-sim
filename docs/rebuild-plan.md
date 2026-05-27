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

1. **RKM-T1.3 — `compute_form.py` loop bound**
   - Change `for i in range(2, len(race_obs))` to `for i in range(MIN_PRIOR_RACES, len(race_obs))`
   - Effort: 5 minutes (already noted as critical to do BEFORE the recompute)

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
   - In `compute_adjustments.py:36` and `compute_situations.py:40`, join on canonical `horse_key` (with birth-year suffix) not bare name
   - May require modifying upstream tables to carry `horse_key` through, or using a more elaborate join condition
   - Effort: 1-2 days

### Optional fixes (MEDIUM severity)

5. **RKM #6** — Reconcile velocity range filters (curves.py 30-70 vs form.py 30-85)
6. **RKM #7** — Document v0 extrapolation conflation with stamina; consider a near-zero anchor
7. **RKM #8** — Resolve `slope > 0.001` clamp inconsistency between curves.py and form.py
8. **RKM #9** — Implement remaining outlier exclusion criteria from the spec (race time > 2× field mean, etc.)

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

1. **WA-T1.1 — Stern k calibration**
   - Build a calibration script: grid-search k from 0.5 to 1.2, segmented by field size and surface. Validate against actual finish frequencies.
   - Replace the hardcoded `STERN_K = 0.81` with the calibrated value (or table lookup if segmented)
   - Effort: 2-3 days
   - **DECIDE:** can defer if you accept k=0.81 with documented caveats.

2. **WA-T1.2 — Payoff model R² inflation**
   - Refactor `fit_payoff_models.py` to use year-stratified train/test split (e.g., train on 1999-2014, test on 2015-2017)
   - Add naive baseline (`log_payoff = -log(p1*p2*p3) + const`) for skill comparison
   - Effort: 1 day

3. **WA-T1.3 — Payoff model uses post-race features**
   - Drop `bad_fav_legs`, `fav_won`, `fav_position`, etc. from feature set
   - Re-fit with pre-race-only features
   - Document the new (lower) R² as the actual forward-predictive number
   - Effort: 1 day

4. **WA-T1.4 — Trainer profiles aggregate, not point-in-time**
   - Either: (a) build a materialized view computed point-in-time per starter (heavy storage), OR (b) ensure all consumers compute trainer A/E at simulation time via point-in-time CTEs (current `load_market_bias` does this; just don't use the static `trainer_ae_profiles` table for live decisions)
   - Recommendation: option (b). The static table stays for research; race-day-sim uses load_market_bias's CTEs
   - Effort: 1-2 days (mostly documentation and removing the import path)

5. **WA-T1.5 — Jitter calibration measures wrong quantity**
   - Document as known limitation. Race-day-sim should not use jitter values for horizontal pool projection until methodology is fixed.
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

1. **RDS-T1.1 — TEMPERATURE = 6500ms produces nearly-uniform probabilities**
   - Calibrate properly using the freshly-recomputed RKM data
   - Likely target: 200-500ms range
   - Effort: 1 day (calibration + verification)

2. **`odds_to_rating` rank-mapping issue (edge-calibration-issue.md)**
   - Refactor edge computation to use probability space directly
   - Display edge as % overlay alongside rating points
   - Effort: 1-2 days

3. **RDS-T1.2 — Off-turf credit applied to entire field**
   - Constrain the +5% multiplier to favorite only
   - Add a separate negative multiplier for turf-only horses on dirt
   - Effort: 2 hours

4. **RDS-T1.3 — Turf rating prior double-counts surface offset**
   - Remove the +5 offset in compute_prior_rating; align with canonical anchor
   - Effort: 1 hour

5. **RDS-T1.4 — Surface-switch trainer A/E double-counts**
   - Mirror the class-drop logic that undoes the generic before applying trainer-specific
   - Effort: 2 hours

6. **RDS-T1.5 — Horizontal parlay_prob unnormalized for takeout**
   - Normalize by full-field overround
   - Effort: 2 hours

7. **PROTO-T3.1 — `register_bet()` performs zero validation**
   - Add validation: programs in race, structural validity, bet type whitelist, pool minimums, win-bet odds floor
   - Critical because: extends the evaluator to handle TRIFECTA, SUPERFECTA, PICK_N (currently anything past EXACTA is silently MISS)
   - Effort: 2 days

### Optional fixes (MEDIUM severity)

8. **PROTO-T3.2 to T3.13** — Equity test gating, press mechanic, opinion classification, etc. See audit doc Tier 3.
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
