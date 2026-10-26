-- Payload keys per provenance action, with counts and date range.
-- Answers the question provenance_check.sql cannot: whether moves and edits share one
-- action and differ only inside payload_jsonb (for example from_path and to_path keys).
-- Events with an empty payload are counted under payload_key '(empty)'.
WITH keyed AS (
    SELECT p.action, k.key AS payload_key, p.occurred_at
    FROM atlas.atlas_provenance p
    CROSS JOIN LATERAL jsonb_object_keys(p.payload_jsonb) AS k(key)
    UNION ALL
    SELECT p.action, '(empty)' AS payload_key, p.occurred_at
    FROM atlas.atlas_provenance p
    WHERE p.payload_jsonb = '{}'::jsonb
)
SELECT action,
       payload_key,
       count(*) AS events,
       min(occurred_at) AS first_at,
       max(occurred_at) AS last_at
FROM keyed
GROUP BY action, payload_key
ORDER BY action, events DESC, payload_key;
