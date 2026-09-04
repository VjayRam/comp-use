# Chat agent design

Status: approved (brainstorming), pending implementation plan.

## Purpose

The dashboard's `InvokeForm` already covers "I know which capability I want,
let me fill in its fields." The chat interface's job (per the Adaptation
brief, §3.3) is different and narrower: **natural-language intent →
capability selection**. A user describes what they want without knowing the
capability catalog by name; the chat agent finds (or, with explicit
confirmation, records) the right capability, collects any missing inputs,
runs it through the existing capability API, and reports the result in plain
language. It is a second front door onto `/capabilities/*` and `/runs/*` —
never a new code path into the discovery/replay engines.

## Architecture

New backend package `comp_use/chat/` (mirrors the existing `discovery/`,
`replay/`, `escalation/` package style):

- `agent.py` — `ChatAgent`: given a session's message history plus a new user
  message, runs one turn and returns either a tool call or a plain-text
  reply. Builds a system prompt from the *site-scoped* capability catalog
  (see below), fetched fresh every turn, never cached.
- `session.py` — `ChatSessionManager` / `ChatSession`: in-memory only (same
  pattern as `RunManager`), holding message history, `target_site` (once
  chosen), and at most one `pending_action` awaiting confirmation. No new
  Postgres table — chat history does not survive a page refresh or a server
  restart, matching the "single ephemeral session" decision.

New endpoints in `server/app.py`:

- `POST /chat/sessions` — start a session, returns `session_id`.
- `POST /chat/sessions/{session_id}/message` — send a user message, returns
  the agent's reply (text and/or a `run_id` if a run was started).

Frontend `Chat.tsx` becomes a real page: a message list, a text input, and —
for any assistant turn that started a run — an embedded live-run widget
inline in that message.

Everything the agent *does* goes through the existing HTTP capability API
(`/capabilities/{name}/invoke`, `/capabilities/{name}/discover`,
`/capabilities/{name}/versions/{v}/approve`) and the existing `/runs/*`
endpoints for polling. The chat agent never touches
`DiscoveryAgent`/`ReplayEngine` directly.

## Target site: asked once per session, scopes everything

Before the agent does anything task-related, it asks which target site to
work against — a pick-list of distinct `target.base_url` values already
seen across the capability catalog, plus an explicit "enter a new URL"
option. The answer is stored as `session.target_site` and reused for the
rest of the conversation without re-asking. If the user later mentions
wanting a different site, that's handled as an ordinary turn (the agent
recognizes the new URL/site and updates `session.target_site`) — no special
tool needed.

`session.target_site` scopes the capability search: the catalog is filtered
to capabilities whose `target.base_url == session.target_site` *before* the
agent tries to match the user's request against it. Smaller, more relevant
search space, and it can't cross-wire a capability recorded against one site
onto a request meant for another. If the user picks a **new** URL (not seen
in any existing capability), the agent's confirmation message for any
resulting discovery states that plainly, since it's a bigger commitment than
reusing a known, already-allowlisted site.

Because the search is already scoped before the model sees candidates,
`propose_invoke`'s tool schema does not need a `target_site` argument at
all — the matched capability already carries its own `target.base_url`.

## Turn-by-turn mechanics

Each turn is one LLM call with `tool_choice="auto"` (the model may call a
tool or just reply in text). Available tools:

- **`propose_invoke(capability_name, params)`** — "I want to run this."
  Before ever asking the user to confirm, the agent validates `params`
  against that capability's `input_schema` itself (reusing the existing
  `validate_required_params` logic — never trusting the LLM to remember
  what's required). Missing fields skip confirmation entirely; the agent
  just asks for them by name. Once complete, the agent — not the LLM —
  composes the confirmation message deterministically ("I'll run
  `meridian_check_balance` with member_number=100234 — proceed?") and
  stores it as `session.pending_action`. Password-like param values are
  masked (`password: ••••••`) in this message.
- **`propose_discovery(capability_name, goal, param_hints?)`** — "no
  existing capability (in the site-scoped catalog) covers this." Reuses
  `session.target_site` directly (see above — never asked twice). The
  capability name is auto-slugified from the goal text (e.g. "close a
  share" → `meridian_close_share`) and stated, not asked, since it's
  informational rather than blocking. Same deterministic confirmation
  message shape as `propose_invoke`.
- **No tool call** — the model just replies in plain text: answering a
  question, asking a clarifying question, or narrating what a run's
  escalation needs.

When `session.pending_action` is set, the *next* turn is a separate,
**forced** single-function call — `resolve_pending(decision: confirm |
cancel | amend)` — reusing the exact `tool_choice`-forced pattern
`_post_chat` already implements for `decide_next_action`. This keeps "did
the user actually agree" out of general free-form tool choice, so a stray
reply can't be misread as agreement to the wrong thing:

- `confirm` executes the stored action against the capability API and
  attaches a live-run widget (see below) to that turn.
- `cancel` drops `pending_action`, no API call.
- `amend` clears `pending_action` and falls through to a normal turn so the
  user can restate what they want.

After a successful discovery run, the new artifact lands as a **draft**
(existing default). The agent reports what got recorded (capability name,
version) and — using the same `propose_invoke`-style confirm loop — offers
to approve it and run it now, chaining into the invoke flow above rather
than being a special case. A failed discovery (never reached `finish`, or
incomplete) is reported plainly, with the run_id visible in the embedded
widget for evidence; no auto-retry.

## Live-run surfacing (discovery and replay alike)

Any run the chat agent starts — `discover` or `invoke` — gets one embedded
live-run widget in the chat thread, keyed by `run_id`:

- Polls `/runs/{run_id}` + `/runs/{run_id}/events` exactly like
  `RunDetailPanel` already does.
- Renders the noVNC feed with the same interactive/blocked gating (only
  unblocked while `status === "escalated"`), reusing `BrowserFeedFrame`.
- When status flips to `escalated` — whether from a risky replay step
  (Post Transfer, Place Hold without supervisor) or a discovery-time
  stuck/loop/dead-end condition — the widget shows the reason plus a "Take
  control" / resume affordance inline, and the agent drops a short chat
  message ("This run needs your input — see below") so it isn't missed in
  a scrolling thread.

**Targeted refactor**: `RunDetailPanel`'s polling/status/escalation/feed
logic is extracted into a shared `RunPanel` component, used both by the
Dashboard (as now) and embedded in Chat, so the polling logic isn't
duplicated.

## Guardrail preservation

- Chat's confirm-before-run gate is a separate layer from the replay
  engine's own risky-step gate. Confirming "should I run this?" in chat
  never sets `confirm_risky=True` — a risky step inside an already-confirmed
  run still escalates through the existing `EscalationController` exactly
  as before. Chat cannot be used to bypass it.
- API failures (network error, a 400/404 despite the pre-check, a
  capability deleted mid-conversation) are reported in plain language and
  never silently retried; `pending_action` is cleared so the conversation
  doesn't get stuck.
- A finished run's `ReplayResult` / discovery result is translated into a
  plain-language summary, but the embedded widget still shows the raw
  structured result underneath (§3.3's "surfacing the structured result").
- If the model's tool call names a capability outside the site-scoped
  catalog it was given, or sends a malformed args shape, the agent rejects
  it locally rather than forwarding to the API.
- The same `guardrail.redact()` already used for evidence is applied before
  anything from the conversation is logged server-side.
- Session state is in-memory/ephemeral by design — a server restart drops
  active chats, the same trade-off `RunManager` already has. No special
  recovery.

## Testing

- Backend: real unit tests for `ChatAgent`'s pure logic — the param
  validation gate, `confirm`/`cancel`/`amend` resolution, and site-scoped
  catalog filtering — using a `FakeLLMClient` the same way
  `DiscoveryAgent`'s tests already do (no real LLM calls). Endpoint tests
  via `TestClient`, matching the existing server test style.
- Frontend: no test suite exists in this repo today; not introducing one
  unprompted. Verified manually in-browser, consistent with how the rest of
  the dashboard was verified.

## Explicitly out of scope

- Persisted conversation history (new Postgres table, thread list UI) —
  deferred; single ephemeral session per the approved design.
- Multi-tool-call turns (the model choosing several tools in one response) —
  one decision per turn, matching the take-home's existing
  decide-then-act loop shape.
