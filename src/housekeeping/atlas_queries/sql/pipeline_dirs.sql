-- One row per directory that holds code: the pipeline candidates.
-- markers lists Makefile, pyproject.toml, *.sbatch and similar files found directly in the dir.
-- has_readme looks at every indexed file in the dir, not only code.
-- projects lists every project tag on the dir's code files, most files first.
-- root_id is optional; NULL means every root.
WITH docs AS (
    SELECT d.document_id,
           d.root_id,
           r.root_path,
           d.rel_path,
           COALESCE(NULLIF(regexp_replace(d.rel_path, '/[^/]*$', ''), d.rel_path), '.') AS dir,
           regexp_replace(d.rel_path, '^.*/', '') AS base,
           lower(substring(d.rel_path from '\.([A-Za-z0-9]+)$')) AS ext,
           d.sha256,
           d.size_bytes,
           d.mtime
    FROM atlas.atlas_document d
    JOIN atlas.atlas_root r ON r.root_id = d.root_id
    WHERE d.status = 'indexed'
      AND (d.version_status IS NULL OR d.version_status = 'canonical')
      AND (%(root_id)s::bigint IS NULL OR d.root_id = %(root_id)s::bigint)
),
code AS (
    SELECT docs.*,
           (base IN ('Makefile', 'makefile', 'GNUmakefile', 'justfile', 'Snakefile', 'CMakeLists.txt',
                     'pyproject.toml', 'Cargo.toml', 'package.json', 'environment.yml',
                     'requirements.txt', 'run.sh')
            OR ext IN ('sbatch', 'slurm')) AS is_marker
    FROM docs
    WHERE ext IN ('py', 'sh', 'bash', 'm', 'mjs', 'js', 'jsx', 'ts', 'tsx', 'rs', 'c', 'cc', 'cpp',
                  'h', 'hpp', 'jl', 'r', 'go', 'sql', 'sbatch', 'slurm', 'ipynb')
       OR base IN ('Makefile', 'makefile', 'GNUmakefile', 'justfile', 'Snakefile', 'CMakeLists.txt',
                   'pyproject.toml', 'Cargo.toml', 'package.json', 'environment.yml',
                   'requirements.txt', 'run.sh')
),
tag AS (
    SELECT DISTINCT ON (document_id) document_id, project, score
    FROM atlas.atlas_doc_relevance
    WHERE status = 'active'
    ORDER BY document_id, score DESC, project
),
readme AS (
    SELECT DISTINCT root_id, dir
    FROM docs
    WHERE base ILIKE 'readme%%'
),
proj AS (
    SELECT c.root_id, c.dir, t.project, count(*) AS n
    FROM code c
    JOIN tag t ON t.document_id = c.document_id
    GROUP BY c.root_id, c.dir, t.project
)
SELECT c.root_id,
       c.root_path,
       c.dir,
       count(*) AS code_files,
       count(*) FILTER (WHERE c.is_marker) AS marker_files,
       string_agg(DISTINCT c.base, ',' ORDER BY c.base) FILTER (WHERE c.is_marker) AS markers,
       string_agg(DISTINCT c.ext, ',' ORDER BY c.ext) AS exts,
       (SELECT string_agg(p.project, ',' ORDER BY p.n DESC, p.project)
        FROM proj p WHERE p.root_id = c.root_id AND p.dir = c.dir) AS projects,
       max(c.mtime) AS newest_mtime,
       sum(c.size_bytes) AS code_bytes,
       EXISTS (SELECT 1 FROM readme r WHERE r.root_id = c.root_id AND r.dir = c.dir) AS has_readme
FROM code c
GROUP BY c.root_id, c.root_path, c.dir
ORDER BY c.root_id, c.dir;
