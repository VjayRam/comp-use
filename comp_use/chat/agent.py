"""The chat agent's turn logic - see docs/superpowers/specs/2026-09-04-chat-
agent-design.md for the full design. Deliberately a pure client of the
existing capability/replay/discovery HTTP surface: ChatAgent never imports
DiscoveryAgent/ReplayEngine, and never starts a run itself - a confirmed
PendingAction comes back on ChatTurnResult.to_execute for the caller (the
/chat/sessions/{id}/message endpoint, see comp_use/server/app.py) to run
through the exact same RunManager.start() path invoke_capability/
discover_capability already use."""
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from comp_use.chat.session import ChatSession, PendingAction
from comp_use.guardrail import is_sensitive_param_name
from comp_use.llm_client import LLMClient


@dataclass
class ChatTurnResult:
    reply_text: str
    to_execute: PendingAction | None = None


_PROPOSE_INVOKE_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_invoke",
        "description": "Propose running an existing recorded capability to satisfy the user's request.",
        "parameters": {
            "type": "object",
            "properties": {
                "capability_name": {"type": "string"},
                "params": {"type": "object", "description": "Typed arguments for the capability, keyed by parameter name."},
            },
            "required": ["capability_name", "params"],
        },
    },
}

_PROPOSE_DISCOVERY_TOOL = {
    "type": "function",
    "function": {
        "name": "propose_discovery",
        "description": "Propose recording a NEW capability because nothing in the catalog covers the user's request.",
        "parameters": {
            "type": "object",
            "properties": {
                "capability_name": {"type": "string", "description": "A new snake_case name for this capability."},
                "goal": {"type": "string", "description": "The natural-language goal to give the discovery agent."},
                "param_hints": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["capability_name", "goal"],
        },
    },
}

_RESOLVE_PENDING_TOOL = {
    "type": "function",
    "function": {
        "name": "resolve_pending",
        "description": "Interpret the user's reply to a pending confirmation.",
        "parameters": {
            "type": "object",
            "properties": {"decision": {"type": "string", "enum": ["confirm", "cancel", "amend"]}},
            "required": ["decision"],
        },
    },
}


def _system_prompt(session: ChatSession, site_catalog: list[dict]) -> str:
    return (
        f"You are a chat agent driving MERIDIAN-style capability APIs against {session.target_site}.\n"
        "Available capabilities for this site (JSON):\n"
        f"{json.dumps(site_catalog)}\n\n"
        "If one of these capabilities satisfies the user's request, call propose_invoke with its "
        "name and every param value you can determine from the conversation. If none of them do, "
        "call propose_discovery. If you need more information before you can call either, just "
        "reply in plain text asking for it."
    )


def _pending_system_note(pending: PendingAction) -> str:
    return (
        f"There is a pending action awaiting the user's confirmation: {pending.kind} "
        f"`{pending.capability_name}`. Call resolve_pending with decision=confirm if the user "
        "agreed, cancel if they declined, or amend if they want to change something about the request."
    )


def _build_messages(session: ChatSession, site_catalog: list[dict], extra_system: str | None = None) -> list[dict]:
    system = _system_prompt(session, site_catalog)
    if extra_system:
        system += "\n\n" + extra_system
    return [{"role": "system", "content": system}] + session.messages


def _known_sites(catalog: list[dict]) -> list[str]:
    seen: list[str] = []
    for c in catalog:
        site = c["target_base_url"]
        if site and site not in seen:
            seen.append(site)
    return seen


def _slugify(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    return text or "capability"


def _target_site_prompt(sites: list[str]) -> str:
    if not sites:
        return "Which site should I work against? Reply with a URL."
    numbered = "\n".join(f"{i + 1}. {s}" for i, s in enumerate(sites))
    return (
        "Which site should I work against?\n"
        f"{numbered}\n"
        f"{len(sites) + 1}. A new URL - reply with it directly"
    )


def _match_site_choice(user_message: str, sites: list[str]) -> str | None:
    text = user_message.strip()
    if text.isdigit():
        idx = int(text) - 1
        return sites[idx] if 0 <= idx < len(sites) else None
    if text.startswith("http://") or text.startswith("https://"):
        parsed = urlparse(text)
        return f"{parsed.scheme}://{parsed.netloc}"
    return next((s for s in sites if s == text), None)


_AFFIRMATIVE_REPLIES = {"yes", "y", "yeah", "yep", "yup", "confirm", "confirmed", "proceed", "ok", "okay", "sure", "go ahead", "do it"}
_NEGATIVE_REPLIES = {"no", "n", "nope", "cancel", "stop", "abort", "don't", "do not"}


def _parse_explicit_decision(user_message: str) -> str | None:
    """Deterministic yes/no parse for a pending confirmation, checked BEFORE ever
    asking the model. This is the single most safety-critical gate in the chat
    surface - the only thing standing between a user's reply and a real, possibly
    irreversible capability run - and relying solely on an LLM tool call to
    interpret it has two failure modes: the model can misread an ambiguous reply,
    and a free-tier model (observed live: minimax/minimax-m3:free via OpenRouter)
    may not honor the forced tool_choice at all, in which case _handle_pending_
    resolution's fallback ("amend") silently clears the pending action and loops
    the user back to square one no matter how many times they say "yes".
    Only an exact-match short reply resolves here; anything else (including a
    reply that merely contains "yes" inside a longer sentence, e.g. "yes but
    change the amount") falls through to the LLM, which is exactly where the
    nuance of confirm/cancel/amend belongs."""
    normalized = user_message.strip().lower().rstrip(".!")
    if normalized in _AFFIRMATIVE_REPLIES:
        return "confirm"
    if normalized in _NEGATIVE_REPLIES:
        return "cancel"
    return None


class ChatAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def turn(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        session.messages.append({"role": "user", "content": user_message})

        if session.target_site is None:
            return self._handle_target_site_selection(session, user_message, catalog)

        if session.pending_action is not None:
            return self._handle_pending_resolution(session, user_message, catalog)

        return self._handle_normal_turn(session, catalog)

    def _handle_target_site_selection(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        sites = _known_sites(catalog)
        chosen = _match_site_choice(user_message, sites)
        if chosen is None:
            reply = _target_site_prompt(sites)
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)
        session.target_site = chosen
        reply = f"Working against {chosen}. What would you like to do?"
        session.messages.append({"role": "assistant", "content": reply})
        return ChatTurnResult(reply_text=reply)

    def _handle_pending_resolution(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        pending = session.pending_action
        decision = _parse_explicit_decision(user_message)
        if decision is None:
            # Only reached for a reply that isn't an exact "yes"/"no" - the model
            # genuinely needs to interpret something ambiguous here (e.g. "actually,
            # use my other account"), which is what resolve_pending's tool call is
            # for. A malformed/unhonored tool call still fails closed to "amend",
            # never "confirm".
            site_catalog = [c for c in catalog if c["target_base_url"] == session.target_site]
            result = self.llm.chat(
                messages=_build_messages(session, site_catalog, extra_system=_pending_system_note(pending)),
                tools=[_RESOLVE_PENDING_TOOL],
                tool_choice={"type": "function", "function": {"name": "resolve_pending"}},
            )
            decision = result["arguments"]["decision"] if result["type"] == "tool_call" else "amend"

        if decision == "confirm":
            session.pending_action = None
            reply = f"Starting {pending.kind} for `{pending.capability_name}`..."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply, to_execute=pending)

        if decision == "cancel":
            session.pending_action = None
            reply = "Okay, cancelled."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        session.pending_action = None
        return self._handle_normal_turn(session, catalog)

    def _handle_normal_turn(self, session: ChatSession, catalog: list[dict]) -> ChatTurnResult:
        site_catalog = [c for c in catalog if c["target_base_url"] == session.target_site]
        result = self.llm.chat(
            messages=_build_messages(session, site_catalog),
            tools=[_PROPOSE_INVOKE_TOOL, _PROPOSE_DISCOVERY_TOOL],
            tool_choice="auto",
        )

        if result["type"] == "text":
            reply = result["text"]
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        args = result["arguments"]
        if result["name"] == "propose_invoke":
            return self._propose_invoke(session, site_catalog, args["capability_name"], args.get("params") or {})
        if result["name"] == "propose_discovery":
            return self._propose_discovery(session, args["capability_name"], args["goal"], args.get("param_hints"))

        reply = "Sorry, I couldn't figure out how to help with that - could you rephrase?"
        session.messages.append({"role": "assistant", "content": reply})
        return ChatTurnResult(reply_text=reply)

    def _propose_invoke(self, session: ChatSession, site_catalog: list[dict], capability_name: str, params: dict) -> ChatTurnResult:
        capability = next((c for c in site_catalog if c["capability_name"] == capability_name), None)
        if capability is None:
            reply = f"I don't have a capability called `{capability_name}` for {session.target_site}."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        missing = [p["name"] for p in capability["input_schema"] if p["required"] and p["name"] not in params]
        if missing:
            reply = f"To run `{capability_name}` I still need: {', '.join(missing)}."
            session.messages.append({"role": "assistant", "content": reply})
            return ChatTurnResult(reply_text=reply)

        shown_params = {k: ("••••••" if is_sensitive_param_name(k) else v) for k, v in params.items()}
        reply = f"I'll run `{capability_name}` with {shown_params} — proceed? (yes/no)"
        session.messages.append({"role": "assistant", "content": reply})
        session.pending_action = PendingAction(kind="invoke", capability_name=capability_name, params=params)
        return ChatTurnResult(reply_text=reply)

    def _propose_discovery(self, session: ChatSession, capability_name: str, goal: str, param_hints: list[str] | None) -> ChatTurnResult:
        slug = _slugify(capability_name)
        reply = (
            f"No existing capability for {session.target_site} covers that. "
            f"I'll record a new one — `{slug}` — against {session.target_site} with goal: \"{goal}\". "
            "Proceed? (yes/no)"
        )
        session.messages.append({"role": "assistant", "content": reply})
        session.pending_action = PendingAction(kind="discover", capability_name=slug, goal=goal, param_hints=param_hints)
        return ChatTurnResult(reply_text=reply)
