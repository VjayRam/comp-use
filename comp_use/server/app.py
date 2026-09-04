from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re

from comp_use.cli import approve_artifact, load_artifact, reject_artifact, retire_artifact, run_discover, run_replay
from comp_use.config import Settings, load_settings
from comp_use.replay.engine import validate_required_params
from comp_use.server.run_manager import RunManager

_CAPABILITY_NAME_RE = re.compile(r"^[a-z0-9_]+$")


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


class InvokeRequest(BaseModel):
    params: dict[str, Any] = {}
    version: int | None = None


class DiscoverRequest(BaseModel):
    goal: str
    start_url: str
    param_hints: list[str] | None = None


class ResumeRequest(BaseModel):
    note: str = ""


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    settings.evidence_dir.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="comp-use capability server")
    app.state.settings = settings
    app.state.run_manager = RunManager()
    app.mount("/evidence", StaticFiles(directory=str(settings.evidence_dir)), name="evidence")

    @app.get("/capabilities")
    def list_capabilities():
        artifacts_dir = settings.artifacts_dir
        if not artifacts_dir.exists():
            return []
        out = []
        for capability_dir in sorted(p for p in artifacts_dir.iterdir() if p.is_dir()):
            name = capability_dir.name
            versions = sorted(int(p.stem[1:]) for p in capability_dir.glob("v*.json"))
            latest_approved = None
            has_pending_draft = False
            for v in reversed(versions):
                candidate = load_artifact(name, artifacts_dir, version=v)
                if candidate.status == "approved" and latest_approved is None:
                    latest_approved = candidate
                if candidate.status == "draft":
                    has_pending_draft = True
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
        capability_dir = settings.artifacts_dir / name
        if not capability_dir.exists():
            raise HTTPException(status_code=404, detail=f"no capability '{name}'")
        out = []
        for v in sorted(int(p.stem[1:]) for p in capability_dir.glob("v*.json")):
            artifact = load_artifact(name, settings.artifacts_dir, version=v)
            out.append({"version": v, "status": artifact.status, "created_from_run_id": artifact.created_from_run_id})
        return out

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

        def target(transport):
            return run_replay(name, body.params, False, False, transport, version=body.version)

        run_id = app.state.run_manager.start("invoke", name, target)
        return {"run_id": run_id, "status": "running"}

    @app.post("/capabilities/{name}/discover", status_code=202)
    def discover_capability(name: str, body: DiscoverRequest):
        _validate_capability_name(name)

        def target(transport):
            artifact = run_discover(
                body.goal, body.start_url, name, False, transport, False, param_hints=body.param_hints,
            )
            if artifact is None:
                return {"succeeded": False, "artifact_version": None}
            return {"succeeded": True, "artifact_version": artifact.version}

        run_id = app.state.run_manager.start("discover", name, target)
        return {"run_id": run_id, "status": "running"}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str):
        record = app.state.run_manager.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
        escalation = None
        if record.escalation is not None:
            screenshot_url = None
            if record.escalation.screenshot_path:
                screenshot_url = f"/evidence/{record.escalation.run_id}/{Path(record.escalation.screenshot_path).name}"
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
        }

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume_run(run_id: str, body: ResumeRequest):
        resumed = app.state.run_manager.resume(run_id, body.note)
        if not resumed:
            record = app.state.run_manager.get(run_id)
            if record is None:
                raise HTTPException(status_code=404, detail=f"unknown run_id '{run_id}'")
            raise HTTPException(status_code=409, detail=f"run is '{record.status}', not 'escalated'")
        return {"status": "running"}

    @app.post("/capabilities/{name}/versions/{version}/approve")
    def approve(name: str, version: int):
        _validate_capability_name(name)
        try:
            artifact = approve_artifact(name, version, settings.artifacts_dir)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return artifact.model_dump(mode="json")

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

    return app
