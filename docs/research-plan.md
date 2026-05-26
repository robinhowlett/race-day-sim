# Research Plan

Database-dependent investigations required before the rating and output systems can be calibrated. Each section produces specific outputs that feed into the rating calibration and output format.

---

## 1. Identify the Canonical Race

**Goal:** Empirically determine which race conditions represent the true "center of mass" of American racing — the single anchor point (100) for the rating scale.

**Approach:** Rather than declaring "4yo+ open dirt route claiming" as canonical, find it from the data.

**Queries:**

```sql
-- Distribution of race conditions by volume
-- What is the most common race type actually run?
SELECT 
    surface,
    CASE WHEN furlongs > 6.5 THEN 'route' ELSE 'sprint' END as zone,
    type,
    CASE WHEN female_only THEN 'female' ELSE 'open/male' END as sex_restrict,
    CASE 
        WHEN min_age = 2 AND max_age = 2 THEN '2yo'
        WHEN min_age = 3 AND max_age = 3 THEN '3yo'
        ELSE '4yo+'
    END as age_group,
    track_condition,
    COUNT(*) as n_races,
    AVG(number_of_runners) as avg_field,
    AVG(purse) as avg_purse
FROM races
WHERE date BETWEEN '2012-01-01' AND '2015-12-31'
  AND breed = 'TB'
  AND number_of_runners >= 5
GROUP BY 1,2,3,4,5,6
ORDER BY n_races DESC
LIMIT 50;
```

```sql
-- For the top conditions, what's the projected time distribution of winners?
-- This identifies which condition produces the most STABLE central tendency
-- (low variance = good anchor; high variance = too noisy)
WITH canonical_candidates AS (
    -- top 10 most common race types
),
winner_projections AS (
    -- projected time for each winner in those race types
)
SELECT condition_group,
       COUNT(*) as n_winners,
       AVG(projected_time) as mean_time,
       STDDEV(projected_time) as std_time,
       STDDEV(projected_time) / AVG(projected_time) as cv  -- coefficient of variation
FROM winner_projections
GROUP BY condition_group
ORDER BY cv ASC;  -- lowest CV = most stable anchor
```

**Criteria for the canonical race:**
1. High volume (most commonly run — gives the largest calibration sample)
2. Low coefficient of variation in winner projected times (stable central tendency)
3. Open competition (not restricted by state-bred, starter conditions, etc.)
4. Conditions that shipping horses frequently cross into/out of (connects to the normalization network)

**Output:** A precise definition of the canonical race conditions, plus the anchor projected time in ms for each surface × zone.

---

## 2. Scaling: What Does One Rating Point Mean?

**Goal:** Determine how many milliseconds of projected time difference = 1 rating point.

**Approach:** From the canonical race distribution, define the scale such that meaningful competitive separation maps to interpretable point differences.

**Options:**
- A: 1 point = 1 standard deviation / 20 (so the range from -2σ to +2σ spans 80 points, roughly 60-140)
- B: 1 point = the projected time difference corresponding to 1 length at the wire (at canonical race finish velocity)
- C: 1 point = empirically determined from class-level separation (difference between avg claiming winner and avg stakes winner = some fixed span like 30 points)

**Queries:**

```sql
-- Class level separation in projected time
SELECT class_level,
       AVG(projected_time) as avg_time,
       AVG(projected_time) - LAG(AVG(projected_time)) OVER (ORDER BY avg_purse) as gap_from_next
FROM winner_projections
GROUP BY class_level
ORDER BY avg_purse;
```

**Output:** ms-per-point for each surface × zone. Plus validation that the scale produces sensible numbers (champions at 130-145, average winners at 100, non-winners at 80-90).

---

## 3. Weight Impact

**Goal:** Quantify how much weight carried affects velocity, and whether it's linear.

**Queries:**

```sql
-- Approach 1: Within-horse comparison (same horse at different weights)
-- Control for form by looking at horses with stable v0_trend
WITH horse_races AS (
    SELECT s.horse, s.weight, r.furlongs, r.surface,
           vc.adj_v0, cf.v0_trend, cf.current_v0,
           -- Compute residual: actual performance vs expected from curve
           -- (need indiv_fractionals for this)
    FROM starters s
    JOIN races r ON r.id = s.race_id
    JOIN rkm_velocity_curves vc ON ...
    JOIN rkm_current_form cf ON cf.starter_id = s.id
    WHERE ABS(cf.v0_trend) < 0.5  -- stable form only
      AND s.weight IS NOT NULL
)
SELECT weight, 
       AVG(performance_residual) as avg_residual,
       COUNT(*) as n
FROM horse_races
GROUP BY weight
ORDER BY weight;

-- Approach 2: Handicap races specifically (weight is intentionally varied)
-- Look at whether higher-weight horses underperform their curves
SELECT 
    weight - 120 as weight_over_standard,
    AVG(surprise) as avg_surprise,  -- from rkm_race_performance
    COUNT(*) as n
FROM starters s
JOIN races r ON r.id = s.race_id
JOIN rkm_race_performance rp ON rp.starter_id = s.id
WHERE r.type LIKE '%HANDICAP%'
  AND s.weight IS NOT NULL
GROUP BY weight - 120
ORDER BY 1;
```

**Output:** ft/s per lb (or rating points per lb). Whether it's linear or varies by weight range. Whether it differs by distance (more impact at routes where more work is done against gravity?).

---

## 4. Post Position Bias

**Goal:** Quantify track-specific post position effects on performance.

**Queries:**

```sql
-- Win rate by post position, segmented by track and distance zone
SELECT track, 
       CASE WHEN furlongs > 6.5 THEN 'route' ELSE 'sprint' END as zone,
       s.pp,
       COUNT(*) as n_starts,
       AVG(CASE WHEN s.official_position = 1 THEN 1.0 ELSE 0.0 END) as win_rate,
       AVG(CASE WHEN s.official_position <= 3 THEN 1.0 ELSE 0.0 END) as top3_rate
FROM starters s
JOIN races r ON r.id = s.race_id
WHERE r.number_of_runners >= 8
  AND s.pp IS NOT NULL
  AND r.date BETWEEN '2010-01-01' AND '2016-12-31'
GROUP BY track, zone, s.pp
HAVING COUNT(*) >= 50
ORDER BY track, zone, s.pp;

-- More precise: does pp correlate with v0 residuals after controlling for ability?
SELECT track, zone, s.pp,
       AVG(rp.surprise) as avg_surprise  -- positive surprise = ran faster than curve predicted
FROM starters s
JOIN races r ON r.id = s.race_id
JOIN rkm_race_performance rp ON rp.starter_id = s.id
WHERE s.pp IS NOT NULL
GROUP BY track, zone, s.pp
HAVING COUNT(*) >= 100;
```

**Output:** Per-track, per-zone pp bias table. Horses drawing biased posts have a systematic advantage/disadvantage that the model doesn't currently capture — this is a potential form of hidden value.

---

## 5. Medication & Equipment Changes

**Goal:** Quantify the first-time effect of Lasix, blinkers, and other equipment changes.

**Queries:**

```sql
-- Parse medication_equipment field for changes between consecutive starts
-- This requires joining a horse's sequential starts and comparing their med/equip strings
WITH sequential_starts AS (
    SELECT s.id, s.horse, s.medication_equipment, r.date,
           LAG(s.medication_equipment) OVER (PARTITION BY s.horse ORDER BY r.date) as prev_med_equip
    FROM starters s
    JOIN races r ON r.id = s.race_id
    WHERE s.horse IS NOT NULL
    ORDER BY s.horse, r.date
)
SELECT 
    CASE 
        WHEN medication_equipment LIKE '%L%' AND (prev_med_equip IS NULL OR prev_med_equip NOT LIKE '%L%') 
        THEN 'FIRST_LASIX'
        WHEN medication_equipment LIKE '%b%' AND (prev_med_equip IS NULL OR prev_med_equip NOT LIKE '%b%')
        THEN 'FIRST_BLINKERS'
        ELSE 'NO_CHANGE'
    END as change_type,
    AVG(rp.surprise) as avg_surprise,
    COUNT(*) as n
FROM sequential_starts ss
JOIN rkm_race_performance rp ON rp.starter_id = ss.id
GROUP BY change_type;
```

**Output:** Average performance surprise (ft/s) for first-time Lasix, first-time blinkers, etc. If the effect is large and consistent, it's a factor the model should account for.

---

## 6. Trainer Change / Claimed Horses

**Goal:** Do horses improve after being claimed or changing trainers?

**Queries:**

```sql
-- Horses that were claimed: compare performance before vs after claim
WITH claimed_horses AS (
    SELECT s.horse, r.date as claim_date, s.new_trainer_name
    FROM starters s
    JOIN races r ON r.id = s.race_id
    WHERE s.claimed = true
)
SELECT 
    CASE WHEN r.date > ch.claim_date THEN 'AFTER_CLAIM' ELSE 'BEFORE_CLAIM' END as period,
    AVG(rp.surprise) as avg_surprise,
    AVG(cf.v0_trend) as avg_trend,
    COUNT(*) as n
FROM claimed_horses ch
JOIN starters s ON s.horse = ch.horse
JOIN races r ON r.id = s.race_id
JOIN rkm_race_performance rp ON rp.starter_id = s.id
LEFT JOIN rkm_current_form cf ON cf.starter_id = s.id
WHERE r.date BETWEEN ch.claim_date - interval '180 days' AND ch.claim_date + interval '180 days'
GROUP BY period;
```

**Output:** Average improvement after claim in ft/s surprise and v0_trend. If consistent, "recently claimed" becomes a positive signal the model should weight.

---

## 7. Track Condition Impact

**Goal:** Determine whether horses have condition-specific abilities (some handle mud, some don't).

**Queries:**

```sql
-- Compare same horse's performance on fast vs off tracks
WITH condition_splits AS (
    SELECT s.horse,
           CASE WHEN r.track_condition IN ('Fast', 'Firm', 'Good') THEN 'fast' ELSE 'off' END as going,
           rp.surprise,
           COUNT(*) OVER (PARTITION BY s.horse, 
               CASE WHEN r.track_condition IN ('Fast', 'Firm', 'Good') THEN 'fast' ELSE 'off' END) as n_in_condition
    FROM starters s
    JOIN races r ON r.id = s.race_id
    JOIN rkm_race_performance rp ON rp.starter_id = s.id
)
SELECT going, AVG(surprise), STDDEV(surprise), COUNT(DISTINCT horse)
FROM condition_splits
WHERE n_in_condition >= 3
GROUP BY going;

-- Individual horse condition preference
SELECT horse,
       AVG(CASE WHEN going = 'fast' THEN surprise END) as fast_surprise,
       AVG(CASE WHEN going = 'off' THEN surprise END) as off_surprise,
       AVG(CASE WHEN going = 'off' THEN surprise END) - AVG(CASE WHEN going = 'fast' THEN surprise END) as off_preference
FROM condition_splits
WHERE n_in_condition >= 3
GROUP BY horse
HAVING COUNT(CASE WHEN going = 'fast' THEN 1 END) >= 3
   AND COUNT(CASE WHEN going = 'off' THEN 1 END) >= 3
ORDER BY off_preference DESC;
```

**Output:** Whether track condition is a systematic factor, and whether individual horses have stable preferences. If so, on an off track we should adjust expected ratings based on a horse's condition history.

---

## 8. Run-Up Distance Effect

**Goal:** Determine whether run-up distance systematically inflates v0 measurements.

**Queries:**

```sql
-- Compare adj_v0 at tracks with different run-up distances
-- If longer run-ups inflate v0, the track offset should already capture this
-- But verify:
SELECT r.run_up, r.surface,
       CASE WHEN r.furlongs > 6.5 THEN 'route' ELSE 'sprint' END as zone,
       AVG(vc.adj_v0) as avg_adj_v0,
       COUNT(*) as n
FROM races r
JOIN starters s ON s.race_id = r.id
JOIN rkm_velocity_curves vc ON SPLIT_PART(vc.horse_key, '|', 1) = s.horse
    AND vc.surface = r.surface
    AND vc.distance_zone = CASE WHEN r.furlongs > 6.5 THEN 'route' ELSE 'sprint' END
WHERE r.run_up IS NOT NULL
GROUP BY r.run_up, r.surface, zone
ORDER BY r.run_up;
```

**Output:** Whether run_up correlates with adj_v0 after track offsets are applied. If yes, the normalization isn't fully capturing it.

---

## 9. Off-Turf Reliability

**Goal:** Flag races moved from turf to dirt and assess whether those performances should be trusted for the dirt curve.

**Queries:**

```sql
-- How often do off-turf horses underperform vs their dirt curve?
SELECT 
    r.off_turf,
    AVG(rp.surprise) as avg_surprise,
    STDDEV(rp.surprise) as std_surprise,
    COUNT(*) as n
FROM races r
JOIN starters s ON s.race_id = r.id
JOIN rkm_race_performance rp ON rp.starter_id = s.id
WHERE r.surface = 'Dirt'
GROUP BY r.off_turf;
```

**Output:** Whether off-turf runners have systematically different residuals. If so, their dirt observations should be down-weighted or excluded from curve fitting.

---

## 10. Limited-Form Races (Maidens, First-Time Starters)

**Goal:** Develop a framework for betting races where some or all participants have no prior performance curve — rather than automatically passing them.

**Context:** During simulations, maiden races and races with first-time starters were almost always passed because the model had no curves to work with. This created a cascading problem: any horizontal sequence (Pick 4/5/6) that included such a race became unplayable. Tracks deliberately include these races in multi-leg sequences because they add uncertainty that inflates payoffs. Passing them forfeits that value entirely.

### 10a. Crowd accuracy in limited-form races

**Question:** Is the public BETTER or WORSE at pricing horses with no form? Insiders (trainers, clockers, bloodstock agents) may know more than the public in these spots. Or the uncertainty may cause the crowd to anchor on superficial signals (sire, trainer, price paid at auction) and misprice systematically.

```sql
-- Compare favorite win rate in maiden races vs open races
SELECT 
    CASE 
        WHEN r.type LIKE '%MAIDEN%' THEN 'MAIDEN'
        ELSE 'NON-MAIDEN'
    END as race_category,
    AVG(CASE WHEN s.choice = 1 AND s.official_position = 1 THEN 1.0 ELSE 0.0 END) as fav_win_rate,
    AVG(CASE WHEN s.choice = 1 THEN s.odds END) as avg_fav_odds,
    COUNT(DISTINCT r.id) as n_races
FROM races r
JOIN starters s ON s.race_id = r.id
WHERE r.date BETWEEN '2012-01-01' AND '2015-12-31'
  AND r.number_of_runners >= 7
GROUP BY 1;

-- Favorite ROI in maidens vs non-maidens (is the fav overbet or underbet?)
SELECT 
    race_category,
    AVG(CASE WHEN s.choice = 1 AND s.official_position = 1 THEN s.odds ELSE -1.0 END) as flat_bet_roi
FROM ...;

-- Longshot (10/1+) win rate in maidens vs non-maidens
SELECT 
    race_category,
    AVG(CASE WHEN s.odds >= 10 AND s.official_position = 1 THEN 1.0 ELSE 0.0 END) as longshot_win_rate,
    COUNT(CASE WHEN s.odds >= 10 AND s.official_position = 1 THEN 1 END) as longshot_wins,
    COUNT(CASE WHEN s.odds >= 10 THEN 1 END) as longshot_starts
FROM ...;
```

**Output:** Whether maidens have higher/lower favorite win rates, whether longshots hit more often, and what the flat-bet ROI looks like by choice rank. This tells us: is the uncertainty random (public prices are still efficient) or systematic (specific biases to exploit)?

### 10b. Pool dynamics in limited-form races

**Question:** Do maiden races have smaller exotic pools (scared money stays away), more chalk concentration (everyone bets the one horse they "know"), or more dispersed betting (nobody knows, so they spread)?

```sql
-- Pool sizes: maiden vs non-maiden
SELECT 
    CASE WHEN r.type LIKE '%MAIDEN%' THEN 'MAIDEN' ELSE 'NON-MAIDEN' END as category,
    e.bet_type,
    AVG(e.pool) as avg_pool,
    AVG(e.pool::float / r.number_of_runners) as avg_pool_per_runner,
    COUNT(*) as n
FROM races r
JOIN exotics e ON e.race_id = r.id
WHERE e.bet_type IN ('EXACTA', 'TRIFECTA', 'SUPERFECTA')
  AND r.number_of_runners >= 7
  AND r.date BETWEEN '2012-01-01' AND '2015-12-31'
GROUP BY 1, 2;

-- HHI (betting concentration) in maidens vs non-maidens
-- Higher HHI = more concentrated on one horse = more vulnerable to upsets
SELECT 
    CASE WHEN r.type LIKE '%MAIDEN%' THEN 'MAIDEN' ELSE 'NON-MAIDEN' END,
    AVG(rm.hhi) as avg_hhi
FROM races r
JOIN race_metrics rm ON rm.race_id = r.id
GROUP BY 1;
```

**Output:** Whether maiden pools are thinner (less liquidity = harder to bet into) or have higher concentration (more opportunity for upset-based overlays). If HHI is higher in maidens, the crowd is MORE concentrated on chalk — meaning longshot exotic combos are MORE underlaid than in open races.

### 10c. "Bomb" frequency and exotic payoffs

**Question:** Do maiden races produce more extreme payoffs? If so, they're inherently valuable to include in horizontals.

```sql
-- Exotic payoff distribution: maiden vs non-maiden
SELECT 
    CASE WHEN r.type LIKE '%MAIDEN%' THEN 'MAIDEN' ELSE 'NON-MAIDEN' END as category,
    e.bet_type,
    AVG(e.payoff / NULLIF(e.unit, 0)) as avg_payoff,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY e.payoff / NULLIF(e.unit, 0)) as median_payoff,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY e.payoff / NULLIF(e.unit, 0)) as p90_payoff,
    AVG(CASE WHEN s_winner.odds >= 10 THEN 1.0 ELSE 0.0 END) as pct_longshot_winners
FROM races r
JOIN exotics e ON e.race_id = r.id AND e.bet_type = 'TRIFECTA' AND e.payoff > 0
JOIN starters s_winner ON s_winner.race_id = r.id AND s_winner.official_position = 1
WHERE r.number_of_runners >= 7
GROUP BY 1, 2;
```

**Output:** Median and P90 trifecta payoffs in maidens vs non-maidens. If maiden tris pay significantly more (because upsets are more frequent), this directly informs horizontal strategy: maiden legs ADD value to sequences when you can spread.

### 10d. Trainer patterns with first-time starters

**Question:** Some trainers are known for winning first time out (ready on debut). Others use first starts as education. This is observable in the data without needing workout info.

```sql
-- Trainer win% with first-time starters (no last_raced_date)
SELECT 
    s.trainer_last, s.trainer_first,
    COUNT(*) as n_firsters,
    AVG(CASE WHEN s.official_position = 1 THEN 1.0 ELSE 0.0 END) as win_rate_firsters,
    AVG(s.odds) as avg_odds_firsters
FROM starters s
JOIN races r ON r.id = s.race_id
WHERE s.last_raced_date IS NULL  -- first time starter
  AND r.type LIKE '%MAIDEN%'
  AND r.date BETWEEN '2010-01-01' AND '2016-12-31'
GROUP BY s.trainer_last, s.trainer_first
HAVING COUNT(*) >= 20
ORDER BY win_rate_firsters DESC;

-- Compare: does the public properly price trainer first-out records?
-- (if trainer wins 30% first out but their horses go off at odds implying 20%, they're underbet)
SELECT 
    trainer_tier,  -- bucket by first-out win rate
    AVG(actual_win_rate) as actual,
    AVG(implied_win_rate_from_odds) as market_implied,
    actual - implied as edge
FROM trainer_first_out_analysis
GROUP BY trainer_tier;
```

**Output:** Trainer first-out win rates. Whether the market properly prices this information. If certain trainers systematically win first out at higher rates than their odds imply — that's a DIRECT edge in maiden races even without individual horse curves.

### 10e. How to USE this in simulations

Based on the research outputs, the approach for maiden races becomes:

**If research shows maiden favorites are overbet (lower ROI than non-maiden favorites):**
- This is a "bad favorite" situation BY DEFAULT in many maiden races
- Spread wide against the chalk — the crowd is systematically wrong
- ITP principles apply: the uncertainty makes these BETTER exotic races, not worse

**If research shows trainer first-out patterns are mispriced:**
- Use trainer records as a partial substitute for individual horse form
- A first-time starter from a 30% first-out trainer at 8/1 has positive expected value
- Include them on top of exacta/trifecta tickets

**If research shows maiden exotics pay more on average:**
- These races are VALUABLE in horizontal sequences
- Don't pass them — spread them as "survival" legs with the understanding that the payoff boost when your other (high-conviction) legs hit more than compensates
- The Pick 5 that includes two maiden races isn't a problem — it's an opportunity because the payoffs are inflated by the uncertainty

**If research shows pools are thinner but more concentrated:**
- Small pools + crowd on one horse = classic ITP setup (get where nobody else is)
- Your spread tickets are going where the $48 recreational tickets can't reach

**For the simulation protocol:** Replace "PASS — no model data" with a structured assessment:
- What do we know about the favorite? (trainer record, breeding, public money patterns)
- Is the pool structure favorable? (concentrated on chalk = overlaid longshots)
- How does this race serve the SEQUENCE? (adds entropy to a horizontal = good if you spread)
- What's the minimum-information bet? (spread to the non-obvious horses, exclude the overbet chalk)

---

## Execution Priority

| # | Research | Blocks | Effort |
|---|---|---|---|
| 1 | Canonical race identification | Rating anchor | Low (query-only) |
| 2 | Scaling (ms per point) | Rating scale | Low (follows from #1) |
| 3 | Weight impact | Handicap race rating adjustment | Medium (regression) |
| 4 | Post position bias | Track-specific edge detection | Medium (per-track analysis) |
| 5 | Medication/equipment | Form prediction improvement | Medium (sequential join) |
| 6 | Trainer change | Form prediction improvement | Low (pre/post comparison) |
| 7 | Track condition | Conditional rating adjustment | Medium (within-horse analysis) |
| 8 | Run-up distance | Normalization validation | Low (quick check) |
| 9 | Off-turf reliability | Curve fitting quality | Low (quick check) |
| 10 | Limited-form races | Horizontal strategy + maiden assessment | Medium (multi-query) |

Items 1-2 block the rating system directly. Items 3-9 refine the model. Item 10 enables the simulation to handle full cards rather than cherry-picking only data-rich races.
