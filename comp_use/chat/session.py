"""In-memory chat session state - deliberately not persisted to Postgres or
disk (see the spec's "Purpose"/"Architecture": a single ephemeral session per
conversation, matching RunManager's own in-memory-only pattern). A server
restart drops every active chat, the same trade-off RunManager already has."""
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class PendingAction:
    """A proposed invoke or discovery run awaiting the user's confirm/cancel/
    amend (see comp_use/chat/agent.py's resolve_pending handling)."""
    kind: Literal["invoke", "discover"]
    capability_name: str
    params: dict | None = None
    goal: str | None = None
    param_hints: list[str] | None = None


@dataclass
class ChatSession:
    session_id: str
    messages: list[dict] = field(default_factory=list)
    target_site: str | None = None
    pending_action: PendingAction | None = None
    last_run_id: str | None = None


class ChatSessionManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions: dict[str, ChatSession] = {}

    def create(self) -> ChatSession:
        session = ChatSession(session_id=uuid.uuid4().hex)
        with self._lock:
            self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> ChatSession | None:
        with self._lock:
            return self._sessions.get(session_id)
