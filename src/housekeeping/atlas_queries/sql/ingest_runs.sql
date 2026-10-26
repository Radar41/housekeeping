-- One row per embed_ingest_run event: when it ran, on what device, and what it did.
-- Atlas logs ingest per run, not per file, so this is the only ingest history there is.
-- unchanged near the document count means an incremental run; indexed near it means a full run.
-- indexed = 0 with failed > 0 means the run started and did nothing.
SELECT p.occurred_at,
       p.payload_jsonb->>'run_id' AS run_id,
       p.payload_jsonb->'backend'->>'model' AS model,
       p.payload_jsonb->'backend'->>'dim' AS dim,
       p.payload_jsonb->'backend'->>'device' AS device,
       p.payload_jsonb->'backend'->>'endpoint' AS endpoint,
       (p.payload_jsonb->'counts'->>'indexed')::bigint AS indexed,
       (p.payload_jsonb->'counts'->>'unchanged')::bigint AS unchanged,
       (p.payload_jsonb->'counts'->>'failed')::bigint AS failed,
       (p.payload_jsonb->'counts'->>'chunks')::bigint AS chunks,
       (p.payload_jsonb->'counts'->>'skipped')::bigint AS skipped,
       p.payload_jsonb->'counts' AS counts
FROM atlas.atlas_provenance p
WHERE p.action = 'embed_ingest_run'
ORDER BY p.occurred_at;
