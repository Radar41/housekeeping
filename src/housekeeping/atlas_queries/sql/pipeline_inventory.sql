-- One row per canonical, indexed code file or pipeline marker (Makefile, pyproject.toml,
-- Cargo.toml, *.sbatch, ...), with its top project tag and header text.
-- header is the first 800 characters of chunk 0: usually the shebang, docstring, or leading
-- comments, which is where the files describe themselves. Nothing is read from disk.
-- Line breaks in header are written as a literal \n and tabs as spaces, so each row stays one TSV line.
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
)
SELECT c.root_id,
       c.root_path,
       c.document_id,
       c.rel_path,
       c.dir,
       c.ext,
       c.is_marker,
       t.project,
       t.score AS project_score,
       c.sha256,
       c.size_bytes,
       c.mtime,
       regexp_replace(regexp_replace(left(ch.chunk_text, 800), '[\r\n]+', ' \n ', 'g'), '\t', ' ', 'g') AS header
FROM code c
LEFT JOIN tag t ON t.document_id = c.document_id
LEFT JOIN atlas.atlas_chunk ch ON ch.document_id = c.document_id AND ch.chunk_index = 0
ORDER BY c.root_id, c.dir, c.rel_path;
