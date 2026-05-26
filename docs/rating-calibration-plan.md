# Rating Calibration Plan

## Goal

Create display ratings that translate raw velocity curve parameters (adj_v0, decay_rate) into numbers that are immediately interpretable — like Timeform ratings but preserving the two-dimensional (speed + stamina) information that drives exotic value, plus a VALUE dimension that quantifies the market discrepancy.

## Display Format

```
Horse         Rating    Stamina  Form    Value   Confidence
Willy Pay     112 (±3)  34       +4      +6      HIGH (28 races)
Tinitus       105 (±3)  78       -3      -2      HIGH (24 races)
Gamblin Fever 106 (±8)  76       +11     +14     MODERATE (8 races)
Winner Jak     98 (±11) 82       +5      +8      LOW (5 races)
```

- **Rating** = projected competitive ability at today's distance (higher = faster projected time)
- **Stamina** = standalone decay measure (higher = holds speed better)
- **Form** = how many rating points above/below career baseline (from v0_trend)
- **Value** = how many rating points above/below what the market implies from odds
- **Confidence** = ± range from sample size / residual std

## Three Scales

### 1. Projected Time Rating

Combines v0 + decay into "how fast would this horse complete today's distance?"

**Anchor: 100 = average allowance winner's projected time.**

But the anchor must be SEGMENTED because:
- A 2yo's 58.0 adj_v0 doesn't mean the same as a 4yo's 58.0 (maturation)
- Fillies run slower than colts at the same class (sex allowance)
- Surface physics differ (dirt sprint v0 clusters around 64, turf route around 56)

**Segmentation needed:**

| Segment | Anchor definition |
|---|---|
| Dirt Sprint, Male 4yo+ | 100 = avg allowance winner projected time |
| Dirt Sprint, Female 4yo+ | 100 = avg allowance winner projected time |
| Dirt Sprint, 3yo | 100 = avg allowance winner projected time |
| Dirt Sprint, 2yo | 100 = avg MSW winner projected time (no allowance for 2yos) |
| Dirt Route, Male 4yo+ | 100 = avg allowance winner projected time |
| ... (same for Turf, Synthetic) | ... |

Each segment gets its own anchor time and ms-per-point scaling. A rating of 110 in "Dirt Route Male 4yo+" means the same relative thing as 110 in "Turf Sprint Female 3yo" — 10 points above average winner for that category.

**Cross-segment comparison:** A 115 dirt route male may or may not beat a 115 turf sprint female in an actual race — the segments are independent scales. BUT within a single race (where all horses are running the same distance/surface), the ratings are directly comparable.

### 2. Stamina Index

Separate scale for decay rate:
- 100 = median decay for that distance zone
- Higher = better stamina (lower decay)
- Scale: each point = 0.02 decay rate improvement

A horse with decay 0.5 in a zone where median is 1.9: Stamina = 100 + (1.9 - 0.5) / 0.02 = 170.

Two horses can have identical Ratings but different Stamina values — that's the information that creates exotic value through pace interaction. The high-Speed/low-Stamina horse is the speed-and-fade type; the low-Speed/high-Stamina horse is the closer.

### 3. Value Rating

**Derived from the gap between model rating and market-implied rating:**

```
market_implied_rating = rating that corresponds to the horse's odds-implied probability
value = current_rating - market_implied_rating
```

How to compute market-implied rating:
- From closing odds, derive each horse's win probability (normalized from odds)
- Convert that probability to a projected finishing time (inverse of the model: what time would produce this probability given the field?)
- Map that time to the rating scale

Positive Value = model says this horse is better than the market thinks (underbet).
Negative Value = model says this horse is worse than the market thinks (overbet).

**Connection to wagering-analytics overlay data:**
- AN1 showed that when prices are on top with fav in 2nd/3rd, trifectas are 15-21% overlaid
- A +10 Value horse on top with a -5 Value horse (overbet fav) in 2nd/3rd is EXACTLY that structure
- The Value column lets you immediately see where the crowd is wrong, which is where exotic equity concentrates

**Conviction = Value / Confidence:**
- Value +14, Confidence ±8 → the edge likely exists (value > uncertainty)
- Value +6, Confidence ±12 → the edge MIGHT exist (value within uncertainty)

### Form

```
form = (current_v0 - career_v0) / ms_per_point_for_v0
```

Expressed as rating points above/below career baseline. A horse with Form +11 is running 11 points better than their career average — that's a significant improvement regardless of their absolute rating.

## Open Questions

### Weight carried

The current model IGNORES weight. A horse's adj_v0 reflects their observed performance at whatever weight they carried. This means:
- Horses consistently carrying high weight (126+) have their true ability UNDERSTATED
- Weight changes between races are a systematic factor the model doesn't capture
- Handicap races are specifically designed to defeat ratings — weight equalizes

**Research needed:**
- What is the empirical ft/s per lb relationship in the data? (regress v0 residuals on weight)
- Does it vary by horse size, distance, surface?
- The commonly cited "1 length per 5 lbs at a mile" = ~1.75 ft/s per 5 lbs = 0.35 ft/s per lb. Is this confirmed by our data?
- Should we weight-adjust the curves? Or just flag it as a known limitation?

The `starters.weight` column is available in the database. A calibration query could examine whether horses carry different weight correlates with systematic v0 residuals.

### 2yo ratings over time

A 2yo in January is not the same animal as that 2yo in October. Their ratings should be expected to INCREASE throughout the year as they mature. The current v0_trend captures this as Form improvement, but a 2yo with Form +8 might just be normal maturation, not exceptional improvement. Consider:
- Age-specific form expectations (expected improvement per month for 2yos, 3yos)
- Or just flag 2yo ratings as inherently volatile

### Fillies/Mares vs Males

Sex allowance in racing (typically 3-5 lbs) acknowledges the performance gap. Our model measures them on the same physical scale, which means fillies will naturally rate lower. Within an all-female race, ratings are directly comparable. In mixed-sex races, the weight allowance partially compensates — but should we also adjust the rating? Or let the weight-carried adjustment handle it?

## Confidence Bands

From `n_races` and `residual_std` on the velocity curve:

```
rating_uncertainty = residual_std / sqrt(n_observations) * scale_factor
```

Where scale_factor converts residual velocity uncertainty to rating points.

| n_races | Typical ± | Label |
|---|---|---|
| 15+ | ±2-4 | HIGH |
| 8-14 | ±5-9 | MODERATE |
| 3-7 | ±10-15 | LOW |
| <3 | not rated | INSUFFICIENT |

## Implementation

1. Run calibration queries (needs robinpc access):
   - Projected times for winners segmented by class/zone/surface/age/sex
   - Standard deviations within segments (for ms-per-point scaling)
   - Weight vs v0 residual regression
2. Set anchor times and scaling per segment
3. Build `src/sim/ratings.py`:
   - `compute_rating(adj_v0, decay_rate, distance_ft, surface, age, sex)`
   - `compute_stamina_index(decay_rate, distance_zone)`
   - `compute_value(rating, odds, field_ratings)`
   - `compute_confidence(n_races, residual_std)`
4. Integrate into runner script output and conversational simulation format

## Calibration Queries (run when on network)

```sql
-- 1. Anchor times by segment
SELECT surface, distance_zone, age_group, sex,
       AVG(projected_time) as anchor_time,
       STDDEV(projected_time) as time_spread
FROM winners_with_projections
WHERE race_type IN ('ALLOWANCE', 'ALLOWANCE OPTIONAL CLAIMING')
GROUP BY surface, distance_zone, age_group, sex;

-- 2. Class level offsets (how much faster are stakes winners vs claiming winners?)
SELECT class_level, surface, distance_zone,
       AVG(projected_time) as avg_winner_time
FROM winners_with_projections
GROUP BY class_level, surface, distance_zone;

-- 3. Weight impact
SELECT weight_carried, 
       AVG(v0_residual) as avg_residual,
       COUNT(*) as n
FROM starters_with_curves
GROUP BY weight_carried
ORDER BY weight_carried;

-- 4. 2yo maturation curve
SELECT months_since_first_start,
       AVG(v0_trend) as avg_trend
FROM current_form_2yos
GROUP BY months_since_first_start;
```
