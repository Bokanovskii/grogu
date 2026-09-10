import { useApp } from "../state/store";
import { NODE_LABEL } from "../lib/selection";
import { stageLabel } from "../api/types";

// The identity strip (visually the top 48px row). It is static text — the
// breadcrumb shows plan · title · stage · mode · selection so the current
// selection is visible even in a mode that cannot render it (acceptance
// criterion). Interactive chrome lives in the ModeBar so the focus order can
// begin at the Control room tab. Stage/mode/plan actions are reachable from the
// tabs, the user menu and the command palette.
export function TopBar() {
  const state = useApp();
  const sel = state.selection[0];
  const selNode = sel ? state.nodes[sel.id] : undefined;
  const selLabel = selNode ? `${NODE_LABEL[selNode.kind]} ${selNode.id}` : sel ? sel.id : "—";

  return (
    <div className="topbar" role="presentation">
      <span className="brand" aria-hidden="true">
        ◉ Grogu
      </span>
      <nav className="breadcrumb" aria-label="Location">
        <span className="crumb-static crumb-plan">
          {state.plan || "— no plan selected —"}
        </span>
        {state.plan ? (
          <>
            <span className="crumb-sep" aria-hidden="true">·</span>
            <span className="crumb-static crumb-title">{state.title}</span>
            <span className="crumb-sep" aria-hidden="true">·</span>
            <span className="crumb-static">{stageLabel(state.stage)}</span>
            <span className="crumb-sep" aria-hidden="true">·</span>
            <span className="crumb-static">{state.mode}</span>
            <span className="crumb-sep" aria-hidden="true">·</span>
            <span className="crumb-selection" aria-label={`Selection: ${selLabel}`}>
              {selLabel}
            </span>
          </>
        ) : null}
      </nav>
    </div>
  );
}
