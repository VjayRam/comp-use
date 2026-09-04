import pytest


@pytest.fixture(autouse=True)
def _no_postgres_by_default(monkeypatch):
    """The whole suite exercises the on-disk fallback (artifacts_dir/evidence_dir via
    tmp_path) deterministically, regardless of whether the developer's local .env sets
    COMP_USE_DB_URL or a real Postgres happens to be reachable. Individual tests that
    want to exercise the Postgres-backed path opt back in explicitly with
    monkeypatch.setenv("COMP_USE_DB_URL", ...) (see e.g. comp_use/pg tests, if any)."""
    # Set (not delete): comp_use.cli.main() calls load_dotenv() on every invocation,
    # which re-populates any var that's absent from os.environ (python-dotenv only
    # skips vars that already EXIST there) - a bare delenv would get silently
    # re-filled from the developer's real .env the moment a test calls cli.main().
    # An empty string survives that (the key already "exists") and is falsy, so
    # db_enabled() still reads it as disabled.
    monkeypatch.setenv("COMP_USE_DB_URL", "")
    from comp_use.pg import store as pg_store

    pg_store._pool = None
