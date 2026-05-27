# Rating Calibration Plan

## Goal

Create display ratings that translate raw velocity curve parameters (adj_v0, decay_rate) into numbers that are immediately interpretable — like Timeform ratings but preserving the two-dimensional (speed + stamina) information that drives exotic value, plus a VALUE dimension that quantifies the market discrepancy.

## Display Format

```
Horse         Rating  Market  Edge     Stamina  Form   Confidence
Willy Pay     112     106     +6 (±3)  34       +4     HIGH (28 races)
Tinitus       105     108     -3 (±3)  78       -3     HIGH (24 races)
Gamblin Fever 106      98     +8 (±6)  76       +11    MODERATE (8 races)
Winner Jak     98      94     +4 (±9)  82       +5     LOW (5 races)
```

- **Rating** = projected competitive ability at today's distance (from curves + group bias adjustments)
- **Market** = odds-implied rating (what rating the market's odds imply for this horse)
- **Edge** = Rating − Market, in rating points (±confidence band). Positive = model thinks horse is better than market says.
- **Stamina** = standalone decay measure (higher = holds speed better)
- **Form** = how many rating points above/below career baseline (from v0_trend)
- **Confidence** = ± range combining curve fit uncertainty + group bias significance

All values in the same unit: **rating points** (1 point = 58ms sprint / 77ms route ≈ 0.3-0.5 lengths).

Edge interpretation: "+6 (±3)" means the model sees this horse as 6 points better than market pricing, even at worst case still +3 pts of edge. "+4 (±9)" means edge probably exists but could be negative — low conviction.

## Three Scales

### 1. Projected Time Rating

Combines v0 + decay into "how fast would this horse complete today's distance?"

**Anchor: 100 = the canonical race winner.**

The canonical race is empirically determined from 1.59M TB races (1991-2017) as the intersection of maximum volume and minimum coefficient of variation — the single most stable, representative performance benchmark in American racing.

**The canonical race (empirically validated):**
- 4yo+ open/male (sexes code "A", not female-only)
- Claiming level, **$5,000–$10,000 claiming price** (the median claiming price is $6,250)
- Dirt surface, Fast track condition
- Non-state-bred, non-restricted
- Field size ≥ 5 starters
- Obvious outliers excluded (no 50+ length losers)

**Reference distances and anchor times:**
- Sprint anchor: **6 furlongs = 71,577ms** (N=2,709 races, CV=0.016)
- Route anchor: **8 furlongs = 99,192ms** (N=1,403 races, CV=0.018)

(CV = coefficient of variation = σ/mean. Lower = more consistent winner times = better calibration anchor.)

**Why $5K-$10K claiming (not $20K-$40K):**
The $5K-$10K tier has the lowest coefficient of variation (CV = σ/μ) of any claiming level: 0.0156 sprint, 0.0177 route. Lower CV = more consistent winner times = more stable anchor for calibration. The ≤$5K tier has higher volume but wider variance (CV=0.0207) because it captures a broader ability range (a barely-competitive $2,500 claimer through a fit $5,000 claimer). The $5K-$10K level is also the population median ($6,250 median claim price), making it the true center of mass of American racing.

**Volume context (2000-2017, open/male, 4yo+, non-state-bred, Fast dirt):**
- CLM ≤$5K: 4,651 sprint / 2,046 route
- CLM $5K-$10K: 2,709 sprint / 1,403 route
- CLM $10K-$16K: 1,425 sprint / 686 route
- CLM $16K-$25K: 954 sprint / 433 route
- All classes combined at 6f: 12,308 races in the full ladder

**Everything is measured relative to this canonical race on its surface:**

- A 2yo MSW winner might rate 82 — expected, they're immature, not "bad"
- A filly in open company at 93 — giving real lengths to males, as the weight allowance (3-5 lbs) acknowledges
- A Grade 1 stakes winner at 125 — clearly 25 points above the journeyman level
- A champion at 140+ — generational talent

**Scaling: milliseconds per rating point**
- Sprint (6f): **58 ms/point** (derived from MCL-to-Stakes span of ~2,913ms ≈ 50 points)
- Route (8f): **77 ms/point** (derived from MCL-to-Stakes span of ~3,326ms ≈ 50 points)

**Implied class ratings (6f sprint):**

| Level | Avg Winner Time (ms) | Rating |
|---|---|---|
| Maiden Claiming | 72,482 | 84 |
| CLM ≤$5K | 72,516 | 84 |
| **CLM $5K-$10K (anchor)** | **71,577** | **100** |
| CLM $10K-$16K | 71,214 | 106 |
| CLM $16K-$25K | 70,798 | 113 |
| Allowance | 70,776 | 114 |
| CLM $25K-$40K | 70,594 | 117 |
| AOC | 70,260 | 123 |
| Stakes (avg) | 69,603 | 134 |

**Length equivalence:**
- At 6f finish velocity (~50.2 ft/s): 1 length ≈ 169ms ≈ 2.9 rating points
- At 8f finish velocity (~49.9 ft/s): 1 length ≈ 170ms ≈ 2.2 rating points

**Universal scale with surface-specific anchors (Option B):**

One rating scale across all surfaces. The dirt anchor defines 100. Other surfaces are bridged via empirical cross-surface horse performance (Item 11 research).

Surface-specific anchor conditions differ because the market structure differs:

| Surface | Sprint Dist | Route Dist | Canonical Class | Condition |
|---|---|---|---|---|
| Dirt | 6f | 8f | CLM $5K-$10K | Fast |
| Turf | 5f | 8f-8.5f | CLM $16K-$25K | Firm |
| Synthetic | 6f | 8f | CLM $5K-$10K | Fast |

**Why turf's canonical class is higher ($16K-$25K):**
Turf claiming below $12,500 barely exists (21 races at ≤$5K vs 828 at $10K-$16K). The turf claiming market starts higher because fewer turf-bred horses enter low claiming — they get placed in allowance or shipped to a turf-friendly track. The turf anchor maps to approximately rating 112 on the universal (dirt-based) scale; the cross-surface conversion factor from Item 11 will refine this.

**Surface decay profiles (empirically measured at 8f claiming route):**

| Surface | Opening v (ft/s) | Closing v (ft/s) | Deceleration | Character |
|---|---|---|---|---|
| Dirt | 55.1 | 49.9 | -9.4% | Speed-favoring, heavy friction |
| Synthetic | 53.8 | 50.7 | -5.7% | Intermediate — closer to turf |
| Turf | 55.8 | 53.9 | -3.3% | Stamina-favoring, minimal friction |

Synthetic sits between dirt and turf on decay but is closer to turf. A horse's turf curve may be a better predictor of synthetic performance than their dirt curve for the stamina component.

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

### 3. Edge (Rating Points)

**Expressed in the same rating-point scale as everything else — a unified unit system.**

```
Edge = Rating - Market
```

Where:
- **Rating** = model's projected ability (from velocity curves + group bias adjustments)
- **Market** = odds-implied rating (what rating would produce this horse's market probability via the Benter logit?)

A horse with Rating 112 and Market 106 has **Edge = +6 (±3)** — the model thinks this horse is 6 rating points better than the market gives it credit for, with a confidence band of ±3.

**Composition:**

```
Horse         Rating  Market  Edge      Confidence
Willy Pay     112     106     +6 (±3)  HIGH
Tinitus       105     108     -3 (±3)  HIGH
Gamblin Fever 106      98     +8 (±6)  LOW
```

**How group bias contributes:**

Group-level A/E adjustments (from the Market Bias Layer) are converted to equivalent rating points and added to the model rating before computing Edge:

```
adjusted_rating = base_rating + sum(group_bias_pts)
```

Where `group_bias_pts` converts each factor's relative A/E to the rating scale. For example:
- Jockey upgrade (relative A/E +4.7%): on a horse at 5/1, this probability shift ≈ +2-3 rating points
- Blinkers OFF (relative A/E +9.3%): ≈ +4-5 rating points equivalent
- Trainer claim (top, relative A/E +15%): ≈ +6-8 rating points equivalent

The conversion depends on the horse's base odds (the same A/E shift produces more rating-point equivalent at longer odds where the probability curve is flatter).

**Confidence band:**

The ± range combines:
1. Rating uncertainty from curve fit (residual_std / √n → ± rating points)
2. Group bias confidence (Archie significance test — if Archie < 3.0 for a factor, its contribution widens the band rather than shifting the point estimate)

**Conviction interpretation:**
- **+6 (±3)** → even at worst case, +3 pts of edge. High conviction.
- **+8 (±6)** → could be as low as +2 or as high as +14. Edge exists but size uncertain.
- **+2 (±4)** → might actually be -2 (underlay). No conviction — pass or use only in exotic spreading.

**Why rating points, not percentage overlay:**

One unit system everywhere. The rating, the form trend, the stamina index, and the edge are all in the same scale. "+6 points of edge" has the same physical meaning as "+6 points of rating" — it's 6 × 58ms = 348ms at a sprint, or roughly 2 lengths faster than the market believes. No translation needed between model outputs.

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

## Market Bias Layer (A/E, Impact Value, WCMI)

Research items 3-12 each have TWO outputs:
1. **Physical effect** — does this factor change velocity? (feeds into RKM rating adjustment)
2. **Market bias** — does the crowd properly price this factor? (feeds into wagering value)

These are independent. A factor can have zero physical effect but massive market bias (e.g., if the crowd over-believes weight matters, top weights become systematically underbet). Conversely, a factor can have real physical impact that the crowd already prices correctly (no wagering edge).

### A/E (Actual / Expected)

Measures whether a GROUP wins more or less often than its odds imply:

```
A/E = actual_winners / expected_winners_from_odds
    = actual_winners / sum(1 / (SP + 1)) for all runners in group
```

- A/E > 1.0 → group wins MORE than market expects → systematically underbet (value)
- A/E < 1.0 → group wins LESS than market expects → systematically overbet (avoid)
- A/E = 1.0 → market prices this group correctly

A/E is the primary metric for detecting exploitable market biases. Every research item (3-12) should compute A/E by characteristic, not just physical effect.

### Impact Value (IV)

Measures whether a GROUP wins more or less than its fair share of races:

```
IV = (group % of winners) / (group % of runners)
```

- IV > 1.0 → wins more than proportional share
- IV < 1.0 → wins less than proportional share
- IV = 1.0 → neutral

IV detects ability signal. A/E detects market bias. They can diverge: a group can have high IV (genuinely better) but neutral A/E (market already knows), or neutral IV (no ability edge) but high A/E (market over-penalizes them).

**IV is critical for Item 10/12** — in limited-form races (maidens, first-time starters), IV by trainer/jockey/sire replaces individual horse curves as the primary assessment tool.

### WCMI (Wisdom of Crowd Market Index)

Adapted from Shannon's Entropy — measures how informed the market is about a race:

```
WCMI = 1 - (-sum(p_i * log_n(p_i)))
where p_i = implied probability of runner i, n = number of runners
```

- WCMI → 0.0: all runners same price, market knows nothing (maximum entropy)
- WCMI → 1.0: market fully resolved, one horse at minimum odds
- WCMI < 0.13: crowd is uninformed — opportunity for informed model (per Matekus)
- WCMI > 0.20: crowd is well-informed — model edge is smaller

**Applications in our system:**
- **Race selection:** Prefer low-WCMI races where our model adds most incremental value
- **Kelly sizing:** Bet more aggressively in low-WCMI markets (model edge is larger)
- **Horizontal strategy:** Low-WCMI legs are where spreading pays — the crowd is guessing, so exotic payoffs are inflated by the uncertainty
- **Maiden race validation:** Maiden races should have systematically lower WCMI (confirms the ITP principle that these races have value from uncertainty)

### Where This Lives in the Architecture

```
RKM (physics)         → individual horse rating, form, stamina
                             ↓
Market Bias Layer     → A/E by characteristic, WCMI per race, IV by group
(wagering-analytics)     ↓
race-day-sim          → combines rating + market bias to construct bets
```

The Market Bias Layer belongs in **wagering-analytics** as a new analysis phase. RKM remains pure physics (velocity curves, normalization). The bias layer uses RKM outputs PLUS market data to identify systematic mispricings at the GROUP level.

**Implications for wagering-analytics:**
The current wagering-analytics computes fair value for exotic *outcomes* (given who finished where, was the payoff fair?). The Market Bias Layer adds: given observable *pre-race characteristics*, does the market systematically misprice certain types of horses? This is a new phase (AN2?) that should compute:
1. A/E tables by factor (weight, PP, medication, trainer first-out rate, etc.)
2. WCMI for every race in the database
3. IV tables for limited-form contexts (trainer × surface × distance for maidens)

These become static lookup tables that race-day-sim consults during the blinded phase — they represent market biases observable from historical data, not post-race information.

---

## Open Questions

### Weight carried ✅ RESEARCHED (Item 3)

"Weight" in racing = carried weight (jockey + tack + lead pads), not the horse's body mass. A typical horse weighs ~1,100-1,200 lbs; carried weight ranges from 108-130 lbs. In claiming races, weight is assigned by a schedule (age/sex allowances). In handicaps/stakes, it's assigned by the racing secretary based on perceived ability — better horses carry more.

**Findings (research-plan Item 3, 2026-05-26):**

Physical effect:
- Within-horse regression: -0.008 ft/s per lb (sprint), -0.005 ft/s per lb (route)
- R² ≈ 0 — weight explains almost nothing about velocity after controlling for ability
- The folk wisdom "1 length per 5 lbs" (0.35 ft/s/lb) is **44× larger** than the observed effect

Market bias (the exploitable finding):
- IV by carried weight: horses assigned top weight (124+ lbs) have IV=1.08 (win 8% more than share — they're better horses)
- A/E by carried weight: top weight A/E=0.789 vs light weight (≤114) A/E=0.814
- The crowd **over-bets top weights** by 2.5-3.7 percentage points — they over-believe "weight stops trains"
- 5lb apprentice jockeys: A/E=0.825 (best value), crowd over-penalizes the "bug" stigma

**Conclusion:** No rating adjustment for weight. The curve already absorbs it. The edge is in the Market Bias Layer — light weights and apprentice-ridden horses are systematically underbet.

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
