// Thin fetch wrapper over comp_use's FastAPI capability server. Deliberately no
// client library (react-query, axios, ...) - a handful of fetch calls + polling is
// simple enough not to need one, per "keep it intentionally simple."

export const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";

export interface CapabilitySummary {
  capability_name: string;
  description: string | null;
  version: number | null;
  input_schema: { name: string; type: string; required: boolean; example: unknown }[];
  output_schema: { name: string; type: string }[];
  has_pending_draft: boolean;
}

export interface InputParam {
  name: string;
  type: string;
  required: boolean;
  example: unknown;
}

/** One version's full artifact, whatever its status - the shape a draft is
 *  reviewed through before it is approved. */
export interface CapabilityVersion {
  capability_name: string;
  version: number;
  status: "draft" | "approved" | "rejected" | "retired";
  description: string | null;
  input_schema: InputParam[];
  output_schema: { name: string; type: string }[];
  created_from_run_id: string | null;
}

export interface RunSummary {
  run_id: string;
  kind: string;
  capability_name: string | null;
  status: string;
  novnc_url: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunDetail {
  run_id: string;
  kind: string;
  status: "running" | "escalated" | "done" | "error";
  escalation: { reason: string; current_step: number; screenshot_url: string | null } | null;
  result: { outcome: string; outputs: Record<string, unknown>; detail: string } | null;
  discover_result: { succeeded: boolean; artifact_version: number | null } | null;
  error: string | null;
  novnc_url: string | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunEvent {
  event_type: string;
  data: Record<string, unknown>;
  created_at: string | null;
}

export interface VersionSummary {
  /** When this version was recorded. Null on the on-disk fallback store, which
   *  has no equivalent stamp (a file's mtime changes on approve/retire, so it
   *  would report the wrong moment). */
  created_at?: string | null;
  version: number;
  status: "draft" | "approved" | "rejected";
  created_from_run_id: string | null;
  is_default: boolean;
}

export interface ChatMessage {
  role: "user" | "assistant";
  content: string;
  run_id: string | null;
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`);
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

async function del<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { method: "DELETE" });
  if (!res.ok) throw new Error(`${path} -> ${res.status}`);
  return res.json();
}

export const api = {
  listCapabilities: () => get<CapabilitySummary[]>("/capabilities"),
  listVersions: (name: string) => get<VersionSummary[]>(`/capabilities/${name}/versions`),
  getVersion: (name: string, version: number) =>
    get<CapabilityVersion>(`/capabilities/${name}/versions/${version}`),
  listRuns: (limit = 50) => get<RunSummary[]>(`/runs?limit=${limit}`),
  getRun: (runId: string) => get<RunDetail>(`/runs/${runId}`),
  getRunEvents: (runId: string) => get<RunEvent[]>(`/runs/${runId}/events`),
  invoke: (name: string, params: Record<string, unknown>, version?: number) =>
    post<{ run_id: string; status: string }>(`/capabilities/${name}/invoke`, { params, version }),
  resume: (runId: string, note: string) =>
    post<{ status: string }>(`/runs/${runId}/resume`, { note }),
  takeover: (runId: string) =>
    post<{ status: string }>(`/runs/${runId}/takeover`, {}),
  interruptRun: (runId: string) =>
    post<{ status: string }>(`/runs/${runId}/interrupt`, {}),
  deleteRun: (runId: string) => del<{ run_id: string; deleted: boolean }>(`/runs/${runId}`),
  // inputExamples carries reviewer-corrected defaults; the server writes them onto
  // the version before flipping its status, so the two can't land separately.
  approveVersion: (name: string, version: number, inputExamples?: Record<string, string>) =>
    post<Record<string, unknown>>(`/capabilities/${name}/versions/${version}/approve`,
      inputExamples ? { input_examples: inputExamples } : {}),
  rejectVersion: (name: string, version: number) =>
    post<Record<string, unknown>>(`/capabilities/${name}/versions/${version}/reject`, {}),
  retireVersion: (name: string, version: number) =>
    post<Record<string, unknown>>(`/capabilities/${name}/versions/${version}/retire`, {}),
  setDefaultVersion: (name: string, version: number) =>
    post<Record<string, unknown>>(`/capabilities/${name}/versions/${version}/set-default`, {}),
  clearDefaultVersion: (name: string, version: number) =>
    post<Record<string, unknown>>(`/capabilities/${name}/versions/${version}/clear-default`, {}),
  deleteCapability: (name: string) =>
    del<{ capability_name: string; versions_deleted: number }>(`/capabilities/${name}`),
  startChatSession: () => post<{ session_id: string }>("/chat/sessions", {}),
  sendChatMessage: (sessionId: string, message: string) =>
    post<{ reply: string; run_id: string | null }>(`/chat/sessions/${sessionId}/message`, { message }),
};
