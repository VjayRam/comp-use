"""The chat agent's turn logic - see docs/superpowers/specs/2026-09-04-chat-
agent-design.md for the full design. Deliberately a pure client of the
existing capability/replay/discovery HTTP surface: ChatAgent never imports
DiscoveryAgent/ReplayEngine, and never starts a run itself - a confirmed
PendingAction comes back on ChatTurnResult.to_execute for the caller (the
/chat/sessions/{id}/message endpoint, see comp_use/server/app.py) to run
through the exact same RunManager.start() path invoke_capability/
discover_capability already use."""
from dataclasses import dataclass
from urllib.parse import urlparse

from comp_use.chat.session import ChatSession, PendingAction
from comp_use.llm_client import LLMClient


@dataclass
class ChatTurnResult:
    reply_text: str
    to_execute: PendingAction | None = None


def _known_sites(catalog: list[dict]) -> list[str]:
    seen: list[str] = []
    for c in catalog:
        site = c["target_base_url"]
        if site and site not in seen:
            seen.append(site)
    return seen


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


class ChatAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def turn(self, session: ChatSession, user_message: str, catalog: list[dict]) -> ChatTurnResult:
        session.messages.append({"role": "user", "content": user_message})

        if session.target_site is None:
            return self._handle_target_site_selection(session, user_message, catalog)

        raise NotImplementedError("normal-turn and pending-resolution handling land in later tasks")

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
