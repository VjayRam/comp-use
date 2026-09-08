import { useEffect, useMemo, useState } from "react";
import { api, type CapabilitySummary, type CapabilityVersion, type RunSummary, type VersionSummary } from "../api";
import { RunPanel, deriveUsage, type RunUsage } from "../components/RunPanel";
import { ResizableSidebar } from "../components/ResizableSidebar";
import { formatAbsolute, formatElapsed, formatRelative } from "@/lib/time";
import type { Page } from "../components/Nav";

function statusColor(status: string): string {
  if (status === "done") return "dot-green";
  if (status === "error") return "dot-red";
  if (status === "escalated") return "dot-amber";
  return "dot-blue";
}

// ---- Version manager: draft/approve/reject/retire per version, delete whole capability ----

function VersionManager({
  capabilityName, onChanged, onDeleted, onReviewDraft,
}: {
  capabilityName: string;
  onChanged: () => void;
  onDeleted: () => void;
  onReviewDraft: (version: number) => void;
}) {
  const [versions, setVersions] = useState<VersionSummary[]>([]);
  const [busyVersion, setBusyVersion] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmingDelete, setConfirmingDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // Keyed by created_from_run_id - each version was produced by exactly one
  // discovery run, so its LLM usage is that run's own llm_usage/llm_usage_summary
  // events, re-derived with the same helper RunPanel uses rather than tracked
  // separately. A version with no created_from_run_id (shouldn't happen in
  // practice) or a run whose events fetch fails just shows no usage badge.
  const [usageByRunId, setUsageByRunId] = useState<Record<string, RunUsage | null>>({});

  async function reload() {
    let list: VersionSummary[];
    try {
      list = await api.listVersions(capabilityName);
    } catch {
      setVersions([]);
      return;
    }
    setVersions(list);
    const entries = await Promise.all(
      list
        .filter((v) => v.created_from_run_id)
        .map(async (v) => {
          const runId = v.created_from_run_id as string;
          try {
            return [runId, deriveUsage(await api.getRunEvents(runId))] as const;
          } catch {
            return [runId, null] as const;
          }
        }),
    );
    setUsageByRunId(Object.fromEntries(entries));
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
            {/* Two fixed lines rather than one wrapping row: the usage badge is long
                enough to push the right-aligned actions onto a line of their own,
                which left the identity, the badge and the buttons each starting at a
                different edge. */}
            <div className="version-head">
              <span className="mono">v{v.version}</span>
              <span className={`status-pill status-${v.status}`}>{v.status}</span>
              {formatRelative(v.created_at) && (
                <span className="muted small" title={`Recorded ${formatAbsolute(v.created_at)}`}>
                  {formatRelative(v.created_at)}
                </span>
              )}
              {v.is_default && <span className="status-pill status-default">default</span>}
            </div>
            {v.created_from_run_id && usageByRunId[v.created_from_run_id] && (
              <span className="usage-badge muted small" title="LLM tool calls and token usage for the discovery run that created this version">
                {usageByRunId[v.created_from_run_id]!.toolCalls} call
                {usageByRunId[v.created_from_run_id]!.toolCalls === 1 ? "" : "s"} ·{" "}
                {usageByRunId[v.created_from_run_id]!.totalTokens} tokens
              </span>
            )}
            <span className="version-actions">
              {v.status === "draft" && (
                <>
                  {/* Opens the draft's own review panel rather than approving on the
                      spot: what it captured, and the defaults it recorded, are the
                      things worth looking at before making it callable. */}
                  <button disabled={busyVersion === v.version} onClick={() => onReviewDraft(v.version)}>
                    Review &amp; approve
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
            Delete workflow
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

// ---- Draft review: a draft rendered exactly like an approved capability ----

/** A draft used to show as an empty shell - the capability listing carries no
 *  schema, description or defaults until a version is approved, so the panel had
 *  nothing to render and a reviewer could not see what discovery had actually
 *  captured. This fetches the version itself and lays it out identically to the
 *  invoke form, with two differences: invoking is refused (with the reason), and
 *  the defaults are editable, because reviewing a draft is exactly when a wrong
 *  recorded default gets noticed. */
function DraftReview({
  capabilityName, version, onApproved, onRejected,
}: {
  capabilityName: string;
  /** null = "whichever version is the pending draft" - a capability with no
   *  approved version reports no version number at all in the listing, and
   *  assuming v1 breaks as soon as an earlier version was rejected. */
  version: number | null;
  onApproved: () => void;
  onRejected: () => void;
}) {
  const [draft, setDraft] = useState<CapabilityVersion | null>(null);
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setDraft(null);
    setError(null);
    setConfirming(false);
    (async () => {
      let target = version;
      if (target === null) {
        const versions = await api.listVersions(capabilityName);
        const drafts = versions.filter((v) => v.status === "draft").map((v) => v.version);
        if (drafts.length === 0) throw new Error(`${capabilityName} has no draft version`);
        target = Math.max(...drafts);
      }
      return api.getVersion(capabilityName, target);
    })()
      .then((v) => {
        if (cancelled) return;
        setDraft(v);
        setValues(
          Object.fromEntries(v.input_schema.map((p) => [p.name, p.example != null ? String(p.example) : ""])),
        );
      })
      .catch((e) => !cancelled && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      cancelled = true;
    };
  }, [capabilityName, version]);

  const original = (name: string) => {
    const p = draft?.input_schema.find((q) => q.name === name);
    return p?.example != null ? String(p.example) : "";
  };
  const edits = Object.keys(values)
    .filter((name) => values[name] !== original(name))
    .map((name) => ({ name, from: original(name), to: values[name] }));

  async function approve(withEdits: boolean) {
    setBusy(true);
    setError(null);
    try {
      await api.approveVersion(
        capabilityName,
        draft!.version,
        withEdits ? Object.fromEntries(edits.map((e) => [e.name, e.to])) : undefined,
      );
      onApproved();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }

  async function reject() {
    setBusy(true);
    setError(null);
    try {
      await api.rejectVersion(capabilityName, draft!.version);
      onRejected();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  if (error && !draft) return <p className="error">{error}</p>;
  if (!draft) return <p className="muted">Loading draft…</p>;

  return (
    <div className="invoke-form">
      <h3>
        {draft.capability_name} <span className="status-pill status-draft">draft v{draft.version}</span>
      </h3>
      {draft.description && (
        <>
          <p className="muted small">The goal this was recorded from:</p>
          <p className="muted draft-goal">{draft.description}</p>
        </>
      )}

      {draft.input_schema.length === 0 && <p className="muted small">This workflow takes no inputs.</p>}
      {draft.input_schema.map((p) => (
        <label key={p.name} className="field">
          <span>
            {p.name}
            {p.required && <span className="req">*</span>}
          </span>
          <input
            value={values[p.name] ?? ""}
            onChange={(e) => setValues((v) => ({ ...v, [p.name]: e.target.value }))}
          />
        </label>
      ))}

      {draft.output_schema.length > 0 && (
        <p className="muted small">
          Returns: {draft.output_schema.map((o) => o.name).join(", ")}
        </p>
      )}

      <button disabled title="A draft can't be invoked — approve it first">
        Invoke
      </button>
      <p className="muted small">Drafts can't be invoked. Approve this version to make it callable.</p>

      {!confirming ? (
        <div className="version-actions">
          <button
            disabled={busy}
            onClick={() => (edits.length > 0 ? setConfirming(true) : approve(false))}
          >
            {busy ? "Approving…" : edits.length > 0 ? `Approve with ${edits.length} change(s)` : "Approve"}
          </button>
          <button disabled={busy} onClick={reject}>
            Reject
          </button>
        </div>
      ) : (
        <div className="confirm-row confirm-defaults">
          <span className="muted small">
            Save these changes to the recorded defaults and approve v{draft.version}?
          </span>
          <ul className="default-diff">
            {edits.map((e) => (
              <li key={e.name}>
                <span className="mono">{e.name}</span>: <s>{e.from || "(empty)"}</s> → <b>{e.to || "(empty)"}</b>
              </li>
            ))}
          </ul>
          <button disabled={busy} onClick={() => approve(true)}>
            {busy ? "Saving…" : "Save defaults & approve"}
          </button>
          <button disabled={busy} onClick={() => setConfirming(false)}>
            Cancel
          </button>
        </div>
      )}
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

  useEffect(() => {
    // capability.input_schema can go from [] (selected while still draft-only,
    // before it's approved) to fully populated while this exact form instance
    // stays mounted - Dashboard keys InvokeForm on capability_name alone, which
    // does not change across that transition, so the lazy useState initializer
    // above never re-runs. Without this, a newly-appeared required param has no
    // key in `values` at all: it LOOKS filled in (its example renders as
    // placeholder ghost text over an empty input) but is silently absent from
    // the submitted params unless the user happens to click into that exact
    // field - surfacing as a "missing required param" 400 on invoke for every
    // field they didn't personally touch. Only fills in params not already
    // present, so it never clobbers something the user already typed.
    setValues((prev) => {
      let changed = false;
      const next = { ...prev };
      for (const p of capability.input_schema) {
        if (!(p.name in next)) {
          next[p.name] = p.example != null ? String(p.example) : "";
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [capability.input_schema]);

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
  // Distinguishes "nothing recorded yet" from "the first fetch hasn't landed" -
  // without it the sidebar claims there are no capabilities every time the page
  // mounts, for as long as the request takes.
  const [capabilitiesLoaded, setCapabilitiesLoaded] = useState(false);
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedCapability, setSelectedCapability] = useState<CapabilitySummary | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  // A draft version the user asked to review, overriding the default view. Cleared
  // whenever the selection changes so it can't leak onto another capability.
  const [reviewingDraft, setReviewingDraft] = useState<number | null>(null);
  const [capabilitiesOpen, setCapabilitiesOpen] = useState(true);
  const [runsOpen, setRunsOpen] = useState(true);

  async function reloadCapabilities() {
    const list = await api.listCapabilities().catch(() => null);
    if (list === null) return;
    setCapabilities(list);
    setCapabilitiesLoaded(true);
    setSelectedCapability((prev) =>
      prev ? list.find((c) => c.capability_name === prev.capability_name) ?? prev : prev,
    );
  }

  useEffect(() => {
    reloadCapabilities();
  }, []);

  // A draft opened for review belongs to the capability that was selected at the
  // time; leaving it set would render another capability's panel against it.
  useEffect(() => {
    setReviewingDraft(null);
  }, [selectedCapability?.capability_name]);

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
    () =>
      [...runs].sort((a, b) => {
        // Discovery runs first as a block: they're the ones being reviewed and
        // approved, so they shouldn't have to be hunted for among the invokes
        // that a single approved workflow accumulates.
        const aDiscover = a.kind === "discover";
        const bDiscover = b.kind === "discover";
        if (aDiscover !== bDiscover) return aDiscover ? -1 : 1;
        // Within each block, newest first. run_id's kind prefix ("discover_" vs
        // "invoke_") sorts before actual recency if compared as plain strings,
        // sinking newer discovery runs below older invoke runs - started_at
        // reflects real chronology.
        const aTime = a.started_at ?? "";
        const bTime = b.started_at ?? "";
        if (aTime !== bTime) return aTime < bTime ? 1 : -1;
        return a.run_id < b.run_id ? 1 : -1;
      }),
    [runs],
  );

  return (
    <div className="dashboard">
      <ResizableSidebar>
        <button className="new-workflow-button" onClick={() => onNavigate("chat")}>
          + New workflow
        </button>
        <section>
          <button className="section-toggle" onClick={() => setCapabilitiesOpen((o) => !o)}>
            <span className={`chevron ${capabilitiesOpen ? "open" : ""}`}>&#9656;</span>
            <h2>Workflows</h2>
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
              {capabilities.length === 0 && (
                <li className="muted">{capabilitiesLoaded ? "No approved workflows yet." : "Loading…"}</li>
              )}
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
                    {/* Two lines: what it was, then when. A run id truncated into
                        "discover_…" identifies nothing, and the full id is in the
                        detail panel header anyway - so the capability leads, and
                        the id becomes the second-line subtitle it deserves to be. */}
                    <span className="run-row">
                      <span className="run-row-title">
                        {r.capability_name ?? r.kind}
                      </span>
                      <span className="run-row-meta muted">
                        <span className="mono">{r.run_id}</span>
                        {formatRelative(r.started_at) && (
                          <span title={formatAbsolute(r.started_at) ?? undefined}>
                            {formatRelative(r.started_at)}
                          </span>
                        )}
                        {formatElapsed(r.started_at, r.finished_at) && (
                          <span title="How long this run took">
                            {formatElapsed(r.started_at, r.finished_at)}
                          </span>
                        )}
                      </span>
                    </span>
                    {/* Marks where the pinned discovery block ends - without it the
                        top of the list just looks out of chronological order. */}
                    {r.kind === "discover" && <span className="badge badge-discover">discover</span>}
                  </button>
                </li>
              ))}
              {sortedRuns.length === 0 && <li className="muted">No runs yet.</li>}
            </ul>
          )}
        </section>
      </ResizableSidebar>

      <main className="main">
        {selectedRunId ? (
          <RunPanel key={selectedRunId} runId={selectedRunId} onDeleted={() => setSelectedRunId(null)} />
        ) : selectedCapability ? (
          <div className="capability-panel">
            {/* An approved version stays the primary view, so a capability someone
                invokes routinely doesn't move just because a draft appeared on it.
                The review panel shows for a draft the user explicitly opened, or
                when there is no approved version to show at all. */}
            {reviewingDraft !== null || selectedCapability.version === null ? (
              <DraftReview
                key={`draft-${selectedCapability.capability_name}-${reviewingDraft ?? "only"}`}
                capabilityName={selectedCapability.capability_name}
                version={reviewingDraft}
                onApproved={() => {
                  setReviewingDraft(null);
                  reloadCapabilities();
                }}
                onRejected={() => {
                  setReviewingDraft(null);
                  reloadCapabilities();
                }}
              />
            ) : (
              <InvokeForm
                key={selectedCapability.capability_name}
                capability={selectedCapability}
                onInvoked={(id) => setSelectedRunId(id)}
              />
            )}
            <VersionManager
              key={`versions-${selectedCapability.capability_name}`}
              capabilityName={selectedCapability.capability_name}
              onChanged={reloadCapabilities}
              onReviewDraft={setReviewingDraft}
              onDeleted={() => {
                setSelectedCapability(null);
                reloadCapabilities();
              }}
            />
          </div>
        ) : (
          <div className="empty-state">
            <p className="empty-state-pill muted">Select a workflow to invoke it, or a run to watch it.</p>
          </div>
        )}
      </main>
    </div>
  );
}
