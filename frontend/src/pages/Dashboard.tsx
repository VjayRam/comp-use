import { useEffect, useMemo, useState } from "react";
import { api, type CapabilitySummary, type RunDetail, type RunEvent, type RunSummary, type VersionSummary } from "../api";
import { BrowserFeedFrame } from "../components/BrowserFeedFrame";
import type { Page } from "../components/Nav";

const ACTIVE_STATUSES = new Set(["running", "escalated"]);

function statusColor(status: string): string {
  if (status === "done") return "dot-green";
  if (status === "error") return "dot-red";
  if (status === "escalated") return "dot-amber";
  return "dot-blue";
}

// ---- Version manager: draft/approve/reject/retire per version, delete whole capability ----

function VersionManager({
  capabilityName, onChanged, onDeleted,
}: {
  capabilityName: string;
  onChanged: () => void;
  onDeleted: () => void;
}) {
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [busyVersion, setBusyVersion] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  async function reload() {
    try {
      setVersions(await api.listVersions(capabilityName));
    } catch {
      setVersions([]);
    }
  }

  useEffect(() => {
    reload();
    setConfirmingDelete(false);
  }, [capabilityName]);

  async function act(version: number, fn: (name: string, version: number) => Promise<unknown>) {
    setBusyVersion(version);
    setError(null);
    try {
      await fn(capabilityName, version);
      await reload();
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusyVersion(null);
    }
  }

  async function confirmDelete() {
    setDeleting(true);
    setError(null);
    try {
      await api.deleteCapability(capabilityName);
      onDeleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  }

  return (
    <div className="version-manager">
      <h4>Versions</h4>
      {versions.filter((v) => v.status === "approved").length > 1 && (
        <p className="muted small">
          Multiple approved versions — the one marked "default" is what an unpinned invoke runs.
        </p>
      )}
      {versions.length === 0 && <p className="muted small">No versions found.</p>}
      <ul className="version-list">
        {versions.map((v) => (
          <li key={v.version} className="version-row">
            <span className="mono">v{v.version}</span>
            <span className={`status-pill status-${v.status}`}>{v.status}</span>
            {v.is_default && <span className="status-pill status-default">default</span>}
            <span className="version-actions">
              {v.status === "draft" && (
                <>
                  <button disabled={busyVersion === v.version} onClick={() => act(v.version, api.approveVersion)}>
                    Approve
                  </button>
                  <button disabled={busyVersion === v.version} onClick={() => act(v.version, api.rejectVersion)}>
                    Reject
                  </button>
                </>
              )}
              {v.status === "approved" && !v.is_default && (
                <button disabled={busyVersion === v.version} onClick={() => act(v.version, api.setDefaultVersion)}>
                  Set default
                </button>
              )}
              {v.status === "approved" && v.is_default && (
                <button disabled={busyVersion === v.version} onClick={() => act(v.version, api.clearDefaultVersion)}>
                  Clear default
                </button>
              )}
              {v.status === "approved" && (
                <button disabled={busyVersion === v.version} onClick={() => act(v.version, api.retireVersion)}>
                  Retire
                </button>
              )}
            </span>
          </li>
        ))}
      </ul>

      <div className="danger-zone">
        {!confirmingDelete ? (
          <button className="danger-button" onClick={() => setConfirmingDelete(true)}>
            Delete capability
          </button>
        ) : (
          <div className="confirm-row">
            <span className="muted small">Delete all {versions.length} version(s) of "{capabilityName}"? Run history is kept.</span>
            <button className="danger-button" disabled={deleting} onClick={confirmDelete}>
              {deleting ? "Deleting…" : "Confirm delete"}
            </button>
            <button disabled={deleting} onClick={() => setConfirmingDelete(false)}>
              Cancel
            </button>
          </div>
        )}
      </div>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// ---- Invoke form: one text input per declared input param ----

function InvokeForm({ capability, onInvoked }: { capability: CapabilitySummary; onInvoked: (runId: string) => void }) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(capability.input_schema.map((p) => [p.name, p.example != null ? String(p.example) : ""])),
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const { run_id } = await api.invoke(capability.capability_name, values, capability.version ?? undefined);
      onInvoked(run_id);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="invoke-form">
      <h3>{capability.capability_name}</h3>
      {capability.description && <p className="muted">{capability.description}</p>}
      {capability.input_schema.map((p) => (
        <label key={p.name} className="field">
          <span>
            {p.name}
            {p.required && <span className="req">*</span>}
          </span>
          <input
            value={values[p.name] ?? ""}
            placeholder={p.example != null ? String(p.example) : ""}
            onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
          />
        </label>
      ))}
      <button disabled={busy} onClick={run}>
        {busy ? "Starting…" : "Invoke"}
      </button>
      {error && <p className="error">{error}</p>}
    </div>
  );
}

// ---- Run detail: status + event log + browser feed + resume ----

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

function RunDetailPanel({ runId, onDeleted }: { runId: string; onDeleted: () => void }) {
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
      // The takeover only takes effect once the agent's loop reaches its next step
      // boundary and actually pauses (see comp_use/discovery/agent.py /
      // replay/engine.py's takeover_requested() checks) - the next poll tick picks
      // up status="escalated" and the feed unblocks then, not immediately here.
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
      onDeleted();
    } catch (e) {
      setDeleteError(e instanceof Error ? e.message : String(e));
      setDeleteBusy(false);
    }
  }

  const canTakeControl = detail.status === "running" && !!detail.novnc_url;
  const feedInteractive = detail.status === "escalated";
  // The stream is only meaningful while there's something live to watch/control -
  // once a run finishes, its sandbox container is already gone (see cli.py's
  // _browser_session), so the panel would just show a dead/disconnected frame.
  const showFeed = ACTIVE_STATUSES.has(detail.status) && !!detail.novnc_url;
  // Mirrors the server's own gate (DELETE /runs/{run_id} 409s on an active run) -
  // its background thread/transport are still live while running/escalated.
  const canDelete = !ACTIVE_STATUSES.has(detail.status);

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

// ---- Top-level Dashboard page ----

export function Dashboard({ onNavigate }: { onNavigate: (page: Page) => void }) {
  const [capabilities, setCapabilities] = useState<CapabilitySummary[]>([]);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedCapability, setSelectedCapability] = useState<CapabilitySummary | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(true);
  const [runsOpen, setRunsOpen] = useState(true);

  async function reloadCapabilities() {
    const list = await api.listCapabilities().catch(() => null);
    if (list === null) return;
    setCapabilities(list);
    setSelectedCapability((prev) =>
      prev ? list.find((c) => c.capability_name === prev.capability_name) ?? prev : prev,
    );
  }

  useEffect(() => {
    reloadCapabilities();
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function poll() {
      try {
        const r = await api.listRuns();
        if (!cancelled) setRuns(r);
      } catch {
        /* transient - next tick retries */
      }
      if (!cancelled) window.setTimeout(poll, 3000);
    }
    poll();
    return () => {
      cancelled = true;
    };
  }, []);

  const sortedRuns = useMemo(
    () => [...runs].sort((a, b) => (a.run_id < b.run_id ? 1 : -1)),
    [runs],
  );

  return (
    <div className="dashboard">
      <aside className="sidebar">
        <button className="new-workflow-button" onClick={() => onNavigate("chat")}>
          + New workflow
        </button>
        <section>
          <button className="section-toggle" onClick={() => setCapabilitiesOpen((o) => !o)}>
            <span className={`chevron ${capabilitiesOpen ? "open" : ""}`}>&#9656;</span>
            <h2>Capabilities</h2>
          </button>
          {capabilitiesOpen && (
            <ul className="list">
              {capabilities.map((c) => (
                <li key={c.capability_name}>
                  <button
                    className={!selectedRunId && selectedCapability?.capability_name === c.capability_name ? "list-item active" : "list-item"}
                    onClick={() => {
                      setSelectedCapability(c);
                      setSelectedRunId(null);
                    }}
                  >
                    {c.capability_name}
                    {c.has_pending_draft && <span className="badge">draft</span>}
                  </button>
                </li>
              ))}
              {capabilities.length === 0 && <li className="muted">No approved capabilities yet.</li>}
            </ul>
          )}
        </section>

        <section>
          <button className="section-toggle" onClick={() => setRunsOpen((o) => !o)}>
            <span className={`chevron ${runsOpen ? "open" : ""}`}>&#9656;</span>
            <h2>Runs</h2>
          </button>
          {runsOpen && (
            <ul className="list">
              {sortedRuns.map((r) => (
                <li key={r.run_id}>
                  <button
                    className={selectedRunId === r.run_id ? "list-item active" : "list-item"}
                    onClick={() => setSelectedRunId(r.run_id)}
                  >
                    <span className={`dot ${statusColor(r.status)}`} />
                    <span className="mono small">{r.run_id}</span>
                    <span className="muted small">{r.capability_name}</span>
                  </button>
                </li>
              ))}
              {sortedRuns.length === 0 && <li className="muted">No runs yet.</li>}
            </ul>
          )}
        </section>
      </aside>

      <main className="main">
        {selectedRunId ? (
          <RunDetailPanel key={selectedRunId} runId={selectedRunId} onDeleted={() => setSelectedRunId(null)} />
        ) : selectedCapability ? (
          <div className="capability-panel">
            <InvokeForm
              key={selectedCapability.capability_name}
              capability={selectedCapability}
              onInvoked={(id) => setSelectedRunId(id)}
            />
            <VersionManager
              key={`versions-${selectedCapability.capability_name}`}
              capabilityName={selectedCapability.capability_name}
              onChanged={reloadCapabilities}
              onDeleted={() => {
                setSelectedCapability(null);
                reloadCapabilities();
              }}
            />
          </div>
        ) : (
          <p className="muted">Select a capability to invoke it, or a run to watch it.</p>
        )}
      </main>
    </div>
  );
}
