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


MARKET_BIAS_SQL = """
WITH race_starters AS (
    SELECT s.id AS starter_id,
        s.horse, s.trainer_last, s.trainer_first,
        s.jockey_last, s.jockey_first,
        s.jockey_allowance, s.weight,
        s.last_raced_date,
        s.claimed,
        r.id AS race_id, r.number AS race_number,
        r.surface, r.type AS race_type, r.off_turf, r.date AS race_date,
        r.furlongs, r.purse
    FROM handycapper.races r
    JOIN handycapper.starters s ON s.race_id = r.id
    WHERE r.track = %(track)s AND r.date = %(race_date)s
),
-- Trainer FTS record (point-in-time)
trainer_fts AS (
    SELECT s.trainer_last, s.trainer_first,
        COUNT(*) AS fts_starts,
        SUM(CASE WHEN s.official_position = 1 THEN 1 ELSE 0 END) AS fts_wins,
        SUM(1.0 / (s.odds + 1)) AS fts_expected
    FROM handycapper.starters s
    JOIN handycapper.races r ON r.id = s.race_id
    WHERE s.last_raced_date IS NULL
      AND r.type LIKE '%%MAIDEN%%'
      AND r.breed = 'TB'
      AND r.date < %(race_date)s
      AND r.number_of_runners >= 5
      AND s.odds IS NOT NULL AND s.odds > 0
      AND (s.trainer_last, s.trainer_first) IN (
          SELECT DISTINCT trainer_last, trainer_first FROM race_starters
      )
    GROUP BY s.trainer_last, s.trainer_first
),
-- Trainer claim record (point-in-time)
trainer_claim AS (
    SELECT post.trainer_last, post.trainer_first,
        COUNT(*) AS claim_starts,
        SUM(CASE WHEN post.official_position = 1 THEN 1 ELSE 0 END) AS claim_wins,
        SUM(1.0 / (post.odds + 1)) AS claim_expected
    FROM handycapper.starters claimed
    JOIN handycapper.races cr ON cr.id = claimed.race_id
    JOIN handycapper.starters post ON post.horse = claimed.horse
    JOIN handycapper.races pr ON pr.id = post.race_id
    WHERE claimed.claimed = true
      AND cr.breed = 'TB' AND cr.date < %(race_date)s
      AND pr.date > cr.date AND pr.date <= cr.date + interval '180 days'
      AND pr.date < %(race_date)s
      AND pr.number_of_runners >= 5
      AND post.odds IS NOT NULL AND post.odds > 0
      AND (post.trainer_last, post.trainer_first) IN (
          SELECT DISTINCT trainer_last, trainer_first FROM race_starters
      )
    GROUP BY post.trainer_last, post.trainer_first
),
-- Trainer class drop record (point-in-time)
trainer_drop AS (
    SELECT sq.trainer_last, sq.trainer_first,
        COUNT(*) AS drop_starts,
        SUM(CASE WHEN sq.official_position = 1 THEN 1 ELSE 0 END) AS drop_wins,
        SUM(1.0 / (sq.odds + 1)) AS drop_expected
    FROM (
        SELECT s.trainer_last, s.trainer_first, s.official_position, s.odds, r.purse,
            LAG(r.purse) OVER (PARTITION BY s.horse ORDER BY r.date) AS prev_purse
        FROM handycapper.starters s
        JOIN handycapper.races r ON r.id = s.race_id
        WHERE r.breed = 'TB' AND r.date < %(race_date)s
          AND r.number_of_runners >= 5
          AND s.horse IS NOT NULL AND s.odds IS NOT NULL AND s.odds > 0
          AND (s.trainer_last, s.trainer_first) IN (
              SELECT DISTINCT trainer_last, trainer_first FROM race_starters
          )
    ) sq
    WHERE sq.prev_purse IS NOT NULL AND sq.purse < sq.prev_purse * 0.7
    GROUP BY sq.trainer_last, sq.trainer_first
),
-- Trainer layoff record (point-in-time: 90+ days off)
trainer_layoff AS (
    SELECT s.trainer_last, s.trainer_first,
        COUNT(*) AS layoff_starts,
        SUM(CASE WHEN s.official_position = 1 THEN 1 ELSE 0 END) AS layoff_wins,
        SUM(1.0 / (s.odds + 1)) AS layoff_expected
    FROM handycapper.starters s
    JOIN handycapper.races r ON r.id = s.race_id
    WHERE r.breed = 'TB' AND r.date < %(race_date)s
      AND r.number_of_runners >= 5
      AND s.last_raced_date IS NOT NULL
      AND (r.date - s.last_raced_date) >= 90
      AND s.odds IS NOT NULL AND s.odds > 0
      AND (s.trainer_last, s.trainer_first) IN (
          SELECT DISTINCT trainer_last, trainer_first FROM race_starters
      )
    GROUP BY s.trainer_last, s.trainer_first
),
-- Trainer surface switch record (point-in-time)
trainer_switch AS (
    SELECT sq.trainer_last, sq.trainer_first,
        COUNT(*) AS switch_starts,
        SUM(CASE WHEN sq.official_position = 1 THEN 1 ELSE 0 END) AS switch_wins,
        SUM(1.0 / (sq.odds + 1)) AS switch_expected
    FROM (
        SELECT s.trainer_last, s.trainer_first, s.official_position, s.odds, r.surface,
            LAG(r.surface) OVER (PARTITION BY s.horse ORDER BY r.date) AS prev_surface
        FROM handycapper.starters s
        JOIN handycapper.races r ON r.id = s.race_id
        WHERE r.breed = 'TB' AND r.date < %(race_date)s
          AND r.number_of_runners >= 5
          AND s.horse IS NOT NULL AND s.odds IS NOT NULL AND s.odds > 0
          AND r.surface IN ('Dirt', 'Turf', 'Synthetic')
          AND (s.trainer_last, s.trainer_first) IN (
              SELECT DISTINCT trainer_last, trainer_first FROM race_starters
          )
    ) sq
    WHERE sq.prev_surface IS NOT NULL AND sq.surface != sq.prev_surface
    GROUP BY sq.trainer_last, sq.trainer_first
),
-- Jockey career win rate (point-in-time, for upgrade/downgrade detection)
jockey_career AS (
    SELECT s.jockey_last, s.jockey_first,
        COUNT(*) AS career_starts,
        SUM(CASE WHEN s.official_position = 1 THEN 1 ELSE 0 END)::float / COUNT(*) AS career_win_pct
    FROM handycapper.starters s
    JOIN handycapper.races r ON r.id = s.race_id
    WHERE r.breed = 'TB' AND r.date < %(race_date)s
      AND r.number_of_runners >= 5
      AND (s.jockey_last, s.jockey_first) IN (
          SELECT DISTINCT jockey_last, jockey_first FROM race_starters
          UNION
          SELECT DISTINCT prev_s.jockey_last, prev_s.jockey_first
          FROM race_starters rs
          JOIN handycapper.starters prev_s ON prev_s.horse = rs.horse AND prev_s.id != rs.starter_id
          JOIN handycapper.races prev_r ON prev_r.id = prev_s.race_id AND prev_r.date < %(race_date)s
      )
    GROUP BY s.jockey_last, s.jockey_first
    HAVING COUNT(*) >= 50
),
-- Jockey trailing 12m at this track (point-in-time)
jockey_track AS (
    SELECT s.jockey_last, s.jockey_first,
        COUNT(*) AS jock_starts_12m,
        SUM(CASE WHEN s.official_position = 1 THEN 1 ELSE 0 END) AS jock_wins_12m
    FROM handycapper.starters s
    JOIN handycapper.races r ON r.id = s.race_id
    WHERE r.track = %(track)s
      AND r.date BETWEEN (%(race_date)s::date - interval '365 days') AND (%(race_date)s::date - interval '1 day')
      AND r.number_of_runners >= 5
      AND (s.jockey_last, s.jockey_first) IN (
          SELECT DISTINCT jockey_last, jockey_first FROM race_starters
      )
    GROUP BY s.jockey_last, s.jockey_first
),
-- Equipment: current race meds/equip
current_meds AS (
    SELECT rs.starter_id,
        bool_or(m.code = 'L') AS has_lasix
    FROM race_starters rs
    LEFT JOIN handycapper.meds m ON m.starter_id = rs.starter_id
    GROUP BY rs.starter_id
),
current_equip AS (
    SELECT rs.starter_id,
        bool_or(e.code = 'b') AS has_blinkers
    FROM race_starters rs
    LEFT JOIN handycapper.equip e ON e.starter_id = rs.starter_id
    GROUP BY rs.starter_id
),
-- Previous start info (for detecting changes)
prev_start AS (
    SELECT DISTINCT ON (rs.starter_id)
        rs.starter_id,
        prev_s.id AS prev_starter_id,
        prev_r.surface AS prev_surface,
        prev_r.purse AS prev_purse,
        prev_s.jockey_last AS prev_jockey_last,
        prev_s.jockey_first AS prev_jockey_first,
        prev_s.claimed AS was_claimed_last,
        prev_r.date AS prev_race_date
    FROM race_starters rs
    JOIN handycapper.starters prev_s ON prev_s.horse = rs.horse AND prev_s.id != rs.starter_id
    JOIN handycapper.races prev_r ON prev_r.id = prev_s.race_id AND prev_r.date < %(race_date)s
    ORDER BY rs.starter_id, prev_r.date DESC
),
prev_meds AS (
    SELECT ps.starter_id,
        bool_or(m.code = 'L') AS prev_lasix
    FROM prev_start ps
    LEFT JOIN handycapper.meds m ON m.starter_id = ps.prev_starter_id
    GROUP BY ps.starter_id
),
prev_equip AS (
    SELECT ps.starter_id,
        bool_or(e.code = 'b') AS prev_blinkers
    FROM prev_start ps
    LEFT JOIN handycapper.equip e ON e.starter_id = ps.prev_starter_id
    GROUP BY ps.starter_id
)
SELECT
    rs.starter_id, rs.race_number, rs.horse,
    rs.trainer_last, rs.trainer_first,
    rs.jockey_last, rs.jockey_first,
    rs.jockey_allowance, rs.weight,
    rs.last_raced_date IS NULL AS is_fts,
    rs.race_type, rs.off_turf, rs.surface, rs.purse,
    -- Trainer FTS (point-in-time)
    tf.fts_starts AS trainer_fts_starts,
    CASE WHEN tf.fts_expected > 0 THEN tf.fts_wins / tf.fts_expected END AS trainer_fts_ae,
    -- Trainer claim (point-in-time)
    tc.claim_starts AS trainer_claim_starts,
    CASE WHEN tc.claim_expected > 0 THEN tc.claim_wins / tc.claim_expected END AS trainer_claim_ae,
    -- Trainer class drop (point-in-time)
    td.drop_starts AS trainer_drop_starts,
    CASE WHEN td.drop_expected > 0 THEN td.drop_wins / td.drop_expected END AS trainer_drop_ae,
    -- Trainer layoff (point-in-time)
    tl.layoff_starts AS trainer_layoff_starts,
    CASE WHEN tl.layoff_expected > 0 THEN tl.layoff_wins / tl.layoff_expected END AS trainer_layoff_ae,
    -- Trainer surface switch (point-in-time)
    ts.switch_starts AS trainer_switch_starts,
    CASE WHEN ts.switch_expected > 0 THEN ts.switch_wins / ts.switch_expected END AS trainer_switch_ae,
    -- Jockey career win% (point-in-time, for tier + upgrade detection)
    jc.career_starts AS jock_career_starts,
    jc.career_win_pct AS jock_career_win_pct,
    -- Jockey track form (trailing 12m)
    jt.jock_starts_12m, jt.jock_wins_12m,
    CASE WHEN jt.jock_starts_12m > 0 THEN jt.jock_wins_12m::float / jt.jock_starts_12m END AS jock_track_win_pct,
    -- Previous jockey career win% (for upgrade/downgrade)
    jc_prev.career_win_pct AS prev_jock_career_win_pct,
    CASE
        WHEN jc.career_win_pct IS NOT NULL AND jc_prev.career_win_pct IS NOT NULL
            AND jc.career_win_pct > jc_prev.career_win_pct + 0.05 THEN 'UPGRADE'
        WHEN jc.career_win_pct IS NOT NULL AND jc_prev.career_win_pct IS NOT NULL
            AND jc_prev.career_win_pct > jc.career_win_pct + 0.05 THEN 'DOWNGRADE'
        WHEN ps.prev_jockey_last IS NOT NULL
            AND (rs.jockey_last != ps.prev_jockey_last OR rs.jockey_first != ps.prev_jockey_first)
            THEN 'LATERAL'
        ELSE 'SAME'
    END AS jockey_switch_type,
    -- Equipment changes
    COALESCE(cm.has_lasix, false) AS has_lasix,
    COALESCE(pm.prev_lasix, false) AS prev_lasix,
    COALESCE(cm.has_lasix, false) AND NOT COALESCE(pm.prev_lasix, false) AS first_time_lasix,
    COALESCE(ce.has_blinkers, false) AS has_blinkers,
    COALESCE(pe.prev_blinkers, false) AS prev_blinkers,
    NOT COALESCE(ce.has_blinkers, false) AND COALESCE(pe.prev_blinkers, false) AS blinkers_off,
    COALESCE(ce.has_blinkers, false) AND NOT COALESCE(pe.prev_blinkers, false) AS first_time_blinkers,
    -- Surface switch
    ps.prev_surface,
    ps.prev_surface IS NOT NULL AND ps.prev_surface != rs.surface AS surface_switch,
    -- Class move
    ps.prev_purse,
    CASE
        WHEN ps.prev_purse IS NOT NULL AND rs.purse < ps.prev_purse * 0.7 THEN 'DROP'
        WHEN ps.prev_purse IS NOT NULL AND rs.purse > ps.prev_purse * 1.3 THEN 'RISE'
        ELSE 'SAME'
    END AS class_move,
    -- Layoff
    CASE WHEN ps.prev_race_date IS NOT NULL
        THEN (%(race_date)s::date - ps.prev_race_date)
    END AS days_since_prev,
    CASE WHEN ps.prev_race_date IS NOT NULL
        THEN (%(race_date)s::date - ps.prev_race_date) >= 90
        ELSE false
    END AS is_layoff,
    -- Claimed last race
    COALESCE(ps.was_claimed_last, false) AS claimed_last_race,
    -- Off-turf flag
    rs.off_turf
FROM race_starters rs
LEFT JOIN trainer_fts tf ON tf.trainer_last = rs.trainer_last AND tf.trainer_first = rs.trainer_first
LEFT JOIN trainer_claim tc ON tc.trainer_last = rs.trainer_last AND tc.trainer_first = rs.trainer_first
LEFT JOIN trainer_drop td ON td.trainer_last = rs.trainer_last AND td.trainer_first = rs.trainer_first
LEFT JOIN trainer_layoff tl ON tl.trainer_last = rs.trainer_last AND tl.trainer_first = rs.trainer_first
LEFT JOIN trainer_switch ts ON ts.trainer_last = rs.trainer_last AND ts.trainer_first = rs.trainer_first
LEFT JOIN jockey_career jc ON jc.jockey_last = rs.jockey_last AND jc.jockey_first = rs.jockey_first
LEFT JOIN jockey_track jt ON jt.jockey_last = rs.jockey_last AND jt.jockey_first = rs.jockey_first
LEFT JOIN current_meds cm ON cm.starter_id = rs.starter_id
LEFT JOIN current_equip ce ON ce.starter_id = rs.starter_id
LEFT JOIN prev_start ps ON ps.starter_id = rs.starter_id
LEFT JOIN prev_meds pm ON pm.starter_id = rs.starter_id
LEFT JOIN prev_equip pe ON pe.starter_id = rs.starter_id
LEFT JOIN jockey_career jc_prev ON jc_prev.jockey_last = ps.prev_jockey_last AND jc_prev.jockey_first = ps.prev_jockey_first
ORDER BY rs.race_number, rs.starter_id
"""


def load_market_bias(conn, track: str, race_date: str) -> pd.DataFrame:
    """Load point-in-time market bias signals for all starters on this card.

    All data is backward-looking from race_date — no future leakage.
    Returns one row per starter with trainer A/E, jockey form, equipment
    changes, surface switches, and claim status.
    """
    return pd.read_sql(
        MARKET_BIAS_SQL, conn,
        params={"track": track, "race_date": race_date},
    )


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
