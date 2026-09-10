import { useRef, useState } from "react";
import { useReactFlow } from "@xyflow/react";
import { useActions } from "../../state/store";
import { useCanvasOps } from "./ops";
import type { Rect } from "../../lib/geometry";

export type CanvasTool = "select" | "hand" | "rectangle" | "ellipse" | "arrow" | "freehand" | "frame" | "connector" | "comment";

// A transparent overlay above the flow that captures pointer drawing when a
// shape tool is active. On release it converts the screen path to flow
// coordinates, creates the annotation as a region patch, and opens the
// anchoring thread composer. When the select/hand tool is active it is
// pointer-transparent so the flow handles the gesture.
export function AnnotationLayer({ tool, onDone }: { tool: CanvasTool; onDone: () => void }) {
  const { screenToFlowPosition } = useReactFlow();
  const ops = useCanvasOps();
  const actions = useActions();
  const layerRef = useRef<HTMLDivElement>(null);
  const [drawing, setDrawing] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null);
  const [freehand, setFreehand] = useState<number[][]>([]);

  const drawingTool = ["rectangle", "ellipse", "arrow", "freehand", "frame"].includes(tool);
  if (!drawingTool) return null;

  const localPoint = (e: React.PointerEvent) => {
    const rect = layerRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };

  const onPointerDown = (e: React.PointerEvent) => {
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    const p = localPoint(e);
    setDrawing({ x0: p.x, y0: p.y, x1: p.x, y1: p.y });
    if (tool === "freehand") setFreehand([[e.clientX, e.clientY]]);
  };
  const onPointerMove = (e: React.PointerEvent) => {
    if (!drawing) return;
    const p = localPoint(e);
    setDrawing((d) => (d ? { ...d, x1: p.x, y1: p.y } : d));
    if (tool === "freehand") setFreehand((pts) => [...pts, [e.clientX, e.clientY]]);
  };
  const onPointerUp = async (e: React.PointerEvent) => {
    if (!drawing) {
      onDone();
      return;
    }
    // Convert both corners from screen to flow coordinates.
    const rect = layerRef.current!.getBoundingClientRect();
    const a = screenToFlowPosition({ x: rect.left + drawing.x0, y: rect.top + drawing.y0 });
    const b = screenToFlowPosition({ x: rect.left + drawing.x1, y: rect.top + drawing.y1 });
    const flowRect: Rect = {
      x: Math.min(a.x, b.x),
      y: Math.min(a.y, b.y),
      w: Math.abs(b.x - a.x),
      h: Math.abs(b.y - a.y),
    };
    void e;
    let id: string | null = null;
    if (tool === "frame") {
      id = await ops.createAnnotation("frame", flowRect);
    } else if (tool === "freehand") {
      const flowPts = freehand.map((p) => {
        const fp = screenToFlowPosition({ x: p[0]!, y: p[1]! });
        return [fp.x, fp.y];
      });
      id = await ops.createAnnotation("freehand", flowRect, flowPts);
    } else {
      id = await ops.createAnnotation(
        tool as "rectangle" | "ellipse" | "arrow",
        flowRect,
      );
    }
    setDrawing(null);
    setFreehand([]);
    if (id) {
      if (tool === "frame") {
        actions.select(id, "region");
      } else {
        // Annotation shapes exist to carry feedback, so open their composer
        // only after the region itself has been durably saved.
        setTimeout(
          () => actions.openOverlay({ kind: "newComment", nodeId: id! }),
          0,
        );
      }
    }
    onDone();
  };

  return (
    <div
      ref={layerRef}
      className="annotation-layer"
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      role="presentation"
    >
      {drawing ? (
        <svg className="annotation-preview" width="100%" height="100%">
          {tool === "ellipse" ? (
            <ellipse
              cx={(drawing.x0 + drawing.x1) / 2}
              cy={(drawing.y0 + drawing.y1) / 2}
              rx={Math.abs(drawing.x1 - drawing.x0) / 2}
              ry={Math.abs(drawing.y1 - drawing.y0) / 2}
              fill="none"
              stroke="var(--accent)"
              strokeWidth={1.5}
            />
          ) : tool === "arrow" ? (
            <line x1={drawing.x0} y1={drawing.y0} x2={drawing.x1} y2={drawing.y1} stroke="var(--accent)" strokeWidth={2} />
          ) : tool === "freehand" ? (
            <path
              d={freehand
                .map((p, i) => {
                  const rect = layerRef.current!.getBoundingClientRect();
                  return `${i === 0 ? "M" : "L"}${p[0]! - rect.left},${p[1]! - rect.top}`;
                })
                .join(" ")}
              fill="none"
              stroke="var(--accent)"
              strokeWidth={2}
            />
          ) : (
            <rect
              x={Math.min(drawing.x0, drawing.x1)}
              y={Math.min(drawing.y0, drawing.y1)}
              width={Math.abs(drawing.x1 - drawing.x0)}
              height={Math.abs(drawing.y1 - drawing.y0)}
              fill="rgba(50,85,255,0.08)"
              stroke="var(--accent)"
              strokeWidth={1.5}
              rx={6}
            />
          )}
        </svg>
      ) : null}
    </div>
  );
}
