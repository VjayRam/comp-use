from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

import re

from comp_use.cli import load_artifact
from comp_use.config import Settings, load_settings
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

    return app
