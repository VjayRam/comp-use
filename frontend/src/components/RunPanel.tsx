import { useEffect, useState } from "react";
import { api, type RunDetail, type RunEvent } from "../api";
import { BrowserFeedFrame } from "./BrowserFeedFrame";

const ACTIVE_STATUSES = new Set(["running", "escalated"]);

function statusColor(status: string): string {
  if (status === "done") return "dot-green";
  if (status === "error") return "dot-red";
  if (status === "escalated") return "dot-amber";
  return "dot-blue";
}

function eventToLine(e: RunEvent): string {
  switch (e.event_type) {
    case "decision": {
      const d = e.data as { action?: string };
      return `decision: ${d.action ?? "?"}`;
    }
    case "skipped_decision":
      return `skipped: ${(e.data as { reason?: string }).reason ?? ""}`;
    case "loop_detected":
      return `LOOP DETECTED: ${JSON.stringify(e.data)}`;
    case "sandbox_started":
      return `sandbox started`;
    case "replay_step":
      return `step ${(e.data as { index?: number }).index} -> ${(e.data as { action?: string }).action}`;
    default:
      return `${e.event_type}: ${JSON.stringify(e.data).slice(0, 120)}`;
  }
}

export function RunPanel({ runId, onDeleted }: { runId: string; onDeleted?: () => void }) {
  const [detail, setDetail] = useState<RunDetail | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [note, setNote] = useState("");
  const [confirmingTakeover, setConfirmingTakeover] = useState(false);
  const [takeoverBusy, setTakeoverBusy] = useState(false);
  const [takeoverError, setTakeoverError] = useState<string | null>(null);
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
        {canTakeControl && !confirmingTakeover && (
          <button className="takeover-button" onClick={() => setConfirmingTakeover(true)}>
            Take control
          </button>
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
