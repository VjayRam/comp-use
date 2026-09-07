import json
import logging
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from comp_use.cli import (
    approve_artifact,
    build_llm_client,
    clear_default_version,
    delete_capability,
    list_capability_names,
    list_versions as _cli_list_versions,
    load_artifact,
    reject_artifact,
    retire_artifact,
    run_discover,
    run_replay,
    save_artifact,
    set_default_version,
)
from comp_use.chat.agent import ChatAgent
from comp_use.chat.catalog import build_chat_catalog
from comp_use.chat.session import ChatSessionManager
from comp_use.config import Settings, load_settings
from comp_use.pg import store as pg_store
from comp_use.replay.engine import validate_required_params
from comp_use.server.run_manager import RunManager

_CAPABILITY_NAME_RE = re.compile(r"^[a-z0-9_]+$")
_RUN_ID_RE = re.compile(r"^[a-z0-9_]+$")


def _validate_capability_name(name: str) -> None:
    """`name` is used to build a filesystem path (`artifacts_dir / name`) in
    every route below - reject anything that isn't a plain lowercase/digits/
    underscore token before it ever reaches Path(), so a value like ".." can't
    resolve outside artifacts_dir. Path traversal on a regulated financial
    artifact store is a live vulnerability, not just a missing feature."""
    if not _CAPABILITY_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=f"invalid capability name {name!r}: must match {_CAPABILITY_NAME_RE.pattern}",
        )


def _validate_run_id(run_id: str) -> None:
    """`run_id` is used to build a filesystem path (`evidence_dir / run_id`) in
    several routes below - most notably DELETE /runs/{run_id}, which calls
    shutil.rmtree() on the result. Same path-traversal exposure
    _validate_capability_name exists to prevent (e.g. a run_id of
    "../../artifacts" would let a caller delete an arbitrary directory this
    process can write to) - reject anything that isn't a plain lowercase/digits/
    underscore token before it ever reaches Path()."""
    if not _RUN_ID_RE.match(run_id):
        raise HTTPException(
            status_code=400,
            detail=f"invalid run_id {run_id!r}: must match {_RUN_ID_RE.pattern}",
        )


def _mint_run_id(kind: str) -> str:
    """A millisecond timestamp alone collides under real concurrency (two
    invokes - or an invoke and a chat-triggered invoke - landing in the same
    millisecond), silently overwriting one run's RunManager record and
    interleaving both runs' evidence/Postgres rows under one id. The uuid
    suffix makes that practically impossible; RunManager.start() also now
    rejects a collision outright rather than overwriting."""
    return f"{kind}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"


class InvokeRequest(BaseModel):
    params: dict[str, Any] = {}
    version: int | None = None


class DiscoverRequest(BaseModel):
    goal: str
    start_url: str
    param_hints: list[str] | None = None


class ResumeRequest(BaseModel):
    note: str = ""


class ApproveRequest(BaseModel):
    """Optional body for approve: reviewer-corrected default values.

    A discovery run records whatever literal it happened to type as each
    parameter's `example`, which is the value a caller sees pre-filled. Reviewing
    a draft is exactly when a wrong one gets noticed, so the correction and the
    approval are one operation - a separate edit call could succeed while the
    approve that followed it failed, leaving a draft carrying values nobody
    agreed to.
    """

    input_examples: dict[str, str] | None = None


class ChatMessageRequest(BaseModel):
    message: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="comp-use capability server")
    app.state.settings = settings
    app.state.run_manager = RunManager()
    # This process owns no runs from a previous one, so anything Postgres still shows
    # as open is a run whose process died before its `finally` could write a terminal
    # status. Left alone they show as live on the dashboard forever.
    if pg_store.db_enabled():
        pg_store.abandon_orphaned_runs()
    app.state.chat_sessions = ChatSessionManager(redaction_patterns=settings.redaction_patterns)
    app.state.chat_agent = ChatAgent(build_llm_client(settings))
    app.mount("/evidence", StaticFiles(directory=str(settings.evidence_dir)), name="evidence")

    # The dashboard (a separate Vite dev server / static build, not this process)
    # calls this API cross-origin. Regulated financial data is behind this API, but
    # this project has no auth layer at all yet (documented cut - see EXT_TASK_FIXES.md
    # and REPORT.md's Safety section) - wildcard origin is consistent with that
    # existing posture, not a new gap introduced here.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    def _capability_names() -> list[str]:
        return list_capability_names(settings.artifacts_dir)

    def _capability_versions(name: str) -> list[int]:
        return _cli_list_versions(name, settings.artifacts_dir)

    @app.get("/capabilities")
    def list_capabilities():
        out = []
        for name in _capability_names():
            versions = _capability_versions(name)
            approved_candidates = []  # descending version order
            has_pending_draft = False
            for v in reversed(versions):
                try:
                    candidate = load_artifact(name, settings.artifacts_dir, version=v)
                except Exception as exc:
                    # A single malformed artifact file (e.g. one written before a
                    # schema change - observed live with old "fixed"-type
                    # ValueSource artifacts after the ValueSource cleanup) must
                    # never take down the whole /capabilities listing for every
                    # OTHER capability too. Skip just this version, keep going.
                    logging.getLogger(__name__).warning(
                        "skipping unloadable artifact %s v%s: %s", name, v, exc
                    )
                    continue
                if candidate.status == "approved":
                    approved_candidates.append(candidate)
                if candidate.status == "draft":
                    has_pending_draft = True
            # Mirror load_artifact(version=None)'s own resolution: an explicit
            # is_default wins over "just pick the highest approved version" - the
            # summary's "version" must be the one an unpinned invoke would actually
            # run, or the dashboard's invoke form would be pre-filled/pinned against
            # the wrong version's schema.
            latest_approved = next(
                (c for c in approved_candidates if c.is_default), approved_candidates[0] if approved_candidates else None,
            )
            out.append({
                "capability_name": name,
                "description": latest_approved.description if latest_approved else None,
                "version": latest_approved.version if latest_approved else None,
                "input_schema": [p.model_dump() for p in latest_approved.input_schema] if latest_approved else [],
                "output_schema": [p.model_dump() for p in latest_approved.output_schema] if latest_approved else [],
                "has_pending_draft": has_pending_draft,
            })
        return out

    @app.get("/capabilities/{name}")
    def get_capability(name: str):
        _validate_capability_name(name)
        try:
            artifact = load_artifact(name, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.get("/capabilities/{name}/versions")
    def list_versions(name: str):
        _validate_capability_name(name)
        versions = _capability_versions(name)
        if not versions:
            raise HTTPException(status_code=404, detail=f"no capability '{name}'")
        created_at_by_version: dict[int, str] = {}
        if pg_store.db_enabled():
            for row in pg_store.list_artifact_versions(name):
                if row.get("created_at") is not None:
                    created_at_by_version[row["version"]] = row["created_at"].isoformat()
        out = []
        for v in versions:
            try:
                artifact = load_artifact(name, settings.artifacts_dir, version=v)
            except Exception as exc:
                # Same reasoning as list_capabilities() above: one malformed/legacy
                # artifact file must not 500 this endpoint for every other version of
                # the same capability.
                logging.getLogger(__name__).warning(
                    "skipping unloadable artifact %s v%s: %s", name, v, exc
                )
                continue
            out.append({
                "version": v, "status": artifact.status,
                "created_from_run_id": artifact.created_from_run_id, "is_default": artifact.is_default,
                # When this version was recorded. Postgres stamps every artifact row;
                # the on-disk fallback has no equivalent, so it reports None rather
                # than inventing a time from the file's mtime (which changes whenever
                # the version is approved or retired, not when it was discovered).
                "created_at": created_at_by_version.get(v),
            })
        return out

    @app.get("/capabilities/{name}/versions/{version}")
    def get_capability_version(name: str, version: int):
        """The full artifact for one version, whatever its status.

        `GET /capabilities/{name}` deliberately resolves to the latest APPROVED
        version, which is right for callers but leaves a draft unreadable: the
        dashboard could show that a draft existed but not what it had captured -
        its goal, its inputs, or the defaults recorded for them - which is
        exactly what a reviewer needs to see before approving it.
        """
        _validate_capability_name(name)
        try:
            artifact = load_artifact(name, settings.artifacts_dir, version=version)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/invoke", status_code=202)
    def invoke_capability(name: str, body: InvokeRequest):
        _validate_capability_name(name)
        try:
            artifact = load_artifact(name, settings.artifacts_dir, version=body.version)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        # load_artifact(version=None) already guarantees "approved". A pinned version
        # gets no such guarantee (it loads that exact file regardless of status), so
        # reject it here - fast, before a background run is even started - rather than
        # only inside run_replay once the run is already underway.
        if body.version is not None and artifact.status != "approved":
            raise HTTPException(
                status_code=409,
                detail=f"version {body.version} of '{name}' is '{artifact.status}', not "
                "'approved' - only an approved version can be invoked",
            )
        validation_error = validate_required_params(artifact, body.params)
        if validation_error:
            raise HTTPException(status_code=400, detail=validation_error)

        # Minted up front and threaded into both RunManager (which the dashboard polls
        # via /runs/{run_id}) and run_replay's own run_id param, so the evidence/Postgres
        # records run_replay writes land under the SAME id the frontend is polling -
        # previously each generated its own independent timestamp id and the two never
        # matched, so the events panel always showed nothing.
        run_id = _mint_run_id("invoke")

        def target(transport):
            return run_replay(name, body.params, False, False, transport, version=body.version, run_id=run_id)

        app.state.run_manager.start("invoke", name, target, run_id=run_id)
        return {"run_id": run_id, "status": "running"}

    @app.post("/capabilities/{name}/discover", status_code=202)
    def discover_capability(name: str, body: DiscoverRequest):
        _validate_capability_name(name)

        run_id = _mint_run_id("discover")

        def target(transport):
            artifact = run_discover(
                body.goal, body.start_url, name, False, transport, False,
                param_hints=body.param_hints, run_id=run_id,
            )
            if artifact is None:
                return {"succeeded": False, "artifact_version": None}
            return {"succeeded": True, "artifact_version": artifact.version}

        app.state.run_manager.start("discover", name, target, run_id=run_id)
        return {"run_id": run_id, "status": "running"}

    @app.get("/runs")
    def list_runs(limit: int = Query(50, ge=1, le=500)):
        # Postgres-backed when configured (persists across server restarts, which is
        # the whole point of comp_use/pg - see EXT_TASK_FIXES.md). Falls back to
        # RunManager's in-memory records (this process's runs only, lost on restart)
        # so the dashboard's run history still works with zero extra setup when
        # COMP_USE_DB_URL isn't configured.
        if pg_store.db_enabled():
            rows = pg_store.list_runs(limit=limit)
            return [
                {
                    "run_id": r["id"],
                    "kind": r["kind"],
                    "capability_name": r["capability_name"],
                    "status": r["status"],
                    "novnc_url": r["novnc_url"],
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                }
                for r in rows
            ]
        records = sorted(app.state.run_manager.list(), key=lambda r: r.run_id, reverse=True)[:limit]
        return [
            {
                "run_id": r.run_id,
                "kind": r.kind,
                "capability_name": r.capability_name,
                "status": r.status,
                "novnc_url": r.novnc_url,
                "started_at": r.started_at,
                "finished_at": r.finished_at,
            }
            for r in records
        ]

    @app.get("/runs/{run_id}/events")
    def get_run_events(run_id: str):
        _validate_run_id(run_id)
        # Same Postgres-first, file-fallback shape as list_runs() above - falls back
        # to the JSONL evidence log (EvidenceLogger's own source of truth) rather than
        # RunManager, since RunManager never held per-step events at all, only the
        # final result.
        if pg_store.db_enabled():
            rows = pg_store.list_events(run_id)
            return [
                {"event_type": r["event_type"], "data": r["data"], "created_at": r["created_at"].isoformat()}
                for r in rows
            ]
        log_path = settings.evidence_dir / run_id / "log.jsonl"
        if not log_path.exists():
            return []
        events = []
        for line in log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                event = {"event_type": record["event_type"], "data": record["data"], "created_at": None}
            except (json.JSONDecodeError, KeyError) as exc:
                # A truncated final line (a run killed mid-write leaves one) must not
                # 500 the whole events panel for every OTHER event this run logged.
                logging.getLogger(__name__).warning(
                    "skipping malformed event line in %s: %s", log_path, exc
                )
                continue
            events.append(event)
        return events

    @app.get("/runs/{run_id}/screenshots/{label}")
    def get_screenshot(run_id: str, label: str):
        _validate_run_id(run_id)
        # Counterpart to the /evidence static mount above, for the Postgres-backed
        # screenshot path (EvidenceLogger.save_screenshot() when COMP_USE_DB_URL is
        # configured - see comp_use/pg/schema.sql's run_screenshots table).
        png_bytes = pg_store.get_screenshot(run_id, label)
        if png_bytes is None:
            raise HTTPException(status_code=404, detail=f"no screenshot '{label}' for run '{run_id}'")
        return Response(content=png_bytes, media_type="image/png")

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        _validate_run_id(run_id)
        record = app.state.run_manager.get(run_id)
        if record is None:
            # Same Postgres-first-... well here, Postgres-ONLY fallback as list_runs/
            # get_run_events: RunManager only knows about runs from THIS process's
            # lifetime, so any run from before the server's last restart 404'd here
            # even though list_runs/list_events already showed it fine (observed
            # live: the dashboard's run detail panel spun on "Loading..." forever
            # for an old run). No live escalation/resume/takeover is possible for a
            # run whose process is gone either way - this only restores the ability
            # to view (and delete) its history.
            if pg_store.db_enabled():
                row = pg_store.get_run(run_id)
                if row is not None:
                    is_discover = row["kind"] == "discover"
                    return {
                        "run_id": row["id"],
                        "kind": row["kind"],
                        "status": row["status"],
                        "escalation": None,
                        "result": None if is_discover else row["result"],
                        "discover_result": row["result"] if is_discover else None,
                        "error": None,
                        "novnc_url": row["novnc_url"],
                        # .get(): a timestamp is presentation, so a row without one
                        # (an older schema, a partial read) must degrade to "no time
                        # shown" rather than 500 the whole run view.
                        "started_at": row.get("started_at").isoformat() if row.get("started_at") else None,
                        "finished_at": row.get("finished_at").isoformat() if row.get("finished_at") else None,
                    }
            raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
        escalation = None
        if record.escalation is not None:
            screenshot_url = None
            shot_path = record.escalation.screenshot_path
            if shot_path and shot_path.startswith("postgres:"):
                # "postgres:{run_id}/{label}" - see EvidenceLogger.save_screenshot()
                shot_run_id, label = shot_path.removeprefix("postgres:").split("/", 1)
                screenshot_url = f"/runs/{shot_run_id}/screenshots/{label}"
            elif shot_path:
                screenshot_url = f"/evidence/{record.escalation.run_id}/{Path(shot_path).name}"
            escalation = {
                "reason": record.escalation.reason,
                "current_step": record.escalation.current_step,
                "screenshot_url": screenshot_url,
            }
        return {
            "run_id": record.run_id,
            "kind": record.kind,
            "status": record.status,
            "escalation": escalation,
            "result": record.result.model_dump(mode="json") if record.result else None,
            "discover_result": record.discover_result,
            "error": record.error,
            "novnc_url": record.novnc_url,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
        }

    @app.post("/runs/{run_id}/takeover", status_code=202)
    def takeover_run(run_id: str):
        _validate_run_id(run_id)
        # Voluntary human takeover - the run isn't stuck or risky, an operator just
        # wants to drive the live browser for a moment. Reuses the exact same
        # escalate()/resume() handshake as an agent-triggered escalation (see
        # DiscoveryAgent._run_loop / ReplayEngine.run's takeover_requested() checks):
        # this call only flips a flag the loop polls once per step, it doesn't pause
        # anything itself - GET /runs/{run_id} will report status="escalated" once
        # the loop actually reaches that check, and the SAME POST /runs/{run_id}/resume
        # hands control back.
        taken = app.state.run_manager.request_takeover(run_id)
        if not taken:
            record = app.state.run_manager.get(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
            raise HTTPException(status_code=409, detail=f"run is '{record.status}', not 'running'")
        return {"status": "takeover_requested"}

    @app.post("/runs/{run_id}/interrupt", status_code=202)
    def interrupt_run(run_id: str):
        _validate_run_id(run_id)
        # Stop a run for good, and with it the sandbox container underneath. Like
        # takeover above, this only sets a flag the step loop polls, so it returns
        # 202 and not 200: the run ends at its NEXT step boundary, which a slow
        # in-flight action (a page load, an LLM call) can delay by a few seconds.
        # A run parked on an escalation never reaches a step boundary, so
        # QueueTransport.interrupt() also unblocks its wait_for_resume().
        # GET /runs/{run_id} reports status="interrupted" once the worker unwinds.
        stopped = app.state.run_manager.interrupt(run_id)
        if not stopped:
            record = app.state.run_manager.get(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
            raise HTTPException(
                status_code=409,
                detail=f"run is '{record.status}' - only an active run can be interrupted",
            )
        return {"status": "interrupt_requested"}

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeRequest):
        _validate_run_id(run_id)
        resumed = app.state.run_manager.resume(run_id, body.note)
        if not resumed:
            record = app.state.run_manager.get(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
            raise HTTPException(status_code=409, detail=f"run is '{record.status}', not 'escalated'")
        return {"status": "running"}

    @app.delete("/runs/{run_id}")
    def delete_run(run_id: str, force: bool = False):
        _validate_run_id(run_id)
        # An active run's background thread/transport is still live and referenced
        # from RunManager's own record - refuse to delete out from under it, same
        # gate takeover uses in reverse. `force=true` is the escape hatch for a run
        # stuck on an abandoned escalation nobody is ever going to resume: it
        # force-cancels the wait (RunManager.cancel / QueueTransport.cancel) rather
        # than deleting immediately, since the worker thread/browser still needs to
        # unwind first - retry the DELETE once status is no longer
        # 'running'/'escalated' to actually remove the record.
        record = app.state.run_manager.get(run_id)
        if record is not None and record.status in ("running", "escalated"):
            if not force:
                raise HTTPException(
                    status_code=409,
                    detail=f"run is '{record.status}' - cannot delete an active run "
                    "(pass ?force=true to cancel an abandoned escalation first)",
                )
            app.state.run_manager.cancel(run_id)
            return {"run_id": run_id, "deleted": False, "cancel_requested": True}

        removed_from_memory = app.state.run_manager.delete(run_id)
        removed_from_pg = bool(pg_store.delete_run(run_id)) if pg_store.db_enabled() else False
        # File-fallback evidence (used when COMP_USE_DB_URL isn't configured - see
        # EvidenceLogger) has no separate "exists" check worth doing first; rmtree
        # itself is the check.
        run_dir = settings.evidence_dir / run_id
        removed_from_disk = run_dir.exists()
        if removed_from_disk:
            shutil.rmtree(run_dir, ignore_errors=True)

        if not (removed_from_memory or removed_from_pg or removed_from_disk):
            raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
        return {"run_id": run_id, "deleted": True}

    @app.post("/capabilities/{name}/versions/{version}/approve")
    def approve(name: str, version: int, body: ApproveRequest | None = None):
        _validate_capability_name(name)
        try:
            if body is not None and body.input_examples:
                _apply_input_examples(name, version, body.input_examples)
            artifact = approve_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    def _apply_input_examples(name: str, version: int, examples: dict[str, str]) -> None:
        """Write reviewer-corrected defaults onto a version's input_schema.

        Saved before the status flip so the two land together: a caller can never
        see an approved capability whose defaults are still the un-reviewed ones.
        An unknown param name is rejected rather than ignored - silently dropping
        a corrected value is how a reviewer ends up believing they fixed
        something they did not.
        """
        artifact = load_artifact(name, settings.artifacts_dir, version=version)
        known = {p.name for p in artifact.input_schema}
        unknown = sorted(set(examples) - known)
        if unknown:
            raise ValueError(
                f"{name} v{version} has no input param(s) named {', '.join(unknown)} - "
                f"it accepts: {', '.join(sorted(known))}"
            )
        for param in artifact.input_schema:
            if param.name in examples:
                param.example = examples[param.name]
        save_artifact(artifact, settings.artifacts_dir)

    @app.post("/capabilities/{name}/versions/{version}/reject")
    def reject(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = reject_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/versions/{version}/retire")
    def retire(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = retire_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/versions/{version}/set-default")
    def set_default(name: str, version: int):
        # Which version an unpinned invoke/replay picks when more than one version
        # of a capability is approved (e.g. a rollback-safe re-record) - see
        # set_default_version()'s docstring and Artifact.is_default.
        _validate_capability_name(name)
        try:
            artifact = set_default_version(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.post("/capabilities/{name}/versions/{version}/clear-default")
    def clear_default(name: str, version: int):
        # Reverts `version` to NOT being the explicit default - unpinned resolution
        # falls back to "highest approved version wins" rather than moving the
        # default to some other specific version (that's set-default's job). See
        # clear_default_version()'s docstring.
        _validate_capability_name(name)
        try:
            artifact = clear_default_version(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        return artifact.model_dump(mode="json")

    @app.delete("/capabilities/{name}")
    def delete(name: str):
        # Deletes every version of the capability. Run history is untouched -
        # runs/run_events/run_screenshots key on run_id, never capability_name+version
        # (see delete_capability's docstring), so past invocations of a deleted
        # capability stay visible in the dashboard's Runs list.
        _validate_capability_name(name)
        deleted = delete_capability(name, settings.artifacts_dir)
        if deleted == 0:
            raise HTTPException(status_code=404, detail=f"no capability '{name}'")
        return {"capability_name": name, "versions_deleted": deleted}

    @app.post("/chat/sessions", status_code=201)
    def create_chat_session():
        session = app.state.chat_sessions.create()
        return {"session_id": session.session_id}

    @app.post("/chat/sessions/{session_id}/message")
    def send_chat_message(session_id: str, body: ChatMessageRequest):
        session = app.state.chat_sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"unknown session_id '{session_id}'")

        run_id = None
        reply_text = None
        with session.lock:
            catalog = build_chat_catalog(settings)
            result = app.state.chat_agent.turn(session, body.message, catalog)
            reply_text = result.reply_text

            pending = result.to_execute
            if pending is not None:
                if pending.kind == "invoke":
                    # Re-validate before starting a run: the proposal happened one or
                    # more conversational turns earlier against a catalog snapshot that
                    # may now be stale (e.g. the capability was deleted from the
                    # dashboard mid-conversation). Mirrors invoke_capability's own
                    # pre-run checks above, so this degrades into a plain-language chat
                    # reply instead of a raw FileNotFoundError surfacing as a run
                    # "error" while the chat message still said "Starting invoke...".
                    try:
                        artifact = load_artifact(pending.capability_name, settings.artifacts_dir, version=None)
                    except FileNotFoundError:
                        reply_text = f"I can't run `{pending.capability_name}` anymore — it no longer exists."
                    else:
                        validation_error = validate_required_params(artifact, pending.params or {})
                        if validation_error:
                            reply_text = f"I can't run `{pending.capability_name}` anymore — {validation_error}."
                        else:
                            run_id = _mint_run_id("invoke")

                            def target(transport, _name=pending.capability_name, _params=pending.params, _run_id=run_id):
                                return run_replay(_name, _params, False, False, transport, run_id=_run_id)

                            app.state.run_manager.start("invoke", pending.capability_name, target, run_id=run_id)
                else:
                    # Nothing meaningful to pre-validate for a discovery proposal -
                    # discovery doesn't require an existing artifact.
                    run_id = _mint_run_id("discover")

                    def target(
                        transport, _name=pending.capability_name, _goal=pending.goal,
                        _hints=pending.param_hints, _run_id=run_id, _start_url=session.target_site,
                    ):
                        artifact = run_discover(
                            _goal, _start_url, _name, False, transport, False,
                            param_hints=_hints, run_id=_run_id,
                        )
                        if artifact is None:
                            return {"succeeded": False, "artifact_version": None}
                        return {"succeeded": True, "artifact_version": artifact.version}

                    app.state.run_manager.start("discover", pending.capability_name, target, run_id=run_id)
                if run_id is not None:
                    session.last_run_id = run_id

        return {"reply": reply_text, "run_id": run_id}

    return app
