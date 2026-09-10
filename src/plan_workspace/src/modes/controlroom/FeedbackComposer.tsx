import { useMemo, useState } from "react";
import { ApiFailure } from "../../api/client";
import type {
  AgentRow,
  FeedbackCapability,
  FeedbackConsequence,
  FeedbackScope,
  Role,
} from "../../api/types";
import { useControl } from "../../state/control";
import { useActions } from "../../state/store";

const drafts = new Map<string, string>();

type TargetKind = "agent" | "role" | "plan";

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
  const audience = `${role}s`;
  const draftKey = `${plan}:${agent?.agent_key ?? role}`;
  const [target, setTarget] = useState<TargetKind>(agent ? "agent" : "plan");
  const [binding, setBinding] = useState(false);
  const [text, setText] = useState(drafts.get(draftKey) ?? "");
  const [error, setError] = useState<string | null>(null);

  const targets: { value: TargetKind; label: string; enabled: boolean }[] = [
    { value: "agent", label: "This agent", enabled: !!agent },
    { value: "role", label: `All ${audience} on this plan`, enabled: true },
    { value: "plan", label: "Everyone on this plan", enabled: !!plan },
  ];

  const capability = useMemo(
    () =>
      findCapability(
        control.snapshot?.feedback_capabilities ?? [],
        target,
        plan,
        role,
      ),
    [control.snapshot?.feedback_capabilities, plan, role, target],
  );
  const bindingAllowed = capability?.binding === true;
  const activeBinding = binding && bindingAllowed;
  const scope = scopeFor(target, agent, plan, role, audience);
  const canSend = text.trim().length > 0;

  async function send() {
    setError(null);
    try {
      const response = await control.sendFeedback(
        scope,
        text.trim(),
        activeBinding,
      );
      const receiptPlan = response.record.plan ?? plan;
      const receiptTarget =
        agent && target === "agent" ? agent.role || agent.agent : scope.label;
      actions.toast(
        `Feedback sent to ${receiptTarget} on ${receiptPlan}.`,
        "success",
        { label: "View ledger", event: "noop" },
      );
      actions.live(`Feedback saved for ${receiptTarget} on ${receiptPlan}.`);
      drafts.delete(draftKey);
      setText("");
      setBinding(false);
      onSent?.();
    } catch (failure) {
      const status = failure instanceof ApiFailure ? failure.status : 0;
      const message =
        status > 0
          ? `Feedback was not saved: the workspace server returned ${status}. Nothing was sent. Try again, or record it from the terminal with grogu plan steer.`
          : "Feedback was not saved: the workspace server could not be reached. Nothing was sent. Try again, or record it from the terminal with grogu plan steer.";
      setError(message);
      actions.live(message);
    }
  }

  return (
    <div className="feedback-composer">
      <div className="fc-target" role="radiogroup" aria-label="Feedback target">
        {targets.map((item) => (
          <button
            key={item.value}
            type="button"
            role="radio"
            aria-checked={target === item.value}
            disabled={!item.enabled}
            className={`segmented-item${target === item.value ? " is-active" : ""}`}
            onClick={() => {
              setTarget(item.value);
              setBinding(false);
              setError(null);
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      <textarea
        className="fc-textarea"
        rows={3}
        value={text}
        autoFocus
        onChange={(event) => {
          setText(event.target.value);
          drafts.set(draftKey, event.target.value);
        }}
        placeholder={
          nudge
            ? "What should this agent know about the current blocker?"
            : "What should this scope know?"
        }
        aria-label="Feedback message"
      />

      <div className="fc-binding">
        <label className="switch">
          <input
            type="checkbox"
            checked={activeBinding}
            disabled={!bindingAllowed}
            onChange={(event) => setBinding(event.target.checked)}
          />
          <span className="switch-track" aria-hidden="true" />
          <span className="switch-label">
            {activeBinding ? "Binding" : "Advisory"}
          </span>
        </label>
        <p className="fc-consequence">
          {activeBinding && capability
            ? bindingConsequence(capability.consequence, plan)
            : bindingAllowed
              ? `Advisory. Saved for ${scope.label}. It does not close any gate.`
              : `Advisory. Saved for ${scope.label}. It does not close any gate. Binding is unavailable for this target.`}
        </p>
      </div>

      {error ? (
        <p className="fc-error" role="alert">
          {error}
        </p>
      ) : null}

      <div className="fc-footer">
        <button type="button" className="btn btn-text" onClick={onCancel}>
          Cancel
        </button>
        <button
          type="button"
          className="btn btn-primary"
          disabled={!canSend}
          onClick={() => void send()}
        >
          {activeBinding ? "Send as binding" : "Send"}
        </button>
      </div>
    </div>
  );
}

function scopeFor(
  target: TargetKind,
  agent: AgentRow | null,
  plan: string,
  role: Role,
  audience: string,
): FeedbackScope {
  if (target === "agent" && agent) {
    return {
      kind: "agent",
      agent_key: agent.agent_key,
      label: `${agent.role || "agent"} ${agent.agent}`,
    };
  }
  if (target === "plan") {
    return { kind: "plan", plan, label: `everyone on ${plan}` };
  }
  return { kind: "role", role, label: `${audience} on ${plan}` };
}

function findCapability(
  capabilities: FeedbackCapability[],
  target: TargetKind,
  plan: string,
  role: Role,
): FeedbackCapability | undefined {
  const scope = target;
  return capabilities.find(
    (capability) =>
      capability.plan === plan &&
      capability.scope === scope &&
      (scope !== "role" || capability.role === role),
  );
}

function bindingConsequence(
  consequence: FeedbackConsequence,
  plan: string,
): string {
  if (consequence.requires_replan || consequence.release === "replan") {
    return `Binding. Requires replan for ${plan}; release follows the recorded replan consequence.`;
  }
  const gates = consequence.gates.map((gate) => `${gate} gate`).join(", ");
  if (consequence.release === "acknowledgement_or_withdrawal") {
    return `Binding. Closes the ${gates || "recorded gate"} for ${plan} until acknowledgement or withdrawal.`;
  }
  return `Binding. Applies the recorded ${gates || "gate"} consequence for ${plan}.`;
}
