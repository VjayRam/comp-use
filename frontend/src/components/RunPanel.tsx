import { useEffect, useState } from "react";
import { api, type RunDetail, type RunEvent } from "../api";
import { AgentActivity } from "@/components/agents/agent-activity";
import { eventToActivity } from "@/lib/runActivity";
import { formatAbsolute, formatElapsed, formatRelative } from "@/lib/time";
import { BrowserFeedFrame } from "./BrowserFeedFrame";

const ACTIVE_STATUSES = new Set(["running", "escalated"]);

export interface RunUsage {
  toolCalls: number;
  totalTokens: number;
}

// Derived from the run's own event log (llm_usage per call, llm_usage_summary at
// the end) rather than tracked separately - the events are already the single
// source of truth for what actually happened, and re-deriving on every poll means
// this never drifts from what the log itself shows. Prefers the final summary
// event once a discovery run has finished; falls back to the latest per-call
// running total while it's still in progress. Returns null for a run with no LLM
// usage at all (e.g. a pure replay, which never calls an LLM).
export function deriveUsage(events: RunEvent[]): RunUsage | null {
  for (let i = events.length - 1; i >= 0; i--) {
    const e = events[i];
    if (e.event_type === "llm_usage_summary") {
      const d = e.data as { tool_call_count?: number; total_tokens?: number };
      return { toolCalls: d.tool_call_count ?? 0, totalTokens: d.total_tokens ?? 0 };
    }
  }
  let toolCalls = 0;
  let totalTokens = 0;
  let seen = false;
  for (const e of events) {
    if (e.event_type === "llm_usage") {
      const d = e.data as { call_number?: number; running_total_tokens?: number };
      seen = true;
      toolCalls = d.call_number ?? toolCalls;
      totalTokens = d.running_total_tokens ?? totalTokens;
    }
  }
  return seen ? { toolCalls, totalTokens } : null;
}

/** Whether a result has anything beyond its outcome line worth expanding. */
function hasResultBody(result: { outputs: Record<string, unknown>; detail: string }): boolean {
  return Boolean(result.detail) || Object.keys(result.outputs ?? {}).length > 0;
}

function statusColor(status: string): string {
  if (status === "done") return "dot-green";
  if (status === "error") return "dot-red";
  if (status === "escalated") return "dot-amber";
  return "dot-blue";
}

// Mirrors comp_use/guardrail.py's is_sensitive_param_name - a decision/replay-step
// event's typed value is shown on the log so a reviewer can see what actually
// happened, but a credential-shaped one is masked here too (defense in depth: the
// backend already masks replay_step's value server-side, since that's the
// caller's real data; a discovery decision's value is whatever the LLM itself
// chose to type and isn't masked at the source, so this is the only place it is).

export function RunPanel({
  runId,
  onDeleted,
  onUsageUpdate,
}: {
  runId: string;
  onDeleted?: () => void;
  onUsageUpdate?: (usage: RunUsage | null) => void;
}) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [note, setNote] = useState("");
  const [confirmingTakeover, setConfirmingTakeover] = useState(false);
  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [takeoverError, setTakeoverError] = useState<string | null>(null);
  // A takeover request only takes effect at the running loop's NEXT step boundary
  // (see RunManager.request_takeover's docstring) - if a slow in-flight action is
  // blocking that boundary, there can be a real, sometimes multi-second gap between
  // confirming and the status actually flipping to "escalated". Without this,
  // clicking "Confirm take control" just made the button disappear with nothing
  // else visible until the poll eventually caught up, reading as broken/unresponsive.
  const [takeoverRequested, setTakeoverRequested] = useState(false);
  // Collapsed by default: the outcome is the answer, and the raw outputs behind it
  // can run to hundreds of lines that push the event log off the screen.
  const [resultExpanded, setResultExpanded] = useState(false);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let timer: number | undefined;

    async function poll() {
      try {
        const d = await api.getRun(runId);
        const evs = await api.getRunEvents(runId);
        if (cancelled) return;
        setDetail(d);
        setEvents(evs);
        onUsageUpdate?.(deriveUsage(evs));
        if (d.status !== "running") setTakeoverRequested(false);
        if (ACTIVE_STATUSES.has(d.status)) {
          timer = window.setTimeout(poll, 1500);
        }
      } catch {
        if (!cancelled) timer = window.setTimeout(poll, 3000);
      }
    }
    poll();
    return () => {
      cancelled = true;
      if (timer) window.clearTimeout(timer);
    };
  }, [runId]);

  if (!detail) return <div className="panel">Loading…</div>;

  async function resume() {
    await api.resume(runId, note);
    setNote("");
  }

  async function confirmTakeover() {
    setTakeoverBusy(true);
    setTakeoverError(null);
    try {
      await api.takeover(runId);
      setConfirmingTakeover(false);
      setTakeoverRequested(true);
    } catch (e) {
      setTakeoverError(e instanceof Error ? e.message : String(e));
    } finally {
      setTakeoverBusy(false);
    }
  }

  async function confirmDeleteRun() {
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await api.deleteRun(runId);
      onDeleted?.();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e));
      setDeleteBusy(false);
    }
  }

  const usage = deriveUsage(events);
  const canTakeControl = detail.status === "running" && !!detail.novnc_url;
  const feedInteractive = detail.status === "escalated";
  const showFeed = ACTIVE_STATUSES.has(detail.status) && !!detail.novnc_url;
  // Mirrors the server's own 409 gate on DELETE /runs/{run_id} (an active run's
  // background thread/transport is still live and referenced from RunManager's
  // record, so deleting it out from under itself is refused there too). Also
  // requires a real onDeleted callback: without one there's nothing to unmount
  // this panel (e.g. Chat.tsx embeds RunPanel with no onDeleted, since there's
  // no sensible chat-side action to take once the run behind a message bubble
  // is gone) - showing the button there would let a click succeed against the
  // backend while this component kept polling the now-404'd run forever.
  const canDelete = !ACTIVE_STATUSES.has(detail.status) && onDeleted !== undefined;

  return (
    <div className="run-detail">
      <div className="run-detail-header">
        <span className="mono">{runId}</span>
        <span className={`dot ${statusColor(detail.status)}`} />
        <span>{detail.status}</span>
        {formatRelative(detail.started_at) && (
          <span className="muted small" title={formatAbsolute(detail.started_at) ?? undefined}>
            started {formatRelative(detail.started_at)}
          </span>
        )}
        {formatElapsed(detail.started_at, detail.finished_at) && (
          <span className="muted small" title="How long this run took">
            took {formatElapsed(detail.started_at, detail.finished_at)}
          </span>
        )}
        {usage && (
          <span className="usage-badge muted small" title="LLM tool calls and token usage for this run">
            {usage.toolCalls} call{usage.toolCalls === 1 ? "" : "s"} · {usage.totalTokens} tokens
          </span>
        )}
        {canDelete && !confirmingDelete && (
          <button className="danger-button" onClick={() => setConfirmingDelete(true)}>
            Delete run
          </button>
        )}
        {canDelete && confirmingDelete && (
          <span className="confirm-row confirm-row-inline">
            <span className="muted small">Delete this run's evidence permanently?</span>
            <button className="danger-button" disabled={deleteBusy} onClick={confirmDeleteRun}>
              {deleteBusy ? "Deleting…" : "Confirm delete"}
            </button>
            <button disabled={deleteBusy} onClick={() => setConfirmingDelete(false)}>
              Cancel
            </button>
          </span>
        )}
        {canTakeControl && !confirmingTakeover && !takeoverRequested && (
          <button className="takeover-button" onClick={() => setConfirmingTakeover(true)}>
            Take control
          </button>
        )}
        {takeoverRequested && (
          <span className="muted small">
            Takeover requested — waiting for the agent to reach the next step…
          </span>
        )}
        {canTakeControl && confirmingTakeover && (
          <span className="confirm-row confirm-row-inline">
            <span className="muted small">Pause the agent and hand you the browser?</span>
            <button className="takeover-button" disabled={takeoverBusy} onClick={confirmTakeover}>
              {takeoverBusy ? "Requesting…" : "Confirm take control"}
            </button>
            <button disabled={takeoverBusy} onClick={() => setConfirmingTakeover(false)}>
              Cancel
            </button>
          </span>
        )}
      </div>
      {takeoverError && <p className="error">{takeoverError}</p>}
      {deleteError && <p className="error">{deleteError}</p>}

      {detail.escalation && (
        <div className="escalation-box">
          <strong>{detail.escalation.reason === "manual takeover requested by operator" ? "You're in control:" : "Escalated:"}</strong>{" "}
          {detail.escalation.reason}
          {detail.escalation.screenshot_url && (
            <img src={detail.escalation.screenshot_url} alt="escalation screenshot" className="escalation-shot" />
          )}
          <div className="resume-form">
            <input
              placeholder="What did you do? (optional)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
            />
            <button onClick={resume}>
              {detail.escalation.reason === "manual takeover requested by operator" ? "Hand back to agent" : "Resume"}
            </button>
          </div>
        </div>
      )}

      {detail.result && (
        <div className="result-box">
          <div className="result-head">
            <span>
              <strong>outcome:</strong> {detail.result.outcome}
            </span>
            {/* Only offered when there is something to expand - a run whose result is
                just its outcome would otherwise show a toggle that reveals nothing. */}
            {hasResultBody(detail.result) && (
              <button className="link-button" onClick={() => setResultExpanded((v) => !v)}>
                {resultExpanded ? "Hide full result" : "View full result"}
              </button>
            )}
          </div>
          {resultExpanded && (
            <>
              {detail.result.detail && <div className="muted">{detail.result.detail}</div>}
              {Object.keys(detail.result.outputs ?? {}).length > 0 && (
                <pre>{JSON.stringify(detail.result.outputs, null, 2)}</pre>
              )}
            </>
          )}
        </div>
      )}
      {detail.discover_result && (
        <div className="result-box">
          <strong>discovery:</strong> {detail.discover_result.succeeded ? "succeeded" : "failed"}
          {detail.discover_result.artifact_version != null && ` (v${detail.discover_result.artifact_version})`}
        </div>
      )}
      {detail.error && <div className="error-box">{detail.error}</div>}

      <div className={showFeed ? "split" : "split split-full"}>
        <div className="log-panel">
          {events.length === 0 ? (
            <p className="muted">waiting for events…</p>
          ) : (
            <AgentActivity
              items={events.map(eventToActivity)}
              status={detail.status === "running" ? "working" : "complete"}
              // A finished run's log is the thing a reviewer came to read, so it
              // stays open; only a live run collapses itself on completion.
              collapseOnComplete={false}
              defaultOpen
              activeLabel={detail.kind === "discover" ? "Recording…" : "Replaying…"}
              summary={`${events.length} event${events.length === 1 ? "" : "s"}`}
              maxHeight={2000}
              contentClassName="pr-2"
            />
          )}
        </div>
        {showFeed && (
          <div className="feed-panel">
            <BrowserFeedFrame url={detail.novnc_url!} interactive={feedInteractive} />
          </div>
        )}
      </div>
    </div>
  );
}
