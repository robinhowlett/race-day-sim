# Edge Calibration Issue — Why 0/15 Conviction Picks Failed

## The Bug

`odds_to_rating()` uses a rank-based mapping: it takes the horse's percentile in the odds distribution and maps it to the same percentile in the field's RATING distribution. This means:

- Market rating is a reshuffled version of the model's own ratings
- Edge = how much the model disagrees with the market about ranking ORDER
- Edge magnitude is a function of the field's rating SPREAD, not a calibrated measure

## Why This Overstates Edges

Field A: ratings [120, 110, 100, 90, 80]. Spread = 40 pts.
Field B: ratings [105, 102, 100, 98, 95]. Spread = 10 pts.

If the model ranks horse X first but the market ranks them third:
- In Field A: edge ≈ +20 to +40 (because the spread is wide)
- In Field B: edge ≈ +5 to +10 (because the spread is tight)

Same disagreement, wildly different "edge" numbers. The conviction threshold (edge - band > 0) is easier to pass in wide-spread fields — which are exactly the fields with a dominant horse where the model is MORE likely to be wrong about that horse's superiority.

## The Correct Approach

Edge should be computed in **probability space**, then converted to rating points at a fixed, calibrated scale:

```
model_prob = from Benter combination (using model rating)
market_prob = from odds (normalized)
prob_edge = model_prob - market_prob

# Convert to rating points using a FIXED scale, not the field's spread
# At typical mid-field odds (5/1 = 17%), a 5% prob shift ≈ X rating points
edge_pts = prob_edge * PROB_TO_POINTS_SCALE
```

The PROB_TO_POINTS_SCALE should be calibrated empirically: "how many rating points of difference corresponds to a 1% probability shift?" This depends on field size and odds level but should be a stable function, not derived from the specific field's rating distribution.

## Alternatively: Use Probability Directly

Display edge as a probability difference instead of converting to points:

```
Horse         Rating  Model%  Market%  Edge%   Confidence
Flying Gal    112     12%     5.5%     +6.5%   ±4%
Mad Anthony   108     15%     10.4%    +4.6%   ±3%
```

"+6.5% probability edge" is directly interpretable: the model thinks this horse should be ~8/1 but the market has them at ~17/1. Whether that's worth a bet depends on the horse's odds (6.5% edge at 17/1 = significant overlay; 6.5% edge at 2/1 = less significant).

## Impact on the 0/15 Record

The rank-mapping systematically inflated edges for horses the model ranked higher than the market in fields with wide ability spreads. A horse ranked 2nd by the model but 5th by the market in a wide-spread field shows "+20 edge" when the actual probability difference might be only 3-4%.

The conviction threshold (edge - band > 0) was then met by horses with only modest probability edges — creating false confidence.

## Fix Options

1. **Compute edge in probability space** — use `probability.py`'s Benter combination to produce model_prob, compare to odds_prob, express edge as % probability difference with ± band from rating uncertainty propagated through the probability function.

2. **Fix odds_to_rating** — instead of rank mapping, use the same softmax relationship as `model_probs_from_curves` but inverted. Given a probability (from odds), what rating produces that probability in the same softmax? This produces a market rating that's independent of the field's actual rating distribution.

3. **Display both** — show the probability edge (calibrated, interpretable) alongside the rating (for ability comparison). Use probability edge for bet decisions, rating for field assessment.

## Recommended: Option 3

The rating system works for its intended purpose (comparing horses on a physical scale). The problem is using it for edge computation. Keep ratings for "who's fastest" and compute edge separately in probability space where it's properly calibrated.
