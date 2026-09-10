import { useEffect, useState } from "react";
import { ModeLayout } from "../../shell/ModeLayout";
import { Outline } from "./Outline";
import { ReadingColumn } from "./ReadingColumn";
import { RightPanel } from "../shared/RightPanel";
import { readableStage, useActions, useApp } from "../../state/store";
import { SealedStagePanel, UnwrittenStagePanel } from "../../shell/StatePanels";

export function DocumentMode() {
  const state = useApp();
  const actions = useActions();
  const [markOffsets, setMarkOffsets] = useState<Record<string, number>>({});
  const status = readableStage(state.stages, state.stage);

  // Default the right panel to Comments the first time Document mounts.
  useEffect(() => {
    if (state.panels.rightTab === "inspector" && state.selection.length === 0) {
      actions.patchPanels({ rightTab: "comments" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // C opens a comment on the selected node (object-anchored).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key.toLowerCase() !== "c" || e.metaKey || e.ctrlKey || e.altKey) return;
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      const sel = state.selection[0];
      if (sel) {
        const block = document.getElementById(`doc-node-${sel.id}`);
        block?.querySelector<HTMLButtonElement>(".doc-node-comment")?.click();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [state.selection]);

  const body =
    status === "sealed" ? (
      <div className="reading-column">
        <SealedStagePanel stage={state.stage} />
      </div>
    ) : status === "unwritten" ? (
      <div className="reading-column">
        <UnwrittenStagePanel stage={state.stage} />
      </div>
    ) : (
      <ReadingColumn onMarkOffsets={setMarkOffsets} />
    );

  return (
    <ModeLayout
      left={<Outline />}
      leftTitle="Outline"
      right={
        <RightPanel
          order={["comments", "inspector", "agents"]}
          markOffsets={markOffsets}
          round={1}
          changesRequested={false}
        />
      }
      rightTitle="Comments"
    >
      {body}
    </ModeLayout>
  );
}
