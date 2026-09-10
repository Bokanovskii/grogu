import type {
  Activity,
  AgentRow,
  Connection,
  Lifecycle,
  Role,
} from "../../api/types";
import { blockerCount, waitsOnUser } from "./presentation";

export type AttentionFilter = "waiting" | "blockers" | "failures" | "unread";

export interface ControlFilters {
  roles: Role[];
  plans: string[];
  workstreams: string[];
  lifecycles: Lifecycle[];
  activities: Activity[];
  connections: Connection[];
  attention: AttentionFilter[];
}

export const EMPTY_FILTERS: ControlFilters = {
  roles: [],
  plans: [],
  workstreams: [],
  lifecycles: [],
  activities: [],
  connections: [],
  attention: [],
};

export function filtersActive(filters: ControlFilters): boolean {
  return Object.values(filters).some((values) => values.length > 0);
}

export function applyFilters(
  agents: AgentRow[],
  filters: ControlFilters,
  waitingKeys: Set<string>,
): AgentRow[] {
  return agents.filter((agent) => {
    if (filters.roles.length && !filters.roles.includes(agent.role as Role)) return false;
    if (filters.plans.length && !filters.plans.includes(agent.plan)) return false;
    if (
      filters.workstreams.length &&
      !filters.workstreams.includes(agent.workstream)
    ) {
      return false;
    }
    if (
      filters.lifecycles.length &&
      !filters.lifecycles.includes(agent.lifecycle)
    ) {
      return false;
    }
    if (
      filters.activities.length &&
      !filters.activities.includes(agent.activity)
    ) {
      return false;
    }
    if (
      filters.connections.length &&
      !filters.connections.includes(agent.connection)
    ) {
      return false;
    }
    if (
      filters.attention.length &&
      !filters.attention.some((value) => {
        if (value === "waiting") {
          return waitingKeys.has(agent.agent_key) || waitsOnUser(agent);
        }
        if (value === "blockers") return blockerCount(agent) > 0;
        if (value === "failures") return agent.failures.length > 0;
        return agent.steering.unread > 0;
      })
    ) {
      return false;
    }
    return true;
  });
}

const LIFECYCLE_RANK: Record<Lifecycle, number> = {
  failed: 0,
  running: 1,
  registered: 2,
  unknown: 3,
  cancelled: 4,
  finished: 5,
};

export function defaultSort(
  agents: AgentRow[],
  waitingKeys: Set<string>,
): AgentRow[] {
  return [...agents].sort((a, b) => {
    const aWaiting = waitingKeys.has(a.agent_key) || waitsOnUser(a);
    const bWaiting = waitingKeys.has(b.agent_key) || waitsOnUser(b);
    if (aWaiting !== bWaiting) return aWaiting ? -1 : 1;
    if (a.activity === "blocked" && b.activity !== "blocked") return -1;
    if (b.activity === "blocked" && a.activity !== "blocked") return 1;
    const lifecycle = LIFECYCLE_RANK[a.lifecycle] - LIFECYCLE_RANK[b.lifecycle];
    if (lifecycle !== 0) return lifecycle;
    return (b.last_observed_at ?? 0) - (a.last_observed_at ?? 0);
  });
}

export function distinct<T>(items: T[]): T[] {
  return [...new Set(items)];
}
