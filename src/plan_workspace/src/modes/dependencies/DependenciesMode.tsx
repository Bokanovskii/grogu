import { useEffect, useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Panel,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import { ModeLayout } from "../../shell/ModeLayout";
import { readableStage, useActions, useApp } from "../../state/store";
import { stageLabel, type EdgeKind, type NodeKind, type Stage } from "../../api/types";
import { NODE_GLYPH, NODE_LABEL, EDGE_GLYPH } from "../../lib/selection";
import { Chip, SegmentedControl } from "../../shell/ui";
import { SealedStagePanel, UnwrittenStagePanel } from "../../shell/StatePanels";
import { dagreLayout } from "./layout";
import { computeImpact, findCycles } from "./graphAlgo";
import { HUMAN_EDGE_LABEL, humanTitle, markdownSummary } from "../../lib/humanize";
import { RightPanel } from "../shared/RightPanel";

const DEP_EDGE_KINDS: EdgeKind[] = ["depends_on", "blocks", "refines", "contains", "validates"];
const ALL_DISPLAY_KINDS: EdgeKind[] = [...DEP_EDGE_KINDS, "diagram_edge"];

function DepNode({
  data,
}: {
  data: {
    label: string;
    kind: NodeKind;
    shade: string;
    cycle: boolean;
    direction: "TB" | "LR";
    id: string;
    summary: string;
    stage: Stage;
  };
}) {
  const horizontal = data.direction === "LR";
  return (
    <div
      className={`dep-node dep-shade-${data.shade}${
        data.cycle ? " dep-cycle" : ""
      }`}
    >
      <Handle
        type="target"
        position={horizontal ? Position.Left : Position.Top}
        className="dep-handle"
      />
      <span className="dep-node-glyph" aria-hidden="true">
        {NODE_GLYPH[data.kind]}
      </span>
      <span className="dep-node-copy">
        <span className="dep-node-label">{data.label}</span>
        <span className="dep-node-summary">{data.summary}</span>
        <span className="dep-node-meta">
          {NODE_LABEL[data.kind]} · {stageLabel(data.stage)}
        </span>
      </span>
      <Handle
        type="source"
        position={horizontal ? Position.Right : Position.Bottom}
        className="dep-handle"
      />
    </div>
  );
}

const depNodeTypes = { dep: DepNode };

function DependenciesGraph({
  edgeKinds,
  nodeKinds,
  scope,
  direction,
  setDirection,
}: {
  edgeKinds: EdgeKind[];
  nodeKinds: NodeKind[];
  scope: "stage" | "all";
  direction: "TB" | "LR";
  setDirection: (d: "TB" | "LR") => void;
}) {
  const state = useApp();
  const actions = useActions();
  const flow = useReactFlow();
  const selectedId = state.selection[0]?.id;
  const kindSet = useMemo(() => new Set(edgeKinds), [edgeKinds]);
  const nodeKindSet = useMemo(() => new Set(nodeKinds), [nodeKinds]);

  const { nodes, edges } = useMemo(() => {
    const planNodes = Object.values(state.nodes).filter(
      (node) =>
        (scope === "all" || node.stage === state.stage) &&
        node.kind !== "thread" &&
        nodeKindSet.has(node.kind),
    );
    const nodeIds = new Set(planNodes.map((node) => node.id));
    const displayEdges = Object.values(state.edges).filter(
      (edge) =>
        kindSet.has(edge.kind) &&
        nodeIds.has(edge.from) &&
        nodeIds.has(edge.to),
    );
    const arranged = dagreLayout(planNodes, displayEdges, direction);
    const impact = selectedId ? computeImpact(selectedId, displayEdges, kindSet) : null;
    const cyc = findCycles(displayEdges, kindSet);
    const cycleNodes = new Set(cyc.flat());

    const flowNodes: Node[] = planNodes.map((n) => {
      let shade = "none";
      if (selectedId === n.id) shade = "self";
      else if (impact?.direct.has(n.id)) shade = "direct";
      else if (impact?.transitive.has(n.id)) shade = "transitive";
      return {
        id: n.id,
        type: "dep",
        position: arranged.positions[n.id] ?? { x: 0, y: 0 },
        data: {
          label: humanTitle(n.title) || "Untitled plan item",
          kind: n.kind,
          shade,
          cycle: cycleNodes.has(n.id),
          direction,
          summary: markdownSummary(n.body || n.title, 145),
          id: n.id,
          stage: n.stage,
        },
        selected: state.selection.some((s) => s.id === n.id),
        draggable: false,
        connectable: false,
      };
    });
    const flowEdges: Edge[] = displayEdges.map((e) => ({
      id: e.id,
      source: e.from,
      target: e.to,
      type: e.kind === "diagram_edge" ? "straight" : "smoothstep",
      label: HUMAN_EDGE_LABEL[e.kind] ?? "relates to",
      labelStyle: { fontSize: 11, fontWeight: 600 },
      labelBgPadding: [4, 2],
      labelBgBorderRadius: 3,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
      className: `edge-kind-${e.kind}${
        selectedId && (e.from === selectedId || e.to === selectedId)
          ? " edge-highlight"
          : ""
      }${cycleNodes.has(e.from) && cycleNodes.has(e.to) ? " edge-cycle" : ""}`,
    }));
    return { nodes: flowNodes, edges: flowEdges };
  }, [
    state.nodes,
    state.edges,
    state.stage,
    scope,
    kindSet,
    nodeKindSet,
    direction,
    selectedId,
    state.selection,
  ]);

  useEffect(() => {
    const frame = window.requestAnimationFrame(() => {
      void flow
        .fitView({ padding: 0.12, minZoom: 0.65, maxZoom: 1.1, duration: 0 })
        .then(() => {
          const viewport = flow.getViewport();
          void flow.setViewport(
            {
              ...viewport,
              y: direction === "LR" ? 96 : 72,
              zoom: Math.max(0.65, viewport.zoom),
            },
            { duration: 140 },
          );
        });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [flow, direction, state.stage, scope, kindSet, nodeKindSet]);

  return (
    <div className="deps-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={depNodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_e, n) => {
          actions.select(n.id, "node");
          actions.patchPanels({ rightTab: "inspector" });
        }}
        fitView
        fitViewOptions={{ padding: 0.12, minZoom: 0.65, maxZoom: 1.1 }}
        minZoom={0.1}
        maxZoom={4}
        aria-label="Dependency graph"
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="var(--canvas-grid)"
        />
        <Panel position="top-left">
          <div className="deps-toolbar">
            <SegmentedControl
              ariaLabel="Layout direction"
              value={direction}
              onChange={setDirection}
              options={[
                { value: "LR", label: "Left-right" },
                { value: "TB", label: "Top-down" },
              ]}
            />
            <span className="deps-view-note">Auto-arranged view · not saved</span>
          </div>
        </Panel>
        {nodes.length === 0 ? (
          <Panel position="top-center">
            <div className="deps-empty">
              No {stageLabel(state.stage).toLowerCase()} nodes match these filters.
            </div>
          </Panel>
        ) : null}
      </ReactFlow>
    </div>
  );
}

export function DependenciesMode() {
  const state = useApp();
  const actions = useActions();
  const status = readableStage(state.stages, state.stage);
  const [direction, setDirection] = useState<"TB" | "LR">("TB");
  const [edgeKinds, setEdgeKinds] = useState<EdgeKind[]>([
    "depends_on",
    "blocks",
    "validates",
  ]);
  const [scope, setScope] = useState<"stage" | "all">("stage");
  const nodeKinds: NodeKind[] = ["goal", "task", "criterion", "risk", "decision", "directive"];
  const [visibleNodeKinds, setVisibleNodeKinds] = useState<NodeKind[]>([
    "goal",
    "task",
    "risk",
    "decision",
  ]);

  const kindSet = useMemo(() => new Set(edgeKinds), [edgeKinds]);
  const nodeKindSet = useMemo(() => new Set(visibleNodeKinds), [visibleNodeKinds]);
  const stageNodeIds = useMemo(
    () =>
      new Set(
        Object.values(state.nodes)
          .filter(
            (node) =>
              (scope === "all" || node.stage === state.stage) &&
              node.kind !== "thread" &&
              nodeKindSet.has(node.kind),
          )
          .map((node) => node.id),
      ),
    [state.nodes, state.stage, scope, nodeKindSet],
  );
  const cycles = useMemo(
    () =>
      findCycles(
        Object.values(state.edges).filter(
          (edge) =>
            stageNodeIds.has(edge.from) &&
            stageNodeIds.has(edge.to),
        ),
        kindSet,
      ),
    [state.edges, stageNodeIds, kindSet],
  );

  const toggleKind = (k: EdgeKind) =>
    setEdgeKinds((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));
  const toggleNodeKind = (kind: NodeKind) =>
    setVisibleNodeKinds((current) =>
      current.includes(kind)
        ? current.filter((value) => value !== kind)
        : [...current, kind],
    );

  const left = (
    <div className="deps-filter">
      <section className="filter-group">
        <h3 className="filter-heading">Map detail</h3>
        <p className="deps-filter-help">
          Starts with the delivery roadmap. Add supporting criteria, directives,
          and clarification links only when you need them.
        </p>
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">Edge kinds</h3>
        <div className="filter-chips">
          {ALL_DISPLAY_KINDS.map((k) => (
            <Chip key={k} glyph={EDGE_GLYPH[k]} onClick={() => toggleKind(k)} active={edgeKinds.includes(k)}>
              {HUMAN_EDGE_LABEL[k] ?? k.replaceAll("_", " ")}
            </Chip>
          ))}
        </div>
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">Node kinds</h3>
        <div className="filter-chips">
          {nodeKinds.map((k) => (
            <Chip
              key={k}
              glyph={NODE_GLYPH[k]}
              onClick={() => toggleNodeKind(k)}
              active={visibleNodeKinds.includes(k)}
            >
              {NODE_LABEL[k]}
            </Chip>
          ))}
        </div>
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">View scope</h3>
        <SegmentedControl
          ariaLabel="Dependency view scope"
          value={scope}
          onChange={setScope}
          options={[
            { value: "stage", label: "This stage" },
            { value: "all", label: "All stages" },
          ]}
        />
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">Access scope</h3>
        <div className="deps-scope-note">
          <strong>{state.role || "reviewer"}</strong>
          <span>
            {scope === "stage"
              ? `${stageLabel(state.stage)} only.`
              : "All readable stages."}{" "}
            The server has already excluded anything this session role cannot
            read.
          </span>
        </div>
      </section>
    </div>
  );

  const right = (
    <div className="deps-right">
      <section className="legend">
        <h3 className="filter-heading">Legend</h3>
        <ul className="legend-list">
          {ALL_DISPLAY_KINDS.map((k) => (
            <li key={k}>
              <span aria-hidden="true">{EDGE_GLYPH[k]}</span> {HUMAN_EDGE_LABEL[k] ?? k.replaceAll("_", " ")}
            </li>
          ))}
          <li>
            <span className="dep-swatch dep-shade-self" /> selected
          </li>
          <li>
            <span className="dep-swatch dep-shade-direct" /> direct dependent
          </li>
          <li>
            <span className="dep-swatch dep-shade-transitive" /> transitive
          </li>
        </ul>
      </section>
      <section className="cycles-drawer">
        <h3 className="filter-heading">Cycles</h3>
        {cycles.length === 0 ? (
          <p className="inspector-muted">No cycles.</p>
        ) : (
          <ul className="cycles-list">
            {cycles.map((cyc, i) => (
              <li key={i} className="cycle-item">
                <span>
                  Cycle {i + 1} · {cyc.length - 1} nodes: {cyc.join(" → ")}
                </span>
                <button type="button" className="rel-link" onClick={() => actions.select(cyc[0]!, "node")}>
                  Reveal
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );

  const body =
    status === "sealed" ? (
      <div className="deps-wrap">
        <SealedStagePanel stage={state.stage} />
      </div>
    ) : status === "unwritten" ? (
      <div className="deps-wrap">
        <UnwrittenStagePanel stage={state.stage} />
      </div>
    ) : (
      <ReactFlowProvider>
        <DependenciesGraph
          edgeKinds={edgeKinds}
          nodeKinds={visibleNodeKinds}
          scope={scope}
          direction={direction}
          setDirection={setDirection}
        />
      </ReactFlowProvider>
    );

  return (
    <ModeLayout
      left={left}
      leftTitle="Map filters"
      right={
        state.selection.length ? (
          <RightPanel order={["inspector", "comments", "agents"]} round={1} changesRequested={false} />
        ) : (
          right
        )
      }
      rightTitle={state.selection.length ? "Plan item" : "How to read this map"}
      compact
    >
      {body}
    </ModeLayout>
  );
}
