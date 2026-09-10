import { useState } from "react";
import { useActions, useApp } from "../../state/store";
import { NODE_GLYPH } from "../../lib/selection";
import { sectionsForStage } from "../../lib/sections";
import { stageLabel } from "../../api/types";

// The Document-mode outline: stage ▸ sections ▸ node titles. Selection mirrors
// and drives the reading column. Sibling drag-reorder writes a new `order` as
// one revision; drop targets show a 2px accent bar between rows.
export function Outline() {
  const state = useApp();
  const actions = useActions();
  const sections = sectionsForStage(state.nodes, state.stage);
  const [dragId, setDragId] = useState<string | null>(null);
  const [overId, setOverId] = useState<string | null>(null);

  function reorder(kind: string, draggedId: string, targetId: string) {
    const group = sections.find((s) => s.kind === kind)?.nodes ?? [];
    const targetIdx = group.findIndex((n) => n.id === targetId);
    if (targetIdx < 0) return;
    const before = group[targetIdx - 1];
    const target = group[targetIdx];
    // New order = midpoint between the row above the target and the target.
    const newOrder = before ? Math.round((before.order + target!.order) / 2) : target!.order - 1000;
    void actions.writeGesture(
      [{ op: "replace", path: `/nodes/${draggedId}/order`, value: newOrder }],
      `reorder ${draggedId}`,
      "workspace",
      `reorder ${draggedId}`,
    );
  }

  return (
    <nav className="outline" aria-label="Document outline">
      <div className="outline-stage">{stageLabel(state.stage)}</div>
      {sections.length === 0 ? (
        <p className="inspector-muted">This stage has no content yet.</p>
      ) : (
        sections.map((section) => (
          <div key={section.kind} className="outline-section">
            <div className="outline-section-label">{section.label}</div>
            <ul className="outline-list">
              {section.nodes.map((n) => (
                <li
                  key={n.id}
                  className={`outline-row${state.selection.some((s) => s.id === n.id) ? " is-selected" : ""}${
                    overId === n.id ? " is-drop-target" : ""
                  }`}
                  draggable
                  onDragStart={() => setDragId(n.id)}
                  onDragOver={(e) => {
                    e.preventDefault();
                    setOverId(n.id);
                  }}
                  onDragLeave={() => setOverId((v) => (v === n.id ? null : v))}
                  onDrop={(e) => {
                    e.preventDefault();
                    if (dragId && dragId !== n.id) reorder(section.kind, dragId, n.id);
                    setDragId(null);
                    setOverId(null);
                  }}
                >
                  <button
                    type="button"
                    className="outline-btn"
                    onClick={() => {
                      actions.select(n.id, "node");
                      document.getElementById(`doc-node-${n.id}`)?.scrollIntoView({ block: "center" });
                    }}
                  >
                    <span className="outline-glyph" aria-hidden="true">
                      {NODE_GLYPH[n.kind]}
                    </span>
                    <span className="outline-title">{n.title || n.id}</span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))
      )}
    </nav>
  );
}
