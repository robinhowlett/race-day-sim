"""The blinder layer — extracts only pre-race information for a given track + date.

Enforces the information firewall: Claude never sees results before committing bets.
"""

import numpy as np
import pandas as pd

# Empirical cross-zone correlation between sprint and route curves on the same
# surface — measured on 117K paired starters in rkm_velocity_curves (1991-2017).
# Used as a confidence haircut for cross-zone fallback (when primary-zone
# curve is missing but opposite-zone exists).
CROSS_ZONE_R = {"Dirt": 0.38, "Synthetic": 0.51, "Turf": 0.25}

# Average shifts when going sprint → route on the same surface. Apply with sign:
# sprint→route adds the (negative) shift; route→sprint subtracts it.
# Source: same 117K paired observations as CROSS_ZONE_R.
SPRINT_TO_ROUTE_V0_SHIFT    = {"Dirt": -2.81, "Synthetic": -3.33, "Turf": -4.09}
SPRINT_TO_ROUTE_DECAY_SHIFT = {"Dirt": -1.17, "Synthetic": -1.21, "Turf": -0.78}

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
    -- RKM velocity curve (career, only if first_race precedes sim date)
    vc.v0,
    vc.decay_rate,
    vc.adj_v0,
    vc.adj_decay,
    vc.n_races     AS curve_races,
    vc.n_observations,
    vc.first_race  AS curve_first_race,
    vc.last_race   AS curve_last_race,
    -- Current form (entering this race — point-in-time safe)
    cf.current_v0,
    cf.current_decay,
    cf.career_v0,
    cf.career_decay,
    cf.v0_trend,
    cf.n_recent_races,
    cf.days_since_last,
    -- Cross-zone fallback curve: opposite zone, same surface. Used when
    -- the primary-zone curve is missing. Post-processing in Python applies
    -- a surface-specific v0/decay shift and confidence haircut (RKM
    -- distance-zone discontinuity workaround — see ratings._cross_zone fns).
    vc_alt.adj_v0   AS alt_zone_adj_v0,
    vc_alt.adj_decay AS alt_zone_adj_decay,
    vc_alt.distance_zone AS alt_zone,
    vc_alt.n_races  AS alt_zone_curve_races
FROM handycapper.races r
JOIN handycapper.starters s ON s.race_id = r.id
LEFT JOIN handycapper.rkm_velocity_curves vc
    ON SPLIT_PART(vc.horse_key, '|', 1) = s.horse
    AND vc.surface = r.surface
    AND vc.distance_zone = CASE WHEN r.furlongs > 6.5 THEN 'route' ELSE 'sprint' END
    AND vc.first_race < %(race_date)s
LEFT JOIN handycapper.rkm_velocity_curves vc_alt
    ON SPLIT_PART(vc_alt.horse_key, '|', 1) = s.horse
    AND vc_alt.surface = r.surface
    AND vc_alt.distance_zone = CASE WHEN r.furlongs > 6.5 THEN 'sprint' ELSE 'route' END
    AND vc_alt.first_race < %(race_date)s
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
      'EXACTA', 'TRIFECTA', 'SUPERFECTA', 'QUINELLA', 'HI_5',
      'DAILY_DOUBLE', 'PICK_3', 'PICK_4', 'PICK_5', 'PICK_6'
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
    NULL::date     AS curve_first_race,
    NULL::date     AS curve_last_race,
    NULL::numeric  AS alt_zone_adj_v0,
    NULL::numeric  AS alt_zone_adj_decay,
    NULL::text     AS alt_zone,
    NULL::int      AS alt_zone_curve_races,
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


def _apply_cross_zone_fallback(df: pd.DataFrame) -> pd.DataFrame:
    """Fill missing primary-zone curves from the opposite-zone curve.

    For starters whose adj_v0/adj_decay are NULL but alt_zone_adj_v0 exists,
    use the opposite-zone values shifted by the surface-specific empirical
    sprint↔route gap. Adds a column `physics_confidence_haircut` (1.0 = no
    haircut; <1.0 = cross-zone fallback was used) so downstream rating logic
    can reduce w_physics accordingly.
    """
    if "alt_zone_adj_v0" not in df.columns:
        df["physics_confidence_haircut"] = 1.0
        return df

    # Identify rows that need the fallback: primary curve missing, alt exists
    needs_fallback = df["adj_v0"].isna() & df["alt_zone_adj_v0"].notna()
    df["physics_confidence_haircut"] = 1.0

    if not needs_fallback.any():
        return df

    primary_zone = np.where(df["furlongs"] > 6.5, "route", "sprint")
    for idx in df.index[needs_fallback]:
        surface = df.at[idx, "surface"]
        r = CROSS_ZONE_R.get(surface)
        v0_shift = SPRINT_TO_ROUTE_V0_SHIFT.get(surface)
        decay_shift = SPRINT_TO_ROUTE_DECAY_SHIFT.get(surface)
        if r is None or v0_shift is None or decay_shift is None:
            continue  # unknown surface; leave as missing

        alt_v0 = float(df.at[idx, "alt_zone_adj_v0"])
        alt_decay = float(df.at[idx, "alt_zone_adj_decay"])

        # Direction: if needed zone is route and we have sprint, shift sprint
        # → route by adding the negative gap. If needed zone is sprint and we
        # have route, subtract (move route values up to sprint level).
        if primary_zone[df.index.get_loc(idx)] == "route":
            adj_v0 = alt_v0 + v0_shift
            adj_decay = alt_decay + decay_shift
        else:
            adj_v0 = alt_v0 - v0_shift
            adj_decay = alt_decay - decay_shift

        df.at[idx, "adj_v0"] = adj_v0
        df.at[idx, "adj_decay"] = adj_decay
        df.at[idx, "physics_confidence_haircut"] = r
        # Carry curve_races count from alt zone so w_physics has something
        # to work with — caller will multiply w by haircut.
        if pd.isna(df.at[idx, "curve_races"]) and "alt_zone_curve_races" in df.columns:
            df.at[idx, "curve_races"] = df.at[idx, "alt_zone_curve_races"]

    return df


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
                "alt_zone_adj_v0", "alt_zone_adj_decay",
                "current_v0", "current_decay", "career_v0", "career_decay", "v0_trend"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df = _apply_cross_zone_fallback(df)
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
-- Trainer × dimension keys for which we need the latest snapshot.
-- Cross-product of today's trainers × 5 dimensions (~few hundred rows).
trainer_dim_keys AS (
    SELECT DISTINCT rs.trainer_last, rs.trainer_first, d.dimension
    FROM race_starters rs
    CROSS JOIN (VALUES ('fts'), ('claim'), ('drop'), ('layoff'), ('switch')) d(dimension)
),
-- For each (trainer, dimension), pull the single most-recent snapshot
-- before race_date via LATERAL + LIMIT 1. Lookup is per-key — uses the
-- composite (trainer_last, trainer_first, snapshot_date, dimension)
-- index for an index-only descending walk that stops at the first row.
trainer_ae_latest AS (
    SELECT k.trainer_last, k.trainer_first, k.dimension,
           snap.starts, snap.wins, snap.expected
    FROM trainer_dim_keys k
    LEFT JOIN LATERAL (
        SELECT starts, wins, expected
        FROM handycapper.rs_trainer_ae_daily snap
        WHERE snap.trainer_last = k.trainer_last
          AND snap.trainer_first = k.trainer_first
          AND snap.dimension = k.dimension
          AND snap.snapshot_date < %(race_date)s
        ORDER BY snap.snapshot_date DESC
        LIMIT 1
    ) snap ON true
),
trainer_fts AS (
    SELECT trainer_last, trainer_first,
           starts AS fts_starts, wins AS fts_wins, expected AS fts_expected
    FROM trainer_ae_latest WHERE dimension = 'fts'
),
trainer_claim AS (
    SELECT trainer_last, trainer_first,
           starts AS claim_starts, wins AS claim_wins, expected AS claim_expected
    FROM trainer_ae_latest WHERE dimension = 'claim'
),
trainer_drop AS (
    SELECT trainer_last, trainer_first,
           starts AS drop_starts, wins AS drop_wins, expected AS drop_expected
    FROM trainer_ae_latest WHERE dimension = 'drop'
),
trainer_layoff AS (
    SELECT trainer_last, trainer_first,
           starts AS layoff_starts, wins AS layoff_wins, expected AS layoff_expected
    FROM trainer_ae_latest WHERE dimension = 'layoff'
),
trainer_switch AS (
    SELECT trainer_last, trainer_first,
           starts AS switch_starts, wins AS switch_wins, expected AS switch_expected
    FROM trainer_ae_latest WHERE dimension = 'switch'
),
-- Today's jockey keys only. The previous-jockey lookup (for
-- upgrade/downgrade detection) gets its own LATERAL in the final
-- SELECT, fed from prev_start.prev_jockey_*. Splitting this avoids
-- a UNION-on-DISTINCT-on-full-starters-table that the planner can't
-- push the snapshot_date filter through.
jockey_keys_today AS (
    SELECT DISTINCT jockey_last, jockey_first FROM race_starters
),
jockey_career AS (
    SELECT k.jockey_last, k.jockey_first, snap.career_starts, snap.career_win_pct
    FROM jockey_keys_today k
    LEFT JOIN LATERAL (
        SELECT career_starts, career_win_pct
        FROM handycapper.rs_jockey_career_daily snap
        WHERE snap.jockey_last = k.jockey_last
          AND snap.jockey_first = k.jockey_first
          AND snap.snapshot_date < %(race_date)s
        ORDER BY snap.snapshot_date DESC
        LIMIT 1
    ) snap ON true
    WHERE snap.career_starts IS NOT NULL
),
-- Jockey trailing 12m at this track. Weekly snapshots are precomputed
-- per (snapshot_week_start, track, jockey). For race date R, find the
-- snapshot for the ISO-week containing R. The snapshot's window is
-- [week_start - 365 days, week_start - 1 day], which approximates
-- "trailing 12 months at this track as of this week." Slight stale
-- bias: a Sunday race sees the prior Monday's snapshot, so up to 6
-- days less recent than the inline CTE — acceptable for a 365-day
-- rolling window.
jockey_track AS (
    SELECT jt.jockey_last, jt.jockey_first,
           jt.starts_12m AS jock_starts_12m,
           jt.wins_12m   AS jock_wins_12m
    FROM handycapper.rs_jockey_track_weekly jt
    WHERE jt.snapshot_week_start = date_trunc('week', %(race_date)s::date)::date
      AND jt.track = %(track)s
      AND (jt.jockey_last, jt.jockey_first) IN (
          SELECT DISTINCT jockey_last, jockey_first FROM race_starters
      )
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
-- Per-starter most-recent prior race. Replaces the previous CTE that
-- did Sort+DistinctON over a 15M-row sequential scan; LATERAL with
-- LIMIT 1 lets the planner stop at the first prior start for each of
-- today's horses (~88 horses × ~10 idx hits each, vs full table scan).
prev_start AS (
    SELECT rs.starter_id, ps.prev_starter_id, ps.prev_surface, ps.prev_purse,
           ps.prev_jockey_last, ps.prev_jockey_first, ps.was_claimed_last,
           ps.prev_race_date
    FROM race_starters rs
    LEFT JOIN LATERAL (
        SELECT prev_s.id AS prev_starter_id,
               prev_r.surface AS prev_surface,
               prev_r.purse AS prev_purse,
               prev_s.jockey_last AS prev_jockey_last,
               prev_s.jockey_first AS prev_jockey_first,
               prev_s.claimed AS was_claimed_last,
               prev_r.date AS prev_race_date
        FROM handycapper.starters prev_s
        JOIN handycapper.races prev_r ON prev_r.id = prev_s.race_id
        WHERE prev_s.horse = rs.horse
          AND prev_s.id != rs.starter_id
          AND prev_r.date < %(race_date)s
        ORDER BY prev_r.date DESC
        LIMIT 1
    ) ps ON true
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
    -- Jockey career win rate (point-in-time, for tier + upgrade detection)
    jc.career_starts AS jock_career_starts,
    jc.career_win_pct AS jock_career_win_pct,
    -- Jockey track form (trailing 12m)
    jt.jock_starts_12m, jt.jock_wins_12m,
    CASE WHEN jt.jock_starts_12m > 0 THEN jt.jock_wins_12m::float / jt.jock_starts_12m END AS jock_track_win_pct,
    -- Previous jockey career win rate (for upgrade/downgrade)
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
        THEN (DATE %(race_date)s - ps.prev_race_date)
    END AS days_since_prev,
    CASE WHEN ps.prev_race_date IS NOT NULL
        THEN (DATE %(race_date)s - ps.prev_race_date) >= 90
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
LEFT JOIN LATERAL (
    SELECT career_win_pct
    FROM handycapper.rs_jockey_career_daily snap
    WHERE ps.prev_jockey_last IS NOT NULL
      AND snap.jockey_last = ps.prev_jockey_last
      AND snap.jockey_first = ps.prev_jockey_first
      AND snap.snapshot_date < %(race_date)s
    ORDER BY snap.snapshot_date DESC
    LIMIT 1
) jc_prev ON true
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
    """Load actual results for post-race reveal. Only call AFTER bets are committed.

    Returns one row per starter with finish data plus per-starter WPS payoffs
    and per-race vertical-exotic payoffs. All payoffs are normalized to per-$1
    (raw `payoff / unit`) so callers can multiply by stake without worrying
    about the chart's base unit. Horizontal exotic payoffs (DD/Pick N) are
    not in this DataFrame — load via the exotics table separately.
    """
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
        -- WPS payoffs from the wps table (per-starter, base unit usually $2)
        w_win.payoff  AS win_payoff_raw,
        w_win.unit    AS win_unit,
        w_plc.payoff  AS place_payoff_raw,
        w_plc.unit    AS place_unit,
        w_shw.payoff  AS show_payoff_raw,
        w_shw.unit    AS show_unit,
        -- Vertical exotic payoffs normalized to per-$1
        e_ex.payoff / NULLIF(e_ex.unit, 0) AS exacta_payoff,
        e_qn.payoff / NULLIF(e_qn.unit, 0) AS quinella_payoff,
        e_tri.payoff / NULLIF(e_tri.unit, 0) AS trifecta_payoff,
        e_sup.payoff / NULLIF(e_sup.unit, 0) AS super_payoff,
        e_hi5.payoff / NULLIF(e_hi5.unit, 0) AS hi5_payoff,
        -- Base units (so caller can show "$2 WPS / $1 Exacta / $0.50 Tri" provenance)
        e_ex.unit  AS exacta_unit,
        e_qn.unit  AS quinella_unit,
        e_tri.unit AS trifecta_unit,
        e_sup.unit AS super_unit,
        e_hi5.unit AS hi5_unit
    FROM handycapper.races r
    JOIN handycapper.starters s ON s.race_id = r.id
    LEFT JOIN handycapper.wps w_win ON w_win.starter_id = s.id AND w_win.type = 'Win'   AND w_win.payoff > 0
    LEFT JOIN handycapper.wps w_plc ON w_plc.starter_id = s.id AND w_plc.type = 'Place' AND w_plc.payoff > 0
    LEFT JOIN handycapper.wps w_shw ON w_shw.starter_id = s.id AND w_shw.type = 'Show'  AND w_shw.payoff > 0
    LEFT JOIN handycapper.exotics e_ex  ON e_ex.race_id = r.id  AND e_ex.bet_type  = 'EXACTA'     AND e_ex.payoff > 0
    LEFT JOIN handycapper.exotics e_qn  ON e_qn.race_id = r.id  AND e_qn.bet_type  = 'QUINELLA'   AND e_qn.payoff > 0
    LEFT JOIN handycapper.exotics e_tri ON e_tri.race_id = r.id AND e_tri.bet_type = 'TRIFECTA'   AND e_tri.payoff > 0
    LEFT JOIN handycapper.exotics e_sup ON e_sup.race_id = r.id AND e_sup.bet_type = 'SUPERFECTA' AND e_sup.payoff > 0
    LEFT JOIN handycapper.exotics e_hi5 ON e_hi5.race_id = r.id AND e_hi5.bet_type = 'HI_5'       AND e_hi5.payoff > 0
    WHERE r.track = %(track)s AND r.date = %(race_date)s
    ORDER BY r.number, s.official_position
    """
    return pd.read_sql(sql, conn, params={"track": track, "race_date": race_date})
