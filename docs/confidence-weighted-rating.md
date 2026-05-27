# Confidence-Weighted Rating System

## Problem Statement

The current system has a binary boundary: either a horse has enough data for a velocity curve (rated) or it doesn't (unrated/INSUF). This creates two problems:

1. **Overconfidence in sparse curves:** A horse with 2 prior races gets the same "rated" treatment as one with 40 races, even though the 2-race curve is wildly uncertain.
2. **Zero information for debut/zone-switch horses:** We show nothing when the horse has no same-zone history, even though trainer/jockey/sire signals provide genuine (if weaker) evidence.

Additionally, `rkm_velocity_curves` contains future data leakage (career-aggregate including post-sim-date races). Only `rkm_current_form` is point-in-time safe, and it requires ≥2 prior same-zone races.

## Design: A Blending Spectrum

Instead of binary (rated vs unrated), produce a single rating for EVERY horse by blending two information sources with confidence-dependent weights:

```
final_rating = w_physics × physics_rating + (1 - w_physics) × prior_rating
```

Where:
- **physics_rating** = from velocity curve (current_form when available)
- **prior_rating** = from group-level signals (trainer A/E, jockey tier, class level of race)
- **w_physics** = weight on the curve, function of data quality

### The Weight Function

`w_physics` is a sigmoid-like function of data quality:

| Data Quality | w_physics | Interpretation |
|---|---|---|
| 0 same-zone races | 0.00 | Pure prior (trainer/jockey/class signals only) |
| 1 same-zone race | 0.15 | Mostly prior, slight physics hint |
| 2 same-zone races | 0.30 | Blended — prior still dominates |
| 3 same-zone races | 0.50 | Equal weight |
| 5 same-zone races | 0.70 | Physics dominates, prior informs edges |
| 8+ same-zone races | 0.85 | Mostly physics, prior as tiebreaker |
| 15+ same-zone races | 0.95 | Almost entirely physics |

The exact function: `w_physics = 1 - exp(-n_races / 5)` (reaches 0.63 at n=5, 0.86 at n=10, 0.95 at n=15).

### Recency Modifier

The weight also decays with staleness. A horse with 10 races but the most recent was 14 months ago should get less physics weight than one with 10 races and a start last week:

```
recency_factor = DECAY_FACTOR ^ (days_since_last / 30)  # same 0.90 per 30 days
w_physics_final = w_physics × recency_factor
```

A horse with 15 races (w=0.95) but 12 months off (recency=0.28) gets effective w=0.27 — most of the rating comes from prior signals (the trainer is freshening them — what's the trainer's layoff A/E?).

## The Prior Rating

When physics is absent or weak, what does the "prior" actually produce?

### Components of the Prior

1. **Class-implied rating:** The race's purse/class maps to a rating level from the canonical ladder. A horse entered in a CLM $10K race at 6f dirt is "expected" to be roughly 106 (from research). This is the baseline.

2. **Trainer A/E adjustment:** If the trainer's FTS A/E = 1.3, the horse is expected to outperform the class level by the equivalent point adjustment. This shifts the prior upward.

3. **Jockey A/E adjustment:** Jockey tier / track form contributes similarly.

4. **Equipment/context signals:** First-time Lasix, blinkers off, claimed, surface switch — all multiplicative adjustments to the prior probability, converted to rating points.

5. **Odds-implied position:** The market's pricing provides information. At 4/1 in a 10-horse field, the market thinks this horse is roughly the 2nd or 3rd best in the race. That maps to a percentile in the field's expected rating distribution.

### Prior Computation

```
class_rating = canonical_rating_for(race_type, purse, surface, distance)
bias_adjustment = log(bias_multiplier) × 20  # same conversion as current
prior_rating = class_rating + bias_adjustment
```

For FTS with no curve: this IS the rating. For horses with partial data, it blends with the physics.

## What Changes in the Pipeline

### Data Source for Physics

**`rkm_current_form` is the ONLY physics input for race-day-sim.** The career `rkm_velocity_curves` table is used for:
- Research and calibration (it's useful for understanding horses across their full career)
- Feeding INTO the current_form computation (as the career baseline for v0_trend)
- NOT used directly in the simulation rating

If `current_form` is NULL (horse has <2 same-zone races before this date), the physics contribution is zero and `w_physics = 0`.

### Handling the Current Gap (57-67% coverage)

The ~40% of starters without current_form fall into:
- **First-time starters** → w_physics = 0, pure prior
- **Zone-switchers** (sprinter trying a route for first time) → w_physics = 0 for new zone, but could use cross-zone correlation (r=0.34) as a weak physics signal
- **Long layoff + surface change** → old form exists but exponentially decayed; w_physics is naturally low due to recency_factor
- **Early career (1 race)** → Not enough for current_form (needs 2); w_physics could be 0.15 if we lower MIN_PRIOR_RACES to 1 in the form computation

### Potential RKM Change: MIN_PRIOR_RACES = 1

Currently `form.py` requires ≥2 prior races. If we lower to 1 (with ≥4 velocity points from that single race), coverage improves. The resulting curve is unreliable (1 data point = no trend estimation), but it provides a starting velocity estimate with a very wide confidence band.

This would give:
- 1 prior race → w_physics = 0.15, very wide band (±15+ pts)
- 2 prior races → w_physics = 0.30, wide band (±10 pts)
- Gradually tightening as data accumulates

## Display Format

```
Pgm  Horse              Rating (±band)  Market  Edge         Basis
 7   City Prospect      91 (±3)         94      -3 (±3)     FORM [15 races, 13d ago]
 5   Carson Camp        70 (±3)         70      +1 (±3)     FORM [40 races, jock↑]
 1   Al Lloyd           58 (±3)         58      -2 (±3)     FORM [31 races, jock↓]
10   Thunder Minister    —               —       —          DEBUT [trainer bias 1.05]
 3   Fashion Scoup       —               —       —          DEBUT [trainer bias 1.39]
```

For horses with w_physics < 0.30 (mostly prior-based), the display shows:
- No numeric rating (the prior isn't precise enough to display as a point estimate)
- The bias_mult as a "trainer confidence" indicator
- A qualitative label: DEBUT, ZONE-SWITCH, LAYOFF-RETURN

For horses with w_physics ≥ 0.30:
- Full numeric rating with ± band
- Band width = function of both w_physics and residual_std

## The ± Band Computation

```
base_uncertainty = residual_std / sqrt(n_observations)  # standard error of the curve fit
recency_inflation = 1 / recency_factor                  # staler data = wider band
physics_band = base_uncertainty × recency_inflation × (ms_per_pt conversion)
prior_band = 15  # the prior alone has inherent ~15pt uncertainty

final_band = w_physics × physics_band + (1 - w_physics) × prior_band
```

This produces:
- 15+ races, recent: ±3 (tight, trustworthy)
- 8 races, somewhat recent: ±6
- 3 races, recent: ±10
- 1 race or stale: ±15+
- No curve: no numeric display (band > useful range)

## Edge Interpretation with Bands

The Edge (±band) becomes the primary decision tool:

- **Band doesn't cross zero:** Conviction. The model disagrees with market even at worst case.
- **Band crosses zero slightly:** Weak opinion. Use in exotic spreading, not keying.
- **Band is enormous (±15+):** The rating is mostly guess. Rely on bias signals for ticket inclusion, not model rating.

## What This Replaces

| Current | New |
|---|---|
| `rkm_velocity_curves` used directly in blinder.py | Never used directly — only as input to form computation |
| Binary rated/INSUF | Continuous w_physics spectrum |
| Form shown as separate "form" column (v0_trend) | Form IS the rating (current_v0 already incorporates trend) |
| Missing data = NaN = excluded from analysis | Missing data = prior-based assessment with appropriate uncertainty |
| Fixed ±3/6/10 bands from n_races buckets | Computed band from n_observations + recency + residual_std |

## Implementation Phases

### Phase 1: Fix the leakage (immediate)
- Add `AND vc.first_race < %(race_date)s` filter to blinder.py
- Prefer `current_v0` over `adj_v0` (already done)
- Horses where current_form is NULL AND curve started after sim date → INSUF (correctly)

### Phase 2: Implement w_physics blending (medium effort)
- Add `n_same_zone_races_before_date` count to the blinder query
- Compute w_physics in ratings.py
- Compute prior_rating from class + bias signals
- Blend: `rating = w × physics + (1-w) × prior`

### Phase 3: Lower MIN_PRIOR_RACES in RKM (requires RKM recompute)
- Change form.py to allow n=1 (with wide uncertainty)
- Recompute rkm_current_form with new threshold
- Coverage jumps from ~60% to potentially ~75-80%

### Phase 4: Cross-zone weak signal
- For zone-switchers: use the other zone's curve (at reduced weight, scaled by the r=0.34 correlation)
- A dirt sprinter trying a route for the first time gets w_physics = 0.34 × (their sprint w_physics)
