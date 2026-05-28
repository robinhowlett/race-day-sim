# ITP Wagering Principles — Quick Reference

Verified against source transcripts (4 podcasts: BWTB Ep 41-42, Jason Beem, Better Betting, Plus The Points). Every principle below has direct transcript support.

---

## The Hierarchy

1. **Learn what NOT to bet** — the single most impactful improvement
2. Then learn what TO bet
3. Then learn how MUCH to bet

---

## Core Truth

> "A sharp bettor with an average opinion will always outperform a sharp handicapper who bets poorly."

The edge is in bet STRUCTURE, not in picking winners.

---

## Equity Over Survival

Horizontals are parlays with variables. In each leg, you're betting your per-combo cost to WIN on each horse you include.

- If the 2/1 wins your 4-deep leg: you get back $75 on $100 invested → LOST equity
- If the 4/1 wins: you get back $125 → GAINED equity
- If 3 of your 4 selections would LOSE equity when they win: "flashing stop sign"

**Goal: GAIN equity in every leg, not "get by."**

---

## Bad Favorites

The single most common lucrative spot, findable daily at all tracks.

Getting a favorite off the board in verticals = a 4-leg parlay (out of win, place, show, 4th). When the favorite fails, EVERYTHING else becomes an overlay.

**Before attacking a bad favorite in exotics:** Check will-pays/exacta board to confirm the favorite's exotic usage matches their win-pool price. If a 4/5 shot is used like a 2/5 in the gimmicks, attacking is even more profitable. If they're NOT being used as heavily in exotics as the win pool suggests, be cautious.

---

## Separation = Edge

> "When everybody's in the same boat, find another boat."

Your ticket's value comes from being where other tickets AREN'T. If the public lands on the same combinations, even winning produces mediocre returns.

**Usage ≠ Win Probability:** A 5/1 in a race with two 7/5 co-favorites has LOW exotic usage (everyone's on the 7/5s) → HIGH value. A 5/1 in a spread race where everyone goes 4-deep has MORE usage → LESS value.

**Connections bias:** An 8/1 with Chad Brown / Irad Ortiz gets more public usage than an 8/1 with an unknown trainer. Factor this into separation estimates.

---

## Sequence Bias

When you can predict where EVERY $144 ticket in the pool is heading — pounce.

- Getting outside in ONE leg of a biased sequence = good
- Getting outside in TWO legs = "usually real good"
- THREE legs = "jackpot"

The best multipliers come from SEQUENCE-level bias, not individual race bias.

---

## Ticket Construction

### Singling
- Strong favorite you LIKE: Single. If he loses, you lose.
- Mid-price horse you like: Single. Treat the horizontal like a win bet.
- Two even-money horses, can't separate: Pick one or don't play. "Never play caveman ticket using both."
- Blindly singling the 4/1 over using all three favorites: cuts ticket by 2/3, keeps the best portion.

### Spreading
- Bad favorite (your strongest opinion): Spread to ALL contenders. Don't leave holes.
- Spread race, no strong opinion: Use only the two longest prices. Or pass.
- No opinion at all: Don't play the sequence.

### Hurdles
A hurdle = deliberately reducing survival probability for massive equity gain when right.

- Singling a 5/2 when public goes 3-4 deep = hurdle
- Going 4-deep against a 4/5 = hurdle
- Each hurdle REDUCES combinations → play remaining combos at 2×, 3×, 4× minimum
- "Win percentage goes down, ROI increases astronomically"
- Only need 1-2 per sequence. Don't over-create.

### Funnel Tickets = Anti-Pattern
Wider early, narrower late = most common recreational mistake. Stems from "staying alive" psychology. A 20/1 in the LAST leg is worth more than a 20/1 in the first leg.

---

## Vertical Wagering

### Attack Conditions (need BOTH):
1. Bad favorite — a horse you believe is vulnerable
2. Depth of field — enough competitive horses to fill the board

Even when you LIKE a horse, you still want depth. "If you like the same horse everybody else likes, your horse is worthless."

Without depth, even a bad favorite likely hits the board. A 6-horse field is almost always a pass for verticals.

### The Basket of Bets
When conditions are right:
- Win bet on your best-value horse
- Exacta: price on TOP (kill shot)
- Trifecta/super boxes with your contenders, EXCLUDING the favorite
- Press layer: key your best horse on top for more

Increase basket volume as confidence grows. "The volume of the basket compensates for the ROI drop" — it's better to have 20% + 10% + 5% ROI combos than just the 20% alone.

### Kill Shot (Price On Top Only)
- Exacta: price on top, NEVER both ways
- "Why would I give away 10-15% of my profit on that bet?"
- Over thousands of bets, the kill shot approach maximizes profit

### Win-Only Type Horses
Some horses are "win-only" — they either win or are no good. Don't use them underneath in exotics. "Once I say he doesn't win, your love kind of wanes."

### First-Time Starters

**Note (2026-05-28):** This rule is the original ITP guidance. It has been
**superseded** by research findings — see `wagering-framework.md` §"Maiden
Races and First-Time Starters". The operational rule used by `ratings.py`
and the simulation protocol is:

- Generic FTS as a group are overbet (A/E = 0.776) — fading is correct.
- Elite-FTS-trainer horses (≥10 prior FTS, A/E ≥ 1.0) are an exception:
  legitimate inclusion underneath at 8/1+ value.
- Maiden favorites are the most reliable contender (A/E = 0.837).
- Don't spread to random longshots in maiden races.

`itp-principles.md` is a historical reference for ITP source material, not
the operational protocol. Where they conflict, `wagering-framework.md` wins.

Original ITP guidance (historical):

- Use on TOP only (a few dollars in case you're wrong about something you know nothing about)
- NEVER use underneath — you have no evidence they'll fill place/show

### No Saver Exactas
Using the favorite underneath "just in case" is defensive. If your opinion is "beat the favorite" — commit.

### Place Betting: Never
"I've never seen anybody run numbers where place betting has a higher ROI than win betting in any long-term sample."

The mechanism: your entire payout on a 20/1 shot depends on whether the FAVORITE finishes second. Your opinion means nothing to the place price — it's hostage to the chalk.

---

## Pool Selection

### Small Pools ($10K-$50K)
- Most players betting $24-$48 tickets — narrow coverage
- If you can play $288-$472 properly, you reach places they CAN'T
- "If you have a great opinion and bet properly at smaller tracks, you basically have a printing press"
- CAWs generally aren't involved in pools this small

### Carryovers
- When every dollar is plus-EV: bet MORE, be LESS efficient
- "Just like card counting — bet as much as you can when odds are in your favor"
- You get wiggle room to use a favorite here or there
- But limit "bad" things to small percentages of total play

### CAW Dynamic
- CAWs create visible usage CLUSTERS in will-pays (you can see what they like)
- CAWs NOT in smaller pools at smaller tracks — those are pure recreational money
- Win-pool odds are sharper due to CAWs; exotic pools remain less efficient
- When CAWs are absent = biggest edge for skilled recreational players

---

## Race Selection

1. Can I beat the favorite? (Is there a hole?)
2. Is there depth? (If fav fails, enough horses to fill the board?)
3. Can I get the favorite completely off the board? (Not just lose — 4th+)
4. Do I have a secondary opinion? (Someone I actually like)

"Every race tells you how to bet it" — no set formula. Some races = verticals only, some = horizontal inclusion, some = win bet only, some = pass.

---

## Information Edge

- Bet at the last minute when possible — most money comes in late
- Check exacta/trifecta will-pays to see where usage IS before committing
- Look at 60 races, bet 8

---

## Source Citation

All principles above verified against:
- Bet With The Best Podcast Episodes 41-42 (March 2025)
- Jason Beem Horse Racing Podcast (November 2021)
- Better Betting Podcast (March 2022)
- Plus The Points Podcast

Full spec with deeper context: `docs/specs/itp-wagering-framework.md`
