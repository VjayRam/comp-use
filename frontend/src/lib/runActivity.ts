import type { AgentActivityItem } from "@/components/agents/agent-activity";
import type { RunEvent } from "@/api";
import { formatAbsolute } from "@/lib/time";

/** Turns a run's evidence events into beui AgentActivity rows.
 *
 *  The log used to be one flat line of text per event, which made the three
 *  things a reader actually looks for - what was driven, what came back, and
 *  where it went wrong - weigh exactly the same as everything else. The mapping
 *  below gives each event a shape instead: browser work reads as a tool row with
 *  its target in monospace, reasoning and bookkeeping read as trace rows, and
 *  anything that needs a decision or went wrong reads as a step row so it stands
 *  out of the stream.
 */

interface EventLocator {
  strategy?: string;
  value?: Record<string, unknown>;
}

const SENSITIVE_PARAM_HINTS = ["password", "passcode", "secret", "token", "pin", "otp"];

function isSensitiveParamName(name: string | undefined): boolean {
  if (!name) return false;
  const lower = name.toLowerCase();
  return SENSITIVE_PARAM_HINTS.some((hint) => lower.includes(hint));
}

/** The shortest thing that still identifies the control, for a one-line row. */
function describeLocator(locator: EventLocator | null | undefined): string | null {
  if (!locator?.value) return null;
  const { strategy, value } = locator;
  if (strategy === "role") {
    const role = value.role as string | undefined;
    const name = value.name as string | undefined;
    return name ? `${role ?? "element"} "${name}"` : (role ?? null);
  }
  if (strategy === "text") return `text "${value.text as string}"`;
  if (strategy === "css") return (value.css as string | undefined) ?? null;
  return JSON.stringify(value);
}

/** Browser actions map onto beui's tool vocabulary; everything else is a trace. */
function toolActionFor(action: string | undefined): string {
  if (action === "extract") return "read";
  if (action === "type_text" || action === "select_option") return "edit";
  if (action === "navigate" || action === "click") return "run";
  return "run";
}

function valueSuffix(text: string | null | undefined, paramName?: string): string {
  if (text == null || text === "") return "";
  return ` = "${isSensitiveParamName(paramName) ? "••••••" : text}"`;
}

export function eventToActivity(event: RunEvent, index: number): AgentActivityItem {
  const id = `${index}-${event.event_type}`;
  const at = formatAbsolute(event.created_at);

  switch (event.event_type) {
    case "decision":
    case "replay_step": {
      const d = event.data as {
        action?: string;
        index?: number;
        text?: string | null;
        value?: string | null;
        target?: string | null;
        locator?: EventLocator | null;
        value_source?: { param_name?: string } | null;
      };
      const where = describeLocator(d.locator) ?? d.target ?? "";
      const shown = d.value ?? d.text;
      return {
        id,
        type: "tool",
        action: toolActionFor(d.action),
        target: `${d.action ?? "?"}${where ? ` ${where}` : ""}${valueSuffix(shown, d.value_source?.param_name)}`,
      };
    }

    case "extracted_value": {
      const d = event.data as { extract_as?: string; preview?: string };
      return {
        id,
        type: "tool",
        action: "read",
        target: `${d.extract_as ?? "value"} = ${d.preview ?? ""}`,
      };
    }

    case "skipped_decision": {
      const d = event.data as { reason?: string; error?: string };
      return {
        id,
        type: "trace",
        kind: "wrench",
        label: `skipped (${d.reason ?? "?"})`,
        detail: d.error ? d.error.split("\n")[0] : undefined,
      };
    }

    // Anything a person has to act on, or that changed the run's course, reads as
    // a step so it doesn't disappear into a wall of tool rows.
    case "escalation_requested":
      return {
        id,
        type: "step",
        status: "active",
        label: `Escalated: ${(event.data as { reason?: string }).reason ?? ""}`,
        meta: at ?? undefined,
      };
    case "escalation_resumed":
      return { id, type: "step", status: "complete", label: "Operator resumed the run", meta: at ?? undefined };
    case "fault_detected": {
      const d = event.data as { outcome?: string; detail?: string };
      return {
        id,
        type: "step",
        status: "active",
        label: `${d.outcome ?? "fault"} — ${d.detail ?? ""}`,
        meta: at ?? undefined,
      };
    }
    case "loop_detected":
      return { id, type: "step", status: "active", label: "Loop detected — the same action stopped making progress", meta: at ?? undefined };
    case "input_error":
      return { id, type: "step", status: "active", label: `Input error: ${(event.data as { detail?: string }).detail ?? ""}`, meta: at ?? undefined };

    case "possible_incomplete_capability": {
      const reason = (event.data as { reason?: string }).reason;
      const label =
        reason === "no_extract_performed"
          ? "Nothing was extracted — this capability may return no result"
          : reason === "combobox_seen_but_never_selected"
            ? "A dropdown was never selected — a field may be left at its default"
            : `Possibly incomplete (${reason ?? "unknown"})`;
      return { id, type: "step", status: "pending", label, meta: at ?? undefined };
    }
    case "finish_rejected_no_extract":
      return { id, type: "step", status: "pending", label: "Finish declined once — nothing had been extracted yet", meta: at ?? undefined };
    case "finish_rejected_missing_params":
      return {
        id,
        type: "step",
        status: "pending",
        label: `Finish declined once — never entered ${((event.data as { missing?: string[] }).missing ?? []).join(", ")}`,
        meta: at ?? undefined,
      };
    case "extract_locator_rejected":
      return { id, type: "step", status: "pending", label: `Extract locator rejected — ${(event.data as { reason?: string }).reason ?? ""}`, meta: at ?? undefined };

    case "llm_usage": {
      const d = event.data as {
        call_number?: number;
        prompt_tokens?: number | null;
        completion_tokens?: number | null;
        total_tokens?: number | null;
      };
      return {
        id,
        type: "trace",
        kind: "thinking",
        label: `LLM call #${d.call_number ?? "?"}`,
        detail: `${d.prompt_tokens ?? "?"} in / ${d.completion_tokens ?? "?"} out · ${d.total_tokens ?? "?"} tokens`,
      };
    }
    case "llm_usage_summary": {
      const d = event.data as { tool_call_count?: number; total_tokens?: number };
      return {
        id,
        type: "trace",
        kind: "thinking",
        label: "LLM usage total",
        detail: `${d.tool_call_count ?? 0} call(s) · ${d.total_tokens ?? 0} tokens`,
      };
    }

    case "sandbox_started":
      return { id, type: "trace", kind: "run", label: "Sandbox browser started" };
    case "recovery_action_performed":
      return { id, type: "trace", kind: "run", label: "Recovery action performed" };
    case "recoverable_retry":
      return { id, type: "trace", kind: "run", label: "Retrying after a recoverable condition", detail: (event.data as { detail?: string }).detail };

    default:
      return {
        id,
        type: "trace",
        kind: "message",
        label: event.event_type.replace(/_/g, " "),
        detail: JSON.stringify(event.data).slice(0, 120),
      };
  }
}
