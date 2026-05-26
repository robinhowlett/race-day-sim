"""Post-race evaluation — compare bets to actual results."""

import numpy as np


def evaluate_race(bets: dict, results: dict, bankroll: float) -> dict:
    """Evaluate bets against actual race results.

    Args:
        bets: {
            "win_bets": {horse_idx: amount},
            "exotic_tickets": [{type, combinations, cost}],
            "pass_race": bool
        }
        results: {
            "finish_order": [horse_idx in finishing order],
            "exacta_payoff": float (per $1),
            "trifecta_payoff": float (per $1),
            "super_payoff": float (per $1),
            "winner_odds": float,
        }

    Returns:
        dict with returns, P&L, hit flags
    """
    total_invested = 0.0
    total_returned = 0.0
    details = []

    if bets.get("pass_race"):
        return {
            "invested": 0, "returned": 0, "pnl": 0,
            "passed": True, "details": ["PASS — no bet"],
        }

    # Win bets
    winner_idx = results["finish_order"][0] if results.get("finish_order") else None
    for horse_idx, amount in bets.get("win_bets", {}).items():
        total_invested += amount
        if horse_idx == winner_idx:
            win_return = amount * (results["winner_odds"] + 1)
            total_returned += win_return
            details.append(f"WIN #{horse_idx}: bet ${amount:.2f}, returned ${win_return:.2f}")
        else:
            details.append(f"WIN #{horse_idx}: bet ${amount:.2f}, LOST")

    # Exotic tickets
    for ticket in bets.get("exotic_tickets", []):
        cost = ticket["cost"]
        total_invested += cost

        # Check if any combination on the ticket matches the actual result
        hit = False
        for combo in ticket.get("combinations", []):
            actual_order = results["finish_order"][:len(combo)]
            if list(combo) == actual_order:
                # Determine payoff based on ticket type
                if ticket["type"] == "EXACTA":
                    payoff = results.get("exacta_payoff", 0) * (cost / len(ticket["combinations"]))
                elif ticket["type"] == "TRIFECTA":
                    payoff = results.get("trifecta_payoff", 0) * (cost / len(ticket["combinations"]))
                elif ticket["type"] == "SUPERFECTA":
                    payoff = results.get("super_payoff", 0) * (cost / len(ticket["combinations"]))
                else:
                    payoff = 0

                total_returned += payoff
                hit = True
                details.append(f"{ticket['type']}: CASHED ${payoff:.2f} on combo {combo}")
                break

        if not hit:
            details.append(f"{ticket['type']}: ${cost:.2f} LOST")

    pnl = total_returned - total_invested
    roi = (pnl / total_invested * 100) if total_invested > 0 else 0

    return {
        "invested": round(total_invested, 2),
        "returned": round(total_returned, 2),
        "pnl": round(pnl, 2),
        "roi": round(roi, 1),
        "passed": False,
        "details": details,
    }


def day_summary(race_results: list[dict], starting_bankroll: float) -> dict:
    """Summarize a full simulated day."""
    total_invested = sum(r["invested"] for r in race_results)
    total_returned = sum(r["returned"] for r in race_results)
    races_played = sum(1 for r in race_results if not r["passed"])
    races_passed = sum(1 for r in race_results if r["passed"])

    return {
        "starting_bankroll": starting_bankroll,
        "ending_bankroll": starting_bankroll + total_returned - total_invested,
        "total_invested": round(total_invested, 2),
        "total_returned": round(total_returned, 2),
        "total_pnl": round(total_returned - total_invested, 2),
        "roi": round((total_returned / total_invested - 1) * 100, 1) if total_invested > 0 else 0,
        "races_played": races_played,
        "races_passed": races_passed,
        "race_details": race_results,
    }
