// The p-fixture-heavy dataset from the design's "one representative complex
// plan" section. It exercises every mode, state and annotation kind. The
// fixture server mutates a deep copy of this initial state per process.

const REV0 = "r0031";

function node(id, kind, stage, title, body, extra = {}) {
  return {
    id,
    kind,
    stage,
    title,
    body,
    attrs: extra.attrs ?? {},
    ext: extra.ext ?? {},
    ...(extra.geometry ? { geometry: extra.geometry } : {}),
    order: extra.order ?? 1000,
    created_rev: extra.created_rev ?? "r0001",
    updated_rev: extra.updated_rev ?? REV0,
  };
}

function edge(id, kind, from, to, attrs = {}) {
  return { id, kind, from, to, attrs, created_rev: "r0007" };
}

export function buildInitialState() {
  const nodes = {};
  const edges = {};
  const add = (n) => (nodes[n.id] = n);
  const addE = (e) => (edges[e.id] = e);

  // Goal
  add(node("goal-1", "goal", "design", "Migrate storage backend without data loss",
    "Move the plan store to the typed graph while every existing reader keeps working.",
    { order: 100, geometry: { x: 40, y: 40, w: 300, h: 120, z: 1 } }));

  // Directives (6), mixed audiences
  const audiences = [["engineer"], ["engineer", "tester"], ["designer"], ["everyone"], ["architect"], ["reviewer"]];
  for (let i = 1; i <= 6; i++) {
    add(node(`dir-${i}`, "directive", "design", `Directive ${i}: keep compiled Markdown authoritative`,
      i === 2
        ? "Markdown is compiled, never authored. The graph is the only source of truth, and the compiled artifact is what every other consumer reads."
        : "An instruction the audience must carry out during this migration.",
      {
        order: 200 + i,
        attrs: { binding: i % 2 ? "must" : "should", audience: audiences[i - 1], status: i === 6 ? "withdrawn" : "active" },
        geometry: { x: 40 + i * 20, y: 200 + i * 10, w: 280, h: 100, z: 2 },
      }));
  }

  // Constraints, invariants, decisions
  for (let i = 1; i <= 3; i++)
    add(node(`con-${i}`, "constraint", "design", `Constraint ${i}: no new runtime dependency`, "Standard library only.", { order: 300 + i }));
  for (let i = 1; i <= 2; i++)
    add(node(`inv-${i}`, "invariant", "design", `Invariant ${i}: append-only history`, "Nothing is ever destroyed.", { order: 400 + i }));
  for (let i = 1; i <= 3; i++)
    add(node(`dec-${i}`, "decision", "design", `Decision ${i}: RFC 6902 patch`, "Chosen over merge patch; rejected alternatives recorded.", { order: 500 + i }));

  // Criteria (bound to tasks by validates)
  for (let i = 1; i <= 4; i++)
    add(node(`crit-${i}`, "criterion", "design", `Criterion ${i}: round-trips faithfully`, "parse(render(ir)) == ir.", {
      order: 600 + i, geometry: { x: 700, y: 40 + i * 140, w: 220, h: 100, z: 1 },
    }));

  // Tasks (8) forming a DAG with an intentional refactor cycle task-3<->task-4.
  const taskTitles = [
    "Design the graph schema", "Write the compiler", "Build the patch engine", "Wire the write protocol",
    "Add the revision log", "Implement selectors", "Impact analysis", "Markdown importer",
  ];
  for (let i = 1; i <= 8; i++)
    add(node(`task-${i}`, "task", "design", taskTitles[i - 1], `Unit of work ${i} with an order and dependencies.`, {
      order: 700 + i,
      attrs: { status: i <= 2 ? "in_progress" : "todo" },
      geometry: { x: 100 + ((i - 1) % 4) * 260, y: 420 + Math.floor((i - 1) / 4) * 180, w: 220, h: 120, z: 3 },
    }));
  add(node(
    "task-implementation",
    "task",
    "implementation",
    "Implementation-only canvas task",
    "This node verifies that each stage owns a distinct canvas.",
    {
      order: 700,
      attrs: { status: "todo" },
      geometry: { x: 120, y: 120, w: 260, h: 120, z: 2 },
    },
  ));

  // Diagrams (2), each with parsed sub-nodes and a diagram self-loop.
  for (let d = 1; d <= 2; d++) {
    add(node(`dia-${d}`, "diagram", "design", `Flow ${d}`,
      "```mermaid\nflowchart TD\n  Parse -->|retry| Parse\n  Parse --> Emit\n```",
      { order: 800 + d, geometry: { x: 100 + d * 300, y: 800, w: 260, h: 160, z: 1 } }));
    for (let k = 1; k <= 3; k++) add(node(`dia${d}n-${k}`, "note", "design", `Flow ${d} node ${k}`, "", { order: 810 + d * 10 + k }));
    addE(edge(`de-${d}-1`, "diagram_edge", `dia${d}n-1`, `dia${d}n-1`, { op: "retry", label: "retry" })); // self-loop
    addE(edge(`de-${d}-2`, "diagram_edge", `dia${d}n-1`, `dia${d}n-2`));
    addE(edge(`de-${d}-3`, "diagram_edge", `dia${d}n-2`, `dia${d}n-3`));
    addE(edge(`de-${d}-4`, "diagram_edge", `dia${d}n-3`, `dia${d}n-1`)); // cycle within the drawing
    addE(edge(`dcont-${d}`, "contains", `dia-${d}`, `dia${d}n-1`));
  }

  // Region frame grouping 6 tasks; sized so it clips 2 tasks (moved state).
  add(node("reg-1", "region", "design", "Storage tasks", "", {
    order: 900, attrs: { shape: "frame" }, geometry: { x: 80, y: 400, w: 560, h: 200, z: -1 },
  }));
  for (let i = 1; i <= 6; i++) addE(edge(`rcont-${i}`, "contains", "reg-1", `task-${i}`));

  // Annotation regions: rectangle, ellipse, arrow, freehand.
  add(node("reg-2", "region", "design", "rectangle annotation", "", { order: 950, attrs: { shape: "rectangle", anchor_state: "resolved" }, geometry: { x: 700, y: 640, w: 200, h: 120, z: 5 } }));
  add(node("reg-3", "region", "design", "ellipse annotation", "", { order: 951, attrs: { shape: "ellipse", anchor_state: "resolved" }, geometry: { x: 940, y: 640, w: 160, h: 120, z: 5 } }));
  add(node("reg-4", "region", "design", "arrow annotation", "", { order: 952, attrs: { shape: "arrow", anchor_state: "resolved" }, geometry: { x: 700, y: 800, w: 180, h: 100, z: 5 } }));
  add(node("reg-5", "region", "design", "freehand annotation", "", {
    order: 953,
    attrs: { shape: "freehand", anchor_state: "resolved", points: [[0, 60], [30, 10], [60, 70], [90, 20], [120, 60]] },
    geometry: { x: 940, y: 800, w: 140, h: 100, z: 5 },
  }));

  // Edges: depends_on DAG + the intentional cycle; validates; refines.
  addE(edge("edge-1", "depends_on", "task-2", "task-1"));
  addE(edge("edge-2", "depends_on", "task-3", "task-4")); // cycle
  addE(edge("edge-3", "depends_on", "task-4", "task-3")); // cycle
  addE(edge("edge-4", "depends_on", "task-5", "task-4"));
  addE(edge("edge-5", "depends_on", "task-6", "task-2"));
  addE(edge("edge-6", "depends_on", "task-7", "task-6"));
  addE(edge("edge-7", "depends_on", "task-8", "task-2"));
  addE(edge("edge-8", "blocks", "task-1", "task-8"));
  addE(edge("edge-9", "refines", "task-2", "goal-1"));
  for (let i = 1; i <= 4; i++) addE(edge(`ve-${i}`, "validates", `crit-${i}`, `task-${i}`));

  // Threads (as nodes).
  add(node("thr-1", "thread", "design", "Discussion on dir-2", "Should this cover the JSON twin too?", {
    order: 1000,
    attrs: { anchor_node: "dir-2", thread_kind: "discussion", status: "open", anchor_state: "resolved", round: 1 },
    ext: {
      "dev.grogu.thread": {
        kind: "discussion", status: "open", anchor_state: "resolved", round: 1,
        quote_context: "Markdown is compiled, never authored.",
        comments: [{ id: "c1", author: "charlie", role: "reviewer", body: "Should this cover the JSON twin too?", at: "2026-09-06T09:00:00Z" }],
      },
    },
  }));
  add(node("thr-2", "thread", "design", "Blocking question on task-3", "Is this task still needed after the rewrite?", {
    order: 1001,
    attrs: { anchor_node: "task-3", thread_kind: "blocking", status: "open", anchor_state: "orphaned", round: 1 },
    ext: {
      "dev.grogu.thread": {
        kind: "blocking", status: "open", anchor_state: "orphaned", round: 1,
        quote_context: "Design the graph schema",
        comments: [{ id: "c2", author: "charlie", role: "reviewer", body: "Is this task still needed after the rewrite?", at: "2026-09-06T09:05:00Z" }],
      },
    },
  }));
  add(node("thr-3", "thread", "design", "Composite annotation over tasks", "These four should move together.", {
    order: 1002,
    attrs: { anchor_node: "reg-2", thread_kind: "suggestion", status: "open", anchor_state: "shifted", round: 1 },
    ext: {
      "dev.grogu.thread": {
        kind: "suggestion", status: "open", anchor_state: "shifted", round: 1,
        anchor_last_exact: "r0028",
        quote_context: "task-1, task-2, task-5, task-6",
        resolved_members: 2, total_members: 3,
        comments: [{ id: "c3", author: "grogu", role: "grogu", body: "These four should move together.", at: "2026-09-06T09:10:00Z" }],
      },
    },
  }));

  const counters = { goal: 1, dir: 6, con: 3, inv: 2, dec: 3, crit: 4, task: 8, dia: 2, reg: 5, note: 6, thr: 3, edge: 20, qn: 0, ev: 0, ref: 0, risk: 0 };

  // Proposal derived from the composite thread, pending.
  const proposals = [
    {
      id: "pr-4", at: "2026-09-06T09:12:00Z", from_thread: "thr-3", base: REV0,
      why: "Move the four tasks together and tidy the region.",
      ops: [
        { op: "replace", path: "/nodes/task-1/title", value: "Design the graph schema (moved)" },
      ],
      status: "pending", decided_at: "", decided_by: "", why_not: "",
    },
  ];

  // Revisions with mixed origins.
  const revisions = [
    revEnv(31, "r0030", "add the dependency-impact criterion", "user", "charlie", "architect"),
    revEnv(30, "r0029", "rename task-3", "user", "charlie", "engineer"),
    revEnv(29, "r0028", "auto-layout canvas", "layout", "charlie", "engineer"),
    revEnv(28, "r0027", "undo rename", "undo", "charlie", "engineer"),
    revEnv(27, "r0026", "accept proposal pr-2", "proposal:pr-2", "grogu", "architect"),
    revEnv(1, "", "migrate legacy plan", "migration", "charlie", "architect"),
  ];

  // Agents — one per state badge plus a plain one.
  const now = Date.now();
  const agents = [
    agent("a-1", "engineer", "core", "p-fixture-heavy", "live", now - 12_000, { action: "applying patch", tool: "bash", rev: "r0031" }),
    agent("a-2", "tester", "plan-workspace", "p-fixture-heavy", "idle", now - 180_000, { action: "waiting on integration", rev: "r0030" }),
    agent("a-3", "engineer", "plan-workspace", "p-fixture-heavy", "stuck", now - 480_000, { action: "compiling markdown", tool: "bash", rev: "r0031", stuck_s: 300 }),
    agent("a-4", "architect", "", "p-fixture-heavy", "finished", now - 600_000, { action: "gate implement passed", rev: "r0031" }),
    agent("a-5", "reviewer", "integration", "p-fixture-heavy", "disconnected", now - 1_320_000, { action: "reviewing", rev: "r0029" }),
    agent("a-6", "designer", "", "p-fixture-heavy", "error", now - 90_000, { action: "budget exceeded", error: "budget exceeded", errcode: "budget_exceeded", rev: "r0031" }),
    agent("a-7", "engineer", "integration", "p-fixture-heavy", "waiting", now - 60_000, { action: "awaiting feedback", rev: "r0031", unread: 1 }),
  ];
  for (const a of agents) {
    a.session_key = `session-${a.agent_key}`;
    a.root_session_key = "session-a-4";
    a.parent_agent_key = a.agent_key === "a-4" ? null : "a-4";
    a.lineage_status = a.agent_key === "a-4" ? "root" : "recorded";
  }

  // Feedback (5) with delivery states + gate bindings.
  const feedback = [
    fb("f-01", { kind: "agent", agent_key: "a-1", label: "agent plan-v2-engineer-core" }, false, "Looks good, keep going.", "delivered", now - 300_000),
    fb("f-02", { kind: "role_plan", role: "engineer", plan: "p-fixture-heavy", label: "engineer on p-fixture-heavy" }, true, "Please fold in the amendment before the gate.", "acknowledged", now - 900_000, { gate: { stage: "implementation", state: "open" }, ack: "ev-ack-2" }),
    fb("f-03", { kind: "agent", agent_key: "a-3", label: "agent plan-v2-engineer-workspace" }, true, "Stop and rebase on r0031.", "delivered", now - 120_000, { gate: { stage: "implementation", state: "blocked" } }),
    fb("f-04", { kind: "role", role: "reviewer", label: "all reviewer" }, false, "Reviewers, please prioritise this plan.", "undeliverable", now - 200_000, { reason: "no reviewer bound" }),
    fb("f-05", { kind: "agent", agent_key: "a-7", label: "agent plan-v2-engineer-integration" }, true, "Never mind, withdrawn.", "withdrawn", now - 400_000),
  ];

  // Per-agent audit events + per-object activity.
  const auditByAgent = {};
  for (const a of agents) auditByAgent[a.agent_key] = agentEvents(a, now);
  const activityByObject = {
    "task-1": [{ id: "oa-1", type: "revision", at: now - 200_000, summary: "task-1 renamed at r0029", objects: { plan: "p-fixture-heavy", ids: ["task-1"] }, basis: "observed" }],
    "dir-2": [{ id: "oa-2", type: "patch", at: now - 500_000, summary: "dir-2 body edited", objects: { plan: "p-fixture-heavy", ids: ["dir-2"] }, basis: "observed" }],
  };

  return {
    plan: "p-fixture-heavy",
    title: "Migrate storage backend",
    role: "architect",
    HEAD: REV0,
    graph: { nodes, edges, counters },
    stages: { readable: ["design", "implementation"], sealed: ["testing", "evaluation"], written: { design: "designer", implementation: "engineer" } },
    proposals,
    revisions,
    agents,
    feedback,
    auditByAgent,
    activityByObject,
    sealed_edge_count: 3,
    opsByRevision: {},
  };
}

function revEnv(seq, parent, intent, origin, actor, role) {
  const rev = "r" + String(seq).padStart(4, "0");
  const at = new Date(Date.now() - (32 - seq) * 3600_000).toISOString();
  return {
    revision: rev, seq, parent, at, actor, role, agent: `plan-v2-${role}`,
    intent, origin, ops: [], before_digest: "sha256:" + "0".repeat(64), after_digest: "sha256:" + seq.toString(16).padStart(64, "0"),
    summary: intent,
  };
}

function agent(key, role, workstream, plan, badge, lastAt, o = {}) {
  const lifecycleMap = { live: "running", idle: "running", stuck: "running", finished: "finished", disconnected: "running", error: "failed", waiting: "running" };
  const connectionMap = { disconnected: "disconnected", idle: "live", live: "live", stuck: "live", finished: "live", error: "live", waiting: "live" };
  return {
    agent_key: key, agent: `plan-v2-${role}-${workstream || "root"}`, run_id: `run-${key}`,
    role, roles: [role], workstream, plan, plan_title: "Migrate storage backend",
    revision: { last_read: o.rev ?? "r0031", current: "r0031", relation: o.rev === "r0031" ? "current" : "behind" },
    lifecycle: lifecycleMap[badge], activity: badge === "stuck" ? "possibly_stuck" : badge === "idle" ? "quiet" : "active",
    connection: connectionMap[badge], badge,
    started_at: badge === "finished" ? lastAt - 3600_000 : lastAt - 600_000,
    last_observed_at: lastAt, elapsed_ms: 600_000, elapsed_basis: "lifecycle_start",
    current_action: o.action ? { tool_name: o.tool ?? o.action, phase: "started", at: lastAt, summary: null } : null,
    last_tool: o.tool ? { tool_name: o.tool, duration_ms: 340, outcome: "ok" } : null,
    tools: { started: 61, completed: 59, failed: badge === "error" ? 3 : 0, inflight: 1, complete: false },
    grogu_commands: { calls: 61, failures: badge === "error" ? 2 : 0, last_command: "plan gate" },
    failures: badge === "error" ? [{ code: "budget_exceeded", at: lastAt }] : [],
    blockers: badge === "stuck" ? ["no outcome for 900s"] : badge === "waiting" ? ["unread binding feedback"] : [],
    steering: { unread: o.unread ?? 0, delivered: 3, acknowledged: 2 },
    coverage: { lifecycle: "complete", tools: "partial", outcomes: badge === "disconnected" ? "unavailable" : "partial" },
    basis: "observed",
    ...(o.stuck_s ? { stuck_threshold_s: o.stuck_s } : {}),
    ...(o.error ? { last_error: o.error } : {}),
    ...(o.errcode ? { error_token: o.errcode } : {}),
  };
}

function agentEvents(a, now) {
  const ev = [];
  let t = a.last_observed_at;
  ev.push({ id: `${a.agent_key}-e1`, type: "role_bound", at: t - 500_000, summary: `role bound: ${a.role}`, basis: "observed" });
  ev.push({ id: `${a.agent_key}-e2`, type: "workstream_started", at: t - 480_000, summary: `workstream ${a.workstream || "root"} started`, basis: "observed" });
  ev.push({ id: `${a.agent_key}-e3`, type: "revision", at: t - 200_000, summary: `wrote r0031`, objects: { plan: a.plan, ids: ["task-1"] }, basis: "observed" });
  if (a.last_tool)
    ev.push({ id: `${a.agent_key}-e4`, type: "tool", at: t - 3000, summary: `${a.last_tool.tool_name} completed`, tool: a.last_tool, basis: "observed" });
  if (a.badge === "error")
    ev.push({ id: `${a.agent_key}-e5`, type: "error", at: t - 1000, summary: "budget exceeded", basis: "observed" });
  if (a.badge === "finished")
    ev.push({ id: `${a.agent_key}-e6`, type: "gate_passed", at: t - 1000, summary: "gate implement passed", basis: "observed" });
  if (a.badge === "disconnected")
    ev.push({ id: `${a.agent_key}-e7`, type: "disconnected", at: t, summary: "collector disconnected", basis: "observed" });
  void now;
  return ev.sort((x, y) => y.at - x.at);
}

function fb(id, scope, binding, text, state, at, o = {}) {
  const order = ["sent", "routed", "delivered", "acknowledged"];
  const history = [];
  const idx = order.indexOf(state);
  for (let i = 0; i <= idx && idx >= 0; i++) history.push({ state: order[i], at: at + i * 1000 });
  if (state === "undeliverable" || state === "withdrawn") history.push({ state: "sent", at }, { state, at: at + 2000 });
  return {
    id, scope, binding, text, state, at, history,
    ...(o.reason ? { reason: o.reason } : {}),
    ...(o.gate ? { gate: o.gate } : {}),
    ...(o.ack ? { ack_event: o.ack } : {}),
  };
}
