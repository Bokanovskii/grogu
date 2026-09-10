import { useControl } from "../../state/control";
import { useActions, useApp } from "../../state/store";
import {
  LIFECYCLE_META,
  freshnessText,
  lastSafeAction,
} from "../controlroom/presentation";

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
        const meta = LIFECYCLE_META[a.lifecycle];
        return (
          <li key={a.agent_key} className={`agents-pane-row tone-${meta.tone}`}>
            <div className="apr-main">
              <div className="apr-line-1">
                <span className={`tone-${meta.tone}`} aria-hidden="true">
                  {meta.glyph}
                </span>{" "}
                {meta.label} · {a.role} · {a.workstream || "—"}
              </div>
              <div className="apr-line-2">
                {lastSafeAction(a, now)} · {freshnessText(a, now)}
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
              Watch
            </button>
          </li>
        );
      })}
    </ul>
  );
}
