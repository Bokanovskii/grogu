import { useMemo } from "react";
import type { AgentRow, ControlTopology } from "../../api/types";
import {
  ACTIVITY_META,
  LIFECYCLE_META,
  lastSafeAction,
} from "./presentation";

export function FlowTopology({
  agents,
  topology,
  now,
  onSelect,
}: {
  agents: AgentRow[];
  topology?: ControlTopology;
  now: number;
  onSelect: (agent: AgentRow) => void;
}) {
  const rows = useMemo(
    () => flattenTopology(agents, topology),
    [agents, topology],
  );
  if (!topology || rows.length === 0) return null;

  return (
    <section className="cr-topology" aria-labelledby="cr-topology-title">
      <div className="cr-topology-head">
        <h2 id="cr-topology-title">Agent and session topology</h2>
        <span>Lineage {topology.coverage}</span>
      </div>
      <ul className="cr-topology-list">
        {rows.map(({ agent, depth, cyclic }) => {
          const lifecycle = LIFECYCLE_META[agent.lifecycle];
          const activity = ACTIVITY_META[agent.activity];
          return (
            <li
              key={agent.agent_key}
              className="cr-topology-node"
              style={{ "--tree-depth": depth } as React.CSSProperties}
            >
              <button type="button" onClick={() => onSelect(agent)}>
                <span className="cr-topology-branch" aria-hidden="true">
                  {depth > 0 ? "↳" : "●"}
                </span>
                <strong>{agent.agent}</strong>
                <span>{agent.session_key ?? "session unavailable"}</span>
                <span>
                  {lifecycle.label} · {activity.label}
                </span>
                <span>
                  {agent.plan_title ?? agent.plan} · {agent.workstream || "root"}
                </span>
                <span>{lastSafeAction(agent, now)}</span>
                {cyclic ? <span className="tone-danger">Lineage cycle</span> : null}
              </button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

function flattenTopology(
  agents: AgentRow[],
  topology?: ControlTopology,
): { agent: AgentRow; depth: number; cyclic: boolean }[] {
  if (!topology) return [];
  const byKey = new Map(agents.map((agent) => [agent.agent_key, agent]));
  const children = new Map<string, string[]>();
  const hasParent = new Set<string>();
  for (const edge of topology.edges) {
    if (edge.kind !== "spawned" || !byKey.has(edge.from) || !byKey.has(edge.to)) {
      continue;
    }
    children.set(edge.from, [...(children.get(edge.from) ?? []), edge.to]);
    hasParent.add(edge.to);
  }
  for (const values of children.values()) values.sort();
  const roots = agents
    .filter((agent) => !hasParent.has(agent.agent_key))
    .sort((left, right) => left.agent.localeCompare(right.agent));
  const result: { agent: AgentRow; depth: number; cyclic: boolean }[] = [];
  const emitted = new Set<string>();

  function visit(agent: AgentRow, depth: number, path: Set<string>) {
    const cyclic = path.has(agent.agent_key);
    if (cyclic || emitted.has(agent.agent_key)) {
      if (cyclic) result.push({ agent, depth, cyclic: true });
      return;
    }
    emitted.add(agent.agent_key);
    result.push({ agent, depth, cyclic: false });
    const nextPath = new Set(path).add(agent.agent_key);
    for (const childKey of children.get(agent.agent_key) ?? []) {
      const child = byKey.get(childKey);
      if (child) visit(child, depth + 1, nextPath);
    }
  }

  for (const root of roots) visit(root, 0, new Set());
  for (const agent of agents) {
    if (!emitted.has(agent.agent_key)) visit(agent, 0, new Set());
  }
  return result;
}
