# Gaps in schema.txt for atlas_queries

schema.txt is the only source of truth. These queries are the closest correct ones; each gap below is what they cannot do.

## provenance_check.sql
- Missing: a documented list of action values. atlas_provenance.action is free text, so whether moves and edits are distinct event types is answered by the query output (one row group per distinct action), not by the schema.
- Missing: any constraint on action. Would fill it: a CHECK or lookup table of event types.
- payload_jsonb is not inspected, because its keys are not described. If moves and edits share one action and differ only in payload, the query will not separate them.

## inbox_three_way.sql
- Missing: a stored version chain per document other than atlas_version_chain. Chains with chain_kind = 'sibling_set' are ignored; only 'version_chain' with status 'active' counts.
- Missing: a revision timestamp that is certain. "Newer side" compares atlas_document.mtime, which is the file mtime and can be reset by a copy or migration. Would fill it: a content-derived or ingest-time revision date per document.
- atlas_document.status is not filtered, so a 'missing' outside member can still be the canonical member. Would fill it: a decision on whether missing members count.

## project_table.sql
- Missing: has_git. No column or table records whether a directory is a git repo. Closest: true when an indexed document path has a .git component. If .git is excluded from indexing (atlas_root.excludes), this is always false.
- Missing: has_readme. No column records it. Closest: true when an indexed path in the project has a basename starting with README.
- Missing: a project to directory mapping. atlas_doc_relevance has only document_id and project, so "directory" means the directory part of rel_path (parent_rel_path is not used, it can be unpopulated) of each scored document, and a project spread over many directories lists them all.
- Missing: newest revision date as a project property. Closest: max(atlas_document.mtime) over the project's documents.
- Would fill all four: a project table with project name, root directory, git flag, readme flag, last commit date.
