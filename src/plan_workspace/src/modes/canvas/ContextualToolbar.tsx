import { NodeToolbar, Position } from "@xyflow/react";
import { useActions, useApp } from "../../state/store";
import { useCanvasOps } from "./ops";
import { Menu } from "../../shell/Menu";

// The floating contextual toolbar. NodeToolbar auto-positions above the topmost
// selected node and flips below near the top edge. It carries the actions for
// the current selection and nothing else; Delete lives in the overflow menu,
// not the main row.
export function ContextualToolbar() {
  const state = useApp();
  const actions = useActions();
  const ops = useCanvasOps();

  const nodeIds = state.selection.filter((s) => s.type !== "edge" && s.type !== "mark").map((s) => s.id);
  if (nodeIds.length === 0) return null;
  const primary = nodeIds[0]!;
  const multi = nodeIds.length > 1;

  return (
    <NodeToolbar nodeId={nodeIds} isVisible position={Position.Top} offset={12}>
      <div className="contextual-toolbar" role="toolbar" aria-label="Selection toolbar">
        <button
          type="button"
          className="ct-btn"
          onClick={() => actions.openOverlay({ kind: "newComment", nodeId: primary })}
        >
          Comment
        </button>
        {multi ? (
          <button type="button" className="ct-btn" onClick={() => ops.group()}>
            Group
          </button>
        ) : null}
        <button type="button" className="ct-btn" onClick={() => ops.duplicate()}>
          Duplicate
        </button>
        <button type="button" className="ct-btn" onClick={() => actions.openOverlay({ kind: "impactPreview" })}>
          View impact ▸
        </button>
        <Menu
          label="More…"
          chevron={false}
          align="end"
          className="ct-more"
          items={[
            { label: "Bring to front", hint: "⌘]", onSelect: () => ops.zOrder("front") },
            { label: "Bring forward", hint: "]", onSelect: () => ops.zOrder("forward") },
            { label: "Send backward", hint: "[", onSelect: () => ops.zOrder("backward") },
            { label: "Send to back", hint: "⌘[", onSelect: () => ops.zOrder("back") },
            ...(state.selection.some((s) => state.nodes[s.id]?.kind === "region")
              ? [{ label: "Ungroup", hint: "⌘⇧G", onSelect: () => ops.ungroup() }]
              : []),
            { label: "Delete", hint: "⌫", danger: true, onSelect: () => ops.remove() },
          ]}
        />
      </div>
    </NodeToolbar>
  );
}
