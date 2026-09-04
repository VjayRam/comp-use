"""Postgres evidence store - sync (psycopg2), not Hawkeye's async asyncpg, since
comp_use's DiscoveryAgent/ReplayEngine/EvidenceLogger are entirely synchronous
(Playwright's sync API throughout) - an async driver would need every call site
wrapped in asyncio.run(), which is worse than just using a sync one.

Every write here is best-effort: wrapped in try/except, logs a warning, never raises.
Same principle as Hawkeye's orchestrator/db/store.py - a Postgres outage must never
crash a discovery/replay run. The JSONL evidence files remain the source of truth;
this is an additional, queryable destination for the dashboard.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

_pool = None


def db_enabled() -> bool:
    return bool(os.environ.get("COMP_USE_DB_URL"))


def _get_pool():
    global _pool
    if _pool is None:
        import psycopg2.pool

        dsn = os.environ["COMP_USE_DB_URL"]
        _pool = psycopg2.pool.ThreadedConnectionPool(1, 10, dsn)
    return _pool


def _execute(query: str, params: tuple) -> None:
    if not db_enabled():
        return
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
        conn.commit()
    except Exception as exc:
        logger.warning("Postgres write failed (non-fatal): %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
    finally:
        if conn is not None:
            _pool.putconn(conn)


def start_run(
    run_id: str, kind: str, capability_name: str | None, goal: str | None,
    novnc_url: str | None = None, container_name: str | None = None,
) -> None:
    _execute(
        """
        INSERT INTO runs (id, kind, capability_name, goal, status, novnc_url, container_name)
        VALUES (%s, %s, %s, %s, 'running', %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        (run_id, kind, capability_name, goal, novnc_url, container_name),
    )


def set_novnc_url(run_id: str, novnc_url: str, container_name: str) -> None:
    _execute(
        "UPDATE runs SET novnc_url = %s, container_name = %s WHERE id = %s",
        (novnc_url, container_name, run_id),
    )


def finish_run(run_id: str, status: str, result: dict[str, Any] | None) -> None:
    _execute(
        """
        UPDATE runs SET status = %s, result = %s, finished_at = now()
        WHERE id = %s
        """,
        (status, json.dumps(result) if result is not None else None, run_id),
    )


def delete_run(run_id: str) -> int:
    """run_events and run_screenshots both FK run_id -> runs.id ON DELETE CASCADE
    (see schema.sql), so deleting the runs row is enough to remove a run's full
    history in one statement."""
    if not db_enabled():
        return 0
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM runs WHERE id = %s", (run_id,))
            deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception as exc:
        logger.warning("Postgres write failed (non-fatal): %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return 0
    finally:
        if conn is not None:
            _pool.putconn(conn)


def insert_event(run_id: str, event_type: str, data: dict[str, Any]) -> None:
    _execute(
        "INSERT INTO run_events (run_id, event_type, data) VALUES (%s, %s, %s)",
        (run_id, event_type, json.dumps(data)),
    )


def _query(query: str, params: tuple) -> list[dict[str, Any]]:
    """Read counterpart to _execute() - same non-fatal contract (an unreachable
    Postgres returns an empty list rather than raising), since the dashboard reading
    stale/empty history is a far smaller problem than it crashing a page."""
    if not db_enabled():
        return []
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(query, params)
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("Postgres read failed (non-fatal): %s", exc)
        return []
    finally:
        if conn is not None:
            _pool.putconn(conn)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT id, kind, capability_name, goal, status, novnc_url, result, started_at, finished_at
        FROM runs ORDER BY started_at DESC LIMIT %s
        """,
        (limit,),
    )


def get_run(run_id: str) -> dict[str, Any] | None:
    rows = _query(
        """
        SELECT id, kind, capability_name, goal, status, novnc_url, result, started_at, finished_at
        FROM runs WHERE id = %s
        """,
        (run_id,),
    )
    return rows[0] if rows else None


def list_events(run_id: str) -> list[dict[str, Any]]:
    return _query(
        "SELECT event_type, data, created_at FROM run_events WHERE run_id = %s ORDER BY created_at ASC",
        (run_id,),
    )


def save_screenshot(run_id: str, label: str, png_bytes: bytes) -> None:
    _execute(
        """
        INSERT INTO run_screenshots (run_id, label, png_bytes) VALUES (%s, %s, %s)
        ON CONFLICT (run_id, label) DO UPDATE SET png_bytes = EXCLUDED.png_bytes, created_at = now()
        """,
        (run_id, label, memoryview(png_bytes)),
    )


def get_screenshot(run_id: str, label: str) -> bytes | None:
    """Direct (non-list) read, so it can return raw bytes instead of the
    dict-of-columns shape _query() gives every other reader here."""
    if not db_enabled():
        return None
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT png_bytes FROM run_screenshots WHERE run_id = %s AND label = %s",
                (run_id, label),
            )
            row = cur.fetchone()
            return bytes(row[0]) if row else None
    except Exception as exc:
        logger.warning("Postgres screenshot read failed (non-fatal): %s", exc)
        return None
    finally:
        if conn is not None:
            _pool.putconn(conn)


# ---- Artifacts (capability specs) ----
# Primary store for Artifact JSON when db_enabled() - see schema.sql's artifacts
# table comment. comp_use/cli.py falls back to the on-disk artifacts/<name>/vN.json
# layout only when no DB is configured.

def save_artifact(
    capability_name: str, version: int, status: str, data: dict[str, Any],
    created_from_run_id: str | None,
) -> None:
    _execute(
        """
        INSERT INTO artifacts (capability_name, version, status, data, created_from_run_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (capability_name, version)
        DO UPDATE SET status = EXCLUDED.status, data = EXCLUDED.data
        """,
        (capability_name, version, status, json.dumps(data), created_from_run_id),
    )


def load_artifact(capability_name: str, version: int) -> dict[str, Any] | None:
    rows = _query(
        "SELECT data FROM artifacts WHERE capability_name = %s AND version = %s",
        (capability_name, version),
    )
    return rows[0]["data"] if rows else None


def latest_approved_artifact(capability_name: str) -> dict[str, Any] | None:
    # Prefers the version explicitly marked is_default (see Artifact.is_default in
    # comp_use/schemas.py) when one exists; otherwise falls back to the highest
    # version number, same as before is_default existed. (data->>'is_default')::boolean
    # is NULL for old rows written before this field existed - NULLS LAST keeps those
    # behind any explicit true/false so an old undecorated row never outranks a
    # deliberately-chosen default.
    rows = _query(
        """
        SELECT data FROM artifacts WHERE capability_name = %s AND status = 'approved'
        ORDER BY (data->>'is_default')::boolean DESC NULLS LAST, version DESC LIMIT 1
        """,
        (capability_name,),
    )
    return rows[0]["data"] if rows else None


def list_artifact_versions(capability_name: str) -> list[dict[str, Any]]:
    return _query(
        """
        SELECT version, status, created_from_run_id FROM artifacts
        WHERE capability_name = %s ORDER BY version ASC
        """,
        (capability_name,),
    )


def list_capability_names() -> list[str]:
    rows = _query("SELECT DISTINCT capability_name FROM artifacts ORDER BY capability_name ASC", ())
    return [r["capability_name"] for r in rows]


def delete_capability(capability_name: str) -> int:
    """Deletes every version of a capability. Runs/run_events/run_screenshots are
    untouched - they key on run_id, not capability_name+version, and carry no FK to
    artifacts, so a capability's run history survives its artifact being deleted."""
    if not db_enabled():
        return 0
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM artifacts WHERE capability_name = %s", (capability_name,))
            deleted = cur.rowcount
        conn.commit()
        return deleted
    except Exception as exc:
        logger.warning("Postgres write failed (non-fatal): %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return 0
    finally:
        if conn is not None:
            _pool.putconn(conn)


def set_artifact_status(capability_name: str, version: int, status: str) -> bool:
    if not db_enabled():
        return False
    conn = None
    try:
        pool = _get_pool()
        conn = pool.getconn()
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE artifacts SET status = %s WHERE capability_name = %s AND version = %s",
                (status, capability_name, version),
            )
            updated = cur.rowcount > 0
        conn.commit()
        return updated
    except Exception as exc:
        logger.warning("Postgres write failed (non-fatal): %s", exc)
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass
        return False
    finally:
        if conn is not None:
            _pool.putconn(conn)
