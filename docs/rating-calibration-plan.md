# Rating Calibration Plan

## Goal

Create display ratings that translate raw velocity curve parameters (adj_v0, decay_rate) into numbers that are immediately interpretable — like Timeform ratings but preserving the two-dimensional (speed + stamina) information that drives exotic value, plus a VALUE dimension that quantifies the market discrepancy.

## Display Format

```
Horse         Rating    Stamina  Form    Value       Confidence
Willy Pay     112 (±3)  34       +4      +40% (±15) HIGH (28 races)
Tinitus       105 (±3)  78       -3      -10% (±12) HIGH (24 races)
Gamblin Fever 106 (±8)  76       +11     +65% (±30) MODERATE (8 races)
Winner Jak     98 (±11) 82       +5      +50% (±45) LOW (5 races)
```

- **Rating** = projected competitive ability at today's distance (higher = faster projected time)
- **Stamina** = standalone decay measure (higher = holds speed better)
- **Form** = how many rating points above/below career baseline (from v0_trend)
- **Value** = estimated overlay percentage (what the model says this horse SHOULD pay vs what the market implies). Includes its own confidence range.
- **Confidence** = ± range on the Rating from sample size / residual std

Value interpretation: "+40% (±15)" means "the model estimates you'd get 40% more than fair value betting this horse, and we're fairly confident the overlay is real (even at the low end, +25% overlay)." A "+50% (±45)" means "potentially huge edge but uncertain — at the low end it could be only +5%.""

## Three Scales

### 1. Projected Time Rating

Combines v0 + decay into "how fast would this horse complete today's distance?"

**Anchor: 100 = the canonical race winner.**

The canonical race is the single most representative performance benchmark in American racing — NOT an average across all conditions, but a specific, defined context:

**The canonical race:**
- 4yo+ open (males and females eligible, but predominantly male fields)
- Claiming level ($20K-$40K purse range)
- Dirt surface, fast track condition
- Route distance (8-9 furlongs)
- Mid-tier track (not a premier meet, not a bush track — the middle 60% of tracks by handle)
- Field size 8-10 starters
- Non-state-bred, non-restricted
- Obvious outliers excluded (no 50+ length losers)

This is the most common race run in America. The shipping-horse network already normalizes across tracks, so "mid-tier track" is handled by adj_v0. The remaining dimensions (age, sex, class, conditions) define what 100 means.

**Everything is measured relative to this canonical race on its surface:**

- A 2yo MSW winner might rate 82 — expected, they're immature, not "bad"
- A filly in open company at 93 — giving real lengths to males, as the weight allowance (3-5 lbs) acknowledges
- A Grade 1 stakes winner at 125 — clearly 25 points above the journeyman level
- A champion at 140+ — generational talent

**Surface-specific canonical races:**

The physics differ so much between dirt/turf/synthetic that each surface needs its own 100-point anchor:
- Dirt route: canonical as described above
- Dirt sprint: same conditions but ≤6.5f
- Turf route: same conditions but turf surface (slower raw times, different decay profiles)
- Turf sprint: same (rare in US, sparse data)

Within a single race all horses are on the same surface at the same distance, so ratings are directly comparable regardless of which surface anchor was used.

**What this means for 2yos, fillies, etc.:**

They naturally rate LOWER than the canonical race — and that's correct. A 2yo rated 85 isn't "bad" — they're developing. A filly rated 92 isn't "weak" — she's giving weight to males. The rating captures the PHYSICAL REALITY without needing adjustment factors. The bettor sees "this 2yo rates 85 vs this field where the 4yo rates 105" and immediately knows the projection hierarchy.

If a 2yo rates 105, THAT is remarkable — they're performing at a level above the average mature horse. That's the kind of signal that identifies a future champion.

### 2. Stamina Index

Separate scale for decay rate:
- 100 = median decay for that distance zone
- Higher = better stamina (lower decay)
- Scale: each point = 0.02 decay rate improvement

A horse with decay 0.5 in a zone where median is 1.9: Stamina = 100 + (1.9 - 0.5) / 0.02 = 170.

Two horses can have identical Ratings but different Stamina values — that's the information that creates exotic value through pace interaction. The high-Speed/low-Stamina horse is the speed-and-fade type; the low-Speed/high-Stamina horse is the closer.

### 3. Value (Estimated Overlay %)

**Expressed as the estimated percentage overlay/underlay on this horse — not rating points, but expected return above/below fair.**

```
model_win_prob = from Benter combination (model + odds combined)
odds_implied_prob = from closing odds (market consensus)
value_pct = (model_win_prob / odds_implied_prob - 1) * 100
```

"+40%" means "the model thinks this horse should be 40% shorter in the market than they are." In betting terms, if the model is right, a $1 win bet on this horse has an expected return of $1.40 long-term.

**Why percentage, not rating points:** "10 rating points of value" requires knowing what that means in betting terms. "+40% overlay" needs no translation — every horseplayer understands "I'm getting 40% more than I should."

**Value also needs a confidence range:**
- The rating itself has uncertainty (±X)
- Each bound of that uncertainty implies a different value estimate
- Value at rating low-end: model_prob_at_low / odds_prob - 1
- Value at rating high-end: model_prob_at_high / odds_prob - 1
- Display as: "+40% (±15)" meaning the overlay is between +25% and +55%

**Conviction:**
- "+40% (±15)" → even at the worst case, there's a +25% edge. Strong conviction.
- "+50% (±45)" → could be anywhere from +5% to +95%. The edge probably exists but the size is uncertain.
- "+10% (±20)" → might actually be -10% (underlay). No conviction — pass.

**Connection to wagering-analytics:**
- AN1 showed trifectas are 15-21% overlaid when prices are on top + fav in 2nd/3rd
- A positive-Value horse on top × negative-Value horse (overbet fav) underneath = the structural overlay compounds
- The Value % on individual horses translates to LARGER % overlays in exotics because the mispricing multiplies across positions

### Form

```
form = (current_v0 - career_v0) / ms_per_point_for_v0
```

Expressed as rating points above/below career baseline. A horse with Form +11 is running 11 points better than their career average — that's a significant improvement regardless of their absolute rating.

## Performance-Impacting Factors (Research Needed)

### Known factors from starters table

| Field | Mechanism | Research question |
|---|---|---|
| `weight` | Biomechanical: more mass = more energy to accelerate and sustain | Regress v0 residual on weight. Expected ~0.3-0.5 ft/s per lb? Varies by horse/distance? |
| `jockey_allowance` | Weight reduction for apprentice riders — but also a skill proxy (apprentices may be less tactical) | Does allowance produce the same velocity gain per lb as a top jock at heavier weight? Or is the skill offset real? |
| `pp` (post position) | Geometric: inside = shorter path. Tactical: outside = cleaner trip. Varies by track geometry and distance. | Does pp correlate with v0 residuals controlling for ability? Is it track-specific (some tracks have strong rail bias)? |
| `medication_equipment` | Lasix (L) = diuretic preventing exercise-induced pulmonary hemorrhage. Blinkers (b) = focus aid. First-time application often produces one-time performance jump. | Does "first time Lasix" or "blinker change" correlate with positive v0 surprise? How large? One-time or sustained? |
| `last_raced_days_since` | Layoff fitness: too fresh = undertrained? Too long = need a race? The "bounce" after peak effort. | Is there an optimal days-since-last? Does the model's days_since_last in current_form already capture this? |
| `last_raced_position` | Coming off a win = peak form vs energy depletion ("bounce"). Coming off a loss = declining or saving ground? | Independently predictive after controlling for v0_trend? |
| `claimed` / `new_trainer_name` | Trainer change = new methods, potentially different training approach unlocks latent ability. Claiming trainers specifically look for improvement angles. | Do newly claimed horses systematically improve? By how much? How quickly? |
| `entry` (coupled entry) | Not a performance factor but a wagering structure factor — coupled entries share a win pool, which affects odds-implied probability and exotic construction. | Impacts value calculation, not the rating itself. |

### Known factors from races table

| Field | Mechanism | Research question |
|---|---|---|
| `track_condition` | Off tracks (sloppy/muddy/yielding) change the biomechanics — some horses handle it, some don't. Our adj_v0 currently blends all conditions together for a horse. | Should we separate curves by track condition? Or flag "untested on off-going" as uncertainty? |
| `run_up` | Distance from gate to timing start. Longer run_up = more acceleration before clock starts = inflated apparent v0. | Regress v0 on run_up distance — is there a systematic effect? Should we adjust? |
| `temp_rail` | Moves the running path — affects inside/outside bias and total distance traveled. | Low priority — hard to quantify without detailed track geometry. |
| `off_turf` | Race was moved from turf to dirt. Horses entered for turf surface may hate dirt. Their performance on dirt may be unrepresentative. | Flag off_turf starters as having unreliable dirt curves? |
| `field_size` | More horses = more traffic trouble = more randomness. Also affects pace dynamics (more speed types in bigger fields). | Does field size systematically affect v0 residuals? Probably adds noise rather than bias. |
| `weather` / `wind_speed` / `wind_direction` | Wind affects sprints more than routes. Headwind = slower apparent speed. | Low priority — data quality on wind is poor in Equibase charts. |

### Priority for research (when database available)

1. **Weight** — most direct biomechanical effect, cleanest signal, directly actionable for handicap races
2. **Post position** — track-specific biases are well-known in the industry, we can validate/quantify
3. **Medication/equipment changes** — "first time Lasix" is one of the most commonly cited positive indicators
4. **Trainer change (claimed)** — actionable signal that's currently invisible to the model
5. **Track condition** — determines whether to trust a horse's curve at all on today's surface
6. **Run-up distance** — systematic bias in v0 measurement that may affect cross-track normalization

### Factors that DON'T need separate research:

- `last_raced_days_since` → already captured in `rkm_current_form.days_since_last` + time-weighted decay
- `last_raced_position` → correlates with v0_trend (recent winner = likely positive trend)
- `field_size` → adds noise, not systematic bias; already handled by probability normalization

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

## Prerequisites

All calibration queries and empirical research are documented in `docs/research-plan.md`. Items 1-2 (canonical race identification + scaling) must complete before this output format can be implemented. Items 3-9 (weight, pp, medication, etc.) are refinements that improve accuracy but aren't required for initial deployment.
