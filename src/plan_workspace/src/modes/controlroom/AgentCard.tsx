import { forwardRef } from "react";
import type { AgentRow } from "../../api/types";
import {
  ACTIVITY_META,
  LIFECYCLE_META,
  blockerCount,
  coverageText,
  elapsedText,
  eventText,
  freshnessText,
  lastSafeAction,
  ownershipText,
  revisionText,
} from "./presentation";

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

export const AgentCard = forwardRef<HTMLDivElement, Props>(function AgentCard(
  {
    agent,
    now,
    selected,
    tabIndex,
    onWatch,
    onFeedback,
    onNudge,
    onNavigate,
    onFocus,
  },
  ref,
) {
  const lifecycle = LIFECYCLE_META[agent.lifecycle];
  const activity = ACTIVITY_META[agent.activity];
  const titleId = `agent-${agent.agent_key.replace(/[^a-zA-Z0-9_-]/g, "-")}-title`;
  const nudge = agent.activity === "blocked" || agent.steering.unread > 0;

  return (
    <div
      ref={ref}
      className={`agent-card tone-${lifecycle.tone}${selected ? " is-selected" : ""}`}
      role="listitem"
      aria-labelledby={titleId}
      data-control-agent-row=""
      data-agent-key={agent.agent_key}
      tabIndex={tabIndex}
      onFocus={onFocus}
      onKeyDown={(event) => {
        const key = event.key.toLowerCase();
        if (event.key === "Enter" || key === "w") {
          event.preventDefault();
          onWatch();
        } else if (key === "f") {
          event.preventDefault();
          onFeedback();
        } else if (key === "n" && nudge) {
          event.preventDefault();
          onNudge();
        }
      }}
    >
      <div className="agent-card-head" id={titleId}>
        <span>{agent.role || "—"}</span>
        <span aria-hidden="true">·</span>
        <span className="agent-card-name" title={agent.agent}>
          {agent.agent}
        </span>
      </div>
      <div className="agent-card-context">
        <button
          type="button"
          className="agent-plan"
          title={agent.plan_title ?? agent.plan}
          onClick={onNavigate}
        >
          {agent.plan}
        </button>
        <span>· {agent.workstream || "—"}</span>
        <span title={revisionText(agent)}>· {revisionText(agent)}</span>
      </div>
      <div className="agent-card-states">
        <span className={`tone-${lifecycle.tone}`}>
          {lifecycle.label} <span aria-hidden="true">{lifecycle.glyph}</span>
        </span>
        <span className={`tone-${activity.tone}`}>
          {activity.label} <span aria-hidden="true">{activity.glyph}</span>
        </span>
        <span>{freshnessText(agent, now)}</span>
      </div>
      <div className="agent-card-fact">
        <span title={ownershipText(agent)}>Stage: {ownershipText(agent, true)}</span>
        <span>· {elapsedText(agent)}</span>
      </div>
      <div className="agent-card-action" title={lastSafeAction(agent, now)}>
        {lastSafeAction(agent, now)}
      </div>
      <div className="agent-card-counters">
        <span aria-label={`Blockers ${blockerCount(agent)}`}>⚑ {blockerCount(agent)}</span>
        <span aria-label={`Failures ${agent.failures.length}`}>✗ {agent.failures.length}</span>
        <span aria-label={`Unread steering ${agent.steering.unread}`}>✉ {agent.steering.unread}</span>
        <span
          aria-label={`Coverage: lifecycle ${agent.coverage.lifecycle}, tools ${agent.coverage.tools}, outcomes ${agent.coverage.outcomes}`}
        >
          Cov: {coverageText(agent)}
        </span>
      </div>
      <div className="agent-card-events" aria-label="Recent events">
        {agent.recent_events?.length
          ? agent.recent_events.slice(0, 3).map((event) => (
              <span key={event.id} title={eventText(event, now)}>
                {eventText(event)}
              </span>
            ))
          : "No inline events available"}
      </div>
      <div className="agent-actions">
        <button type="button" className="btn-primary-text" onClick={onWatch}>
          Watch
        </button>
        <button type="button" className="btn-secondary-text" onClick={onFeedback}>
          Send feedback
        </button>
        {nudge ? (
          <button type="button" className="btn-secondary-text" onClick={onNudge}>
            Nudge
          </button>
        ) : null}
      </div>
    </div>
  );
});
