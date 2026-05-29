SET search_path = handycapper;

-- sim_candidates — materialized view defining the simulator's playable
-- universe. Filters: TB only, 2005-2017, no Grade 1/2 day-track
-- combinations, ≥8 races, ≥7 with trifecta results, avg field ≥7,
-- avg trifecta pool ≥$10K. Used by scripts/pick_sim_day.py and the
-- FLB/exotic POCs to scope to deployable race days.
--
-- Materialized (not regular VIEW) because the GROUP BY HAVING is
-- expensive enough that re-running it on every JOIN would be slow.
-- REFRESH MATERIALIZED VIEW handycapper.sim_candidates after any
-- substantial change to the underlying races/exotics tables.
--
-- Originally created via raw SQL during the simulator setup and never
-- tracked in a migration. Captured here from production state on
-- 2026-05-29 via pg_get_viewdef.

DROP MATERIALIZED VIEW IF EXISTS sim_candidates;

CREATE MATERIALIZED VIEW sim_candidates AS
SELECT
    r.track,
    r.date,
    count(DISTINCT r.id) AS n_races,
    round(avg(r.number_of_runners), 1) AS avg_field,
    count(DISTINCT
        CASE
            WHEN e.bet_type::text = 'TRIFECTA'::text AND e.payoff > 0::numeric
            THEN r.id
            ELSE NULL::bigint
        END) AS races_w_tri,
    round(avg(
        CASE
            WHEN e.bet_type::text = 'TRIFECTA'::text THEN e.pool
            ELSE NULL::numeric
        END), 0) AS avg_tri_pool
FROM races r
LEFT JOIN exotics e ON e.race_id = r.id
WHERE r.breed::text = 'TB'::text
    AND r.date >= '2005-01-01'::date
    AND r.date <= '2017-12-31'::date
    AND NOT (EXISTS (
        SELECT 1 FROM races g
        WHERE g.track::text = r.track::text
            AND g.date = r.date
            AND (g.grade = ANY (ARRAY[1, 2]))
    ))
GROUP BY r.track, r.date
HAVING count(DISTINCT r.id) >= 8
    AND count(DISTINCT
        CASE
            WHEN e.bet_type::text = 'TRIFECTA'::text AND e.payoff > 0::numeric
            THEN r.id
            ELSE NULL::bigint
        END) >= 7
    AND avg(r.number_of_runners) >= 7.0
    AND avg(
        CASE
            WHEN e.bet_type::text = 'TRIFECTA'::text THEN e.pool
            ELSE NULL::numeric
        END) >= 10000::numeric;

-- Production has no index on this matview. pick_sim_day.py and the
-- POCs do full scans (44K rows × 13 years = small enough). Add a
-- (track, date) index here proactively because the cost is trivial
-- and rebuild-time queries against this matview from JOIN clauses
-- will benefit. Live production state did not have this index;
-- adding it during rebuild is a forward-looking improvement.
CREATE INDEX IF NOT EXISTS idx_sim_candidates_track_date
    ON sim_candidates (track, date);
