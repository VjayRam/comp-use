import { useEffect, useState } from "react";
import { api, type RunDetail, type RunEvent } from "../api";
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
const SENSITIVE_PARAM_HINTS = ["password", "passwd", "pwd", "secret", "token", "apikey", "api_key"];

function isSensitiveParamName(name: string | null | undefined): boolean {
  if (!name) return false;
  const lowered = name.toLowerCase();
  return SENSITIVE_PARAM_HINTS.some((h) => lowered.includes(h));
}

interface EventLocator {
  strategy?: string;
  value?: Record<string, unknown>;
}

function describeLocator(locator: EventLocator | null | undefined): string | null {
  if (!locator || !locator.value) return null;
  const { strategy, value } = locator;
  if (strategy === "role") {
    const role = value.role as string | undefined;
    const name = value.name as string | undefined;
    return name ? `${role} "${name}"` : (role ?? null);
  }
  if (strategy === "text") return value.text ? `text "${value.text}"` : null;
  if (strategy === "css") return (value.css as string | undefined) ?? null;
  return JSON.stringify(value);
}

function eventToLine(e: RunEvent): string {
  switch (e.event_type) {
    case "decision": {
      const d = e.data as {
        action?: string;
        text?: string | null;
        target?: string | null;
        locator?: EventLocator | null;
        value_source?: { param_name?: string } | null;
      };
      const parts = [`decision: ${d.action ?? "?"}`];
      const loc = describeLocator(d.locator);
      if (loc) parts.push(`on ${loc}`);
      if (d.target) parts.push(`-> ${d.target}`);
      if (d.text != null && d.text !== "") {
        parts.push(`= "${isSensitiveParamName(d.value_source?.param_name) ? "••••••" : d.text}"`);
      }
      return parts.join(" ");
    }
    case "skipped_decision":
      return `skipped: ${(e.data as { reason?: string }).reason ?? ""}`;
    case "loop_detected":
      return `LOOP DETECTED: ${JSON.stringify(e.data)}`;
    case "possible_incomplete_capability": {
      const reason = (e.data as { reason?: string }).reason;
      if (reason === "no_extract_performed") {
        return "WARNING: this run never extracted anything - the goal's result may not be captured. Review before approving.";
      }
      if (reason === "combobox_seen_but_never_selected") {
        return "WARNING: a dropdown was visible but never selected - a field may be left at its default. Review before approving.";
      }
      return `WARNING: possibly incomplete capability (${reason ?? "unknown reason"})`;
    }
    case "sandbox_started":
      return `sandbox started`;
    case "llm_usage": {
      const d = e.data as {
        call_number?: number;
        prompt_tokens?: number | null;
        completion_tokens?: number | null;
        total_tokens?: number | null;
        running_total_tokens?: number;
      };
      return (
        `LLM call #${d.call_number ?? "?"}: ${d.prompt_tokens ?? "?"} in / ` +
        `${d.completion_tokens ?? "?"} out (${d.total_tokens ?? "?"} tokens) ` +
        `— running total ${d.running_total_tokens ?? "?"}`
      );
    }
    case "llm_usage_summary": {
      const d = e.data as {
        tool_call_count?: number;
        prompt_tokens?: number;
        completion_tokens?: number;
        total_tokens?: number;
      };
      return (
        `LLM usage total: ${d.tool_call_count ?? "?"} tool call(s), ` +
        `${d.prompt_tokens ?? 0} in / ${d.completion_tokens ?? 0} out ` +
        `(${d.total_tokens ?? 0} tokens)`
      );
    }
    case "replay_step": {
      const d = e.data as {
        index?: number;
        action?: string;
        locator?: EventLocator | null;
        target?: string | null;
        value?: string | null;
      };
      const parts = [`step ${d.index} -> ${d.action}`];
      const loc = describeLocator(d.locator);
      if (loc) parts.push(`on ${loc}`);
      if (d.target) parts.push(`-> ${d.target}`);
      if (d.value != null && d.value !== "") parts.push(`= "${d.value}"`);
      return parts.join(" ");
    }
    default:
      return `${e.event_type}: ${JSON.stringify(e.data).slice(0, 120)}`;
  }
}

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
          <strong>outcome:</strong> {detail.result.outcome}
          {detail.result.detail && <div className="muted">{detail.result.detail}</div>}
          {Object.keys(detail.result.outputs ?? {}).length > 0 && (
            <pre>{JSON.stringify(detail.result.outputs, null, 2)}</pre>
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
          {events.length === 0 && <p className="muted">waiting for events…</p>}
          {events.map((e, i) => (
            <div key={i} className="log-line">
              {eventToLine(e)}
            </div>
          ))}
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
