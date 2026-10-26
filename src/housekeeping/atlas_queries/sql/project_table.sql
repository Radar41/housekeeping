-- One row per project in atlas_doc_relevance (active rows only).
-- directories: every distinct directory holding a document of the project,
-- as root_path plus the directory part of rel_path, joined with ' | '.
-- newest_revision: latest document mtime. A document scored by several methods
-- counts once. has_git and has_readme are inferred from indexed paths, see GAPS.md.
SELECT rel.project,
       string_agg(DISTINCT concat_ws('/', rtrim(r.root_path, '/'), substring(d.rel_path from '^(.*)/[^/]*$')),
                  ' | ' ORDER BY concat_ws('/', rtrim(r.root_path, '/'), substring(d.rel_path from '^(.*)/[^/]*$'))) AS directories,
       (max(d.mtime) AT TIME ZONE 'UTC')::date AS newest_revision,
       COALESCE(bool_or(d.rel_path ~ '(^|/)\.git(/|$)'), false) AS has_git,
       COALESCE(bool_or(lower(regexp_replace(d.rel_path, '^.*/', '')) ~ '^readme(\.|$)'), false) AS has_readme,
       count(DISTINCT d.document_id) AS document_count,
       '' AS decision
FROM atlas.atlas_doc_relevance rel
JOIN atlas.atlas_document d ON d.document_id = rel.document_id
JOIN atlas.atlas_root r ON r.root_id = d.root_id
WHERE rel.status = 'active'
GROUP BY rel.project
ORDER BY rel.project;
