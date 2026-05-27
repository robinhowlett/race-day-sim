# Wagering Framework

A quantitative approach to pari-mutuel exotic wagering. Combines velocity-curve modeling (RKM), empirical market bias detection (AN2), and efficient ticket construction into a unified system.

---

## Foundation: Why Exotics, Not Win Bets

Pari-mutuel markets have one structural feature that distinguishes them from fixed-odds: **the takeout is applied once per pool, regardless of complexity.** A trifecta ticket and a win ticket both pay the same ~20-25% rake to the house. But a trifecta has n×(n-1)×(n-2) possible outcomes vs n for a win bet.

This means:
- In a 10-horse field: 720 trifecta combinations share the pool vs 10 win outcomes
- The crowd cannot cover all combinations — their $48 tickets reach maybe 12-24 of 720 outcomes
- Any combination NOT covered by the crowd is structurally underlaid

Win betting has a second problem: the favorite-longshot bias is well-documented and heavily arbitraged. Exotic pools are less efficient because the combinatorial explosion makes full-field coverage impossible for recreational bettors, and the multi-dimensional nature (order matters) introduces mispricing the crowd can't easily detect.

**Horizontal wagers (Pick 3/4/5/6)** add another structural advantage: single takeout on a multi-leg parlay. Three sequential win bets compound takeout: (1-0.17)³ = 57% retained. A Pick 3 retains ~75% (single 25% take). That's an 18 percentage point structural edge for the same effective bet — a multi-race opinion.

---

## The Three Layers

### Layer 1: Model (what should this horse's probability be?)

From RKM velocity curves + Benter logit combination:
- Each horse has a **Rating** (projected ability in rating points, 100 = canonical)
- Ratings map to model probabilities via the Benter combination (α=1.89 weight on model, combined with market odds)
- The model probability reflects physical ability adjusted for current form, surface, distance

### Layer 2: Market Bias (where is the crowd systematically wrong?)

From AN2 A/E tables — group-level mispricings invisible to individual-race handicapping:
- Trainer patterns (FTS record, claim skill, class placement, layoff fitness, surface switching, jockey booking)
- Equipment signals (blinkers off = +9% relative A/E, first-time Lasix = +1.5%)
- Jockey switches (upgrade = +5%, downgrade = -9%)
- Context signals (off-turf favorites underbet, surface switches from synthetic underbet)

These convert to **rating-point adjustments** added before computing Edge.

### Layer 3: Ticket Construction (how to express the edge efficiently)

Given edges identified in Layers 1-2, construct tickets that maximize expected value per dollar risked:
- Choose the right pool type (vertical vs horizontal, which exotic)
- Allocate coverage where edge is largest
- Size according to confidence and pool liquidity

---

## Edge Computation

For each horse in a race:

```
base_rating     = from velocity curves (adj_v0 + decay → projected time → points)
bias_adjustment = sum of applicable group-level point adjustments
adjusted_rating = base_rating + bias_adjustment
market_rating   = odds-implied rating (what rating produces this horse's market probability)
edge            = adjusted_rating - market_rating (±confidence band)
```

**Positive edge** = model thinks the horse is better than the market says.
**Negative edge** = model thinks the horse is worse than the market says (overbet).

The confidence band combines:
- Rating uncertainty from curve fit (sample size, residual variance)
- Group bias significance (Archie test — is the A/E deviation real or noise?)

---

## Race Selection

Not every race is worth betting. Selection is **Edge-driven** — does the model meaningfully disagree with the market?

### 1. Edge Distribution (primary filter)

A race is bettable when:
- At least one horse with Edge > +4 (±band still positive at worst case)
- AND/OR the favorite has Edge < -3 (overbet — others are structurally underlaid)
- Ideally both: an underbet contender AND an overbet favorite

A race with a 2/5 favorite and high market consensus is STILL bettable — perhaps the most bettable of all — if the model says that 2/5 should be 3/1. Concentrated markets that are wrong create the largest overlays on everything else.

### 2. Pool Structure

Even with edge, the pool must support the bet:
- **Pool density** (pool size / combinations) determines how much mispricing can exist per combination
- Minimum pool thresholds: $20K+ for trifectas, $50K+ for Pick 3/4
- Thinner pools at smaller tracks can have larger mispricings — but also more variance and harder to bet into

### 3. WCMI (informs HOW to bet, not WHETHER)

WCMI measures market consensus strength — how concentrated the crowd's opinion is. It is NOT a race selection filter (a concentrated market can be concentrated and wrong). Its role:

- **Ticket construction:** Low WCMI (dispersed market) → spread wider, the crowd is guessing so many combos are underlaid. High WCMI (concentrated market) → be specific about where you disagree.
- **Sizing modifier:** Low WCMI → model's incremental information value is larger (more noise to exploit). High WCMI → the crowd has done more of the work, so size conservatively unless your edge is large.
- **Horizontal leg typing:** Low-WCMI legs add entropy to sequences (good for payoff inflation when you spread). High-WCMI legs are cheap to navigate (single the consensus if you agree, or bet against it if you disagree — but don't spread "just in case").

---

## Ticket Construction Principles

### Choosing the Pool Type

| Situation | Best Pool | Why |
|---|---|---|
| One strong edge horse, moderate depth | Exacta/Trifecta (edge horse keyed on top) | Maximizes payoff from that horse winning |
| Overbet favorite + depth underneath | Trifecta with fav excluded or underneath only | Leverages the structural overlay when chalk fails |
| Multiple races with edge, one per race | Horizontal (Pick 3/4/5) | Single takeout on multi-leg opinion |
| Wide-open race, low WCMI, large pool | Superfecta spread | Thin coverage per combo = largest mispricing potential |

### Keying vs Spreading

**Key** (narrow, 1-2 horses in a position) when:
- Edge is concentrated in one horse (Edge > +6)
- The pool will reward specificity (a 4/1 on top of a tri pays dramatically more than underneath)
- In horizontal legs where one horse is clearly best value

**Spread** (3+ horses in a position) when:
- No single horse dominates but the favorite is overbet (Edge < -4)
- The race has low WCMI (crowd is guessing — you don't need to be precise, just different from them)
- In exotic underneath positions where multiple mid-priced horses are viable

### The Equity Test (per combination)

Every combination in a ticket has an implicit cost (total ticket / number of combos) and an expected return (projected payoff × probability of hitting). Before finalizing:

```
For each combo in the ticket:
  cost_per_combo = total_wager / n_combinations
  projected_payoff = from AN1 payoff model (given these horses' odds)
  hit_probability = from model probabilities (joint probability of this exact order)
  expected_value = projected_payoff × hit_probability
  equity_ratio = expected_value / cost_per_combo
```

- **equity_ratio > 1.0**: positive expectation — include
- **equity_ratio < 0.8**: diluting the ticket — consider removing
- If the majority of combinations are below 1.0, the ticket is poorly constructed regardless of whether one combo is great

### Horizontal Construction

In a multi-leg sequence (Pick 3/4/5/6):

**Per-leg assessment:**
- What's your edge in this leg? (Edge of best horse, or depth of edge if spreading)
- What's the WCMI? (Low = spread, high = narrow/single)
- What's the role of this leg in the sequence? (conviction leg vs uncertainty leg)

**Leg types:**

| Leg Character | Strategy | Ticket Width |
|---|---|---|
| High-conviction (strong edge, high WCMI, clear best horse) | Single or A/B | 1-2 horses |
| Moderate (edge exists but uncertain, moderate WCMI) | Narrow spread | 3-4 horses |
| Open (low WCMI, no standout, multiple edges) | Wide spread | 4-6 horses |
| No opinion (no measurable edge, random) | **Don't play this sequence** | — |

**Constraint:** Every horse in every leg should have equity_ratio > 1.0. Including a horse "to survive" that would lose equity if it won is diluting the ticket.

**Sequence-level edge:** The value of a horizontal isn't just the sum of per-leg edges. It's amplified when your opinions DISAGREE with the crowd across multiple legs simultaneously. If the crowd's $48 ticket uses the favorite in legs 1, 3, and 4 — and you're against the fav in legs 1 AND 3 — you're reaching combinations that virtually no recreational ticket touches.

---

## Context-Specific Guidance

### When the Favorite Is Overbet (Edge < -4)

The most common profitable setup. The overbet favorite means ALL other horses are relatively underlaid — but especially in exotic positions involving the favorite's absence.

- **Vertical:** Key contenders (Edge > +3) on top of trifectas, with mid-priced depth underneath. Exclude the favorite entirely from top position.
- **Horizontal:** In this leg, spread to 3-4 non-favorites. The payoff multiplier when the fav fails in a horizontal leg is enormous because most public tickets die here.
- **Sizing:** Larger allocation — this is a structural edge (the overbet creates overlay across all non-chalk combinations).

### Off-Turf Races

Research shows favorites in off-turf races have A/E = 0.884 (highest of any context). The crowd over-discounts the disruption.

- **Vertical:** Use the favorite strongly in key positions (top of exacta, included in trifecta). Fade horses with turf-only form (they're overbet — Turf→Dirt A/E = 0.775).
- **Horizontal:** Treat as a singling/narrow-spread leg. The favorite holds up more than usual here.

### Maiden Races and First-Time Starters

Research shows maiden favorites are the most reliable (A/E = 0.837) and FTS as a group are overbet (A/E = 0.776). This contradicts the instinct to "spread wide because anything can happen."

- **The crowd concentrates on chalk** (HHI = 0.22) — meaning the UNDERNEATH positions in exotics are where value lives, not the top position
- **Trainer FTS A/E** is the primary contender signal: an elite FTS trainer (20%+ record) at 8/1 is a legitimate inclusion underneath
- **Don't spread to random longshots:** maiden longshot A/E = 0.698 (worst value in the database). Only include longshots with a specific trainer/jockey signal.
- **Horizontal:** Maiden legs are moderate-spread legs (favorite + 1-2 trainer-signal horses), not wide-spread legs

### Claimed Horses (First 3 Starts)

Trainer-specific. Average claim = +3.4% relative A/E. Top claim trainers = A/E > 1.15.

- If the trainer's claim A/E is above 1.0: include on tickets, the market hasn't fully priced the expected improvement
- Combine with other signals (dropping in class, jockey upgrade) for compound edge

### Surface Switches

- **Synthetic → Turf**: +6% relative A/E. Market undervalues this form transfer. Include on tickets.
- **Turf → Dirt**: -3.4%. Market overvalues. Fade unless the horse has established dirt form.
- **Any switch by a top-switch trainer** (A/E > 1.2): the trainer knows something. Include.

### Equipment Changes

- **Blinkers OFF**: strongest equipment signal (+9.3% relative A/E). The market ignores removal. Always factor in.
- **First-time Blinkers**: market overvalues the visible change (-3.7%). Lean against.
- **First-time Lasix**: weak but positive (+1.5%). Use as tiebreaker, not primary.

---

## Sizing

### Base: Quarter-Kelly

```
kelly_fraction = 0.25 × (edge / odds)
```

Capped at 5% of bankroll per race (MAX_EXPOSURE).

### Modifiers:

| Condition | Adjustment |
|---|---|
| WCMI < 0.10 (very uninformed) | 1.5× base sizing |
| WCMI > 0.20 (well-informed) | 0.5× base sizing |
| Confidence band crosses zero | 0.25× (speculative only) |
| Multiple independent edges compound | 1.25× per additional confirmed bias |
| Pool carryover active | Up to 2× (every dollar is plus-EV) |
| Thin pool (density < $5/combo) | 0.75× (more variance, harder to execute) |

---

## What This Framework Does NOT Do

- **Pick winners.** The goal is positive expected value across a portfolio of bets, not predicting individual race outcomes.
- **Guarantee profits in small samples.** Edge in pari-mutuel markets is thin (5-15% on the best spots). Short-term variance dominates. A 20-race sample tells you almost nothing.
- **Replace judgment entirely.** The model provides quantitative inputs. Ticket construction still requires decisions about which pools to target, how to balance coverage vs concentration, and when to pass. The numbers inform those decisions; they don't make them automatically.
- **Work without selectivity.** Betting every race = paying takeout on noise. The framework works by being SELECTIVE — filtering to the 10-15% of races where measurable edge exists and constructing tickets that efficiently exploit it.
