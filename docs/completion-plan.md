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

| ID | Question |
|---|---|
| PROTO-T3.4 | Press mechanic — code multi-cost combos (`Bet.combinations` with per-combo multipliers and a `Basket` class) or keep as judgment guidance? |
| PROTO-T3.7 | Two scaffolds (`run_simulation.py` vs `simulate_race_day.py`) — consolidate or keep both? |
| PROTO-T3.9 | ITP concepts (`kill_shot`, `hurdle`, `basket`, `win_only`) — encode as enforceable rules or document as guidance? |

**Recommendation:** Single-session conversation covering all three. They share a common axis: "what belongs in code vs in the bettor's head?" Likely outcomes: T3.4 → code press support; T3.7 → consolidate to `run_simulation.py`; T3.9 → encode `kill_shot` as a structural validation, leave the others as guidance.

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

| ID | Description | Effort |
|---|---|---|
| RKM-T1.4 | Career baseline future leak in `v0_trend` (still confirmed but unaddressed) | ~1 day |
| RKM-T2.2 | `rkm_current_form` lacks surface/zone partition (one row per starter mixes sprint/route, dirt/turf) | ~2 days |
| WA #7 | Claim query double-counts horses claimed multiple times (per-claim ROW_NUMBER, not per-horse) | ~30 min |
| WA #8 | Drop/layoff filtered to dirt/fast only; other dimensions aren't (composites incoherent) | ~30 min |
| WA #9 | Dimensions are not independent (layoff×drop, layoff×switch overlap; composite scoring misuses) | ~half day, design work |
| WA #11/#13 | Coupled entries treated as independent everywhere — V003, Stern, payoff, WCMI, trainer A/E (~3-5% of US races affected) | ~half day |
| WA #14 | Surface dummies all-zero in EXACTA/TRIFECTA OLS models (NaN p-values; need to re-fit) | ~1 day |
| WA #15 | No winsorization for extreme payoffs in OLS bet types | ~30 min |
| WA #16 | `jock_upgrade` claimed as 6th dimension but never computed (placeholder zeros) | ~1 day |
| WA-T1.3 follow-up | Pick 5/6 OLS rebuild with `log_carryover` feature (existing models exclude carryover, making them unfit for the actual play-decision use case) | ~1 day |
| RDS H5 | `kelly_exotic` formula incorrect (under-stakes, dead code currently) | ~30 min if anyone calls it; otherwise just delete |
| Cross-cutting | Date range chaos — 1991-2017 vs 1999-2017 vs 1997-2016 vs 2005-2017 across scripts | ~1 day |
| RKM #7 | v0 extrapolation conflation with stamina (no near-zero anchor) | ~1 day, requires curve refit |
| RKM #8 | `slope > 0.001` clamp inconsistency between curves.py and form.py | ~30 min |
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
