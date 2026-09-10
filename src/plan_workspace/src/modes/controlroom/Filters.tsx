import type {
  Activity,
  AgentRow,
  Connection,
  Lifecycle,
  Role,
} from "../../api/types";
import { Chip } from "../../shell/ui";
import {
  ACTIVITY_META,
  CONNECTION_LABEL,
  LIFECYCLE_META,
  blockerCount,
  waitsOnUser,
} from "./presentation";
import {
  type AttentionFilter,
  type ControlFilters,
  distinct,
} from "./controlFilters";

const ROLES: Role[] = [
  "architect",
  "designer",
  "engineer",
  "tester",
  "reviewer",
  "supervisor",
];
const LIFECYCLES: Lifecycle[] = [
  "registered",
  "running",
  "finished",
  "failed",
  "cancelled",
  "unknown",
];
const ACTIVITIES: Activity[] = [
  "active",
  "quiet",
  "possibly_stuck",
  "blocked",
  "unknown",
];
const CONNECTIONS: Connection[] = ["live", "stale", "disconnected", "unknown"];
const ATTENTION: { value: AttentionFilter; label: string }[] = [
  { value: "waiting", label: "Waiting on you" },
  { value: "blockers", label: "Blockers" },
  { value: "failures", label: "Failures" },
  { value: "unread", label: "Unread steering" },
];

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value)
    ? list.filter((item) => item !== value)
    : [...list, value];
}

export function Filters({
  agents,
  filters,
  waitingKeys,
  onChange,
}: {
  agents: AgentRow[];
  filters: ControlFilters;
  waitingKeys: Set<string>;
  onChange: (filters: ControlFilters) => void;
}) {
  const plans = distinct(agents.map((agent) => agent.plan).filter(Boolean));
  const workstreams = distinct(
    agents.map((agent) => agent.workstream).filter(Boolean),
  );
  const attentionCount = (value: AttentionFilter) =>
    agents.filter((agent) => {
      if (value === "waiting") {
        return waitingKeys.has(agent.agent_key) || waitsOnUser(agent);
      }
      if (value === "blockers") return blockerCount(agent) > 0;
      if (value === "failures") return agent.failures.length > 0;
      return agent.steering.unread > 0;
    }).length;

  return (
    <div className="cr-filters">
      <FilterGroup title="Role">
        {ROLES.map((role) => (
          <Chip
            key={role}
            onClick={() =>
              onChange({ ...filters, roles: toggle(filters.roles, role) })
            }
            active={filters.roles.includes(role)}
            count={agents.filter((agent) => agent.role === role).length || undefined}
          >
            {role}
          </Chip>
        ))}
      </FilterGroup>

      {plans.length > 0 ? (
        <FilterGroup title="Plan">
          {plans.map((plan) => (
            <Chip
              key={plan}
              onClick={() =>
                onChange({ ...filters, plans: toggle(filters.plans, plan) })
              }
              active={filters.plans.includes(plan)}
            >
              {plan}
            </Chip>
          ))}
        </FilterGroup>
      ) : null}

      {workstreams.length > 0 ? (
        <FilterGroup title="Workstream">
          {workstreams.map((workstream) => (
            <Chip
              key={workstream}
              onClick={() =>
                onChange({
                  ...filters,
                  workstreams: toggle(filters.workstreams, workstream),
                })
              }
              active={filters.workstreams.includes(workstream)}
            >
              {workstream}
            </Chip>
          ))}
        </FilterGroup>
      ) : null}

      <FilterGroup title="Lifecycle">
        {LIFECYCLES.map((lifecycle) => (
          <Chip
            key={lifecycle}
            glyph={LIFECYCLE_META[lifecycle].glyph}
            onClick={() =>
              onChange({
                ...filters,
                lifecycles: toggle(filters.lifecycles, lifecycle),
              })
            }
            active={filters.lifecycles.includes(lifecycle)}
          >
            {LIFECYCLE_META[lifecycle].label}
          </Chip>
        ))}
      </FilterGroup>

      <FilterGroup title="Activity">
        {ACTIVITIES.map((activity) => (
          <Chip
            key={activity}
            glyph={ACTIVITY_META[activity].glyph}
            onClick={() =>
              onChange({
                ...filters,
                activities: toggle(filters.activities, activity),
              })
            }
            active={filters.activities.includes(activity)}
          >
            {ACTIVITY_META[activity].label}
          </Chip>
        ))}
      </FilterGroup>

      <FilterGroup title="Freshness">
        {CONNECTIONS.map((connection) => (
          <Chip
            key={connection}
            onClick={() =>
              onChange({
                ...filters,
                connections: toggle(filters.connections, connection),
              })
            }
            active={filters.connections.includes(connection)}
          >
            {CONNECTION_LABEL[connection]}
          </Chip>
        ))}
      </FilterGroup>

      <FilterGroup title="Attention">
        {ATTENTION.map(({ value, label }) => (
          <Chip
            key={value}
            onClick={() =>
              onChange({
                ...filters,
                attention: toggle(filters.attention, value),
              })
            }
            active={filters.attention.includes(value)}
            count={attentionCount(value) || undefined}
          >
            {label}
          </Chip>
        ))}
      </FilterGroup>
    </div>
  );
}

function FilterGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section className="filter-group" aria-label={`Filter by ${title.toLowerCase()}`}>
      <h3 className="filter-heading">{title}</h3>
      <div className="filter-chips">{children}</div>
    </section>
  );
}
