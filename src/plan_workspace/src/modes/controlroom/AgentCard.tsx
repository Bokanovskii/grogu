import React, { forwardRef } from "react";
import type { AgentRow } from "../../api/types";
import { NODE_GLYPH } from "../../lib/selection";
import { durationMs } from "../../lib/format";
import { BADGE_META, badgeLabel } from "./badges";
import { FeedbackBadge } from "./FeedbackBadge";

interface Props {
  agent: AgentRow;
  now: number;
  selected: boolean;
  tabIndex: number;
  onWatch: () => void;
  onFeedback: () => void;
  onNudge: () => void;
  onNavigate: () => void;
  onFocus: () => void;
}

// One agent card: 320×180, fixed rows in fixed order, blanks kept as "—".
// role="group" with aria-labelledby to row 1. Everything is an observed fact
// except an explicitly-provided authored action summary, which is tagged.
export const AgentCard = forwardRef<HTMLDivElement, Props>(function AgentCard(
  { agent, now, selected, tabIndex, onWatch, onFeedback, onNudge, onNavigate, onFocus },
  ref,
) {
  const meta = BADGE_META[agent.badge];
  const titleId = `agent-${agent.agent_key}-title`;
  const summary = agent.current_action?.summary;
  const action = agent.current_action?.tool_name
    ? `${agent.current_action.tool_name}${agent.current_action.phase ? ` · ${agent.current_action.phase}` : ""}`
    : "Idle";

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === "Enter") {
      e.preventDefault();
      onWatch();
    } else if (e.key.toLowerCase() === "f") {
      e.preventDefault();
      onFeedback();
    } else if (e.key.toLowerCase() === "w") {
      e.preventDefault();
      onWatch();
    } else if (e.key.toLowerCase() === "n" && agent.badge === "stuck") {
      e.preventDefault();
      onNudge();
    }
  };

  return (
    <div
      ref={ref}
      className={`agent-card badge-border-${meta.tone}${selected ? " is-selected" : ""}`}
      role="listitem"
      aria-labelledby={titleId}
      tabIndex={tabIndex}
      onKeyDown={onKeyDown}
      onFocus={onFocus}
    >
      <div className="agent-row agent-row-1" id={titleId}>
        <span className="agent-kind-glyph" aria-hidden="true">
          {NODE_GLYPH.task}
        </span>
        <span className="agent-role">{agent.role || "—"}</span>
        <span className="agent-sep">·</span>
        <span className="agent-workstream">{agent.workstream || "—"}</span>
        <span className="agent-sep">·</span>
        <button
          type="button"
          className="agent-plan"
          title={agent.plan_title ?? agent.plan}
          onClick={onNavigate}
        >
          {agent.plan || "—"}
        </button>
      </div>

      <div className="agent-row agent-row-2">
        <span className="agent-rev">{agent.revision.current || agent.revision.last_read || "—"}</span>
        <span className="agent-sep">•</span>
        <span className={`agent-badge badge-text-${meta.tone}`}>
          <span className="agent-badge-glyph" aria-hidden="true">
            {meta.glyph}
          </span>
          {badgeLabel(agent, now)}
        </span>
        {agent.badge === "stuck" && agent.stuck_threshold_s ? (
          <span className="agent-threshold"> · threshold {Math.round(agent.stuck_threshold_s / 60)}m</span>
        ) : null}
      </div>

      <div className="agent-row agent-row-3">
        <span className="agent-label">Action:</span>{" "}
        {agent.badge === "error" && agent.last_error ? (
          <span className="agent-action agent-action-error">{agent.last_error}</span>
        ) : (
          <span className="agent-action">{action}</span>
        )}
        {summary ? (
          <span className="authored-tag" title="Text a model wrote">
            authored: {summary}
          </span>
        ) : null}
      </div>

      <div className="agent-row agent-row-4">
        <span className="agent-label">Tool:</span>{" "}
        {agent.last_tool ? (
          <span className="agent-tool">
            {agent.last_tool.tool_name} • {durationMs(agent.last_tool.duration_ms)} •{" "}
            <span className={`tool-outcome tool-${agent.last_tool.outcome}`}>{agent.last_tool.outcome}</span>
          </span>
        ) : (
          <span className="agent-tool">—</span>
        )}
      </div>

      <div className="agent-row agent-row-5">
        <span className="agent-counters">
          Failures {agent.failures.length} · Blockers {agent.blockers.length} · Unread steering{" "}
          {agent.steering.unread}
        </span>
        <FeedbackBadge agentKey={agent.agent_key} />
      </div>

      <div className="agent-row agent-row-6 agent-actions">
        <button type="button" className="btn btn-text" onClick={onWatch}>
          Watch
        </button>
        <button type="button" className="btn btn-text" onClick={onFeedback}>
          Send feedback
        </button>
        {agent.badge === "stuck" ? (
          <button type="button" className="btn btn-text" onClick={onNudge}>
            Nudge
          </button>
        ) : null}
      </div>
    </div>
  );
});
