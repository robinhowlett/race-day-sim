# Completion Plan

State of the audit work as of 2026-05-28, and what's left to reach full closure.

## Where we are

Two days of substantive work shipped across rkm, wagering-analytics, race-day-sim. Tier 1 (system-breaking) is essentially closed in code; Tier 3 (protocol/code alignment) is closed except for design questions; Tier 2 has had its highest-impact items addressed. Several findings turned out to be audit-disproven on closer inspection. The single highest-value unaddressed finding is one that surfaced from yesterday's empirical work, not the original audit: **longshot conviction skew vs the favorite-longshot bias**.

## Inventory of remaining work

Five categories, in rough order of impact-per-effort.

### Category A — DB recomputes (waiting on operator)

These are code-fixes-shipped that need a long-running compute job to materialize. No code work needed.

| ID | Action | Compute time |
|---|---|---|
| RKM-T1.3 | Re-run `rkm/scripts/compute_form.py` to materialize 2nd-start snapshots in `rkm_current_form` | 1-2 hrs |
| WA #19 propagation | Optionally re-run `wagering-analytics/scripts/compute_trainer_profiles.py` if anyone reads the static `trainer_ae_profiles` table (currently no one does — it's audit-WA-T1.4 dead weight) | 30 min |

**Recommendation:** Do RKM-T1.3 recompute. Skip the trainer_profiles refresh since the table is unused anyway (and we recommend deleting it).

### Category B — Deferred design decisions (need conversation, not code)

**Closed 2026-05-28:**
- T3.4 (press) — closed earlier via decomposition pattern + `_press_notes()`.
- T3.7 (scaffolds) — closed earlier via consolidation; `simulate_race_day.py` deleted.
- T3.9 basket — closed via "tag, don't wrap": optional `basket_id` field on `Bet`, aggregate-exposure note + per-basket P&L rollup. Full `Basket` class no longer planned.
- T3.9 win-only — closed via empirical multi-dim validation (TB 2010-2017, n>1.6M). The discriminating axis turned out to be **sprint vs route**, not pace × distance × surface × age. Speed-fade horses (top quintile of field by both adj_v0 AND adj_decay) finish 2nd/3rd ~10% LESS often than 1st in sprints across all surfaces (under_to_win 0.75-0.90); route-race speed-fade horses show no asymmetry. `_win_only_notes` fires for EXACTA/TRIFECTA under legs in sprint races only. Cat B now closed.

### Category C — Substantive new finding (highest priority, unaddressed)

**RDS-T2.x — Longshot conviction skew runs against favorite-longshot bias (FLB).**

- 49% of conviction picks at ≥15/1 odds, median 14.8/1
- Empirical FLB says longshots win less than odds-implied probability
- The model is calling "edge" predominantly in the part of the distribution where the market is biased *against* longshots winning
- **Strong candidate for explaining the documented -42% ROI**

Three priority responses:

1. **(Long-term, principled)** FLB correction at rating-to-edge translation. Calibrate from historical strike-rate buckets — for each odds tier, measure model-predicted vs actual win rate, fit a shrinkage factor. Requires 2-3 days of empirical work.
2. **(Interim, defensible)** Tighten conviction threshold by odds tier: `worst > 0` for chalk, `worst > 5` at 7-15/1, `worst > 10` at 15/1+. ~2 hours of work.
3. **(Immediate, UI nudge)** Surface odds tier in conviction display with a verification prompt. ~30 minutes.

**Recommendation:** Do (3) immediately. Do (1) as a focused project. Skip (2) — it's a band-aid.

### Category D — Tier 2 remaining (smaller items)

**Closed 2026-05-28 (Tier 2 quick wins):**
- WA #7 — claim CTE now dedupes by starter_id, picks most-recent claim. 11.4% over-counting eliminated.
- WA #8 — CLAIM filter aligned to Dirt/Fast (matches DROP/LAYOFF); SWITCH stays cross-surface by design with inline rationale.
- WA #15 — `fit_payoff_models.py` winsorizes payoff at 99.5th percentile before log; new `WINSOR_PCT` constant.
- RKM #8 — `POSITIVE_SLOPE_CLAMP_THRESHOLD` extracted to `curves.py`, imported by `form.py`. Asymmetric handling (reject vs clamp) preserved with docstring rationale.
- RDS H5 — `kelly_exotic` deleted (dead code; formula was wrong; `size_bets` is the canonical exotic sizer).

**Still open:**

| ID | Description | Effort |
|---|---|---|
| RKM-T1.4 | Career baseline future leak in `v0_trend` (still confirmed but unaddressed) | ~1 day |
| RKM-T2.2 | `rkm_current_form` lacks surface/zone partition (one row per starter mixes sprint/route, dirt/turf) | ~2 days |
| WA #9 | Dimensions are not independent (layoff×drop, layoff×switch overlap; composite scoring misuses) | ~half day, design work |
| WA #11/#13 | Coupled entries treated as independent everywhere — V003, Stern, payoff, WCMI, trainer A/E (~3-5% of US races affected) | ~half day |
| WA #14 | Surface dummies all-zero in EXACTA/TRIFECTA OLS models (NaN p-values; need to re-fit) | ~1 day |
| WA #16 | `jock_upgrade` claimed as 6th dimension but never computed (placeholder zeros) | ~1 day |
| WA-T1.3 follow-up | Pick 5/6 OLS rebuild with `log_carryover` feature (existing models exclude carryover, making them unfit for the actual play-decision use case) | ~1 day |
| Cross-cutting | Date range chaos — 1991-2017 vs 1999-2017 vs 1997-2016 vs 2005-2017 across scripts | ~1 day |
| RKM #7 | v0 extrapolation conflation with stamina (no near-zero anchor) | ~1 day, requires curve refit |
| RKM #9 | Outlier exclusion criteria from spec (race time > 2× field mean, etc.) not implemented | ~half day |

### Category E — Different repos / out of scope for race-day-sim core

These are real findings but live in upstream repos. Fixing them changes the data quality but not the simulator's behavior on existing data.

- **Tier 4 — Shared docs (12 findings)**: doc drift, low-risk cleanup. Skip unless aesthetic
- **Tier 5 — pdf-importer (9 findings)**: Java/Kotlin repo, would require switching tooling. Affects re-ingest quality only
- **Tier 6 — chart-parser (12 findings)**: same as Tier 5
- **Tier 7 — Trip classification proposal**: forward-looking feature spec, not bugs
- **Tier 8 — race-replay (13 findings)**: separate Node.js app, doesn't affect race-day-sim

## Recommended sequence to full completion

### Sprint 1 (this week, 1-2 sessions)

1. **Conversation: Tier 3 design decisions (T3.4 / T3.7 / T3.9)** — half a session
2. **Implement decisions from Sprint 1 step 1** — half a session
3. **Add odds-tier display to conviction picks** (longshot UI nudge — RDS-T2.x option 3) — 30 min

### Sprint 2 (next week, 2-3 sessions)

4. **Tier 2 quick fixes**: WA #7, #8, #15, RKM #8, RDS H5 dead code cleanup — 1 session
5. **WA #11/#13 coupled entries sweep** — focused half-session per repo
6. **Date range chaos audit + standardization** — half session

### Sprint 3 (following week, 2-3 sessions)

7. **RKM-T1.4 fix**: rewrite `compute_form_at_date` to use trailing career baseline. **Run the form recompute.** — 1 session (+ overnight compute)
8. **WA #14 surface dummies investigation + re-fit** — 1 session
9. **WA-T1.3 Pick 5/6 OLS rebuild** with carryover feature — 1 session
10. ~~**PROTO-T3.9 win-only encoding**~~ — done 2026-05-28 in Cat B. The discriminating axis was sprint vs route; pace × age didn't add signal. Encoded as `_win_only_notes` in `run_simulation.py`.
11. **Choice-rank FLB curve** — for each `choice` rank (1, 2, 3, ...), measure A/E (actual wins ÷ implied-from-odds wins). Tells us whether the favorite label specifically attracts a default-bet pattern beyond what the general FLB curve implies, or whether all ranks are smoothly calibrated by the public. Informs whether per-rank shrinkage factors are needed (in addition to the per-odds-tier FLB correction in Sprint 4). ~half session.
12. **PROTO-T3.9 basket detection** — aggregate exposure across multiple bets sharing a primary key. Compute per-bet EV contribution and flag redundant equity (multiple bets capturing the same probability mass). Catches the "+3 edge conviction expressed as 5 bets" over-investment trap. ~30 min once Sprint 1-2 hurdle/exposure scaffolding is in place.

### Sprint 4 (the substantive piece, multi-session)

10. **RDS-T2.x FLB correction** — empirical calibration of odds-bucket shrinkage factor, applied at rating-to-edge translation. This is the work most likely to move ROI. 2-3 sessions of focused empirical work.

### Sprint 5 (validation, multi-session)

11. **Multi-day sim batch** with all fixes applied. 50+ days, varying tracks. Track P&L, conviction-pick accuracy, edge calibration.
12. **Iterate** on what the multi-day data shows.

### Sprint 6 (optional polish)

13. RKM-T2.2 `rkm_current_form` partition rebuild (medium-term, not blocking)
14. WA #9 dimension independence (design work)
15. RKM #7 v0 extrapolation
16. RKM #9 outlier exclusion

## What "full completion" means

Three possible end-states, depending on where you draw the line:

**(a) Audit fully closed:** every Tier 1-6 finding marked FIXED / DISPROVEN / DEFERRED with explicit rationale. Sprints 1-4. ~3 weeks of focused work.

**(b) System validated:** (a) plus Sprint 5 multi-day batch confirming the cumulative changes have improved measurable outcomes. ~5 weeks total.

**(c) Production-ready system:** (b) plus Sprint 6 polish, plus any new findings Sprint 5 surfaces. ~6-8 weeks.

**Recommendation: aim for (b).** Stopping at (a) means closing the audit without verifying that the closing actually improved the system. Going past (b) is diminishing returns until you have multi-day evidence of where the remaining ROI gap actually lives.

## What's NOT in this plan

- **Re-ingest from PDFs.** That's a separate path ("rebuild from scratch") and is its own multi-week project. The audit findings can be addressed against the existing DB.
- **New features** (live mode, extended bet types, ML enhancements). These are forward-looking; the audit is about closing the existing gap.
- **Tier 5/6/8 (separate repos).** Their findings don't affect race-day-sim's behavior on existing data, only the quality of future re-ingests.
- **The takeout PDF parsing future enhancement.** Captured in audit; deferred indefinitely until takeout precision becomes binding.
