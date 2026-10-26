-- Param: %(root_id)s, the sweep root.
-- One row per document under that root, classified against its version chain
-- (chain_kind = 'version_chain', status = 'active'; sibling sets are ignored):
--   new        no chain member lives outside the root
--   duplicate  some outside member has the same sha256
--   changed    outside members exist but none has the same sha256
-- A document with several outside members is matched to one: identical hash
-- first, then version_status = 'canonical', then newest mtime, then lowest id.
-- newer_side is filled for changed only: inbox, outside, same_mtime, or unknown.
WITH inbox AS (
    SELECT d.document_id, d.rel_path, d.sha256, d.mtime,
           concat_ws('/', rtrim(r.root_path, '/'), d.rel_path) AS full_path
    FROM atlas.atlas_document d
    JOIN atlas.atlas_root r ON r.root_id = d.root_id
    WHERE d.root_id = %(root_id)s
),
chains AS (
    SELECT chain_id, document_id
    FROM atlas.atlas_version_chain
    WHERE chain_kind = 'version_chain' AND status = 'active'
),
own_chain AS (
    SELECT document_id, min(chain_id) AS chain_id
    FROM chains
    GROUP BY document_id
),
pairs AS (
    SELECT i.document_id,
           mine.chain_id,
           o.document_id AS member_id,
           concat_ws('/', rtrim(orr.root_path, '/'), o.rel_path) AS member_path,
           o.mtime AS member_mtime,
           o.version_status = 'canonical' AS member_canonical,
           (i.sha256 IS NOT NULL AND o.sha256 = i.sha256) AS same_hash
    FROM inbox i
    JOIN chains mine ON mine.document_id = i.document_id
    JOIN chains other ON other.chain_id = mine.chain_id
                     AND other.document_id <> mine.document_id
    JOIN atlas.atlas_document o ON o.document_id = other.document_id
                               AND o.root_id <> %(root_id)s
    JOIN atlas.atlas_root orr ON orr.root_id = o.root_id
),
best AS (
    SELECT DISTINCT ON (document_id) *
    FROM pairs
    ORDER BY document_id,
             same_hash DESC,
             member_canonical DESC NULLS LAST,
             member_mtime DESC NULLS LAST,
             member_id
)
SELECT i.document_id AS doc_id,
       i.full_path AS path,
       COALESCE(b.chain_id, oc.chain_id) AS chain_id,
       CASE WHEN b.document_id IS NULL THEN 'new'
            WHEN b.same_hash THEN 'duplicate'
            ELSE 'changed' END AS class,
       b.member_path AS canonical_member_path,
       CASE WHEN b.document_id IS NULL OR b.same_hash THEN NULL
            WHEN i.mtime IS NULL OR b.member_mtime IS NULL THEN 'unknown'
            WHEN i.mtime > b.member_mtime THEN 'inbox'
            WHEN i.mtime < b.member_mtime THEN 'outside'
            ELSE 'same_mtime' END AS newer_side
FROM inbox i
LEFT JOIN best b ON b.document_id = i.document_id
LEFT JOIN own_chain oc ON oc.document_id = i.document_id
ORDER BY i.document_id;
