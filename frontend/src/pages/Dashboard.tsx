import { useEffect, useMemo, useState } from "react";
import { api, type CapabilitySummary, type RunSummary, type VersionSummary } from "../api";
import { RunPanel } from "../components/RunPanel";
import type { Page } from "../components/Nav";

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
          <RunPanel key={selectedRunId} runId={selectedRunId} onDeleted={() => setSelectedRunId(null)} />
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
