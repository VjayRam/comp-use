import { useState } from "react";
import { api, type ChatMessage } from "../api";
import { RunPanel, type RunUsage } from "../components/RunPanel";

// Presets for the target-app dropdown. Selecting one auto-sends its base URL
// as the first chat message - reuses the existing deterministic target-site
// flow (ChatAgent._handle_target_site_selection) as-is, no backend change.
// "Add new site" below extends this list for the rest of the browser tab
// (not persisted) rather than requiring a fresh custom URL every time.
const KNOWN_APPS = [
  { label: "Meridian", baseUrl: "https://web-sample.interface-hiring.com" },
  { label: "Mock Bank", baseUrl: "http://localhost:5000" },
];

// One suggested prompt set per known base URL, derived from what each app's
// recorded capabilities (Meridian) / recognized routes (Mock Bank) actually
// support - shown only once a site is picked, so a prompt never references a
// capability the current session couldn't possibly match.
const SUGGESTED_PROMPTS: Record<string, string[]> = {
  "https://web-sample.interface-hiring.com": [
    "Check member 100234's balance",
    "Transfer $50 from member 100234's checking share to savings",
    "Place a hold on member 100234's checking share",
    "Update member 100234's email address",
  ],
  "http://localhost:5000": [
    "Look up member 12345 and show their account balances",
    "Transfer $100 from member 12345's checking account to savings",
    "Open a new savings account for member 12345 with a $500 deposit",
    "Check member 67890's checking account balance",
  ],
};

const ADD_NEW_VALUE = "__add_new__";

export function Chat() {
  const [apps, setApps] = useState(KNOWN_APPS);
  const [selectedBaseUrl, setSelectedBaseUrl] = useState<string | null>(null);
  const [addingNewApp, setAddingNewApp] = useState(false);
  const [newAppUrl, setNewAppUrl] = useState("");

  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [hasSentTaskMessage, setHasSentTaskMessage] = useState(false);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Live tool-call/token usage for the most recent run in this conversation - only
  // that one RunPanel is wired to report it (see the .map below), so this always
  // tracks "the run currently in progress," not a stale earlier one.
  const [liveUsage, setLiveUsage] = useState<RunUsage | null>(null);

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const { session_id } = await api.startChatSession();
    setSessionId(session_id);
    return session_id;
  }

  async function sendRaw(text: string, sessionIdOverride?: string) {
    setBusy(true);
    setError(null);
    setLiveUsage(null);
    setMessages((m) => [...m, { role: "user", content: text, run_id: null }]);
    try {
      const id = sessionIdOverride ?? (await ensureSession());
      const { reply, run_id } = await api.sendChatMessage(id, text);
      setMessages((m) => [...m, { role: "assistant", content: reply, run_id }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setHasSentTaskMessage(true);
    await sendRaw(text);
  }

  function useSuggestion(text: string) {
    setInput(text);
  }

  async function selectApp(baseUrl: string) {
    // Picking a (different) app starts a fresh conversation - session.target_site
    // is a once-per-session concern on the backend, so switching apps mid-chat
    // would otherwise leave the old site's capabilities in scope. A freshly
    // minted session id is passed straight into sendRaw rather than through
    // setSessionId+ensureSession, since the old sessionId is still closed over
    // in this call and setSessionId(null) would not be visible until re-render.
    setAddingNewApp(false);
    setMessages([]);
    setHasSentTaskMessage(false);
    setSelectedBaseUrl(baseUrl);
    setInput("");
    const { session_id } = await api.startChatSession();
    setSessionId(session_id);
    await sendRaw(baseUrl, session_id);
  }

  function handleDropdownChange(value: string) {
    if (value === ADD_NEW_VALUE) {
      setAddingNewApp(true);
      return;
    }
    selectApp(value);
  }

  function confirmNewApp() {
    const url = newAppUrl.trim();
    if (!url) return;
    setApps((prev) => (prev.some((a) => a.baseUrl === url) ? prev : [...prev, { label: url, baseUrl: url }]));
    setNewAppUrl("");
    selectApp(url);
  }

  const suggestions = selectedBaseUrl ? SUGGESTED_PROMPTS[selectedBaseUrl] : undefined;
  // Only the most recent message carrying a run_id is "the run currently in
  // progress" - that's the one instance of RunPanel wired to update the top bar,
  // so switching to an older run_id's own detail (if the user scrolls up) never
  // fights with it.
  const latestRunMessageIndex = (() => {
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].run_id) return i;
    }
    return -1;
  })();

  return (
    <div className="chat-page">
      <div className="chat-app-picker">
        <label htmlFor="chat-app-select">Target app</label>
        <select
          id="chat-app-select"
          value={selectedBaseUrl ?? ""}
          onChange={(e) => handleDropdownChange(e.target.value)}
        >
          <option value="" disabled>
            Select an app…
          </option>
          {apps.map((a) => (
            <option key={a.baseUrl} value={a.baseUrl}>
              {a.label}
            </option>
          ))}
          <option value={ADD_NEW_VALUE}>+ Add new site…</option>
        </select>
        {liveUsage && (
          <span className="chat-usage-badge" title="LLM tool calls and token usage for the current run">
            {liveUsage.toolCalls} call{liveUsage.toolCalls === 1 ? "" : "s"} · {liveUsage.totalTokens} tokens
          </span>
        )}
        {addingNewApp && (
          <span className="chat-app-add-row">
            <input
              value={newAppUrl}
              onChange={(e) => setNewAppUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") confirmNewApp();
              }}
              placeholder="https://example.com"
            />
            <button onClick={confirmNewApp}>Add</button>
            <button onClick={() => setAddingNewApp(false)}>Cancel</button>
          </span>
        )}
      </div>

      <div className="chat-messages">
        {messages.length === 0 && !addingNewApp && (
          <p className="muted">
            {selectedBaseUrl ? "Tell me what you'd like to do." : "Pick a target app above to get started."}
          </p>
        )}
        {messages.map((m, i) => (
          <div key={i} className={`chat-bubble chat-${m.role}`}>
            <p>{m.content}</p>
            {m.run_id && (
              <RunPanel runId={m.run_id} onUsageUpdate={i === latestRunMessageIndex ? setLiveUsage : undefined} />
            )}
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>

      {suggestions && !hasSentTaskMessage && (
        <div className="chat-suggestions">
          {suggestions.map((s) => (
            <button key={s} className="chat-suggestion-chip" onClick={() => useSuggestion(s)}>
              {s}
            </button>
          ))}
        </div>
      )}

      <div className="chat-input-row">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") send();
          }}
          placeholder="What would you like to do?"
        />
        <button disabled={busy} onClick={send}>
          {busy ? "…" : "Send"}
        </button>
      </div>
    </div>
  );
}
