import type { AgentBadge, AgentRow, Role } from "../../api/types";

export type FreshBucket = "live" | "idle" | "stale" | "dead";

export interface ControlFilters {
  roles: Role[];
  plans: string[];
  workstreams: string[];
  states: AgentBadge[];
  fresh: FreshBucket[];
}

export const EMPTY_FILTERS: ControlFilters = {
  roles: [],
  plans: [],
  workstreams: [],
  states: [],
  fresh: [],
};

export function filtersActive(f: ControlFilters): boolean {
  return (
    f.roles.length > 0 ||
    f.plans.length > 0 ||
    f.workstreams.length > 0 ||
    f.states.length > 0 ||
    f.fresh.length > 0
  );
}

function bucketOf(agent: AgentRow, now: number): FreshBucket {
  const s = (now - agent.last_observed_at) / 1000;
  if (s < 60) return "live";
  if (s < 5 * 60) return "idle";
  if (s < 15 * 60) return "stale";
  return "dead";
}

// Filters combine with AND across categories, OR within a category.
export function applyFilters(agents: AgentRow[], f: ControlFilters, now: number): AgentRow[] {
  return agents.filter((a) => {
    if (f.roles.length && !f.roles.includes(a.role as Role)) return false;
    if (f.plans.length && !f.plans.includes(a.plan)) return false;
    if (f.workstreams.length && !f.workstreams.includes(a.workstream)) return false;
    if (f.states.length && !f.states.includes(a.badge)) return false;
    if (f.fresh.length && !f.fresh.includes(bucketOf(a, now))) return false;
    return true;
  });
}

// Default sort: waiting_on_you first, then stuck, then working (live/idle),
// then the rest. This makes "is anything stuck on me" answerable at a glance.
const BADGE_RANK: Record<AgentBadge, number> = {
  waiting: 0,
  stuck: 1,
  live: 2,
  idle: 3,
  error: 4,
  disconnected: 5,
  finished: 6,
};

export function defaultSort(agents: AgentRow[], waitingKeys: Set<string>): AgentRow[] {
  return [...agents].sort((a, b) => {
    const aw = waitingKeys.has(a.agent_key) ? -1 : 0;
    const bw = waitingKeys.has(b.agent_key) ? -1 : 0;
    if (aw !== bw) return aw - bw;
    const r = BADGE_RANK[a.badge] - BADGE_RANK[b.badge];
    if (r !== 0) return r;
    return a.last_observed_at - b.last_observed_at;
  });
}

export function distinct<T>(items: T[]): T[] {
  return [...new Set(items)];
}
