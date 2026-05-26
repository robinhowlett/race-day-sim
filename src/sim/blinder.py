"""The blinder layer — extracts only pre-race information for a given track + date.

Enforces the information firewall: Claude never sees results before committing bets.
"""

import pandas as pd

PRE_RACE_CARD_SQL = """
SELECT
    r.id           AS race_id,
    r.number       AS race_number,
    r.surface,
    r.furlongs,
    r.type         AS race_type,
    r.conditions,
    r.number_of_runners AS field_size,
    r.purse,
    r.track_condition,
    r.total_wps_pool,
    s.id           AS starter_id,
    s.program,
    s.horse        AS horse_name,
    s.jockey_first || ' ' || s.jockey_last AS jockey,
    s.trainer_first || ' ' || s.trainer_last AS trainer,
    s.odds         AS closing_odds,
    s.choice,
    -- RKM velocity curve (career)
    vc.v0,
    vc.decay_rate,
    vc.adj_v0,
    vc.adj_decay,
    vc.n_races     AS curve_races,
    vc.n_observations,
    -- Current form (entering this race)
    cf.current_v0,
    cf.current_decay,
    cf.career_v0,
    cf.career_decay,
    cf.v0_trend,
    cf.n_recent_races,
    cf.days_since_last
FROM handycapper.races r
JOIN handycapper.starters s ON s.race_id = r.id
LEFT JOIN handycapper.rkm_velocity_curves vc
    ON SPLIT_PART(vc.horse_key, '|', 1) = s.horse
    AND vc.surface = r.surface
    AND vc.distance_zone = CASE WHEN r.furlongs > 6.5 THEN 'route' ELSE 'sprint' END
LEFT JOIN handycapper.rkm_current_form cf
    ON cf.starter_id = s.id
WHERE r.track = %(track)s
  AND r.date = %(race_date)s
ORDER BY r.number, s.choice
"""

POOL_SIZES_SQL = """
SELECT
    r.number AS race_number,
    e.bet_type,
    e.pool
FROM handycapper.races r
JOIN handycapper.exotics e ON e.race_id = r.id
WHERE r.track = %(track)s
  AND r.date = %(race_date)s
  AND e.pool IS NOT NULL
  AND e.bet_type IN (
      'EXACTA', 'TRIFECTA', 'SUPERFECTA',
      'PICK_3', 'PICK_4', 'PICK_5', 'PICK_6'
  )
ORDER BY r.number, e.bet_type
"""


PRE_RACE_CARD_SQL_NO_RKM = """
SELECT
    r.id           AS race_id,
    r.number       AS race_number,
    r.surface,
    r.furlongs,
    r.type         AS race_type,
    r.conditions,
    r.number_of_runners AS field_size,
    r.purse,
    r.track_condition,
    r.total_wps_pool,
    s.id           AS starter_id,
    s.program,
    s.horse        AS horse_name,
    s.jockey_first || ' ' || s.jockey_last AS jockey,
    s.trainer_first || ' ' || s.trainer_last AS trainer,
    s.odds         AS closing_odds,
    s.choice,
    NULL::numeric  AS v0,
    NULL::numeric  AS decay_rate,
    NULL::numeric  AS adj_v0,
    NULL::numeric  AS adj_decay,
    NULL::int      AS curve_races,
    NULL::int      AS n_observations,
    NULL::numeric  AS current_v0,
    NULL::numeric  AS current_decay,
    NULL::numeric  AS career_v0,
    NULL::numeric  AS career_decay,
    NULL::numeric  AS v0_trend,
    NULL::smallint AS n_recent_races,
    NULL::smallint AS days_since_last
FROM handycapper.races r
JOIN handycapper.starters s ON s.race_id = r.id
WHERE r.track = %(track)s
  AND r.date = %(race_date)s
ORDER BY r.number, s.choice
"""


def load_pre_race_card(conn, track: str, race_date: str) -> pd.DataFrame:
    """Load blinded pre-race card for a full day at one track.

    Returns DataFrame with one row per starter, sorted by race number then choice.
    No result information (finish_position, payoffs) is included.
    Falls back to base-tables-only query if rkm tables don't exist.
    """
    try:
        df = pd.read_sql(PRE_RACE_CARD_SQL, conn, params={"track": track, "race_date": race_date})
    except Exception:
        conn.rollback()
        df = pd.read_sql(PRE_RACE_CARD_SQL_NO_RKM, conn, params={"track": track, "race_date": race_date})

    for col in ["closing_odds", "furlongs", "v0", "decay_rate", "adj_v0", "adj_decay",
                "current_v0", "current_decay", "career_v0", "career_decay", "v0_trend"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_pool_sizes(conn, track: str, race_date: str) -> pd.DataFrame:
    """Load exotic pool sizes (pre-race information — pool sizes are public)."""
    return pd.read_sql(POOL_SIZES_SQL, conn, params={"track": track, "race_date": race_date})


def load_race_results(conn, track: str, race_date: str) -> pd.DataFrame:
    """Load actual results for post-race reveal. Only call AFTER bets are committed."""
    sql = """
    SELECT
        r.id AS race_id, r.number AS race_number,
        s.id AS starter_id, s.horse AS horse_name, s.program,
        s.finish_position,
        s.official_position,
        s.wagering_position,
        s.disqualified,
        s.position_dead_heat,
        s.odds, s.choice, s.winner,
        e_ex.payoff / NULLIF(e_ex.unit, 0) AS exacta_payoff,
        e_tri.payoff / NULLIF(e_tri.unit, 0) AS trifecta_payoff,
        e_sup.payoff / NULLIF(e_sup.unit, 0) AS super_payoff
    FROM handycapper.races r
    JOIN handycapper.starters s ON s.race_id = r.id
    LEFT JOIN handycapper.exotics e_ex ON e_ex.race_id = r.id AND e_ex.bet_type = 'EXACTA' AND e_ex.payoff > 0
    LEFT JOIN handycapper.exotics e_tri ON e_tri.race_id = r.id AND e_tri.bet_type = 'TRIFECTA' AND e_tri.payoff > 0
    LEFT JOIN handycapper.exotics e_sup ON e_sup.race_id = r.id AND e_sup.bet_type = 'SUPERFECTA' AND e_sup.payoff > 0
    WHERE r.track = %(track)s AND r.date = %(race_date)s
    ORDER BY r.number, s.official_position
    """
    return pd.read_sql(sql, conn, params={"track": track, "race_date": race_date})
