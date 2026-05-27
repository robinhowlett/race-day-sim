# Simulation Protocol

Step-by-step protocol for running a valid blinded race day simulation.

Core references:
- `docs/wagering-framework.md` — quantitative wagering principles (Edge-driven, multiplicative bias)
- `docs/research-findings.md` — empirical calibration data for all factors
- `docs/rating-calibration-plan.md` — rating scale and display format

---

## Pre-Simulation: Race Day Selection

1. **Hash-based selection** — user provides any text string. `scripts/pick_sim_day.py` hashes it to deterministically pick from 44K candidate days. Candidates are pre-filtered: ≥8 races, ≥7 trifecta results, avg field ≥7, avg tri pool ≥$10K, no Grade 1/2 stakes (contamination risk).
2. **Or direct selection** — user provides track + date if they have a specific scenario to test. Avoid marquee days (Kentucky Derby, Breeders' Cup, Belmont, Travers, etc.) where LLM training data contamination is likely.
3. **Confirm data** — verify card loads via `load_pre_race_card()` + `load_market_bias()`.

---

## Step 1: Load Pre-Race Data (BLINDER ON)

Three data loads at simulation start:

1. **`load_pre_race_card()`** — race card + velocity curves + current form
2. **`load_pool_sizes()`** — exotic pool sizes (public pre-race information)
3. **`load_market_bias()`** — point-in-time trainer/jockey/equipment signals (all backward-looking from race date, no future leakage)

### ALLOWED pre-race:
- `races`: number, furlongs, surface, type, conditions, purse, number_of_runners, off_turf
- `starters`: program, horse, odds, choice, trainer_last, jockey_last, weight, jockey_allowance
- `rkm_current_form`: current_v0, current_decay, career_v0, career_decay, v0_trend, n_recent_races, days_since_last
- `rkm_velocity_curves`: v0, decay_rate, adj_v0, adj_decay, n_observations, n_races, surface, distance_zone
- `exotics`: pool (size only — NOT payoff, NOT winning_numbers)
- `races`: total_wps_pool
- **Market bias signals** (all point-in-time): trainer FTS/claim/drop/layoff/switch A/E, jockey career tier, jockey switch type, equipment changes, surface switch, class move

### FORBIDDEN before bet commitment:
- `starters.finish_position`, `starters.wagering_position`, `starters.winner`
- `exotics.payoff`, `exotics.winning_numbers`
- `rkm_race_situations` — **the entire table.** The stored `has_vulnerable_fav` flag was computed using `pace_scenario` from `rkm_race_performance`, which derives from actual fractional splits (post-race). Even columns that look pre-race computable (`fav_v0`, `fav_edge`) are stored alongside tainted outputs and querying the table risks reading post-race columns (`fav_finish_position`, `fav_missed_board`, `exacta_payoff`, etc.). Compute your own vulnerability assessment from the raw inputs instead.
- `rkm_race_performance` — uses actual fractional times (post-race)
- Any column that encodes how the race actually ran

**To assess favorite vulnerability without rkm_race_situations:** query `rkm_velocity_curves` for decay rates (compare fav's decay to field median), compute your own pace prediction from the field's adj_v0 distribution, and derive edge from odds-implied probability vs model probability (if `rkm_market_analysis` is available, its `edge` column uses pre-race model_prob vs odds_prob — but verify this is not trained on finish_position).

---

## Step 2: Pool Assessment

Before handicapping individual races, assess WHERE the money is:

### Pool metrics to compute per race:

| Pool type | Combinations formula | What it tells you |
|---|---|---|
| WPS | n (win), n (place), n (show) | Total handle / liquidity of the race |
| Exacta | n × (n-1) | 2-horse ordering depth |
| Trifecta | n × (n-1) × (n-2) | 3-horse ordering depth — primary exotic target |
| Superfecta | n × (n-1) × (n-2) × (n-3) | Deep exotic — very thin per-combo density |
| Pick 3 | product of field sizes across 3 legs | Horizontal sequences |
| Pick 4/5/6 | product of field sizes across legs | Larger horizontals — bigger pools, more combinations |

For each pool, compute:
- **Pool density** = pool size / possible combinations. Lower density = more mispricing opportunity (thinner coverage per combo by the public).
- **Minimum meaningful unit** — what's the smallest bet that produces a meaningful return if it hits? A $0.10 trifecta in a $50K pool can return thousands, but a $0.10 superfecta in a $150K pool needs an extreme longshot to matter.

### Decision framework:
- **Large pool + large field (low density)** = best opportunities for separation. The public can't cover everything — combos involving non-obvious horses will be underlaid.
- **Small pool + small field (high density)** = limited separation. The public covers most outcomes. Win bets may offer better value than exotics here.
- **Superfecta pools** — very high combination counts make these inherently thin. Even modest opinions that go against the crowd can find massive overlays. But the hit rate is very low.
- **Horizontal pools** — take out once vs parlaying verticals (which compounds takeout per leg, ~17% × n legs). A Pick 3 at 25% takeout vs 3 parlayed win bets at 17%³ effective ≈ 43%. That's ~18% structural advantage. But: only matters if the pool is liquid enough ($20K+) and you have at least one opinionated leg.
- **No opinion in ANY leg of a horizontal** = don't play it. The takeout reduction doesn't help if you're just spreading chalk in every leg.

---

## Step 3: Handicapping (Per Race)

For each race, compute the unified ratings table via `format_race_ratings()`:

```
Program  Horse         Rating  Market  Edge     Form   Confidence  Bias Factors
3        Willy Pay     112     106     +6 (±3)  +4     HIGH        [jock_upgrade, claimed]
7        Tinitus       105     108     -3 (±3)  -3     HIGH        []
1        Gamblin Fever 106      98     +8 (±6)  +11    MOD         [blinkers_off, drop]
```

All values in rating points (1 pt = 58ms sprint / 77ms route ≈ 0.3-0.5 lengths).

Then assess:

### A. Assess the favorite
Using Edge (is the favorite overbet?), form data (v0_trend, decay), and context:

**Inputs to the assessment:**
- **Decay rate relative to field:** Is the favorite's decay above or below the field median? A favorite with decay 0.5+ above field median fades faster than competitors — vulnerable to contested pace.
- **v0_trend:** Positive = improving (getting faster recently). Negative = declining (losing speed). A declining favorite at short odds is the core "bad favorite" signal.
- **Odds-implied probability vs model probability:** If the model gives the favorite negative edge (overbet relative to ability), that's a red flag.
- **Field composition:** Multiple horses with competitive adj_v0 (within 2 ft/s of best) = depth that can exploit a faltering favorite.
- **Pace projection:** If multiple speed types will contest the lead AND the favorite is a speed horse with high decay, the pace will hurt them specifically.
- **Layoff / recency:** A favorite with 0 recent races or 180+ days off has uncertain current form regardless of career ability.

**The judgment is qualitative, not a precise number.** We don't have a calibrated model that outputs P(fav top 3) = 63.2%. What we have is directional signals that tell us: "this favorite looks strong/moderate/vulnerable given the specific race context." The ticket structure follows from that judgment — strong favorites get respected in ticket construction, vulnerable ones get attacked.

### B. Predict the pace
Compute from the field's adj_v0 and decay_rate profiles (NOT from `rkm_race_performance.pace_scenario`, which is post-race).

**The key insight:** adj_v0 tells you who WANTS to be on the lead — it's initial speed, the ability to break sharply and establish position. But what matters for pace PRESSURE depends on race distance, because the energy systems involved are different.

#### What adj_v0 does and doesn't tell us

**Empirical reality (from 100K+ race sample, dirt 2012-2015):**
- The horse with the highest adj_v0 in a race wins only 12-16% of the time
- They miss the board (4th+) roughly 60-65% of the time regardless of sprint or route
- Having a bigger v0 gap over the 2nd-fastest horse provides almost no additional win rate advantage — in routes, lone speed horses (3.0+ gap) actually win LESS (11.7%) than contested speed (13.0%)

**This means:** adj_v0 is NOT a predictor of who will win. It's a characterization of who will be ON THE LEAD and therefore who will expend energy early. The pace prediction question isn't "who wins?" — it's "what energy pattern will unfold, and who does that pattern help or hurt?"

#### Distribution context

- Sprint adj_v0 values: mean 64.4, std 2.1, IQR 2.7 (dirt)
- Route adj_v0 values: mean 60.8, std 1.9, IQR 2.4 (dirt)
- Typical within-race gap (1st to 2nd): median 0.88 (sprint), 0.77 (route)

The distributions overlap heavily. A gap at the median or below suggests contested pace; a gap at the 75th percentile or above (1.7+ sprint, 1.5+ route) suggests clearer separation on the lead.

#### Sprints (≤ 6.5f): Anaerobic-dominant

In sprints, early position matters because there's less distance to recover lost ground. The race is often decided in the first 2-3 furlongs.

- Contested pace in sprints is LESS destructive than in routes — there's less time to accumulate fatigue. A speed horse can duel and still hold (they fade less over 6f than 9f).
- BUT: when 3+ speed types are packed together (all within 1.5 ft/s), the compounded early pressure can create room for stalkers.
- Key nuance: in sprints, decay rate differences are SMALLER in their effect because the race is short. A horse with decay 4.0 loses 4.0 × (6f × 660ft / 1000) = 15.8 ft/s over the race. A horse with decay 3.0 loses 11.9. That 3.9 ft/s difference at the wire CAN be overcome by a 2+ ft/s head start.

#### Routes (> 6.5f): Aerobic threshold dominates

In routes, energy management matters more than raw initial speed. Running above aerobic threshold for 8+ furlongs is unsustainable.

- The median gap is tighter (0.77 ft/s) because extreme speeds self-select out of route fields
- **The critical question is NOT "who is fastest?" but "if they take the lead, can they SUSTAIN it?"**
- This is where decay rate becomes decisive:
  - Leader has LOW decay (< field median): can sustain. Lone speed + low decay in a route = extremely dangerous. This horse "controls from the front on an easy lead" — hard to run down.
  - Leader has HIGH decay (> field median + 0.3): will fade. High v0 + high decay = they WANT the lead but CAN'T keep it. When two of these are in the same race, they'll burn each other out and collapse.
- A horse with decay 2.0 at 9f loses 2.0 × (9 × 660 / 1000) = 11.9 ft/s over the race. A horse with decay 0.5 loses only 3.0 ft/s. That 8.9 ft/s difference at the wire overwhelms almost any initial v0 advantage.

#### Pace profile classification

Combine adj_v0 position with decay rate to classify each horse's likely running style:

| adj_v0 (relative to field) | Decay rate | Profile | Implication |
|---|---|---|---|
| Top 2-3 | Low (< median) | Sustained speed | Dangerous. Can lead and hold. |
| Top 2-3 | High (> median + 0.3) | Speed-and-fade | Wants lead but vulnerable. Creates pace for others. |
| Middle | Low | Stalker/closer | Benefits from contested pace. Grinds past faders. |
| Middle | High | One-dimensional | Limited; usually off the board |
| Bottom third | Negative/very low | Deep closer | Needs fast pace to have any chance. Lives on collapses. |

#### Pace implications for betting

- **CONTESTED in a route + leader(s) have HIGH DECAY** → the strongest betting signal. Speed will collapse. Low-decay horses in the middle/back of the v0 distribution become the core of your ticket. The favorite, if a speed-and-fade type, is specifically vulnerable.
- **LONE SPEED + LOW DECAY in a route** → respect this horse. They can dictate an easy tempo and never come back. Fighting this horse is usually wrong unless you have evidence of decline (negative v0_trend).
- **CONTESTED in a sprint** → less directionally predictive. Speed can duel and still finish 1-2 in sprints. Look at other factors (form trends, trainer patterns).
- **No clear pace dynamic** → pace alone doesn't give an edge. The race will be decided on pure ability (adj_v0 at the distance being run minus accumulated decay).

### C. Identify contenders
Rank by:
1. **Current form trend** (v0_trend) — who is IMPROVING?
2. **Adjusted v0** relative to field — who has the highest raw ability?
3. **Decay rate** — who maintains speed best at this distance?
4. **Odds** — does the price justify the risk?

### D. Look for career-wide evidence on longshots
For any horse 10/1+ that has:
- High career adj_v0 on the relevant surface/distance zone (even if current form is down)
- Negative decay rate (career stayer profile)
- Previous wins at this level or higher
- A trainer/track pattern worth noting

These horses go into wider exotic spreads in 2nd/3rd positions. The model might flag them as declining, but career ability doesn't disappear entirely.

### E. Determine bet structure
Based on the qualitative favorite assessment and field depth:

| Favorite assessment | Primary structure | Secondary |
|---|---|---|
| **Strong** (low decay, improving, no pace pressure) | Fav on top OR fav in 2nd. Prices underneath. Small fav-excluded flyer only. | Don't fight — look for value in underneath positions |
| **Moderate** (mixed signals, some concern) | Mix: fav in 2nd/3rd structures + some fav-excluded combos | Win bets on best alternate at value |
| **Vulnerable** (declining, high decay, contested pace, overbet) | Primarily fav-excluded. Spread wide underneath. | Fav in 3rd at lighter weight as hedge |

The key question: "Does the favorite have negative Edge?" If Edge < -3 (±band), the entire non-chalk field is structurally underlaid. If Edge is neutral/positive, the favorite deserves respect in ticket construction.

---

## Step 4: Bet Construction

Every bet must be justified by a positive expectation argument. The structure of each ticket is determined by the specific opinions formed in Step 3 — not by templates.

### The Core Principle: Overlays and Underlays in Pari-Mutuel

In a pari-mutuel pool, when one horse is OVERBET (underlay), every other horse in the pool becomes relatively UNDERBET (overlay). This is multiplicative in exotics:

- If a favorite is a 10% underlay in the win pool, combinations involving that favorite in exactas/trifectas are underlaid by more (because the underlay compounds across positions).
- Conversely, combinations EXCLUDING an overbet favorite are overlaid by more than the individual horse overlay would suggest.
- The bigger the crowd's mistake on the favorite, the bigger the equity available on every other combination.

This is why "bad favorites" are so lucrative in exotics — the crowd's error on one horse inflates the value of EVERY combination that excludes them. Your edge isn't just "I think the fav loses" — it's that the entire exotic pool is mispriced in your favor when you're right.

### Translating Opinions to Bets

The question for every race is: **where does the model disagree with the crowd, and which pool type best exploits that disagreement?**

**If your opinion is about WHO WINS (a specific horse is underbet):**
- Win bet is the purest expression
- Exacta with that horse on top extends the opinion into a higher-paying pool
- The horse's odds determine whether win or exotic is the better expression — a 3/1 you think should be 2/1 has modest win edge but could anchor a high-paying trifecta

**If your opinion is about WHO LOSES (a favorite is overbet/vulnerable):**
- Exotics excluding the favorite are where the equity concentrates
- The more positions you can exclude them from, the more the overlay compounds (trifecta > exacta > win for this opinion type)
- Spread wide to multiple alternatives — you don't need to know WHO wins, just that the fav doesn't

**If your opinion is about the PACE SCENARIO (multiple speed types will duel):**
- Closers benefit — but which closers? Low-decay horses in the middle of the v0 distribution
- This is an opinion about a GROUP of horses, not one horse — structure tickets around the group

**If you have BOTH (vulnerable fav + specific alternative you like):**
- This is the ideal basket: win bet on your pick, kill-shot exacta (your pick over the fav if fav hangs around, or your pick over other contenders), trifecta with your pick on top and fav excluded or in a minor position
- The basket ensures you cash something if you're right about either piece (fav loses OR your pick wins)

### Position Construction in Verticals

Who goes where in a trifecta is not a formula — it's an expression of opinion:

- **On top (1st position):** Horses you genuinely believe can WIN. This is your strongest filter. It might be 1 horse (a single — the hurdle) or 3 horses (if the race is truly open). The choice depends on whether you have a specific win opinion or a general "beat the fav" opinion.
- **2nd position:** Horses who can realistically finish 2nd given who you think wins. Consider: if your top pick wins, who fills behind them? If the fav is vulnerable, where do they end up — do they hang around for 2nd/3rd or completely collapse?
- **3rd position:** Cast wider. Include any horse with career evidence, positive form signals, or simply the ability to occasionally hit the board at the distance. This is where 10/1+ shots with high career adj_v0 or negative decay belong — horses the public ignores but who have demonstrated the underlying ability.

The width at each position is proportional to your CONFIDENCE in the opinion for that position. Strong opinion on top = narrow. Uncertain about who fills behind = wide.

### Horizontals

Horizontal play is justified when:
1. The pool's takeout reduction vs parlay creates structural equity
2. You have at least one leg where your opinion gives you a HURDLE (separation from the crowd)
3. The equity math works: in every leg, would the horses you include GAIN equity if they won? (Would their odds return more than your per-combo investment?)

Leg construction:
- **Hurdle legs** (strong opinion): narrow — single or double. This is where you gain equity by being different from the crowd.
- **Survival legs** (no strong opinion): spread to avoid dying, but ONLY to horses whose odds would gain equity if they won. Don't use short-priced favorites in a spread race just to "stay alive" — that's losing equity.
- **Key insight:** a 20/1 shot in the last leg is worth more than a 20/1 in the first leg (because everyone can see the first leg on the board before betting later legs, so late longshots get less public play and are more often overlaid).

### Sizing

Sizing follows from the strength of your edge estimate, not a fixed percentage:
- Stronger opinion + bigger pool + more separation from crowd = larger allocation
- Weaker opinion + smaller pool + uncertain edge = smaller allocation or pass
- Hard caps to avoid ruin: no single race above 5% of bankroll, no single day above 25%
- Minimum meaningful unit: $2 per combo (below this, wins don't justify the tracking overhead)

---

## Step 5: Commitment

State ALL bets in full before requesting reveal:
- Every ticket with exact combinations and dollar amounts
- Total invested per race
- Total invested on the day
- The REASONING behind each bet (what has to happen for it to cash)

No changes after commitment. No adding bets race-by-race after seeing prior results.

---

## Step 6: Reveal

Query actual results:
- `starters.finish_position` for all races
- `exotics.payoff` and `exotics.winning_numbers`
- Pick 3/4/5/6 payoffs

---

## Step 7: Evaluation

For each bet:
- Did it cash? At what payoff?
- Was the reasoning correct even if the bet lost? (right horse wrong position, etc.)
- Was the fav assessment correct? (projected vulnerable → did miss? projected solid → did hit?)

Day summary:
- Total invested, total returned, P&L, ROI
- Cumulative running total across all simulation days
- Key lessons / pattern observations

---

## Limitations of Retrospective Simulation

- **No will-pays available.** In live betting, a sharp bettor checks exacta/trifecta will-pays to confirm where usage clusters before committing. In historical simulation, we can't observe real-time pool dynamics. We can only infer likely usage from odds structure (heavy favorite = likely over-used in gimmicks) and connections (leading trainer/jockey = higher public use than unknowns at same odds).
- **Closing odds are the best available proxy for market consensus**, but in reality sharp money arrives late. Our "odds" column is the final tote price, which is sharper than what most bettors saw when they placed tickets.
- **No carryover information.** We don't track whether pools had carryovers on the simulation date, which would alter optimal strategy (bet more, be less precise in plus-EV pools).

---

## Common Mistakes to Avoid

1. **Reading post-race data before betting** — the `fav_finish_position` column exists for validation AFTER the reveal, not before.
2. **Applying AN1 stats as rigid filters** — "fav in 2nd/3rd = overlay" is an AGGREGATE finding. Use model P(fav top 3) to determine if this specific fav belongs in that structure.
3. **Too narrow in underneath positions** — when you identify the right situation (vulnerable fav) but miss the exotic because a 15/1 shot filled 2nd or 3rd that you didn't include.
4. **Playing for fun** — betting races with zero model data just because they have big pools. No opinion = no bet.
5. **Ignoring pool mechanics** — a $15K Pick 3 pool behaves differently than a $150K one. Consider density, liquidity, and whether whales dominate.
6. **Confusing recognition with knowledge** — if a horse name is familiar from training data, that's contamination. Flag it.

---

## What the Research Provides (see `docs/research-findings.md`)

### AN1 (Exotic Structure)
| Finding | Use as |
|---|---|
| "Price on top, fav 2nd/3rd" = 15-21% overlay | Structure preference when P(fav top 3) is high |
| Exacta overlay strongest with price over fav | Directional preference in keying |
| Pick 3 has structural equity vs parlayed wins | Justification for horizontal play |
| Stern k=0.81 | Place/show correction for Harville fair values |

### AN2 (Market Bias — feeds into Edge via bias_multiplier)
| Signal | Relative A/E | Strongest when |
|---|---|---|
| Blinkers OFF | +9.3% | Market ignores equipment removal |
| Off-turf favorite | +7.5% | Use fav in key positions, fade turf-only horses |
| Synthetic → Turf switch | +6.0% | Form transfers better than market expects |
| Jockey upgrade | +4.7% | Trainer specifically booked better rider |
| Trainer claim (top trainers) | +15-30% | First 3 starts, trainer-specific |
| First-time Lasix | +1.5% | Weak but consistent |
| Jockey downgrade | -9.0% | Market still pricing old jockey's form |
| First-time blinkers | -3.7% | Crowd overvalues visible change |
| Turf → Dirt switch | -3.4% | Turf form doesn't carry |

### Model (RKM + Current Form)
| Signal | Meaning | Betting implication |
|---|---|---|
| Positive v0_trend / high Edge | Running faster than career AND market underprices it | Core of ticket — key on top |
| Negative v0_trend + short odds | Declining favorite | Negative Edge — spread against |
| Low decay + high v0 | Sustained speed (stayer on the lead) | Dangerous; respect in ticket structure |
| High career_v0, low current_v0 | Former class, currently below peak | Potential upside if placed right (class drop, equipment change) |
| career_v0 - current_v0 > 2 + improving trend | Comeback in progress | Market may be slow to adjust — check Edge |
| High adj_v0 | Raw speed ability | Could threaten at any time, especially in sprints |
| Days since last > 180 | Long layoff | Uncertain — career curves valid but current form unknown |
| n_recent_races = 0 | No recent form | Cannot compute v0_trend reliably; rely on career curve only |

---

## Positive Expectation Checklist

Before committing any bet, verify:

- [ ] Do I have a genuine edge? (model sees something the market doesn't)
- [ ] Is the pool large enough to absorb my bet without moving the odds?
- [ ] Is the bet structured to maximize payoff if I'm right? (kill shot direction, not both-ways)
- [ ] Have I spread wide enough underneath to catch longshots in the frame?
- [ ] Am I betting with MY opinion, or am I chasing aggregate statistics?
- [ ] Would ITP play this ticket? (equity in every leg of horizontals, separation from the crowd)
