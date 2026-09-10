import { useControl } from "../../state/control";

export function FeedbackBadge({ agentKey }: { agentKey: string }) {
  const { feedback } = useControl();
  const records = feedback.filter(
    (record) =>
      record.scope.kind === "agent" && record.scope.agent_key === agentKey,
  );
  if (records.length === 0) return null;

  const pending = records.filter((record) => {
    const state = record.delivery_state ?? record.state;
    return state === "sent" || state === "routed" || state === "delivered";
  }).length;
  const acknowledged = records.filter(
    (record) => (record.delivery_state ?? record.state) === "acknowledged",
  ).length;
  const label =
    pending > 0
      ? `${pending} pending receipt${pending === 1 ? "" : "s"}`
      : `${acknowledged} acknowledged`;

  return (
    <span className="feedback-badge" title="Plan-qualified feedback receipts">
      {label}
    </span>
  );
}
