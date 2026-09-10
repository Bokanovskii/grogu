import { useControl } from "../../state/control";
import { useActions, useApp } from "../../state/store";
import { freshnessLabel } from "../../lib/format";
import { BADGE_META } from "../controlroom/badges";

// The right-panel Agents tab inside plan modes: compact 56px condensed cards
// scoped to the current plan, each with Action, Tool and Freshness only, plus a
// Timeline button that opens the full Control-room drawer.
export function AgentsPane() {
  const control = useControl();
  const actions = useActions();
  const state = useApp();
  const now = Date.now();
  const agents = (control.snapshot?.agents ?? []).filter((a) => a.plan === state.plan);

  if (agents.length === 0) {
    return <p className="inspector-empty">No agents on this plan right now.</p>;
  }

  return (
    <ul className="agents-pane">
      {agents.map((a) => {
        const meta = BADGE_META[a.badge];
        return (
          <li key={a.agent_key} className={`agents-pane-row badge-border-${meta.tone}`}>
            <div className="apr-main">
              <div className="apr-line-1">
                <span className={`badge-text-${meta.tone}`} aria-hidden="true">
                  {meta.glyph}
                </span>{" "}
                {a.role} · {a.workstream || "—"}
              </div>
              <div className="apr-line-2">
                {a.current_action?.tool_name ?? "Idle"} · {a.last_tool?.tool_name ?? "—"} ·{" "}
                {freshnessLabel(a.last_observed_at, now)}
              </div>
            </div>
            <button
              type="button"
              className="btn btn-text"
              onClick={() => {
                control.selectAgent(a.agent_key);
                actions.patchPanels({ rightCollapsed: false });
                actions.setMode("control");
                window.setTimeout(() => {
                  document.getElementById("control-agents")?.focus();
                }, 0);
              }}
              title="Open this agent's audit timeline"
            >
              Open timeline
            </button>
          </li>
        );
      })}
    </ul>
  );
}
