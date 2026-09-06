import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  useNodesState,
  useReactFlow,
  type Edge,
  type Node,
  type OnConnect,
  type OnConnectEnd,
  type OnConnectStart,
} from "@xyflow/react";
import { ModeLayout } from "../../shell/ModeLayout";
import { RightPanel } from "../shared/RightPanel";
import { readableStage, useActions, useApp } from "../../state/store";
import { SealedStagePanel, UnwrittenStagePanel } from "../../shell/StatePanels";
import { canvasNodeTypes } from "./nodes";
import { NEW_NODE_TARGETS, toFlowEdges, toFlowNodes, type FlowNodeData } from "./canvasModel";
import { useCanvasOps } from "./ops";
import { ContextualToolbar } from "./ContextualToolbar";
import { ZoomControl } from "./ZoomControl";
import { AnnotationLayer, type CanvasTool } from "./AnnotationLayer";
import { Layers } from "./Layers";
import type { NodeKind } from "../../api/types";
import { NODE_LABEL } from "../../lib/selection";

const TOOLS: { tool: CanvasTool; glyph: string; label: string; chord: string }[] = [
  { tool: "select", glyph: "▲", label: "Select", chord: "V" },
  { tool: "hand", glyph: "✋", label: "Hand (pan)", chord: "H" },
  { tool: "frame", glyph: "▢", label: "Frame", chord: "F" },
  { tool: "rectangle", glyph: "▭", label: "Rectangle", chord: "R" },
  { tool: "ellipse", glyph: "◯", label: "Ellipse", chord: "O" },
  { tool: "arrow", glyph: "↗", label: "Arrow", chord: "A" },
  { tool: "freehand", glyph: "✎", label: "Freehand", chord: "P" },
  { tool: "connector", glyph: "⇢", label: "Connector", chord: "L" },
];

function CanvasInner() {
  const state = useApp();
  const actions = useActions();
  const ops = useCanvasOps();
  const rf = useReactFlow();
  const [tool, setTool] = useState<CanvasTool>("select");
  const [snap, setSnap] = useState(true);
  const [grid, setGrid] = useState(true);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node<FlowNodeData>>([]);
  const draggingRef = useRef(false);
  const connectFrom = useRef<string | null>(null);
  const [newNodeMenu, setNewNodeMenu] = useState<{ x: number; y: number; fromId: string; kinds: NodeKind[]; flow: { x: number; y: number } } | null>(null);

  const selectedIds = useMemo(() => new Set(state.selection.map((s) => s.id)), [state.selection]);
  const flowEdges: Edge[] = useMemo(
    () => toFlowEdges(state.edges, state.nodes, selectedIds),
    [state.edges, state.nodes, selectedIds],
  );

  // Sync nodes from the server graph whenever it advances or selection changes,
  // unless a drag is in flight (which would clobber live positions).
  useEffect(() => {
    if (draggingRef.current) return;
    setNodes(toFlowNodes(state.nodes, selectedIds));
  }, [state.revision, state.nodes, selectedIds, setNodes]);

  const onConnect = useCallback<OnConnect>(
    (conn) => {
      if (conn.source && conn.target) ops.connect(conn.source, conn.target);
    },
    [ops],
  );

  const onConnectStart = useCallback<OnConnectStart>((_e, params) => {
    connectFrom.current = params.nodeId ?? null;
  }, []);

  const onConnectEnd = useCallback<OnConnectEnd>(
    (event, connectionState) => {
      const fromId = connectFrom.current;
      connectFrom.current = null;
      if (!fromId) return;
      // Dropped on empty canvas (no valid target) -> new-node-from-edge menu.
      if (connectionState && !connectionState.isValid) {
        const pt = "changedTouches" in event ? event.changedTouches[0]! : (event as MouseEvent);
        const fromKind = state.nodes[fromId]?.kind ?? "task";
        const kinds = NEW_NODE_TARGETS[fromKind] ?? (["task"] as NodeKind[]);
        const flow = rf.screenToFlowPosition({ x: pt.clientX, y: pt.clientY });
        setNewNodeMenu({ x: pt.clientX, y: pt.clientY, fromId, kinds, flow });
      }
    },
    [rf, state.nodes],
  );

  const commitDrag = useCallback(() => {
    // Only the nodes that took part in the drag: xyflow selects a node on drag,
    // and a multi-selection drags together. Committing all nodes would reconcile
    // containment for unrelated, unmoved nodes.
    const moves = rf
      .getNodes()
      .filter((n) => n.selected)
      .map((n) => ({ id: n.id, x: n.position.x, y: n.position.y }));
    if (moves.length) ops.dragCommit(moves);
    draggingRef.current = false;
  }, [rf, ops]);

  // Map xyflow selection back into the shared store (drives inspector + crumb).
  const syncSelection = useCallback(
    (params: { nodes: Node[]; edges: Edge[] }) => {
      const items = [
        ...params.nodes.map((n) => ({
          id: n.id,
          type: (state.nodes[n.id]?.kind === "region" ? "region" : "node") as "node" | "region",
        })),
        ...params.edges.map((e) => ({ id: e.id, type: "edge" as const })),
      ];
      const cur = state.selection.map((s) => s.id).sort().join(",");
      const next = items.map((s) => s.id).sort().join(",");
      if (cur !== next) actions.setSelection(items);
    },
    [actions, state.nodes, state.selection],
  );

  // Canvas keyboard chords (single-key + a few cmd chords not in the shell).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      const cmd = e.metaKey || e.ctrlKey;
      const k = e.key.toLowerCase();

      if (cmd && k === "d") {
        e.preventDefault();
        ops.duplicate();
        return;
      }
      if (cmd && k === "g") {
        e.preventDefault();
        if (e.shiftKey) ops.ungroup();
        else ops.group();
        return;
      }
      if (cmd && e.key === "[") {
        e.preventDefault();
        ops.zOrder("back");
        return;
      }
      if (cmd && e.key === "]") {
        e.preventDefault();
        ops.zOrder("front");
        return;
      }
      if (cmd) return; // leave remaining cmd chords to the shell

      if (e.key === "[") return ops.zOrder("backward");
      if (e.key === "]") return ops.zOrder("forward");
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        ops.remove();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        const step = e.shiftKey ? 8 : e.metaKey ? 32 : 1;
        const dx = e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0;
        const dy = e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0;
        ops.nudge(dx, dy);
        return;
      }
      if (e.key === "+" || e.key === "=") return void rf.zoomIn();
      if (e.key === "-") return void rf.zoomOut();
      if (e.key === "!" || (e.shiftKey && e.key === "1")) return void rf.fitView({ duration: 200, padding: 0.2 });
      if (e.key === ".") return setGrid((g) => !g);
      if (e.key === ",") return setSnap((s) => !s);
      const t = TOOLS.find((x) => x.chord.toLowerCase() === k);
      if (t) setTool(t.tool);
      if (k === "c") {
        const sel = state.selection[0];
        if (sel) actions.openOverlay({ kind: "newComment", nodeId: sel.id });
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [ops, rf, state.selection, actions]);

  const nodeCount = Object.values(state.nodes).filter((n) => n.geometry).length;

  return (
    <div className={`canvas-wrap tool-${tool}`}>
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        nodeTypes={canvasNodeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStart={() => {
          draggingRef.current = true;
        }}
        onNodeDragStop={commitDrag}
        onConnect={onConnect}
        onConnectStart={onConnectStart}
        onConnectEnd={onConnectEnd}
        onSelectionChange={syncSelection}
        snapToGrid={snap}
        snapGrid={[8, 8]}
        selectionOnDrag={tool === "select"}
        panOnDrag={tool === "hand" ? [0, 1, 2] : [1, 2]}
        panActivationKeyCode="Space"
        deleteKeyCode={null}
        minZoom={0.1}
        maxZoom={4}
        fitView
        onMove={(_e, vp) => {
          if (vp.zoom < 0.33 && nodeCount > 60) actions.notice("Simplified rendering at low zoom.");
        }}
        aria-label="Canvas"
      >
        {grid ? <Background variant={BackgroundVariant.Lines} gap={[8, 8]} /> : null}
        <MiniMap position="bottom-right" pannable zoomable ariaLabel="Canvas minimap" />
        <ContextualToolbar />
        <Panel position="top-left">
          <div className="tool-palette" role="toolbar" aria-label="Canvas tools">
            {TOOLS.map((t) => (
              <button
                key={t.tool}
                type="button"
                className={`tool-btn${tool === t.tool ? " is-active" : ""}`}
                aria-pressed={tool === t.tool}
                title={`${t.label} (${t.chord})`}
                aria-label={t.label}
                onClick={() => setTool(t.tool)}
              >
                <span aria-hidden="true">{t.glyph}</span>
              </button>
            ))}
            <span className="tool-divider" />
            <button
              type="button"
              className={`tool-btn${snap ? " is-active" : ""}`}
              aria-pressed={snap}
              title="Snapping (,)"
              onClick={() => setSnap((s) => !s)}
            >
              <span aria-hidden="true">⌗</span>
            </button>
            <button
              type="button"
              className={`tool-btn${grid ? " is-active" : ""}`}
              aria-pressed={grid}
              title="Grid (.)"
              onClick={() => setGrid((g) => !g)}
            >
              <span aria-hidden="true">▦</span>
            </button>
            <button type="button" className="tool-btn tool-tidy" title="Auto-layout" onClick={() => void ops.autoLayout("TB")}>
              Tidy
            </button>
          </div>
        </Panel>
        <Panel position="bottom-left">
          <ZoomControl />
        </Panel>
      </ReactFlow>

      <AnnotationLayer tool={tool} onDone={() => setTool("select")} />

      {newNodeMenu ? (
        <div
          className="new-node-menu"
          style={{ left: newNodeMenu.x, top: newNodeMenu.y }}
          role="menu"
          aria-label="New node from this edge"
        >
          <div className="nnm-title">New node from this edge</div>
          {newNodeMenu.kinds.map((kind) => (
            <button
              key={kind}
              type="button"
              role="menuitem"
              className="nnm-item"
              onClick={() => {
                ops.newNodeFromEdge(newNodeMenu.fromId, kind, newNodeMenu.flow);
                setNewNodeMenu(null);
              }}
            >
              {NODE_LABEL[kind]}
            </button>
          ))}
          <button type="button" className="nnm-cancel" onClick={() => setNewNodeMenu(null)}>
            Cancel
          </button>
        </div>
      ) : null}
    </div>
  );
}

export function CanvasMode() {
  const state = useApp();
  const status = readableStage(state.stages, state.stage);

  const body =
    status === "sealed" ? (
      <div className="canvas-wrap">
        <SealedStagePanel stage={state.stage} />
      </div>
    ) : status === "unwritten" ? (
      <div className="canvas-wrap">
        <UnwrittenStagePanel stage={state.stage} />
      </div>
    ) : (
      <ReactFlowProvider>
        <CanvasInner />
      </ReactFlowProvider>
    );

  return (
    <ModeLayout
      left={<Layers />}
      leftTitle="Layers"
      right={<RightPanel order={["inspector", "comments", "agents"]} />}
      rightTitle="Inspector"
    >
      {body}
    </ModeLayout>
  );
}
