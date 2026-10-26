-- The hk_stage schema is created by init() (or once by an admin); see processes/10-atlas-staged.md.
CREATE TABLE IF NOT EXISTS hk_stage.run (
    id uuid PRIMARY KEY,
    root_id bigint NOT NULL,
    root_path text NOT NULL,
    start_rel text NOT NULL,
    contract jsonb NOT NULL,
    paused boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS hk_stage.directory (
    run_id uuid NOT NULL REFERENCES hk_stage.run(id),
    rel text NOT NULL,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','error')),
    reason text,
    PRIMARY KEY (run_id, rel)
);
CREATE INDEX IF NOT EXISTS stage_directory_pending
 ON hk_stage.directory(run_id,rel) WHERE state='pending';
CREATE TABLE IF NOT EXISTS hk_stage.file (
    run_id uuid NOT NULL REFERENCES hk_stage.run(id),
    rel text NOT NULL,
    size bigint,
    mtime_ns bigint,
    ctime_ns bigint,
    device bigint,
    inode bigint,
    lane text NOT NULL DEFAULT 'text',
    state text NOT NULL CHECK (state IN
      ('candidate','ready','preserved','review','skip','prepared','published','error')),
    reason text,
    payload jsonb,
    document_id bigint,
    elapsed_s double precision,
    PRIMARY KEY (run_id, rel)
);
CREATE INDEX IF NOT EXISTS stage_file_work
 ON hk_stage.file(run_id, state, lane, rel);
CREATE INDEX IF NOT EXISTS stage_file_size
 ON hk_stage.file(run_id, state, size, rel);
CREATE TABLE IF NOT EXISTS hk_stage.vector (
    run_id uuid NOT NULL REFERENCES hk_stage.run(id),
    text_sha text NOT NULL,
    text_body text NOT NULL,
    vector bytea,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','error')),
    error text,
    elapsed_s double precision,
    PRIMARY KEY (run_id, text_sha)
);
CREATE INDEX IF NOT EXISTS stage_vector_work
 ON hk_stage.vector(run_id,state,text_sha);
CREATE INDEX IF NOT EXISTS stage_vector_length
 ON hk_stage.vector(run_id,state,length(text_body),text_sha);
CREATE TABLE IF NOT EXISTS hk_stage.event (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES hk_stage.run(id),
    ts timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL,
    n bigint NOT NULL DEFAULT 1,
    elapsed_s double precision,
    detail jsonb NOT NULL DEFAULT '{}'
);
