import { useControl } from "../state/control";
import type { AgentRow } from "../api/types";

export function countLiveStuck(agents: AgentRow[]): { live: number; stuck: number } {
  let live = 0;
  let stuck = 0;
  for (const a of agents) {
    if (a.badge === "stuck") stuck += 1;
    if (a.badge === "live" || a.badge === "idle" || a.badge === "waiting") live += 1;
  }
  return { live, stuck };
}

export function AgentsChip({ onClick }: { onClick: () => void }) {
  const { snapshot } = useControl();
  const agents = snapshot?.agents ?? [];
  const { live, stuck } = countLiveStuck(agents);
  const stuckLabel = stuck > 0 ? ` · ${stuck} stuck` : "";
  return (
    <button
      type="button"
      className={`agents-chip${stuck > 0 ? " has-stuck" : ""}`}
      onClick={onClick}
      title="Open Control room"
    >
      <span aria-hidden="true">⌾</span>
      <span>
        Agents · {live} live{stuckLabel}
      </span>
    </button>
  );
}
