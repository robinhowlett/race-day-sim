"""Kelly criterion staking for win bets and exotics.

Implements fractional Kelly for pari-mutuel racing.
Reference: octonion/betting (https://github.com/octonion/betting)
"""

import numpy as np


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
              win_cap: float = 0.03, exotic_cap: float = 0.02) -> dict:
    """Compute bet sizes for a single race given model probabilities and odds.

    Args:
        race_probs: model's win probability per horse
        odds: tote odds per horse
        bankroll: current bankroll
        fraction: Kelly fraction
        max_exposure: max total fraction of bankroll on one race
        win_cap: max fraction on any single win bet
        exotic_cap: max fraction on exotic tickets combined

    Returns:
        dict with win_bets: {horse_idx: amount}, exotic_budget: float, pass_race: bool
    """
    n = len(race_probs)
    win_bets = {}

    for i in range(n):
        f = kelly_win(float(race_probs[i]), float(odds[i]), fraction)
        if f > 0:
            amount = min(f * bankroll, win_cap * bankroll)
            if amount >= 2.0:  # minimum bet size
                win_bets[i] = round(amount, 2)

    total_win = sum(win_bets.values())

    # Exotic budget: remainder of max_exposure after win bets
    exotic_budget = max(0, max_exposure * bankroll - total_win)
    exotic_budget = min(exotic_budget, exotic_cap * bankroll)

    # Pass if no edge found
    pass_race = len(win_bets) == 0 and exotic_budget < 5.0

    return {
        "win_bets": win_bets,
        "exotic_budget": round(exotic_budget, 2),
        "total_exposure": round(total_win + exotic_budget, 2),
        "pass_race": pass_race,
    }
