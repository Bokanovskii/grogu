import { useState } from "react";
import type { AgentRow, FeedbackScope, Role } from "../../api/types";
import { useControl } from "../../state/control";
import { useActions } from "../../state/store";

// The feedback composer. Exactly one target scope must be chosen. A binding
// toggle names, before sending, the gate that will close. Destructive "Abandon
// feedback" is a plain link far from Send.
export function FeedbackComposer({
  agent,
  plan,
  nudge,
  onSent,
  onCancel,
}: {
  agent: AgentRow | null;
  plan: string;
  nudge?: boolean;
  onSent?: () => void;
  onCancel?: () => void;
}) {
  const control = useControl();
  const actions = useActions();
  const role = (agent?.role || "engineer") as Role;

  type TargetKind = "agent" | "role" | "plan" | "role_plan";
  const [target, setTarget] = useState<TargetKind>(agent ? "agent" : plan ? "plan" : "role");
  const [binding, setBinding] = useState(false);
  const [text, setText] = useState(
    nudge && agent
      ? `You appear stuck at ${agent.current_action?.tool_name ?? "your last action"}. What would help?`
      : "",
  );

  const targets: { value: TargetKind; label: string; enabled: boolean }[] = [
    { value: "agent", label: "This agent", enabled: !!agent },
    { value: "role", label: `All ${role}`, enabled: true },
    { value: "plan", label: "This plan", enabled: !!plan },
    { value: "role_plan", label: `${role} on this plan`, enabled: !!plan },
  ];

  function scopeFor(): FeedbackScope {
    if (target === "agent" && agent)
      return { kind: "agent", agent_key: agent.agent_key, label: `agent ${agent.agent}` };
    if (target === "plan") return { kind: "plan", plan, label: `plan ${plan}` };
    if (target === "role_plan") return { kind: "role_plan", role, plan, label: `${role} on ${plan}` };
    return { kind: "role", role, label: `all ${role}` };
  }

  const canSend = text.trim().length > 0;

  async function send() {
    const scope = scopeFor();
    try {
      await control.sendFeedback(scope, text.trim(), binding);
      actions.toast(`Feedback sent to ${scope.label}.`, "success", {
        label: "View ledger",
        event: "noop",
      });
      actions.live(`Feedback sent to ${scope.label}.`);
      setText("");
      setBinding(false);
      onSent?.();
    } catch {
      actions.toast("Could not send feedback.", "danger");
    }
  }

  return (
    <div className="feedback-composer">
      <div className="fc-target" role="radiogroup" aria-label="Feedback target">
        {targets.map((t) => (
          <button
            key={t.value}
            type="button"
            role="radio"
            aria-checked={target === t.value}
            disabled={!t.enabled}
            className={`segmented-item${target === t.value ? " is-active" : ""}`}
            onClick={() => setTarget(t.value)}
          >
            {t.label}
          </button>
        ))}
      </div>

      <textarea
        className="fc-textarea"
        rows={3}
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={`What would you like ${targets.find((t) => t.value === target)?.label.toLowerCase()} to know?`}
        aria-label="Feedback message"
      />

      <div className="fc-binding">
        <label className="switch">
          <input
            type="checkbox"
            checked={binding}
            onChange={(e) => setBinding(e.target.checked)}
          />
          <span className="switch-track" aria-hidden="true" />
          <span className="switch-label">{binding ? "Binding · closes the next gate" : "Advisory"}</span>
        </label>
        {binding ? (
          <p className="fc-binding-explain">
            The target's next gate stays blocked until they acknowledge this. You can cancel or
            replace it any time.
          </p>
        ) : null}
      </div>

      <div className="fc-footer">
        <button type="button" className="btn btn-text" onClick={() => onCancel?.()}>
          Cancel
        </button>
        <button type="button" className="btn btn-text" disabled>
          Save as draft
        </button>
        <span className="fc-divider" aria-hidden="true" />
        <button type="button" className="btn btn-primary" disabled={!canSend} onClick={() => void send()}>
          {binding ? "Send as binding" : "Send"}
        </button>
      </div>
      <div className="fc-abandon">
        <button
          type="button"
          className="link-danger"
          onClick={() => {
            setText("");
            onCancel?.();
          }}
        >
          Abandon feedback
        </button>
      </div>
    </div>
  );
}
