import { useMemo, useRef, useState } from "react";
import type { AgentRow } from "../../api/types";
import {
  ACTIVITY_META,
  LIFECYCLE_META,
  announceAgent,
  blockerCount,
  coverageText,
  elapsedText,
  eventText,
  freshnessText,
  lastSafeAction,
  ownershipText,
  revisionText,
} from "./presentation";

type SortKey =
  | "state"
  | "activity"
  | "freshness"
  | "agent"
  | "role"
  | "plan"
  | "workstream"
  | "revision"
  | "elapsed"
  | "blockers"
  | "failures"
  | "unread";

export function AgentList({
  agents,
  now,
  selected,
  waitingKeys,
  comfortable,
  onSelect,
  onWatch,
  onFeedback,
  onNudge,
  onRefresh,
  onSwitchScope,
  onAnnounce,
}: {
  agents: AgentRow[];
  now: number;
  selected: string | null;
  waitingKeys: Set<string>;
  comfortable: boolean;
  onSelect: (key: string) => void;
  onWatch: (key: string) => void;
  onFeedback: (key: string) => void;
  onNudge: (key: string) => void;
  onRefresh: () => void;
  onSwitchScope: () => void;
  onAnnounce: (message: string) => void;
}) {
  const [sort, setSort] = useState<{ key: SortKey; dir: 1 | -1 }>({
    key: "state",
    dir: 1,
  });
  const [focusedKey, setFocusedKey] = useState<string | null>(
    agents[0]?.agent_key ?? null,
  );
  const rowRefs = useRef(new Map<string, HTMLTableRowElement>());
  const stackedRefs = useRef(new Map<string, HTMLDivElement>());

  const sorted = useMemo(() => {
    const values = [...agents];
    values.sort((a, b) => sort.dir * compare(a, b, sort.key));
    return values;
  }, [agents, sort]);

  const activeKey =
    focusedKey && sorted.some((agent) => agent.agent_key === focusedKey)
      ? focusedKey
      : sorted[0]?.agent_key ?? null;

  function focusAt(index: number) {
    const agent = sorted[Math.max(0, Math.min(sorted.length - 1, index))];
    if (!agent) return;
    setFocusedKey(agent.agent_key);
    const tableRow = rowRefs.current.get(agent.agent_key);
    const stackedRow = stackedRefs.current.get(agent.agent_key);
    const target =
      tableRow && getComputedStyle(tableRow).display !== "none"
        ? tableRow
        : stackedRow;
    target?.focus();
  }

  function onRowKey(
    event: React.KeyboardEvent,
    agent: AgentRow,
    index: number,
  ) {
    const target = event.target as HTMLElement;
    if (
      target.matches("input, textarea, select, [contenteditable='true']") ||
      (target.closest("button") && event.key !== "Escape")
    ) {
      return;
    }
    let next: number | null = null;
    if (event.key === "ArrowDown") next = index + 1;
    else if (event.key === "ArrowUp") next = index - 1;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = sorted.length - 1;
    else if (event.key === "PageDown") next = index + 10;
    else if (event.key === "PageUp") next = index - 10;
    if (next != null) {
      event.preventDefault();
      focusAt(next);
      return;
    }
    const key = event.key.toLowerCase();
    if (event.key === "Enter" || key === "w") {
      event.preventDefault();
      onWatch(agent.agent_key);
    } else if (key === "f") {
      event.preventDefault();
      onFeedback(agent.agent_key);
    } else if (
      key === "n" &&
      (agent.activity === "blocked" || agent.steering.unread > 0)
    ) {
      event.preventDefault();
      onNudge(agent.agent_key);
    } else if (key === "r") {
      event.preventDefault();
      onRefresh();
    } else if (key === "s") {
      event.preventDefault();
      onSwitchScope();
    }
  }

  const header = (key: SortKey, label: string, className = "") => (
    <th
      scope="col"
      className={className}
      aria-sort={
        sort.key === key
          ? sort.dir === 1
            ? "ascending"
            : "descending"
          : "none"
      }
    >
      <button
        type="button"
        className="col-sort"
        onClick={() =>
          setSort((current) => ({
            key,
            dir: current.key === key && current.dir === 1 ? -1 : 1,
          }))
        }
      >
        {label}
        {sort.key === key ? (
          <span aria-hidden="true">{sort.dir === 1 ? " ▲" : " ▼"}</span>
        ) : null}
      </button>
    </th>
  );

  return (
    <div className={`cr-agent-list${comfortable ? " is-comfortable" : ""}`}>
      <div className="agent-table-scroll">
        <table className="agent-table">
          <caption className="sr-only">
            Registered agents, repository program scope
          </caption>
          <thead>
            <tr>
              {header("state", "State", "cr-col-state")}
              {header("activity", "Activity", "cr-col-activity")}
              {header("freshness", "Fresh", "cr-col-fresh")}
              {header("agent", "Agent", "cr-col-agent")}
              {header("role", "Role", "cr-col-role")}
              {header("plan", "Plan", "cr-col-plan")}
              <th scope="col" className="cr-col-stage">Stage</th>
              {header("workstream", "Workstream", "cr-col-workstream")}
              {header("revision", "Rev", "cr-col-revision")}
              {header("elapsed", "Elapsed", "cr-col-elapsed")}
              <th scope="col" className="cr-col-action">Last safe action</th>
              {header("blockers", "⚑", "cr-col-counter")}
              {header("failures", "✗", "cr-col-counter")}
              {header("unread", "✉", "cr-col-counter")}
              <th scope="col" className="cr-col-coverage">Cov</th>
              <th scope="col" className="cr-col-actions">
                <span className="sr-only">Actions</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((agent, index) => {
              const lifecycle = LIFECYCLE_META[agent.lifecycle];
              const activity = ACTIVITY_META[agent.activity];
              const waiting =
                waitingKeys.has(agent.agent_key) ||
                agent.blocker_details?.some((item) => item.waiting_on_user);
              return (
                <tr
                  key={agent.agent_key}
                  ref={(node) => {
                    if (node) rowRefs.current.set(agent.agent_key, node);
                    else rowRefs.current.delete(agent.agent_key);
                  }}
                  data-control-agent-row=""
                  data-agent-key={agent.agent_key}
                  className={`agent-tr tone-${lifecycle.tone}${
                    selected === agent.agent_key ? " is-selected" : ""
                  }${waiting ? " is-waiting" : ""}`}
                  tabIndex={activeKey === agent.agent_key ? 0 : -1}
                  onFocus={() => {
                    setFocusedKey(agent.agent_key);
                    onSelect(agent.agent_key);
                    onAnnounce(announceAgent(agent, now));
                  }}
                  onKeyDown={(event) => onRowKey(event, agent, index)}
                >
                  <td className="cr-col-state">
                    <StateValue
                      glyph={lifecycle.glyph}
                      label={lifecycle.label}
                      tone={lifecycle.tone}
                    />
                    {agent.lifecycle === "unknown" ? (
                      <span className="sr-only">
                        Lifecycle unknown: no supported runtime source reported this
                        agent. Recent Grogu commands are not lifecycle evidence.
                      </span>
                    ) : null}
                  </td>
                  <td className="cr-col-activity">
                    <StateValue
                      glyph={activity.glyph}
                      label={activity.label}
                      tone={activity.tone}
                    />
                  </td>
                  <td className="cr-col-fresh">{freshnessText(agent, now)}</td>
                  <td className="cr-col-agent" title={agent.agent}>
                    {agent.agent}
                  </td>
                  <td className="cr-col-role">{agent.role || "—"}</td>
                  <td className="cr-col-plan" title={agent.plan_title ?? agent.plan}>
                    {agent.plan}
                  </td>
                  <td className="cr-col-stage" title={ownershipText(agent)}>
                    {ownershipText(agent, true)}
                  </td>
                  <td className="cr-col-workstream" title={agent.workstream}>
                    {agent.workstream || "—"}
                  </td>
                  <td className="cr-col-revision" title={revisionText(agent)}>
                    {revisionText(agent)}
                  </td>
                  <td className="cr-col-elapsed" title={elapsedText(agent)}>
                    {elapsedText(agent)}
                  </td>
                  <td className="cr-col-action">
                    <span className="cr-last-action">{lastSafeAction(agent, now)}</span>
                    {agent.recent_events?.length ? (
                      <span className="cr-inline-events" aria-label="Recent events">
                        {agent.recent_events.slice(0, 3).map((event) => (
                          <span key={event.id} title={eventText(event, now)}>
                            {eventText(event)}
                          </span>
                        ))}
                      </span>
                    ) : null}
                  </td>
                  <td className="cr-col-counter" aria-label={`Blockers ${blockerCount(agent)}`}>
                    ⚑ {blockerCount(agent)}
                  </td>
                  <td className="cr-col-counter" aria-label={`Failures ${agent.failures.length}`}>
                    ✗ {agent.failures.length}
                  </td>
                  <td className="cr-col-counter" aria-label={`Unread steering ${agent.steering.unread}`}>
                    ✉ {agent.steering.unread}
                  </td>
                  <td
                    className="cr-col-coverage"
                    aria-label={`Coverage: lifecycle ${agent.coverage.lifecycle}, tools ${agent.coverage.tools}, outcomes ${agent.coverage.outcomes}`}
                  >
                    {coverageText(agent)}
                  </td>
                  <td className="cr-col-actions">
                    <RowActions
                      agent={agent}
                      onWatch={() => onWatch(agent.agent_key)}
                      onFeedback={() => onFeedback(agent.agent_key)}
                      onNudge={() => onNudge(agent.agent_key)}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      <div className="agent-stacked-list" role="list" aria-label="Registered agents">
        {sorted.map((agent, index) => {
          const lifecycle = LIFECYCLE_META[agent.lifecycle];
          const activity = ACTIVITY_META[agent.activity];
          return (
            <div
              key={agent.agent_key}
              ref={(node) => {
                if (node) stackedRefs.current.set(agent.agent_key, node);
                else stackedRefs.current.delete(agent.agent_key);
              }}
              data-control-agent-row=""
              data-agent-key={agent.agent_key}
              role="listitem"
              className={`agent-stacked tone-${lifecycle.tone}${
                selected === agent.agent_key ? " is-selected" : ""
              }`}
              tabIndex={activeKey === agent.agent_key ? 0 : -1}
              onFocus={() => {
                setFocusedKey(agent.agent_key);
                onSelect(agent.agent_key);
                onAnnounce(announceAgent(agent, now));
              }}
              onKeyDown={(event) => onRowKey(event, agent, index)}
            >
              <div className="agent-stacked-main">
                <span className="agent-stacked-agent">{agent.agent}</span>
                <StateValue
                  glyph={lifecycle.glyph}
                  label={lifecycle.label}
                  tone={lifecycle.tone}
                />
                <StateValue
                  glyph={activity.glyph}
                  label={activity.label}
                  tone={activity.tone}
                />
                <span>{freshnessText(agent, now)}</span>
                <span>
                  {agent.role || "—"} · {agent.plan} · {agent.workstream || "—"}
                </span>
                <span>{lastSafeAction(agent, now)}</span>
              </div>
              <RowActions
                agent={agent}
                onWatch={() => onWatch(agent.agent_key)}
                onFeedback={() => onFeedback(agent.agent_key)}
                onNudge={() => onNudge(agent.agent_key)}
              />
            </div>
          );
        })}
      </div>
    </div>
  );
}

function StateValue({
  glyph,
  label,
  tone,
}: {
  glyph: string;
  label: string;
  tone: string;
}) {
  return (
    <span className={`cr-state-value tone-${tone}`}>
      <span aria-hidden="true">{glyph}</span> {label}
    </span>
  );
}

function RowActions({
  agent,
  onWatch,
  onFeedback,
  onNudge,
}: {
  agent: AgentRow;
  onWatch: () => void;
  onFeedback: () => void;
  onNudge: () => void;
}) {
  const nudge = agent.activity === "blocked" || agent.steering.unread > 0;
  return (
    <span className="cr-row-actions">
      <button type="button" className="btn-primary-text" onClick={onWatch}>
        Watch
      </button>
      <button type="button" className="btn-secondary-text" onClick={onFeedback}>
        Feedback
      </button>
      {nudge ? (
        <button type="button" className="btn-secondary-text" onClick={onNudge}>
          Nudge
        </button>
      ) : null}
    </span>
  );
}

function compare(a: AgentRow, b: AgentRow, key: SortKey): number {
  if (key === "state") return a.lifecycle.localeCompare(b.lifecycle);
  if (key === "activity") return a.activity.localeCompare(b.activity);
  if (key === "freshness") {
    return (a.last_observed_at ?? 0) - (b.last_observed_at ?? 0);
  }
  if (key === "agent") return a.agent.localeCompare(b.agent);
  if (key === "role") return (a.role || "").localeCompare(b.role || "");
  if (key === "plan") return a.plan.localeCompare(b.plan);
  if (key === "workstream") return a.workstream.localeCompare(b.workstream);
  if (key === "revision") {
    return (a.revision.last_read ?? "").localeCompare(b.revision.last_read ?? "");
  }
  if (key === "elapsed") return (a.elapsed_ms ?? -1) - (b.elapsed_ms ?? -1);
  if (key === "blockers") return blockerCount(a) - blockerCount(b);
  if (key === "failures") return a.failures.length - b.failures.length;
  return a.steering.unread - b.steering.unread;
}
