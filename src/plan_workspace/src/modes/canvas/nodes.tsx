import { useState } from "react";
import { Handle, NodeResizer, Position, type NodeProps } from "@xyflow/react";
import type { PlanNode } from "../../api/types";
import { useActions } from "../../state/store";
import { NODE_GLYPH, NODE_LABEL } from "../../lib/selection";
import { ri } from "../../lib/geometry";
import { humanTitle } from "../../lib/humanize";

function useGeomWriter(node: PlanNode, derived = false) {
  const actions = useActions();
  return (x: number, y: number, w: number, h: number) => {
    const geom = {
      x: ri(x),
      y: ri(y),
      w: ri(w),
      h: ri(h),
      z: node.geometry?.z ?? 0,
    };
    void actions.writeGesture(
      [{ op: derived ? "add" : "replace", path: `/nodes/${node.id}/geometry`, value: geom }],
      `resize ${node.id}`,
      "workspace",
      `resize ${node.id}`,
    );
  };
}

// A node rendered as a rounded card sized to its content. Top strip: kind glyph,
// id (monospace), status chip. Body truncated to three lines with a ⋯ toggle.
export function CardNode({ data, selected, id }: NodeProps) {
  const { node, derived } = data as { node: PlanNode; derived?: boolean };
  const [expanded, setExpanded] = useState(false);
  const writeGeom = useGeomWriter(node, derived);
  const status = node.attrs?.["status"];

  return (
    <div
      className={`cv-card cv-card-${node.kind}${selected ? " is-selected" : ""}`}
      role="group"
      aria-labelledby={`cv-title-${id}`}
      aria-describedby={`cv-desc-${id}`}
    >
      <NodeResizer
        isVisible={!!selected}
        minWidth={120}
        minHeight={64}
        onResizeEnd={(_e, p) => writeGeom(p.x, p.y, p.width, p.height)}
      />
      <span id={`cv-desc-${id}`} className="visually-hidden">
        {NODE_LABEL[node.kind]} {node.id}, stage {node.stage || "plan level"}
      </span>
      <Handle type="target" position={Position.Left} className="cv-handle cv-handle-target" />
      <div className="cv-card-strip">
        <span className="cv-kind-glyph" aria-hidden="true">
          {NODE_GLYPH[node.kind]}
        </span>
        <span className="cv-card-kind">{NODE_LABEL[node.kind]}</span>
        {status ? <span className="cv-card-status">{String(status)}</span> : null}
      </div>
      <div className="cv-card-title" id={`cv-title-${id}`}>
        {humanTitle(node.title) || <span className="doc-untitled">Untitled plan item</span>}
      </div>
      {node.body ? (
        <div className={`cv-card-body${expanded ? " is-expanded" : ""}`}>{node.body}</div>
      ) : null}
      {node.body && node.body.length > 120 ? (
        <button
          type="button"
          className="cv-card-more"
          onClick={(e) => {
            e.stopPropagation();
            setExpanded((v) => !v);
          }}
        >
          {expanded ? "Show less" : "⋯"}
        </button>
      ) : null}
      <Handle type="source" position={Position.Right} className="cv-handle cv-handle-source" />
      <span className="cv-presence-gutter" aria-hidden="true" />
    </div>
  );
}

// A region rendered as a frame with a 32px title strip, or — when it carries an
// annotation shape — as that shape drawn in an inset SVG. Freehand stores a
// polyline of integer points in attrs.points.
export function RegionNode({ data, selected, id }: NodeProps) {
  const { node, derived } = data as { node: PlanNode; derived?: boolean };
  const writeGeom = useGeomWriter(node, derived);
  const shape = (node.attrs?.["shape"] as string) ?? "frame";
  const w = node.geometry?.w ?? 240;
  const h = node.geometry?.h ?? 160;
  const orphaned = node.attrs?.["anchor_state"] === "orphaned";

  const renderShape = () => {
    if (shape === "frame") return null;
    const stroke = "var(--accent)";
    if (shape === "rectangle") {
      return <rect x={1} y={1} width={w - 2} height={h - 2} rx={6} fill="none" stroke={stroke} strokeWidth={1.5} />;
    }
    if (shape === "ellipse") {
      return <ellipse cx={w / 2} cy={h / 2} rx={w / 2 - 2} ry={h / 2 - 2} fill="none" stroke={stroke} strokeWidth={1.5} />;
    }
    if (shape === "arrow") {
      return (
        <line
          x1={4}
          y1={h - 4}
          x2={w - 6}
          y2={6}
          stroke={stroke}
          strokeWidth={2}
          markerEnd="url(#cv-arrowhead)"
        />
      );
    }
    if (shape === "freehand") {
      const pts = (node.attrs?.["points"] as number[][]) ?? [];
      const d = pts.map((p, i) => `${i === 0 ? "M" : "L"}${p[0]},${p[1]}`).join(" ");
      return <path d={d} fill="none" stroke={stroke} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />;
    }
    return null;
  };

  return (
    <div
      className={`cv-region cv-region-${shape}${selected ? " is-selected" : ""}${orphaned ? " is-orphaned" : ""}`}
      role="group"
      aria-label={`${shape === "frame" ? "Region" : shape + " annotation"} ${node.id}`}
    >
      <NodeResizer
        isVisible={!!selected}
        minWidth={64}
        minHeight={48}
        onResizeEnd={(_e, p) => writeGeom(p.x, p.y, p.width, p.height)}
      />
      {shape === "frame" ? (
        <>
          <div className="cv-region-strip">
            <span className="cv-region-glyph" aria-hidden="true">
              ▢
            </span>
            <span className="cv-region-title" id={`cv-title-${id}`}>
              {node.title || "Region"}
            </span>
          </div>
          <Handle type="target" position={Position.Left} className="cv-handle cv-handle-target" />
          <Handle type="source" position={Position.Right} className="cv-handle cv-handle-source" />
        </>
      ) : (
        <svg className="cv-annotation-svg" width={w} height={h} viewBox={`0 0 ${w} ${h}`} aria-hidden="true">
          <defs>
            <marker id="cv-arrowhead" markerWidth={10} markerHeight={10} refX={8} refY={3} orient="auto">
              <path d="M0,0 L8,3 L0,6 Z" fill="var(--accent)" />
            </marker>
          </defs>
          {renderShape()}
        </svg>
      )}
    </div>
  );
}

export const canvasNodeTypes = { card: CardNode, region: RegionNode };
