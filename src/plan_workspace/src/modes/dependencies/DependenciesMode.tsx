import { useMemo, useState } from "react";
import {
  Background,
  BackgroundVariant,
  MiniMap,
  Panel,
  ReactFlow,
  ReactFlowProvider,
  type Edge,
  type Node,
} from "@xyflow/react";
import { ModeLayout } from "../../shell/ModeLayout";
import { readableStage, useActions, useApp } from "../../state/store";
import type { EdgeKind, NodeKind, Role } from "../../api/types";
import { NODE_GLYPH, NODE_LABEL, EDGE_GLYPH } from "../../lib/selection";
import { Chip, SegmentedControl } from "../../shell/ui";
import { SealedStagePanel, UnwrittenStagePanel } from "../../shell/StatePanels";
import { dagreLayout } from "./layout";
import { computeImpact, findCycles } from "./graphAlgo";

const DEP_EDGE_KINDS: EdgeKind[] = ["depends_on", "blocks", "refines", "contains", "validates"];
const ALL_DISPLAY_KINDS: EdgeKind[] = [...DEP_EDGE_KINDS, "diagram_edge"];

function DepNode({ data }: { data: { label: string; kind: NodeKind; shade: string; cycle: boolean } }) {
  return (
    <div className={`dep-node dep-shade-${data.shade}${data.cycle ? " dep-cycle" : ""}`}>
      <span className="dep-node-glyph" aria-hidden="true">
        {NODE_GLYPH[data.kind]}
      </span>
      <span className="dep-node-label">{data.label}</span>
    </div>
  );
}

const depNodeTypes = { dep: DepNode };

function DependenciesGraph({
  edgeKinds,
  direction,
  setDirection,
}: {
  edgeKinds: EdgeKind[];
  direction: "TB" | "LR";
  setDirection: (d: "TB" | "LR") => void;
}) {
  const state = useApp();
  const actions = useActions();
  const selectedId = state.selection[0]?.id;
  const kindSet = useMemo(() => new Set(edgeKinds), [edgeKinds]);

  const { nodes, edges } = useMemo(() => {
    const displayEdges = Object.values(state.edges).filter((e) => kindSet.has(e.kind));
    const nodeIds = new Set<string>();
    for (const e of displayEdges) {
      nodeIds.add(e.from);
      nodeIds.add(e.to);
    }
    const planNodes = Object.values(state.nodes).filter((n) => nodeIds.has(n.id) && n.kind !== "thread");
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
        data: { label: n.title || n.id, kind: n.kind, shade, cycle: cycleNodes.has(n.id) },
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
      className: `edge-kind-${e.kind}${cycleNodes.has(e.from) && cycleNodes.has(e.to) ? " edge-cycle" : ""}`,
    }));
    return { nodes: flowNodes, edges: flowEdges };
  }, [state.nodes, state.edges, kindSet, direction, selectedId, state.selection]);

  return (
    <div className="deps-wrap">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={depNodeTypes}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_e, n) => actions.select(n.id, "node")}
        fitView
        minZoom={0.1}
        maxZoom={4}
        aria-label="Dependency graph"
      >
        <Background variant={BackgroundVariant.Dots} gap={16} />
        <MiniMap position="bottom-right" ariaLabel="Dependencies minimap" />
        <Panel position="top-left">
          <div className="deps-toolbar">
            <SegmentedControl
              ariaLabel="Layout direction"
              value={direction}
              onChange={setDirection}
              options={[
                { value: "TB", label: "Top-down" },
                { value: "LR", label: "Left-right" },
              ]}
            />
            <span className="deps-view-note">Auto-arranged view · not saved</span>
          </div>
        </Panel>
      </ReactFlow>
    </div>
  );
}

export function DependenciesMode() {
  const state = useApp();
  const actions = useActions();
  const status = readableStage(state.stages, state.stage);
  const [direction, setDirection] = useState<"TB" | "LR">("TB");
  const [edgeKinds, setEdgeKinds] = useState<EdgeKind[]>(ALL_DISPLAY_KINDS);
  const [roleContext, setRoleContext] = useState<Role | "">(state.role || "");

  const kindSet = useMemo(() => new Set(edgeKinds), [edgeKinds]);
  const cycles = useMemo(() => findCycles(Object.values(state.edges), kindSet), [state.edges, kindSet]);

  const toggleKind = (k: EdgeKind) =>
    setEdgeKinds((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));

  const nodeKinds: NodeKind[] = ["goal", "task", "criterion", "risk", "decision", "directive"];

  const left = (
    <div className="deps-filter">
      <section className="filter-group">
        <h3 className="filter-heading">Edge kinds</h3>
        <div className="filter-chips">
          {ALL_DISPLAY_KINDS.map((k) => (
            <Chip key={k} glyph={EDGE_GLYPH[k]} onClick={() => toggleKind(k)} active={edgeKinds.includes(k)}>
              {k}
            </Chip>
          ))}
        </div>
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">Node kinds</h3>
        <div className="filter-chips">
          {nodeKinds.map((k) => (
            <Chip key={k} glyph={NODE_GLYPH[k]}>
              {NODE_LABEL[k]}
            </Chip>
          ))}
        </div>
      </section>
      <section className="filter-group">
        <h3 className="filter-heading">Role context</h3>
        <SegmentedControl
          ariaLabel="Role context"
          value={roleContext || "engineer"}
          onChange={(v) => setRoleContext(v as Role)}
          options={[
            { value: "engineer", label: "As engineer" },
            { value: "tester", label: "As tester" },
            { value: "architect", label: "As architect" },
          ]}
        />
        {roleContext === "tester" && state.stages.sealed.length ? (
          <div className="deps-sealed-inline">
            <SealedStagePanel stage={state.stages.sealed[0]!} />
          </div>
        ) : null}
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
              <span aria-hidden="true">{EDGE_GLYPH[k]}</span> {k}
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
        <DependenciesGraph edgeKinds={edgeKinds} direction={direction} setDirection={setDirection} />
      </ReactFlowProvider>
    );

  return (
    <ModeLayout left={left} leftTitle="Filter" right={right} rightTitle="Legend">
      {body}
    </ModeLayout>
  );
}
