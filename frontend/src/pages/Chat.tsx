import { useState } from "react";
import { api, type ChatMessage } from "../api";
import { RunPanel } from "../components/RunPanel";

export function Chat() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ensureSession(): Promise<string> {
    if (sessionId) return sessionId;
    const { session_id } = await api.startChatSession();
    setSessionId(session_id);
    return session_id;
  }

  async function send() {
    const text = input.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    setInput("");
    setMessages((m) => [...m, { role: "user", content: text, run_id: null }]);
    try {
      const id = await ensureSession();
      const { reply, run_id } = await api.sendChatMessage(id, text);
      setMessages((m) => [...m, { role: "assistant", content: reply, run_id }]);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="chat-page">
      <div className="chat-messages">
        {messages.length === 0 && <p className="muted">Tell me what you'd like to do.</p>}
        {messages.map((m, i) => (
          <div key={i} className={`chat-bubble chat-${m.role}`}>
            <p>{m.content}</p>
            {m.run_id && <RunPanel runId={m.run_id} />}
          </div>
        ))}
        {error && <p className="error">{error}</p>}
      </div>
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
