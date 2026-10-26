-- Documents whose stored chunk text looks like it holds a credential.
-- Returns paths and match kinds only, never the text. Read the file on disk yourself to judge it.
-- key_assignment is loose (name = long value) and mostly false positives; the others are specific.
SELECT d.root_id,
       d.rel_path,
       string_agg(DISTINCT v.kind, ',' ORDER BY v.kind) AS kinds,
       count(DISTINCT c.chunk_id) AS chunks
FROM atlas.atlas_chunk c
JOIN atlas.atlas_document d ON d.document_id = c.document_id
CROSS JOIN LATERAL (VALUES
    (CASE WHEN c.chunk_text ~ 'BEGIN [A-Z ]*PRIVATE KEY' THEN 'private_key' END),
    (CASE WHEN c.chunk_text ~ 'gh[pousr]_[A-Za-z0-9]{36}' THEN 'github_token' END),
    (CASE WHEN c.chunk_text ~ 'sk-[A-Za-z0-9_-]{20,}' THEN 'sk_api_key' END),
    (CASE WHEN c.chunk_text ~ 'AKIA[0-9A-Z]{16}' THEN 'aws_key' END),
    (CASE WHEN c.chunk_text ~ 'xox[bpas]-[A-Za-z0-9-]{10,}' THEN 'slack_token' END),
    (CASE WHEN c.chunk_text ~* '(api[_-]?key|secret|password|token)["'' ]*[:=]["'' ]*[A-Za-z0-9_-]{16,}'
          THEN 'key_assignment' END)
) AS v(kind)
WHERE v.kind IS NOT NULL
GROUP BY d.root_id, d.rel_path
ORDER BY d.root_id, d.rel_path;
