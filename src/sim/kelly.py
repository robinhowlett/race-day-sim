"""Kelly criterion staking for win bets and exotics.

Implements fractional Kelly for pari-mutuel racing.
Reference: octonion/betting (https://github.com/octonion/betting)
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RaceContext:
    """Per-race conditions that shift the basket weight away from base Kelly.

    All fields optional — unset modifiers default to 1.0× (no effect).

    fav_edge:
        Favorite's edge in rating points. Per simulation-protocol.md Step E.5:
          > +5  → 0.25× (you agree with the crowd, small play / pass)
          -3 to +3 → 1.0× (neutral, moderate basket)
          < -3 with band clear → 1.5× (overbet fav, full basket)
          < -10 → 2.0× (massively overbet, maximum basket)
    wcmi:
        Pool-level market consensus (0-1). Low WCMI = dispersed market.
          < 0.10 → 1.5× (uninformed crowd, more noise to exploit)
          > 0.20 → 0.5× (well-informed crowd, conservative)
    band_crosses_zero:
        True if the conviction horse's edge ± band straddles zero. Speculative
        play → 0.25×.
    n_independent_edges:
        Count of confirmed bias signals beyond the primary edge (e.g.,
        favorable trainer, favorable jockey switch, favorable equipment).
        1.25× per additional confirmed bias.
    carryover_active:
        Pool has a carryover that converts effective takeout to negative.
        Up to 2.0× (every dollar is plus-EV).
    pool_density_per_combo:
        Pool dollars per possible combination. < $5/combo = thin pool, 0.75×.
    """
    fav_edge: float | None = None
    wcmi: float | None = None
    band_crosses_zero: bool = False
    n_independent_edges: int = 0
    carryover_active: bool = False
    pool_density_per_combo: float | None = None


def context_multiplier(ctx: RaceContext | None) -> float:
    """Compose all sizing modifiers into a single scalar.

    Modifiers compose multiplicatively, then the result is clamped to a
    sensible range so a stack of modifiers can't blow past sanity bounds.
    """
    if ctx is None:
        return 1.0

    m = 1.0

    # Fav-Edge tier (per simulation-protocol.md:345-350)
    if ctx.fav_edge is not None:
        if ctx.fav_edge > 5:
            m *= 0.25
        elif ctx.fav_edge < -10:
            m *= 2.0
        elif ctx.fav_edge < -3:
            m *= 1.5
        # -3 to +5: no adjustment (1.0× — neutral)

    # WCMI tier (per wagering-framework.md:243-244)
    if ctx.wcmi is not None:
        if ctx.wcmi < 0.10:
            m *= 1.5
        elif ctx.wcmi > 0.20:
            m *= 0.5

    # Speculative band
    if ctx.band_crosses_zero:
        m *= 0.25

    # Independent confirming biases
    if ctx.n_independent_edges and ctx.n_independent_edges > 0:
        m *= 1.25 ** ctx.n_independent_edges

    # Carryover
    if ctx.carryover_active:
        m *= 2.0

    # Thin-pool
    if ctx.pool_density_per_combo is not None and ctx.pool_density_per_combo < 5.0:
        m *= 0.75

    # Clamp: stacking modifiers can't push base Kelly more than 4× up or down.
    # Beyond that, you should be reconsidering whether to bet at all.
    return max(0.05, min(4.0, m))


def kelly_win(p_model: float, odds: float, fraction: float = 0.25) -> float:
    """Fractional Kelly for a single win bet.

    Args:
        p_model: model's estimated win probability
        odds: tote odds (e.g., 4.0 = 4/1)
        fraction: Kelly fraction (0.25 = quarter Kelly)

    Returns: fraction of bankroll to bet (0 if no edge)
    """
    b = odds  # net profit per unit
    q = 1.0 - p_model
    kelly_full = (b * p_model - q) / b
    return max(0.0, kelly_full * fraction)


def kelly_exotic(combinations: list[tuple[float, float]], fraction: float = 0.25) -> float:
    """Fractional Kelly for an exotic ticket covering multiple combinations.

    Args:
        combinations: list of (probability, estimated_payoff_per_dollar) tuples
            for each combination on the ticket
        fraction: Kelly fraction

    Returns: fraction of bankroll to allocate to this ticket
    """
    if not combinations:
        return 0.0

    # Expected value of the ticket
    total_prob = sum(p for p, _ in combinations)
    expected_return = sum(p * payoff for p, payoff in combinations)

    # Simple Kelly approximation for exotic:
    # edge = expected_return - 1 (net expected per dollar)
    # variance approximation from the payoff distribution
    edge = expected_return - 1.0
    if edge <= 0:
        return 0.0

    # Approximate Kelly fraction: edge / avg_payoff
    avg_payoff = expected_return / total_prob if total_prob > 0 else 1.0
    kelly_full = edge / avg_payoff
    return max(0.0, kelly_full * fraction)


def size_bets(race_probs: np.ndarray, odds: np.ndarray, bankroll: float,
              fraction: float = 0.25, max_exposure: float = 0.05,
              win_cap: float = 0.03, exotic_cap: float = 0.02,
              context: RaceContext | None = None) -> dict:
    """Compute bet sizes for a single race given model probabilities and odds.

    Base Kelly fractions are computed per horse, then the per-race `context`
    modifier (fav_edge tier, WCMI, carryover, etc.) scales the entire basket
    multiplicatively. Caps are applied AFTER the modifier so a 2× context
    multiplier on a low base Kelly stays inside max_exposure / win_cap /
    exotic_cap; a 0.25× modifier shrinks a thick basket to a small play.

    Args:
        race_probs: model's win probability per horse
        odds: tote odds per horse
        bankroll: current bankroll
        fraction: Kelly fraction
        max_exposure: max total fraction of bankroll on one race
        win_cap: max fraction on any single win bet
        exotic_cap: max fraction on exotic tickets combined
        context: optional RaceContext with per-race sizing modifiers

    Returns:
        dict with win_bets: {horse_idx: amount}, exotic_budget: float,
        pass_race: bool, context_mult: float (the applied modifier)
    """
    n = len(race_probs)
    mod = context_multiplier(context)

    win_bets = {}
    for i in range(n):
        # Base Kelly first, then apply context modifier, then cap.
        f_base = kelly_win(float(race_probs[i]), float(odds[i]), fraction)
        f_adjusted = f_base * mod
        if f_adjusted > 0:
            amount = min(f_adjusted * bankroll, win_cap * bankroll)
            if amount >= 2.0:  # minimum bet size
                win_bets[i] = round(amount, 2)

    total_win = sum(win_bets.values())

    # Exotic budget: remainder of (max_exposure × mod) after win bets, capped
    # at exotic_cap (which is also scaled by the modifier).
    scaled_max_exposure = max_exposure * mod
    scaled_exotic_cap   = exotic_cap   * mod
    exotic_budget = max(0.0, scaled_max_exposure * bankroll - total_win)
    exotic_budget = min(exotic_budget, scaled_exotic_cap * bankroll)

    # Pass if no edge found
    pass_race = len(win_bets) == 0 and exotic_budget < 5.0

    return {
        "win_bets":      win_bets,
        "exotic_budget": round(exotic_budget, 2),
        "total_exposure": round(total_win + exotic_budget, 2),
        "pass_race":     pass_race,
        "context_mult":  round(mod, 3),
    }
