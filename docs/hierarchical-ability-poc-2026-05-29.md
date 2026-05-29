# Hierarchical Horse-Ability Model — POC Findings

**Status:** Investigation complete. Recommendation: defer the architectural rebuild. POC code preserved in `scripts/poc/hierarchical-ability/` for reference.

**Date:** 2026-05-29

## Question this POC was set up to answer

The audit's RKM-T1.1 finding raised a structural concern: the velocity-curve layer in `rkm_velocity_curves` partitions only by `(horse, surface, distance_zone)`, with track speed corrected via a single per-track offset. Other context dimensions — track condition, race class, age, sex, state-bred restriction — are silently averaged into each horse's stored `adj_v0`, and the downstream `ratings.py` patches them empirically post-hoc.

The first-principles question: **can a hierarchical mixed-effects model on canonical-anchored time residuals produce a cleaner horse-ability signal than the live pipeline, and would that translate into within-race winner-prediction lift?**

## What we built

Three iterations, captured in `scripts/poc/hierarchical-ability/`:

| Iteration | Slice | Response | Result |
|---|---|---|---|
| **POC v0** | NYRA 2014, race-level holdout | absolute `v0` with free intercept | Failed — top-horse list dominated by mid-pack claiming horses; v0 alone is not "ability" because it's the regression intercept of velocity-vs-distance, biased toward early-race speed. |
| **POC time** | NYRA 2014, race-level holdout | canonical-time residual | Reasonable — top horses recovered (Tonalist, Wise Dan, Frosted etc. surface to top tier), context coefficients came out correct direction. Holdout pick-winner: **18.3%** vs live **19.8%**. |
| **POC extended** | NYRA 2010-2016 → 2017 year-out | canonical-time residual | Strong top-horse list (Frosted, Tonalist, American Pharoah, Liam's Map). Pick-winner: **20.5%** vs live **23.3%**. |
| **POC SoCal** | SA/DMR/HOL/BHP 2010-2016 → 2017 | canonical-time residual | Spectacular top-horse list (Game On Dude, Shared Belief, California Chrome, Beholder, Twirling Candy, Mucho Macho Man — all genuine SoCal stars in correct order). Pick-winner: **22.1%** vs live **25.6%**. |

## What worked

The hierarchical framework is **structurally sound** when paired with the right response variable:

1. **Canonical-time residual is the right response.** v0 alone confuses early-speed-with-fade horses (high v0, high decay) and late-grinder stamina horses (low v0, low decay). Canonical-time combines both into one ability metric.

2. **Top horses are recovered correctly.** On SoCal 2010-2016, the top-20 list reads like a SoCal stakes program of the era — Game On Dude #3, Shared Belief #7, California Chrome, Beholder, Mucho Macho Man, Will Take Charge all in their right tier. On NYRA, Frosted, Tonalist, American Pharoah, Liam's Map, Royal Delta, Wise Dan all in the top tier.

3. **Context coefficients come out correct.** Class effects properly ordered (graded > ungraded > allowance > claiming > maiden); off-going adds 0.7-1.3 sec to finish time (ms-positive translates to slower); state-bred restrictions slow horses 0.1-0.6 sec depending on circuit (NYRA wider gap than SoCal); age progression sensible.

4. **Track-effect direction is correct where the live pipeline has it inverted.** On NYRA dirt 2014, the POC recovered BEL > SAR > AQU (matching observed mean ft/s of 55.31, 55.10, 54.14), while the live `rkm_track_offsets` table has the opposite direction. This is a real bug in the live pipeline — see [RKM-T1.1 in the cross-repo audit](cross-repo-audit-2026-05-27.md).

## What didn't work

**On the headline metric — within-race winner prediction — the live pipeline beats the POC by 1.5-3.5pp consistently across slices.**

| Slice | POC pick-winner | Live pick-winner | Gap |
|---|---|---|---|
| NYRA 2014 (single year) | 18.3% | 19.8% | −1.5pp |
| NYRA 2010-2016 → 2017 | 20.5% | 23.3% | −2.8pp |
| SoCal 2010-2016 → 2017 | 22.1% | 25.6% | −3.5pp |

The gap **widens** with longer training windows and more shipper-stable circuits — the opposite of what an "architectural cleanness wins out" hypothesis would predict.

## Why the gap exists despite POC's structural advantages

Within-race ranking has a specific property: **all horses in a race share the same track, conditions, class, distance, surface.** The POC's structural advantages (clean track-effect, clean condition-effect, clean class-effect) all cancel out within a race. Track-effect cleanness matters only for cross-track comparisons; condition-effect cleanness matters only when comparing a horse's pace from one condition to another. Within-race winner picking can't benefit from these.

Meanwhile, the live pipeline has data-window advantages the POC doesn't:

- **Cross-circuit history.** The live `adj_v0` for a horse who shipped to NYRA 2017 from elsewhere reflects their full multi-year career across all tracks they've raced. The POC sees only what they did on the chosen circuit.
- **Per-(horse, surface, zone) fits.** A turf-route specialist has a separate live curve for that combination. The POC's per-horse random intercept is one scalar across surfaces and zones, which loses some signal.
- **Years of empirical tuning.** The downstream `ratings.py` calibrations and the canonical anchor table have been validated against research findings. The POC is fresh.

## Where the rebuild WOULD pay off

The architectural concerns are real for use cases that **require cross-context comparable ability**:

- **Research questions** like "is the typical NYRA stakes horse faster than the typical SoCal stakes horse, controlling for class?" need ability on a portable scale.
- **Cross-circuit shipper assessment** — when a horse moves between regional circuits, the live pipeline's accumulated data is great if they have history; the POC's hierarchical model would extrapolate context cleanly even without it.
- **Class-projection** — predicting how a maiden winner will perform at allowance level requires cleanly-separated horse-ability from class-context.
- **Anything that needs uncertainty quantification** — the POC produces posterior variance for free; live produces a heuristic `1 - exp(-n/5)`.

For race-day-sim's actual ROI use case (within-race wagering), these advantages are dormant.

## Recommendation

**Defer the rebuild.** The POC has demonstrated:

- ✅ Framework is feasible (fits 60-100K observations in 12-20 seconds via `statsmodels.MixedLM`)
- ✅ Top horses recovered correctly when multi-year data is available
- ✅ Track-effect direction empirically correct (where live's are inverted)
- ✅ Context coefficients are interpretable on canonical-anchored scale
- ❌ Does not improve within-race winner prediction over the live pipeline (consistent 1.5-3.5pp gap across slices)
- ❌ The gap widens with the kind of high-density data the rebuild was supposed to help — opposite of expectation

So the architectural critique is empirically sound but not differentiating on the metric we actually care about. The bottleneck for wagering ROI is FLB calibration ([RDS-T2.x](cross-repo-audit-2026-05-27.md)), not absolute ability cleanliness.

The rebuild stays on the table as a future option for any use case requiring cross-context comparable ability — the POC code in `scripts/poc/hierarchical-ability/` is the starting point if/when that need arises.

## Files

- `01_extract_data.py` — NYRA 2014 extraction
- `01b_extract_2010_2017.py` — NYRA 2010-2017 extraction
- `01c_extract_socal_2010_2017.py` — SoCal 2010-2017 extraction
- `02_fit_model.py` — POC v0 (free intercept, abandoned)
- `02b_fit_time_model.py` — POC time (NYRA 2014, race-level holdout)
- `02c_fit_extended.py` — POC time (NYRA 2010-2016 fit, 2017 holdout)
- `02d_fit_socal.py` — POC time (SoCal 2010-2016 fit, 2017 holdout)
- `03_inspect_effects.py` — random-effect inspection (NYRA 2014)
- `04_predict_holdout.py` — predict v0 POC (abandoned)
- `04b_predict_time.py` — predict NYRA 2014 time POC
- `04c_predict_extended.py` — predict NYRA 2017 holdout
- `04d_predict_socal.py` — predict SoCal 2017 holdout
- `tmp/` — extracted CSVs and pickled fits (gitignored)

## Vocabulary glossary (for any future continuation)

The POC introduced cleaner naming than `adj_v0`:

| Quantity | Meaning |
|---|---|
| `peak_speed_observed` | regression intercept of velocity-vs-distance — what production calls `v0` |
| `fade_rate_observed` | velocity loss per 1000 ft of running — what production calls `decay_rate` (positive number) |
| `peak_speed_track_adj` / `fade_rate_track_adj` | track-offset-applied versions — what production currently calls `adj_v0` / `adj_decay` |
| `peak_speed_ability` / `fade_rate_ability` | hypothetical fully context-stripped versions — would be what a full hierarchical-rebuild produces |
| `time_residual` | canonical-time minus actual-time at the actual distance; positive = faster than canonical winner; the POC's response variable |

These names were not migrated into the production schema; production keeps `v0` / `decay_rate` / `adj_v0` / `adj_decay`. A schema rename was scoped but deferred — it touches three downstream repos (race-day-sim, wagering-analytics, race-replay) and is best done as a single coordinated change rather than during exploratory work.
