-- Event types in atlas_provenance, by week, with first and last timestamp per type.
-- Event type is the action column. target_tables shows which tables each type touches.
-- Weeks are ISO weeks in UTC. gap_days is the number of empty days between this
-- week's events and the previous week that had events for the same type.
-- gap_in_july_2026 is true when that empty stretch overlaps July 2026.
WITH events AS (
    SELECT action,
           target_table,
           occurred_at,
           date_trunc('week', occurred_at AT TIME ZONE 'UTC') AS week
    FROM atlas.atlas_provenance
),
by_type AS (
    SELECT action,
           count(*) AS type_total,
           min(occurred_at) AS type_first_at,
           max(occurred_at) AS type_last_at,
           string_agg(DISTINCT target_table, ',' ORDER BY target_table) AS target_tables
    FROM events
    GROUP BY action
),
by_week AS (
    SELECT action,
           week,
           count(*) AS week_events,
           min(occurred_at) AS week_first_at,
           max(occurred_at) AS week_last_at
    FROM events
    GROUP BY action, week
),
gaps AS (
    SELECT w.*,
           lag(w.week) OVER (PARTITION BY w.action ORDER BY w.week) AS prev_week
    FROM by_week w
)
SELECT g.action,
       t.type_total,
       t.type_first_at,
       t.type_last_at,
       t.target_tables,
       g.week::date AS week_start,
       g.week_events,
       g.week_first_at,
       g.week_last_at,
       CASE WHEN g.prev_week IS NULL THEN NULL
            ELSE (g.week::date - (g.prev_week::date + 7)) END AS gap_days,
       COALESCE(
           g.prev_week IS NOT NULL
           AND g.week::date - (g.prev_week::date + 7) > 0
           AND (g.prev_week::date + 7) < DATE '2026-08-01'
           AND g.week::date > DATE '2026-07-01',
           false) AS gap_in_july_2026
FROM gaps g
JOIN by_type t ON t.action = g.action
ORDER BY g.action, g.week;
