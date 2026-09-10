import { useActions, useApp, type RightTab } from "../../state/store";
import { SegmentedControl } from "../../shell/ui";
import { CommentRail } from "./CommentRail";
import { Inspector } from "./Inspector";
import { AgentsPane } from "./AgentsPane";

// The right panel shared by the plan modes: a segmented control selects
// Comments · Inspector · Agents, and the active pane renders below. Default tab
// differs per mode (Document defaults to Comments, Canvas to Inspector) but the
// user's choice is respected once made.
export function RightPanel({
  order = ["comments", "inspector", "agents"],
  markOffsets,
  round,
  changesRequested,
}: {
  order?: RightTab[];
  markOffsets?: Record<string, number>;
  round?: number;
  changesRequested?: boolean;
}) {
  const state = useApp();
  const actions = useActions();
  const tab = order.includes(state.panels.rightTab) ? state.panels.rightTab : order[0]!;

  const label: Record<RightTab, string> = {
    comments: "Comments",
    inspector: "Inspector",
    agents: "Agents",
  };

  return (
    <div className="right-panel" id="right-panel">
      <SegmentedControl
        ariaLabel="Right panel"
        value={tab}
        onChange={(v) => actions.patchPanels({ rightTab: v })}
        options={order.map((o) => ({ value: o, label: label[o] }))}
      />
      <div className="right-panel-body">
        {tab === "comments" ? (
          <CommentRail markOffsets={markOffsets} round={round} changesRequested={changesRequested} />
        ) : tab === "inspector" ? (
          <Inspector />
        ) : (
          <AgentsPane />
        )}
      </div>
    </div>
  );
}
