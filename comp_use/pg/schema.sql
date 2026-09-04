-- Postgres store, adapted from Project-Hawkeye's orchestrator/db/schema.sql but
-- deliberately trimmed to just what comp_use needs: no multi-tenant SaaS structure
-- (organizations/users/projects/vault_secrets/test_suites/... - the "orchestration
-- and everything" this project explicitly doesn't need).
--
-- Postgres is the PRIMARY store for evidence (run_events, run_screenshots) and
-- capability artifacts (artifacts) when COMP_USE_DB_URL is configured - not a mirror
-- of on-disk files. That's a deliberate switch away from the original
-- evidence/<run_id>/... + artifacts/<name>/vN.json layout, which had no cleanup story
-- and was piling up as untracked files every run. The filesystem layout is kept only
-- as a fallback for local dev/tests when no DB is configured (see comp_use/cli.py's
-- load_artifact/save_artifact and EvidenceLogger).
--
-- runs mirrors what EvidenceLogger already tracks per run_id, plus the sandbox fields
-- (novnc_url/container_name) Hawkeye's own test_runs table already carries - kept
-- here for the same reason: a dashboard needs to know where to point a viewer.

CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,              -- 'discover' | 'replay' | 'invoke'
    capability_name TEXT,
    goal            TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    novnc_url       TEXT,
    container_name  TEXT,
    result          JSONB,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_capability ON runs (capability_name);

CREATE TABLE IF NOT EXISTS run_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    event_type  TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_run_events_run ON run_events (run_id, created_at);

-- Screenshots captured by EvidenceLogger.save_screenshot() - previously written as
-- loose PNG files under evidence/<run_id>/<label>.png. Storing the bytes directly
-- (runs are low-volume, one-off browser automation, not a media pipeline) means a
-- run's evidence is fully self-contained in the DB - nothing left on disk to manage
-- or accidentally commit.
CREATE TABLE IF NOT EXISTS run_screenshots (
    run_id      TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    label       TEXT NOT NULL,
    png_bytes   BYTEA NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, label)
);

-- Capability artifacts (comp_use/schemas.py's Artifact model, dumped whole as JSONB)
-- - previously one JSON file per version under artifacts/<capability_name>/vN.json.
-- Same motivation as run_screenshots: this is the thing that was actually piling up
-- untracked in `git status` across dozens of discovery runs, since every capability's
-- every version was a new file nothing ever cleaned up.
CREATE TABLE IF NOT EXISTS artifacts (
    capability_name TEXT NOT NULL,
    version         INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'approved',
    data            JSONB NOT NULL,
    created_from_run_id TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (capability_name, version)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_capability ON artifacts (capability_name, version DESC);
