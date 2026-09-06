import { useControl } from "../../state/control";

// The right-aligned delivery badge on an agent card / row: feedback the current
// user has sent to this agent and its state. Shows unread vs acknowledged.
export function FeedbackBadge({ agentKey }: { agentKey: string }) {
  const { feedback } = useControl();
  const mine = feedback.filter((f) => f.scope.kind === "agent" && f.scope.agent_key === agentKey);
  if (mine.length === 0) return <span className="feedback-badge feedback-badge-empty">—</span>;

  const acknowledged = mine.filter((f) => f.state === "acknowledged").length;
  const pending = mine.filter(
    (f) => f.state === "sent" || f.state === "routed" || f.state === "delivered",
  ).length;

  let label: string;
  if (pending > 0) label = `${pending} sent`;
  else if (acknowledged > 0) label = `${acknowledged} acknowledged`;
  else label = `${mine.length} sent`;

  return (
    <span className="feedback-badge" title="Feedback you sent to this agent">
      <span aria-hidden="true">⚑</span> {label}
    </span>
  );
}
