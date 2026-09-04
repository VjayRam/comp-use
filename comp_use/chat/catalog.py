"""Builds the capability catalog the chat agent reasons over - distinct from
the /capabilities HTTP endpoint's CapabilitySummary shape (comp_use/server/
app.py's list_capabilities()): this adds target_base_url, which the chat
agent needs to scope its search to session.target_site (see
comp_use/chat/agent.py), and drops fields the model has no use for
(output_schema, has_pending_draft)."""
from comp_use.cli import list_capability_names, list_versions, load_artifact
from comp_use.config import Settings


def build_chat_catalog(settings: Settings) -> list[dict]:
    out = []
    for name in list_capability_names(settings.artifacts_dir):
        versions = list_versions(name, settings.artifacts_dir)
        approved_candidates = []  # descending version order
        for v in reversed(versions):
            try:
                candidate = load_artifact(name, settings.artifacts_dir, version=v)
            except Exception:
                # Same reasoning as list_capabilities(): one malformed artifact
                # must never take down the whole catalog for every other
                # capability too.
                continue
            if candidate.status == "approved":
                approved_candidates.append(candidate)
        if not approved_candidates:
            continue  # nothing invocable yet - not part of the chat agent's search space
        chosen = next(
            (c for c in approved_candidates if c.is_default), approved_candidates[0],
        )
        out.append({
            "capability_name": name,
            "description": chosen.description,
            "target_base_url": chosen.target.get("base_url", ""),
            "input_schema": [
                {"name": p.name, "type": p.type, "required": p.required} for p in chosen.input_schema
            ],
        })
    return out
