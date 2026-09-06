import type { AgentBadge, AgentRow, Role } from "../../api/types";
import { Chip } from "../../shell/ui";
import { BADGE_META } from "./badges";
import { type ControlFilters, distinct, type FreshBucket } from "./controlFilters";

const ROLES: Role[] = ["architect", "designer", "engineer", "tester", "reviewer", "supervisor"];
const STATES: AgentBadge[] = [
  "live",
  "idle",
  "stuck",
  "finished",
  "disconnected",
  "error",
  "waiting",
];
const FRESH: { value: FreshBucket; label: string }[] = [
  { value: "live", label: "Live · <60 s" },
  { value: "idle", label: "Idle · 1–5 m" },
  { value: "stale", label: "Stale · 5–15 m" },
  { value: "dead", label: "Dead · >15 m" },
];

function toggle<T>(list: T[], v: T): T[] {
  return list.includes(v) ? list.filter((x) => x !== v) : [...list, v];
}

export function Filters({
  agents,
  filters,
  onChange,
}: {
  agents: AgentRow[];
  filters: ControlFilters;
  onChange: (f: ControlFilters) => void;
}) {
  const roleCount = (r: Role) => agents.filter((a) => a.role === r).length;
  const plans = distinct(agents.map((a) => a.plan).filter(Boolean));
  const workstreams = distinct(agents.map((a) => a.workstream).filter(Boolean));
  const stateCount = (s: AgentBadge) => agents.filter((a) => a.badge === s).length;

  return (
    <div className="cr-filters">
      <section className="filter-group" aria-label="Filter by role">
        <h3 className="filter-heading">Roles</h3>
        <div className="filter-chips">
          {ROLES.map((r) => (
            <Chip
              key={r}
              tone="neutral"
              onClick={() => onChange({ ...filters, roles: toggle(filters.roles, r) })}
              active={filters.roles.includes(r)}
              count={roleCount(r) || undefined}
            >
              {r}
            </Chip>
          ))}
        </div>
      </section>

      {plans.length > 0 ? (
        <section className="filter-group" aria-label="Filter by plan">
          <h3 className="filter-heading">Plans</h3>
          <div className="filter-chips">
            {plans.map((p) => (
              <Chip
                key={p}
                onClick={() => onChange({ ...filters, plans: toggle(filters.plans, p) })}
                active={filters.plans.includes(p)}
              >
                {p}
              </Chip>
            ))}
          </div>
        </section>
      ) : null}

      {workstreams.length > 0 ? (
        <section className="filter-group" aria-label="Filter by workstream">
          <h3 className="filter-heading">Workstreams</h3>
          <div className="filter-chips">
            {workstreams.map((w) => (
              <Chip
                key={w}
                onClick={() => onChange({ ...filters, workstreams: toggle(filters.workstreams, w) })}
                active={filters.workstreams.includes(w)}
              >
                {w}
              </Chip>
            ))}
          </div>
        </section>
      ) : null}

      <section className="filter-group" aria-label="Filter by state">
        <h3 className="filter-heading">State</h3>
        <div className="filter-chips">
          {STATES.map((s) => (
            <Chip
              key={s}
              glyph={BADGE_META[s].glyph}
              onClick={() => onChange({ ...filters, states: toggle(filters.states, s) })}
              active={filters.states.includes(s)}
              count={stateCount(s) || undefined}
            >
              {BADGE_META[s].word}
            </Chip>
          ))}
        </div>
      </section>

      <section className="filter-group" aria-label="Filter by freshness">
        <h3 className="filter-heading">Freshness</h3>
        <div className="filter-chips">
          {FRESH.map((b) => (
            <Chip
              key={b.value}
              onClick={() => onChange({ ...filters, fresh: toggle(filters.fresh, b.value) })}
              active={filters.fresh.includes(b.value)}
            >
              {b.label}
            </Chip>
          ))}
        </div>
      </section>
    </div>
  );
}
