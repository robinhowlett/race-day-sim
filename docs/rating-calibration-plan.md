# Rating Calibration Plan

## Goal

Create a display rating that translates raw velocity curve parameters (adj_v0, decay_rate) into numbers that are immediately interpretable — like Timeform ratings but preserving the two-dimensional (speed + stamina) information that drives exotic value.

## Design

### Two ratings per horse at a given distance:

1. **Projected Time Rating** — combines v0 + decay into a single "how fast would this horse complete today's distance?" number. Anchored to actual winners by class.

2. **Stamina Index** — standalone decay measure, because two horses with identical projected times can have completely different pace profiles. This is the dimension that creates exotic value through pace interaction.

### Calibration approach

Query the full dataset (2012-2015, dirt + turf, sprint + route) and compute:

```sql
-- For each winner at each class level, what was their projected finishing time?
-- projected_time = distance_ft / avg_velocity
-- avg_velocity = adj_v0 - decay_rate * (distance_ft / 2000)
```

Then establish anchor points:

| Class level | Zone | Avg winner projected time | Rating = ? |
|---|---|---|---|
| Graded Stakes | Route | ? ms | 120 |
| Stakes/AOC | Route | ? ms | 110 |
| Allowance | Route | ? ms | 100 (anchor) |
| High Claiming | Route | ? ms | 90 |
| Low Claiming | Route | ? ms | 80 |

**Anchor: 100 = average allowance winner's projected time at that distance/surface.**

Then the scale is: each point = X milliseconds faster/slower than that anchor. The X is determined from the data — probably ~10-15ms per point for routes, ~8-10ms for sprints (scaled to the time range).

### Properties this gives us:

- **Unbounded** — a champion projects a faster time, so they naturally rate 130+, 140+, whatever
- **Distance-specific** — a horse's rating at 6f can differ from their rating at 9f (speed horses rate higher at sprint, stayers rate higher at route)
- **Not circular** — the rating comes from the PHYSICS (curve parameters + distance), not from who they beat. The calibration against winners just sets the SCALE, not the measurement.
- **Comparable across races** — a 112 in Race 3 means the same as a 112 in Race 8
- **Preservable alongside Stamina Index** — so you can see "this horse rates 108 but with a Stamina Index of only 45 — fast but fades"

### Stamina Index

Separate scale for decay rate:
- 100 = field median decay at that distance zone
- Higher = better stamina (lower decay)
- Scale: each point = 0.02 decay rate improvement

So a horse with decay 0.5 in a zone where median is 1.9 would have Stamina Index: 100 + (1.9 - 0.5) / 0.02 = 170. Exceptional stayer.

### Confidence bands

From `n_races` and `residual_std` on the velocity curve:
- HIGH confidence: n_races >= 10, tight residual
- MODERATE: 5-9 races
- LOW: 3-4 races
- INSUFFICIENT: < 3 races (no rating displayed)

Express as ± on the rating: "108 ± 4" (high confidence) vs "108 ± 12" (low confidence)

### Form adjustment

The rating above is the CAREER rating. Apply v0_trend to get the CURRENT rating:

```
current_rating = career_rating + (v0_trend / ms_per_point)
```

Display both: "Career 104, Current 112 ↑" — immediately shows the horse is running 8 points better than career average.

## Implementation

1. Run calibration query (needs robinpc access)
2. Determine `ms_per_point` at each distance/surface combination
3. Build `src/sim/ratings.py` with `compute_rating(adj_v0, decay_rate, distance_ft, surface)` 
4. Integrate into runner script output

## Data needed (run when on network)

```sql
-- Avg projected time for winners by class/zone/surface
-- Distribution of projected times (for percentile context)
-- Std dev of projected times within class (for point scaling)
```
