import { useApp, useActions } from "../../state/store";
import { NODE_GLYPH } from "../../lib/selection";
import type { PlanNode } from "../../api/types";

// The Layers panel: frames (regions) and their contained nodes, then loose
// nodes. Selection mirrors and drives the canvas. This is the accessible
// surface — everything the canvas does is possible from here plus the Inspector.
export function Layers() {
  const state = useApp();
  const actions = useActions();

  const placed = Object.values(state.nodes).filter((n) => n.geometry && n.kind !== "thread");
  const regions = placed.filter((n) => n.kind === "region");
  const containsByRegion = new Map<string, string[]>();
  for (const e of Object.values(state.edges)) {
    if (e.kind === "contains") {
      const list = containsByRegion.get(e.from) ?? [];
      list.push(e.to);
      containsByRegion.set(e.from, list);
    }
  }
  const contained = new Set([...containsByRegion.values()].flat());
  const loose = placed.filter((n) => n.kind !== "region" && !contained.has(n.id));

  const row = (n: PlanNode, indent: number) => {
    const orphaned = n.attrs?.["anchor_state"] === "orphaned";
    return (
      <li
        key={n.id}
        className={`layer-row${state.selection.some((s) => s.id === n.id) ? " is-selected" : ""}`}
        style={{ paddingLeft: 8 + indent * 16 }}
      >
        <button
          type="button"
          className="layer-btn"
          onClick={(e) =>
            actions.select(n.id, n.kind === "region" ? "region" : "node", e.shiftKey ? "extend" : e.metaKey || e.ctrlKey ? "toggle" : "replace")
          }
        >
          {orphaned ? (
            <span className="layer-broken" title="Orphaned — anchor no longer resolves. Re-anchor or resolve.">
              ⚠
            </span>
          ) : null}
          <span className="layer-glyph" aria-hidden="true">
            {NODE_GLYPH[n.kind]}
          </span>
          <span className="layer-title">{n.title || n.id}</span>
          {n.attrs?.["status"] ? <span className="layer-status">{String(n.attrs["status"])}</span> : null}
        </button>
      </li>
    );
  };

  return (
    <nav className="layers" aria-label="Layers">
      <ul className="layer-list">
        {regions.map((r) => (
          <li key={r.id} className="layer-group">
            {row(r, 0)}
            <ul>{(containsByRegion.get(r.id) ?? []).map((cid) => state.nodes[cid] && row(state.nodes[cid]!, 1))}</ul>
          </li>
        ))}
        {loose.map((n) => row(n, 0))}
        {placed.length === 0 ? <li className="inspector-muted layer-empty">Nothing on the canvas yet.</li> : null}
      </ul>
    </nav>
  );
}
