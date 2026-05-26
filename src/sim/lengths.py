"""Length conversion utilities — consistent with chart-parser's 8.75 feet per length.

In Thoroughbred racing, a "length" is the approximate body length of a horse
(~8 feet). Chart-parser uses 8.75 feet as the standard conversion factor when
computing individual fractional times from the leader's time + lengths behind.

This constant is baked into all indiv_fractionals data in the database — every
velocity observation that RKM fits curves to was derived using this same factor.
For output consistency, we use it here when expressing model projections in
lengths rather than ft/s.

Key relationships:
    1 length = 8.75 feet (chart-parser standard)
    At velocity v ft/s: 1 length = 8.75/v seconds
    At 60 ft/s (sprint): 1 length ≈ 0.146 seconds
    At 54 ft/s (turf route): 1 length ≈ 0.162 seconds

Note: a "length" is NOT a fixed time interval — it represents different durations
at different speeds. A horse "3 lengths behind" at the finish of a sprint is
closer in time than a horse "3 lengths behind" at the finish of a route, because
sprint speeds are higher.
"""

FEET_PER_LENGTH = 8.75


def feet_to_lengths(feet: float) -> float:
    return feet / FEET_PER_LENGTH


def lengths_to_feet(lengths: float) -> float:
    return lengths * FEET_PER_LENGTH


def time_to_lengths(time_diff_seconds: float, velocity_ft_per_s: float) -> float:
    """Convert a time gap to lengths at a given velocity.

    If horse A finishes 0.5 seconds before horse B, and the field is
    traveling at 55 ft/s near the finish, the gap in lengths is:
        0.5 * 55 / 8.75 = 3.14 lengths
    """
    feet_gap = time_diff_seconds * velocity_ft_per_s
    return feet_gap / FEET_PER_LENGTH


def lengths_to_time(lengths: float, velocity_ft_per_s: float) -> float:
    """Convert a length gap to seconds at a given velocity."""
    feet_gap = lengths * FEET_PER_LENGTH
    return feet_gap / velocity_ft_per_s


def projected_margin_at_finish(
    v0_a: float, decay_a: float,
    v0_b: float, decay_b: float,
    race_distance_ft: float,
) -> dict:
    """Project the margin in lengths between two horses at the finish.

    Uses the linear deceleration model: v(d) = v0 - decay × (d/1000)
    Integrates to get finishing time, then converts time difference to lengths
    using the winner's finishing velocity.

    Returns dict with:
        margin_lengths: positive = A beats B
        margin_seconds: time gap
        a_time_ms: A's projected finishing time in milliseconds
        b_time_ms: B's projected finishing time
        finish_velocity: winner's velocity at the finish (for context)
    """
    # Average velocity over the race (linear decel = avg is midpoint velocity)
    avg_v_a = v0_a - decay_a * (race_distance_ft / 2000.0)
    avg_v_b = v0_b - decay_b * (race_distance_ft / 2000.0)

    if avg_v_a <= 0:
        avg_v_a = 30.0
    if avg_v_b <= 0:
        avg_v_b = 30.0

    time_a = race_distance_ft / avg_v_a
    time_b = race_distance_ft / avg_v_b
    time_diff = time_b - time_a  # positive = A is faster

    # Velocity at the finish line for the faster horse (for length conversion)
    winner_v0 = v0_a if time_a <= time_b else v0_b
    winner_decay = decay_a if time_a <= time_b else decay_b
    finish_velocity = winner_v0 - winner_decay * (race_distance_ft / 1000.0)
    if finish_velocity <= 0:
        finish_velocity = 40.0

    margin_lengths = time_to_lengths(abs(time_diff), finish_velocity)
    if time_a > time_b:
        margin_lengths = -margin_lengths  # B beats A

    return {
        "margin_lengths": round(margin_lengths, 2),
        "margin_seconds": round(time_diff, 3),
        "a_time_ms": round(time_a * 1000, 0),
        "b_time_ms": round(time_b * 1000, 0),
        "finish_velocity": round(finish_velocity, 1),
    }


def v0_trend_to_lengths(
    v0_trend: float,
    decay_rate: float,
    race_distance_ft: float,
) -> float:
    """Convert a v0_trend value to projected lengths gained/lost at the finish.

    v0_trend is the difference between current_v0 and career_v0. Since the
    deceleration model is v(d) = v0 - decay × d/1000, a change in v0 shifts
    the entire curve up/down by v0_trend ft/s at every point. The time
    difference this creates depends on the absolute speed.

    Uses the full model: time = distance / avg_velocity, where
    avg_velocity = v0 - decay × (distance/2000).
    """
    if race_distance_ft <= 0:
        return 0.0

    # Career curve timing (use decay_rate as given, with v0 = arbitrary baseline)
    base_v0 = 58.0  # representative baseline (doesn't matter — cancels out)
    avg_v_career = base_v0 - decay_rate * (race_distance_ft / 2000.0)
    avg_v_current = (base_v0 + v0_trend) - decay_rate * (race_distance_ft / 2000.0)

    if avg_v_career <= 0 or avg_v_current <= 0:
        return 0.0

    time_career = race_distance_ft / avg_v_career
    time_current = race_distance_ft / avg_v_current
    time_diff = time_career - time_current  # positive = current is faster

    # Use current velocity at finish for length conversion
    finish_v = (base_v0 + v0_trend) - decay_rate * (race_distance_ft / 1000.0)
    if finish_v <= 30.0:
        finish_v = 40.0

    return time_to_lengths(time_diff, finish_v)


def field_margins_in_lengths(
    adj_v0s: list[float],
    decay_rates: list[float],
    race_distance_ft: float,
) -> list[float]:
    """Project margins in lengths between each horse and the best in the field.

    Returns a list where index 0 = 0.0 (the projected winner), and each other
    entry is how many lengths behind the best that horse projects to finish.
    Positive = behind.

    This is the most useful output for race analysis — it shows the projected
    separation between actual competitors, not abstract comparisons.
    """
    n = len(adj_v0s)
    if n == 0:
        return []

    # Compute projected finishing time for each horse
    times = []
    for v0, decay in zip(adj_v0s, decay_rates):
        avg_v = v0 - decay * (race_distance_ft / 2000.0)
        if avg_v <= 0:
            avg_v = 30.0
        times.append(race_distance_ft / avg_v)

    best_time = min(times)
    best_idx = times.index(best_time)

    # Finish velocity of the projected winner (for length conversion)
    finish_v = adj_v0s[best_idx] - decay_rates[best_idx] * (race_distance_ft / 1000.0)
    if finish_v <= 30.0:
        finish_v = 40.0

    margins = []
    for t in times:
        time_behind = t - best_time
        lengths_behind = time_to_lengths(time_behind, finish_v)
        margins.append(round(lengths_behind, 2))

    return margins


def format_lengths(lengths: float) -> str:
    """Format a length margin in traditional racing notation.

    Examples: "nose", "head", "neck", "3/4", "1 1/4", "3", "10"
    """
    abs_l = abs(lengths)

    if abs_l < 0.08:
        text = "nose"
    elif abs_l < 0.15:
        text = "head"
    elif abs_l < 0.38:
        text = "neck"
    elif abs_l < 0.63:
        text = "1/2"
    elif abs_l < 0.88:
        text = "3/4"
    elif abs_l < 1.13:
        text = "1"
    elif abs_l < 1.38:
        text = "1 1/4"
    elif abs_l < 1.63:
        text = "1 1/2"
    elif abs_l < 1.88:
        text = "1 3/4"
    elif abs_l < 2.25:
        text = "2"
    elif abs_l < 2.75:
        text = "2 1/2"
    elif abs_l < 3.25:
        text = "3"
    elif abs_l < 20:
        text = str(int(round(abs_l)))
    else:
        text = f"{abs_l:.0f}"

    return text
