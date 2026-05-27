# Research Findings Summary

Actionable outputs from research-plan.md Items 0-13. Each finding is categorized by where it applies in the architecture.

---

## Rating System (RKM)

### Canonical Race & Scale (Items 1-2)

| Parameter | Sprint (6f) | Route (8f) |
|---|---|---|
| Anchor condition | CLM $5K-$10K, open/male, 4yo+, non-state-bred, Fast dirt |
| Anchor time | 71,577ms | 99,192ms |
| Scale | 58 ms/point | 77 ms/point |
| 1 length ≈ | 169ms ≈ 2.9 pts | 170ms ≈ 2.2 pts |

**Universal scale (Option B):** Single rating number system. Dirt defines 100. Turf/synthetic bridged via crossover horses. Turf canonical = CLM $16K-$25K (maps to ~112 on dirt scale).

### Class Ratings (6f sprint reference)

| Level | Rating |
|---|---|
| Maiden Claiming | 84 |
| CLM ≤$5K | 84 |
| CLM $5K-$10K (anchor) | 100 |
| CLM $10K-$16K | 106 |
| CLM $16K-$25K | 113 |
| Allowance | 114 |
| CLM $25K-$40K | 117 |
| AOC | 123 |
| Stakes (avg) | 134 |
| Champion-level | ~140-145 |

### Surface Decay Profiles (8f claiming route)

| Surface | Opening (ft/s) | Closing (ft/s) | Deceleration |
|---|---|---|---|
| Dirt | 55.1 | 49.9 | -9.4% |
| Synthetic | 53.8 | 50.7 | -5.7% |
| Turf | 55.8 | 53.9 | -3.3% |

Synthetic is between dirt and turf, closer to turf. A horse's turf curve is a better predictor of synthetic performance than their dirt curve for the stamina component.

### Cross-Surface Correlation (Item 11)

| Zone | v0 Correlation (dirt↔turf) | Decay Correlation | Avg Dirt−Turf Diff |
|---|---|---|---|
| Route | 0.34 | 0.41 | +3.85 ft/s |
| Sprint | 0.17 | 0.23 | +2.91 ft/s |

Low correlation = surface specialization is real. Cross-surface predictions need wide confidence bands.

### Data Quality (Item 0b)

- All years 1991-2017 valid for curve fitting (no timing precision cliff)
- Usable range for wagering research: 1999-2017 (exotic data starts 1998-99)
- The rkm-v3 spec's 1997 floor was overly conservative

### Factors That Don't Need Rating Adjustments

| Factor | Why no adjustment |
|---|---|
| Carried weight | Effect is ~0.008 ft/s/lb (negligible). Curve already absorbs typical weight. |
| Run-up distance | Surprise = 0.000 at all run-up distances. Track offset handles it. |
| Track condition (aggregate) | No systematic bias in surprise by condition. |

---

## Market Bias Layer (AN2 — wagering-analytics)

### A/E Tables by Factor

All relative A/E values are measured against baseline (0.80). Positive = underbet (value). Negative = overbet (avoid).

| Factor | Level | Relative A/E | Strength | N |
|---|---|---|---|---|
| **Off-turf race, favorite** | Fav in off-turf | **+7.5%** | Strong | 19K |
| **Blinkers OFF** | Equipment removed | **+9.3%** | Strong | 91K |
| Synthetic → Turf switch | Surface transfer | +6.0% | Strong | 47K |
| Trainer claim (top trainers) | First 3 starts, A/E>1.0 trainers | +15-30% | Strong (trainer-specific) | varies |
| First start after claim (all) | Any trainer | +3.4% | Moderate | 227K |
| First-time Lasix | Added Lasix | +1.5% | Weak but consistent | 106K |
| 5lb apprentice jockey | Bug rider | +3.1% | Moderate | 260K |
| Light carried weight (≤114) | Low weight assignment | +1.8% | Weak | 396K |
| **First-time Blinkers** | Equipment added | **-3.7%** | Moderate (avoid) | 164K |
| **Heavy carried weight (124+)** | Top weight | **-1.4%** | Weak (avoid) | 337K |
| **Turf → Dirt switch** | Surface transfer | **-3.4%** | Moderate (avoid) | 216K |
| **FTS (first-time starter)** | Debut as a group | **-3.0%** | Moderate (avoid unless trainer signal) | 270K |

### Post Position Bias

**Routes (aggregate):** Smooth inside bias. PP1 wins 13.4%, PP12 wins 8.6%. The market slightly overvalues inside posts (PP1 A/E is relatively worse than PP12 A/E after takeout normalization).

**Sprints:** Flatter middle (PP2-8 similar), posts 9+ suffer. PP3 is the best surprise post, not PP1.

**Track-specific extremes (routes):**

| Track | Inside Edge (PP1−PP8 win%) | Character |
|---|---|---|
| FP (Fairmount) | +6.8% | Extreme rail |
| DEL (Delaware) | +6.8% | Extreme rail |
| WRD (Will Rogers) | +6.7% | Extreme rail |
| RP (Remington) | +6.2% | Strong rail |

At biased tracks, the inside edge may exceed what the market prices.

### Maiden Race Structure (Item 10)

| Finding | Value | Implication |
|---|---|---|
| Fav win rate (maidens) | 36.0% | Higher than non-maiden (33.2%) |
| Fav A/E (maidens) | 0.837 | Best of any race type |
| Longshot A/E (maidens) | 0.698 | Worst value — avoid longshot darts |
| HHI (maidens) | 0.219 | More concentrated on chalk |
| Trifecta avg payoff | $348 vs $321 | 8% higher in maidens |
| FTS group A/E | 0.776 | Overbet as a group |

**Trainer FTS A/E (high-volume, A/E > 1.0):** O'Connell (1.14), Hone (1.14), Violette Jr (1.08), Dutrow Jr (1.07), Hollendorfer (1.06). These trainers' debut runners are systematically underbet.

### WCMI (to be computed in AN2)

Race-level market informativeness. Low WCMI = uninformed crowd = larger model edge. Expected patterns:
- Maiden races: lower WCMI (less form available)
- Stakes races: higher WCMI (more public info)
- Threshold: WCMI < 0.13 = strong opportunity for informed model

---

## Ticket Construction (race-day-sim)

### Off-Turf Races

The crowd under-adjusts. Favorites hold up MORE than expected (A/E = 0.884). Turf specialists on dirt underperform. Use the favorite strongly in exotic key positions. Fade turf-only horses underneath. In horizontals: treat off-turf legs as singling/narrow-spread opportunities.

### Maiden Races

Don't pass. Don't spread wide against chalk. The crowd concentrates on favorites AND favorites deliver here more than anywhere else. The value is in the *underneath* positions (2nd/3rd in trifectas) using trainer-signal horses, not in top position with longshots.

Approach:
1. Is there a high-A/E FTS trainer's horse in the field at value odds? → include underneath
2. Is the favorite from a high-FTS trainer too? → strong on top
3. Spread narrowly (fav + 1-2 trainer-signal horses) rather than wide

### Surface Switches

- Synthetic → Turf: include on tickets (underbet, form transfers well)
- Turf → Dirt: fade (overbet, form doesn't transfer, low correlation r=0.34)

### Equipment Changes

- Blinkers OFF: strong positive signal the market misses (+9.3% relative A/E). Include these horses.
- Blinkers ON: the market overvalues the visible change. Lean against first-time blinkers.
- First-time Lasix: slight positive but weak. Use as tiebreaker, not primary factor.

### Claimed Horses

- First 3 starts after claim: +3.4% relative A/E overall
- Trainer-specific: top claim trainers produce A/E > 1.15 (15%+ edge)
- This is a placement/intent signal, not a speed signal — surprise ≈ 0

### Trip Trouble (from narratives)

**Current-race vocabulary:**

| Term | Surprise | Meaning for next-race |
|---|---|---|
| pressed | -0.25 ft/s | Forced pace, depleted — but form still reliable |
| rushed | -0.13 | Used too early — may have more next time |
| checked | -0.10 | Significant interference — excuse for poor run |
| stumbled | -0.09 | Start trouble — discount this performance |
| bumped | -0.05 | Minor interference |
| wide | +0.03 | NOT negative — horse was doing well to be wide |

**Predictive value of previous-race trip:**

| Previous Race | Next Race Surprise | Next Win Rate |
|---|---|---|
| Won easily (drew off, geared down) | +0.097 | 19.9% |
| Hard effort (driving, all out) | +0.058 | 16.9% |
| Had trouble (bumped, checked) | +0.014 | 13.8% |
| Normal | +0.008 | 13.3% |

The "bounce" myth is backwards: horses that won easily come back BETTER, not worse. The trip-trouble "excuse" provides minimal predictive edge (+0.006 ft/s over baseline).

---

## What Remains

**Item 12 (Point-in-time trainer/jockey/sire stats):** Complete (sire blocked by data). See below.

---

## Trainer Decision Framework (Item 12)

Five measurable dimensions of trainer skill, each producing point-in-time A/E lookups:

### (1) Maiden Development — "Do they know what they have and when?"

| Trainer FTS Tier (point-in-time) | A/E | Spread from baseline |
|---|---|---|
| Elite (25%+ FTS record) | 0.851 | +9.7% |
| Strong (18-25%) | 0.826 | +6.5% |
| Average (12-18%) | 0.809 | +4.3% |
| Below avg (6-12%) | 0.782 | +0.8% |
| Poor (<6%) | 0.720 | -7.2% |
| Insufficient (<10 FTS) | 0.710 | -8.5% |

### (2) Claiming Eye — "How well do they spot opportunity?"

Top claim trainers (200+ post-claim starts) show A/E 1.06-1.31. The market partially adjusts but not fully. First 3 starts after claim is the window.

### (3) Class Placement — "How good at finding the right spot?"

| Class Move | A/E | Implication |
|---|---|---|
| Drop >30% | 0.823 | Underbet — horse is better than new level |
| Same level | 0.800 | Baseline |
| Rise >30% | 0.769 | Overbet — crowd chases recent wins |

Top drop trainers show A/E 1.2-1.4 when they specifically choose to drop a horse.

### (4) Freshening/Layoff — "Can they bring them back ready?"

| Days Off | A/E (all trainers) | Implication |
|---|---|---|
| 60-89 days | 0.818 | Mild layoff — slightly underbet |
| 90-179 days | 0.774 | Overbet — crowd gives too much credit |
| 180+ days | 0.718 | Heavily overbet |

Top layoff trainers (A/E > 1.1 off 90+ days) bring horses back genuinely fit. The market applies generic "rusty" discount even to elite freshening trainers.

### (5) Surface Switching — "Do they know when to change surface?"

Aggregate: synthetic→turf A/E = 0.850 (+6%). Trainer-specific switch A/E ranges from 0.80 to 1.80. The best surface-switching trainers have dramatic edge that the market misses entirely.

### Jockey Track Form (trailing 12m)

| Tier | A/E | Note |
|---|---|---|
| Average (10-15% at track) | 0.819 | Best A/E — competent but not overbet |
| Solid (15-20%) | 0.815 | |
| Hot (20%+) | 0.804 | Market fully prices their form |
| Cold (<10%) | 0.796 | |
| Unknown at track (<20 starts) | 0.764 | Worst — no track record = heavily overbet |

The key signal: a regular rider at this track (any competence level) is worth ~5% relative A/E vs an unknown.

### Sire Stats — PERMANENTLY BLOCKED

Breeding table contains winners only (1.85M rows, all official_position=1). Cannot compute sire FTS rates without sire data for ALL starters. The source PDF charts only include breeding data for winners — this is a limitation of the data source, not pdf-importer. Would require an external data feed (e.g., Jockey Club registry, Equibase commercial data) to resolve.

### Compound Effects

These dimensions stack. A horse that hits multiple positive dimensions simultaneously can have 20-30% composite edge:
- Claimed by elite claim trainer
- Dropping in class with a trainer who drops well
- Returning from layoff with a trainer who freshens well
- Switching to preferred surface

The AN2 trainer profile table should store per-trainer A/E for each of the 5 dimensions, consulted contextually during simulation.

---

## Jockey Impact

### Do jockeys matter? Both in ability AND market pricing.

| Jockey Tier (career win%) | N | Win% | A/E | Avg Odds |
|---|---|---|---|---|
| Elite (20%+) | 119K | 22.9% | **0.845** | 5.9 |
| Strong (15-20%) | 873K | 17.2% | 0.824 | 9.3 |
| Average (10-15%) | 1.3M | 12.6% | 0.804 | 14.5 |
| Below avg (<10%) | 779K | 7.8% | **0.742** | 23.8 |

Elite jockeys are still underbet (A/E = 0.845). Below-average jockeys are the worst value in racing (A/E = 0.742).

### Jockey Switch Signal

| Switch Type | N | Win% | A/E | Relative |
|---|---|---|---|---|
| Upgrade (+5% better jockey) | 273K | 14.9% | **0.841** | +4.7% |
| Same jockey | 1.2M | 14.4% | 0.803 | baseline |
| Lateral change | 1.2M | 11.7% | 0.802 | baseline |
| Downgrade (−5% worse jockey) | 292K | 9.5% | **0.730** | -9.0% |

Jockey upgrade = trainer intent signal ("this horse is ready, we want our best chance"). Downgrade = the market is buying yesterday's news with a worse jockey.

Same vs lateral change is identical (0.803 vs 0.802) — the "jockeys barely matter" camp is correct for *lateral* switches but wrong at the extremes.

### Trainer × Jockey Upgrade Interaction

Some trainers use jockey upgrades FAR more effectively (A/E 1.3-1.6 when upgrading vs 0.841 population). These trainers book the expensive rider *selectively* — not routinely — making the upgrade a concentrated intent signal. The market doesn't distinguish "trainer who always uses the top jock" from "trainer who specifically upgraded for THIS race."

For AN2: the trainer profile adds a 6th dimension:

| Dimension | Question |
|---|---|
| (6) Jockey upgrade skill | When this trainer upgrades jockeys, how much extra A/E does it produce? |

**Implementation priority (wagering-analytics first, then race-day-sim):**

1. **wagering-analytics:** Implement AN2 scripts:
   - `compute_wcmi.py` → `race_wcmi` table (low effort, immediate value)
   - `compute_ae_tables.py` → `factor_ae` table (A/E by factor × level)
   - `compute_trainer_profiles.py` → `trainer_ae_profiles` table (5 dimensions per trainer)
   - `compute_jockey_switch_ae.py` → jockey upgrade/downgrade signals
2. **race-day-sim:** Consume AN2 outputs at simulation startup (same pattern as `rkm_velocity_curves`)
3. **race-day-sim:** Update simulation protocol to incorporate market bias signals in handicapping + ticket construction
