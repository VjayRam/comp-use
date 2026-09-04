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
    # Serializes access to this session's own mutable state (messages,
    # pending_action) once retrieved via ChatSessionManager.get() - the
    # manager's own lock only protects the _sessions dict itself (create/get),
    # not a session's fields across the lifetime of a single request. FastAPI
    # runs the synchronous /chat/sessions/{id}/message handler on a
    # threadpool, so two overlapping requests for the SAME session (a network
    # retry racing the original, two tabs on one session) could otherwise both
    # read the same pending_action, both get "confirm" back from the LLM, and
    # both start a run - the single most safety-critical guardrail in this
    # feature shouldn't depend on requests never actually overlapping.
    # threading.Lock is neither picklable nor comparable, hence compare=False.
    lock: threading.Lock = field(default_factory=threading.Lock, compare=False)


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
