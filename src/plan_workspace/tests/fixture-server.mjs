// A local contract fixture server for developing and testing the workspace. It
// implements the frozen HTTP/DTO contract against an in-memory copy of the
// heavy fixture: token exchange, HttpOnly session cookie + readable CSRF cookie,
// strict CSP, Origin/X-Grogu-Token checks on mutations, the patch/revision
// protocol with base==HEAD preconditions and 409 rebase, SSE, and a dagre
// /api/layout. It is NOT the production server (that is the integration
// workstream's Python), only enough of the contract to run the app and the
// browser tests offline. No network access; binds 127.0.0.1.

import http from "node:http";
import { readFileSync, existsSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, normalize, extname } from "node:path";
import crypto from "node:crypto";
import dagre from "@dagrejs/dagre";
import { buildInitialState } from "./fixtures/heavyPlan.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const distDir = join(here, "..", "dist");

const CSP =
  "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; " +
  "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; " +
  "base-uri 'none'; form-action 'none'";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".txt": "text/plain; charset=utf-8",
  ".map": "application/json",
};

// ---------------- state ----------------
const state = buildInitialState();
const sseClients = new Set();

// Sessions: a set of live tokens, spent tokens, and issued session cookies.
const liveTokens = new Set();
const spentTokens = new Set();
const sessions = new Map(); // cookie -> token

function mintToken() {
  const t = crypto.randomBytes(24).toString("hex");
  liveTokens.add(t);
  return t;
}

// ---------------- JSON pointer / patch ----------------
function unescape(t) {
  return t.replace(/~1/g, "/").replace(/~0/g, "~");
}
function ptr(path) {
  if (path === "") return [];
  return path.slice(1).split("/").map(unescape);
}
function getVal(root, path) {
  let n = root;
  for (const t of ptr(path)) {
    if (n == null) throw new Error("missing " + t);
    n = n[t];
  }
  return n;
}
function setVal(root, path, v) {
  const toks = ptr(path);
  let n = root;
  for (let i = 0; i < toks.length - 1; i++) n = n[toks[i]];
  n[toks[toks.length - 1]] = v;
}
function delVal(root, path) {
  const toks = ptr(path);
  let n = root;
  for (let i = 0; i < toks.length - 1; i++) n = n[toks[i]];
  delete n[toks[toks.length - 1]];
}
function hasVal(root, path) {
  try {
    getVal(root, path);
    return true;
  } catch {
    return false;
  }
}
function applyOps(graph, ops) {
  const next = structuredClone(graph);
  for (const op of ops) {
    if (op.op === "add" || op.op === "replace") setVal(next, op.path, structuredClone(op.value));
    else if (op.op === "remove") delVal(next, op.path);
    else if (op.op === "test") {
      if (JSON.stringify(getVal(next, op.path)) !== JSON.stringify(op.value)) throw new Error("test failed");
    } else if (op.op === "move") {
      const v = getVal(next, op.from);
      delVal(next, op.from);
      setVal(next, op.path, v);
    } else if (op.op === "copy") {
      setVal(next, op.path, structuredClone(getVal(next, op.from)));
    }
  }
  return next;
}

function nextRevision() {
  const seq = state.revisions.length ? Math.max(...state.revisions.map((r) => r.seq)) + 1 : 1;
  return { seq, rev: "r" + String(seq).padStart(4, "0") };
}

function changedPaths(ops) {
  const set = new Set();
  for (const op of ops) {
    const p = op.path ?? op.from ?? "";
    const m = p.match(/^\/(?:nodes|edges)\/([^/]+)/);
    if (m) set.add(m[1]);
  }
  return [...set];
}

function commitPatch(ops, intent, origin, role = state.role) {
  const next = applyOps(state.graph, ops); // throws on invalid
  const { seq, rev } = nextRevision();
  const parent = state.HEAD;
  state.graph = next;
  state.HEAD = rev;
  const env = {
    revision: rev, seq, parent, at: new Date().toISOString(),
    actor: "charlie", role, agent: "plan-v2-workspace", intent, origin,
    ops, before_digest: "sha256:" + "0".repeat(64), after_digest: digestOf(next), summary: intent,
  };
  state.revisions.unshift(env);
  state.opsByRevision[rev] = ops;
  broadcast("revision", { revision: rev });
  return { revision: rev, digest: env.after_digest, changed: changedPaths(ops) };
}

function digestOf(graph) {
  const h = crypto.createHash("sha256");
  h.update(JSON.stringify(graph));
  return "sha256:" + h.digest("hex");
}

// ---------------- compiler (simple, deterministic) ----------------
const SECTION_ORDER = ["directive", "goal", "constraint", "invariant", "decision", "criterion", "task", "risk", "question"];
const SECTION_LABEL = {
  directive: "Directives", goal: "Goals", constraint: "Constraints", invariant: "Invariants",
  decision: "Decisions", criterion: "Criteria", task: "Tasks", risk: "Risks", question: "Questions",
};
function compile(role, stage, graph) {
  const nodes = Object.values(graph.nodes).filter((n) => n.stage === stage || (n.kind === "directive" && n.stage === ""));
  const lines = [`# ${state.title} — ${stage}`, ""];
  const elided = [];
  for (const kind of SECTION_ORDER) {
    const group = nodes.filter((n) => n.kind === kind).sort((a, b) => a.order - b.order);
    if (!group.length) continue;
    lines.push(`## ${SECTION_LABEL[kind]}`, "");
    for (const n of group) {
      if (kind === "directive" && (n.attrs?.status ?? "active") !== "active") continue;
      lines.push(`### ${n.title}`, "");
      if (n.body) lines.push(n.body, "");
    }
  }
  const noteCount = nodes.filter((n) => n.kind === "note").length;
  if (noteCount) elided.push({ id: "notes", kind: "note", count: noteCount });
  const markdown = lines.join("\n");
  return { markdown, digest: digestOf({ markdown }), elided };
}

// ---------------- impact ----------------
const IMPACT_KINDS = new Set(["depends_on", "blocks", "refines", "contains", "validates"]);
function impactOf(graph, startId) {
  const adj = new Map();
  for (const e of Object.values(graph.edges)) {
    if (!IMPACT_KINDS.has(e.kind)) continue;
    // reverse edge: who depends on `to`
    const list = adj.get(e.to) ?? [];
    list.push(e.from);
    adj.set(e.to, list);
  }
  const direct = (adj.get(startId) ?? []).map((id) => impactItem(graph, id));
  const seen = new Set([startId]);
  const transitive = [];
  const queue = [startId];
  while (queue.length) {
    const cur = queue.shift();
    for (const nb of adj.get(cur) ?? []) {
      if (!seen.has(nb)) {
        seen.add(nb);
        transitive.push({ ...impactItem(graph, nb), reason: `downstream of ${startId}` });
        queue.push(nb);
      }
    }
  }
  return { direct, transitive, cycles: [], compiled_delta: [...seen].filter((id) => id !== startId) };
}
function impactItem(graph, id) {
  const n = graph.nodes[id];
  return { id, kind: n?.kind ?? "task", title: n?.title ?? id, op: "set", destructive: false };
}

// ---------------- HTTP helpers ----------------
function send(res, status, body, headers = {}) {
  const payload = typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body);
  res.writeHead(status, {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
    ...headers,
  });
  res.end(payload);
}
function json(res, status, obj) {
  send(res, status, obj, { "Content-Type": "application/json; charset=utf-8" });
}
function err(res, status, error, message) {
  json(res, status, { error, message: message ?? error });
}
function parseCookies(req) {
  const out = {};
  const raw = req.headers.cookie;
  if (!raw) return out;
  for (const part of raw.split(";")) {
    const i = part.indexOf("=");
    if (i > -1) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}
function readBody(req) {
  return new Promise((resolve) => {
    let data = "";
    let size = 0;
    req.on("data", (c) => {
      size += c.length;
      if (size > 256 * 1024) req.destroy();
      data += c;
    });
    req.on("end", () => {
      try {
        resolve(data ? JSON.parse(data) : {});
      } catch {
        resolve({});
      }
    });
  });
}
function sessionToken(req) {
  const cookies = parseCookies(req);
  const cookie = cookies["grogu_session"];
  return cookie && sessions.has(cookie) ? sessions.get(cookie) : null;
}
function mutationAllowed(req) {
  const sess = sessionToken(req);
  if (!sess) return false;
  if (req.headers["x-grogu-token"] !== sess) return false;
  const origin = req.headers.origin;
  if (origin) {
    const host = req.headers.host;
    if (!origin.endsWith(host)) return false;
  }
  return true;
}

// ---------------- static ----------------
function serveStatic(res, urlPath) {
  let rel = urlPath.replace(/^\/static\//, "/").replace(/^\//, "");
  if (rel === "" ) rel = "index.html";
  const full = normalize(join(distDir, rel));
  if (!full.startsWith(distDir)) return err(res, 403, "forbidden");
  if (!existsSync(full) || !statSync(full).isFile()) {
    // SPA fallback to index.html for unknown non-asset routes
    if (!extname(rel)) return serveStatic(res, "/index.html");
    return err(res, 404, "not_found");
  }
  const body = readFileSync(full);
  send(res, 200, body, { "Content-Type": MIME[extname(full)] ?? "application/octet-stream" });
}

// ---------------- SSE ----------------
function broadcast(type, data) {
  const line = `event: ${type}\ndata: ${JSON.stringify(data ?? {})}\n\n`;
  for (const c of sseClients) {
    try {
      c.write(line);
    } catch {
      /* dropped */
    }
  }
}

// ---------------- control snapshot ----------------
function controlSnapshot() {
  const waiting = state.agents.filter((a) => a.steering.unread > 0).map((a) => ({ agent_key: a.agent_key, agent: a.agent, reason: `${a.steering.unread} unread` }));
  const plans = {};
  for (const a of state.agents) {
    plans[a.plan] = plans[a.plan] ?? { title: a.plan_title ?? a.plan, agents: 0 };
    plans[a.plan].agents++;
  }
  return {
    fresh_as_of: Date.now(),
    limits: [
      { category: "model reasoning", detail: "never recorded" },
      { category: "tool arguments", detail: "never recorded" },
    ],
    waiting_on_you: waiting,
    agents: state.agents,
    plans,
  };
}

// ---------------- routes ----------------
async function handle(req, res) {
  const url = new URL(req.url, `http://${req.headers.host}`);
  const path = url.pathname;
  const method = req.method;

  // Dev-only helper for the browser tests: mint a fresh single-use launch URL.
  if (path === "/__new_session" && method === "GET") {
    const t = mintToken();
    return json(res, 200, { token: t, url: `/?t=${t}` });
  }

  // Token exchange (single-use) -> cookies -> 303 /
  if (path === "/" && url.searchParams.has("t")) {
    const t = url.searchParams.get("t");
    if (!t || spentTokens.has(t) || !liveTokens.has(t)) {
      // A spent/invalid token returns 403 but still serves the app, with a
      // readable notice cookie so the workspace shows the "session spent" panel
      // (design: "returns 403 and the page shows the Session token spent panel").
      const html = existsSync(join(distDir, "index.html")) ? readFileSync(join(distDir, "index.html")) : "session token spent";
      return send(res, 403, html, {
        "Content-Type": "text/html; charset=utf-8",
        "Set-Cookie": "grogu_notice=token_spent; SameSite=Strict; Path=/",
      });
    }
    liveTokens.delete(t);
    spentTokens.add(t);
    const cookie = crypto.randomBytes(24).toString("hex");
    sessions.set(cookie, t);
    return send(res, 303, "", {
      Location: "/",
      "Set-Cookie": [
        `grogu_session=${cookie}; HttpOnly; SameSite=Strict; Path=/`,
        `grogu_csrf=${t}; SameSite=Strict; Path=/`,
        `grogu_notice=; Path=/; Max-Age=0`,
      ],
    });
  }

  // Static
  if (!path.startsWith("/api/")) {
    if (method !== "GET" && method !== "HEAD") return err(res, 405, "method_not_allowed");
    return serveStatic(res, path);
  }

  // API auth: all mutations require cookie + token + origin.
  const isMutation = method === "POST" || method === "PUT" || method === "PATCH" || method === "DELETE";
  if (isMutation && !mutationAllowed(req)) {
    return err(res, 403, "forbidden", "missing session, token or matching origin");
  }
  // Reads require at least a session cookie (except health for boot probing).
  if (!isMutation && path !== "/api/health" && !sessionToken(req)) {
    return err(res, 403, "forbidden", "no session");
  }

  // ----- read routes -----
  if (path === "/api/health" && method === "GET") {
    return json(res, 200, { ok: true, plan: state.plan, revision: state.HEAD, role: state.role, modes: ["control", "document", "canvas", "dependencies", "revision"] });
  }
  if (path === "/api/doc" && method === "GET") {
    const readable = new Set([...state.stages.readable, ""]);
    const nodes = Object.values(state.graph.nodes).filter((n) => readable.has(n.stage));
    const edges = Object.values(state.graph.edges);
    return json(res, 200, {
      plan: state.plan, title: state.title, revision: state.HEAD, role: state.role,
      stages: state.stages, nodes, edges, counters: state.graph.counters, sealed_edge_count: state.sealed_edge_count,
    });
  }
  if (path === "/api/projection" && method === "GET") {
    const role = url.searchParams.get("role") ?? state.role;
    const stage = url.searchParams.get("stage") ?? "design";
    const c = compile(role, stage, state.graph);
    return json(res, 200, {
      markdown: c.markdown, digest: c.digest, role, stage,
      include: url.searchParams.get("include") ?? "normative",
      budget: url.searchParams.get("budget") ? Number(url.searchParams.get("budget")) : null,
      elided: c.elided, nonequivalent: null,
    });
  }
  if (path === "/api/revisions" && method === "GET") {
    return json(res, 200, { revisions: state.revisions });
  }
  if (path === "/api/diff" && method === "GET") {
    const stage = "design";
    const before = compile(state.role, stage, state.graph).markdown;
    return json(res, 200, { from: url.searchParams.get("from"), to: url.searchParams.get("to"), before, after: before });
  }
  if (path === "/api/proposals" && method === "GET") {
    return json(res, 200, { proposals: state.proposals });
  }
  if (path.match(/^\/api\/proposals\/[^/]+\/preview$/) && method === "GET") {
    const pid = path.split("/")[3];
    const p = state.proposals.find((x) => x.id === pid);
    if (!p) return err(res, 404, "not_found");
    const after = applyOps(state.graph, p.ops);
    const node_diff = p.ops.map((op) => {
      const id = (op.path ?? "").match(/\/nodes\/([^/]+)/)?.[1] ?? "?";
      return {
        id, op: op.op === "add" ? "add" : op.op === "remove" ? "remove" : "set",
        before: { title: state.graph.nodes[id]?.title }, after: { title: op.op === "remove" ? undefined : (op.value?.title ?? getAfterTitle(after, id)) },
      };
    });
    const firstId = node_diff[0]?.id;
    const impact = firstId ? impactOf(state.graph, firstId) : { direct: [], transitive: [], cycles: [], compiled_delta: [] };
    const compiled_diff = ["engineer", "reviewer"].map((role) => ({
      role, before: compile(role, "design", state.graph).markdown, after: compile(role, "design", after).markdown,
    }));
    return json(res, 200, { node_diff, compiled_diff, impact, stale: p.base !== state.HEAD, head: state.HEAD });
  }
  if (path.match(/^\/api\/control\/[^/]+$/) && method === "GET") {
    const key = decodeURIComponent(path.split("/")[3]);
    const agent = state.agents.find((a) => a.agent_key === key);
    if (!agent) return err(res, 404, "not_found");
    return json(res, 200, {
      agent, activity: state.auditByAgent[key] ?? [],
      evidence: [{ id: "ev1", label: "wrote r0031", plan: agent.plan, revision: "r0031", node: "task-1", available: true }],
      limits: [{ category: "model reasoning", detail: "never recorded" }, { category: "tool arguments and results", detail: "never recorded" }],
      blockers: agent.blockers,
    });
  }
  if (path === "/api/control" && method === "GET") {
    return json(res, 200, controlSnapshot());
  }
  if (path === "/api/feedback" && method === "GET") {
    return json(res, 200, { feedback: state.feedback });
  }
  if (path === "/api/activity" && method === "GET") {
    const object = url.searchParams.get("object");
    return json(res, 200, { events: state.activityByObject[object] ?? [] });
  }
  if (path === "/api/events" && method === "GET") {
    res.writeHead(200, {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-store",
      Connection: "keep-alive",
      "Content-Security-Policy": CSP,
    });
    res.write(`event: control\ndata: {}\n\n`);
    sseClients.add(res);
    req.on("close", () => sseClients.delete(res));
    return;
  }

  // ----- mutation routes -----
  if (path === "/api/patch" && method === "POST") {
    const body = await readBody(req);
    if (body.base !== state.HEAD) {
      const opsSince = state.opsByRevision[body.base] ? [] : [];
      return json(res, 409, { error: "stale", revision: state.HEAD, ops_since: opsSince });
    }
    if ((body.ops ?? []).length > 500) return err(res, 422, "too_many_ops", "patch op count capped at 500");
    // Reject ops that touch a sealed stage (I2 write boundary): the destination
    // path, a move/copy source, or an add/replace whose value lands in a sealed
    // stage.
    const sealed = new Set(state.stages.sealed);
    for (let i = 0; i < body.ops.length; i++) {
      const op = body.ops[i];
      const ids = [];
      const pid = (op.path ?? "").match(/\/nodes\/([^/]+)/)?.[1];
      const fid = (op.from ?? "").match(/\/nodes\/([^/]+)/)?.[1];
      if (pid) ids.push(pid);
      if (fid) ids.push(fid);
      for (const id of ids) {
        const n = state.graph.nodes[id];
        if (n && sealed.has(n.stage)) return err(res, 403, "sealed", `path names sealed stage ${n.stage}`);
      }
      const valueStage = op.value && typeof op.value === "object" ? op.value.stage : undefined;
      if (valueStage && sealed.has(valueStage)) return err(res, 403, "sealed", `value lands in sealed stage ${valueStage}`);
    }
    try {
      const result = commitPatch(body.ops, body.intent ?? "", body.origin ?? "workspace");
      return json(res, 200, result);
    } catch (e) {
      return json(res, 422, { error: "invalid", op_index: 0, why: String(e && e.message) });
    }
  }
  if (path === "/api/threads" && method === "POST") {
    const body = await readBody(req);
    const tid = `thr-${++state.graph.counters.thr}`;
    const anchorNode = body.selector?.id ?? body.selector?.node;
    const thread = {
      id: tid, kind: "thread", stage: state.graph.nodes[anchorNode]?.stage ?? "design",
      title: `Thread ${tid}`, body: body.body ?? "",
      attrs: { anchor_node: anchorNode, thread_kind: body.kind ?? "discussion", status: "open", anchor_state: "resolved", round: 1 },
      ext: { "dev.grogu.thread": { kind: body.kind ?? "discussion", status: "open", anchor_state: "resolved", round: 1, quote_context: state.graph.nodes[anchorNode]?.title ?? "", comments: [{ id: "c" + Date.now(), author: "charlie", role: state.role, body: body.body ?? "", at: new Date().toISOString() }] } },
      order: 1100, created_rev: state.HEAD, updated_rev: state.HEAD,
    };
    state.graph.nodes[tid] = thread;
    broadcast("thread", { id: tid });
    return json(res, 200, { thread });
  }
  const threadMut = path.match(/^\/api\/threads\/([^/]+)\/(reply|resolve|reopen|revise)$/);
  if (threadMut && method === "POST") {
    const [, tid, action] = threadMut;
    const t = state.graph.nodes[tid];
    if (!t) return err(res, 404, "not_found");
    const body = await readBody(req);
    const ext = t.ext["dev.grogu.thread"];
    if (action === "reply") {
      ext.comments.push({ id: "c" + Date.now(), author: "charlie", role: state.role, body: body.body ?? "", at: new Date().toISOString() });
    } else if (action === "resolve") {
      ext.status = "resolved";
      t.attrs.status = "resolved";
    } else if (action === "reopen") {
      ext.status = "open";
      t.attrs.status = "open";
    } else if (action === "revise") {
      const request = makeFeedback(
        {
          kind: "role_plan",
          role: "architect",
          plan: state.plan,
          label: "architects on this plan",
        },
        `Revision request for ${tid}: ${body.instruction ?? ""}`,
        true,
      );
      state.feedback.push(request);
      broadcast("feedback", { id: request.id, thread: tid });
      return json(res, 200, {
        request: {
          seq: request.id,
          delivered_to: request.scope.label,
          gates_closed: ["implementation"],
          record: request,
        },
      });
    }
    broadcast("thread", { id: tid });
    return json(res, 200, { thread: t });
  }
  if (path === "/api/proposals" && method === "POST") {
    const body = await readBody(req);
    const pid = `pr-${state.proposals.length + 4}`;
    let ops = body.ops ?? [];
    if (body.restore_from) ops = [{ op: "replace", path: `/nodes/goal-1/body`, value: `Restored from ${body.restore_from}.` }];
    const proposal = { id: pid, at: new Date().toISOString(), from_thread: body.from_thread, base: body.base ?? state.HEAD, why: body.why ?? "", ops, status: "pending", decided_at: "", decided_by: "", why_not: "" };
    state.proposals.push(proposal);
    broadcast("proposal", { id: pid });
    return json(res, 200, { proposal });
  }
  const propAct = path.match(/^\/api\/proposals\/([^/]+)\/(accept|reject)$/);
  if (propAct && method === "POST") {
    const [, pid, action] = propAct;
    const p = state.proposals.find((x) => x.id === pid);
    if (!p) return err(res, 404, "not_found");
    const body = await readBody(req);
    if (action === "accept") {
      const result = commitPatch(p.ops, `accept ${pid}`, `proposal:${pid}`);
      p.status = "accepted";
      p.decided_at = new Date().toISOString();
      return json(res, 200, { revision: result.revision });
    }
    p.status = "rejected";
    p.why_not = body.why_not ?? "";
    return json(res, 200, { proposal: p });
  }
  if (path === "/api/impact" && method === "POST") {
    const body = await readBody(req);
    const id = body.selector?.id;
    return json(res, 200, id ? impactOf(state.graph, id) : { direct: [], transitive: [], cycles: [], compiled_delta: [] });
  }
  if (path === "/api/layout" && method === "POST") {
    const body = await readBody(req);
    const dir = body.direction ?? "TB";
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: dir, nodesep: 40, ranksep: 64 });
    g.setDefaultEdgeLabel(() => ({}));
    const placed = Object.values(state.graph.nodes)
      .filter(
        (node) =>
          node.geometry &&
          node.kind !== "thread" &&
          (body.scope !== "stage" || node.stage === body.stage),
      )
      .sort((a, b) => a.id.localeCompare(b.id));
    for (const n of placed) g.setNode(n.id, { width: n.geometry.w, height: n.geometry.h });
    for (const e of Object.values(state.graph.edges).sort((a, b) => a.id.localeCompare(b.id))) {
      if (g.hasNode(e.from) && g.hasNode(e.to)) g.setEdge(e.from, e.to);
    }
    dagre.layout(g);
    const ops = placed.map((n) => {
      const gn = g.node(n.id);
      return { op: "replace", path: `/nodes/${n.id}/geometry`, value: { ...n.geometry, x: Math.round(gn.x - n.geometry.w / 2), y: Math.round(gn.y - n.geometry.h / 2) } };
    });
    return json(res, 200, { ops });
  }
  const fbAgent = path.match(/^\/api\/control\/([^/]+)\/feedback$/);
  if (fbAgent && method === "POST") {
    const key = decodeURIComponent(fbAgent[1]);
    const body = await readBody(req);
    const rec = makeFeedback({ kind: "agent", agent_key: key, label: `agent ${key}` }, body.text, body.binding);
    state.feedback.push(rec);
    const agent = state.agents.find((a) => a.agent_key === key);
    if (agent && body.binding) agent.steering.unread++;
    return json(res, 200, { seq: rec.id, delivered_to: key, gates_closed: body.binding ? ["implementation"] : [], record: rec });
  }
  if (path === "/api/feedback" && method === "POST") {
    const body = await readBody(req);
    const rec = makeFeedback(body.scope, body.text, body.binding);
    state.feedback.push(rec);
    return json(res, 200, { seq: rec.id, delivered_to: body.scope?.label ?? "", gates_closed: body.binding ? ["implementation"] : [], record: rec });
  }
  const fbWithdraw = path.match(/^\/api\/feedback\/([^/]+)\/withdraw$/);
  if (fbWithdraw && method === "POST") {
    const rec = state.feedback.find((f) => f.id === decodeURIComponent(fbWithdraw[1]));
    if (rec) {
      rec.state = "withdrawn";
      rec.history.push({ state: "withdrawn", at: Date.now() });
      if (rec.gate) rec.gate.state = "open";
    }
    return json(res, 200, { ok: true });
  }
  if (path === "/api/request-changes" && method === "POST") {
    return json(res, 200, { ok: true });
  }
  if (path === "/api/approve" && method === "POST") {
    // Refuses when a role is set (I10).
    if (state.role) return err(res, 403, "role_set", "cannot approve while a role is set");
    return json(res, 200, { ok: true });
  }
  if (path === "/api/shutdown" && method === "POST") {
    json(res, 200, { ok: true });
    broadcast("shutdown", {});
    return;
  }

  return err(res, 404, "not_found");
}

function getAfterTitle(graph, id) {
  return graph.nodes[id]?.title;
}
function makeFeedback(scope, text, binding) {
  const id = `f-${String(state.feedback.length + 1).padStart(2, "0")}-${Date.now().toString(36)}`;
  const at = Date.now();
  return {
    id, scope, binding: !!binding, text: text ?? "", state: "delivered",
    history: [{ state: "sent", at }, { state: "routed", at: at + 100 }, { state: "delivered", at: at + 200 }],
    ...(binding ? { gate: { stage: "implementation", state: "blocked" } } : {}),
    at,
  };
}

const server = http.createServer((req, res) => {
  handle(req, res).catch((e) => {
    try {
      err(res, 500, "server_error", String(e && e.message));
    } catch {
      /* ignore */
    }
  });
});

const PORT = Number(process.env.PORT ?? 5179);
server.listen(PORT, "127.0.0.1", () => {
  const launch = mintToken();
  // Printed for humans; the browser tests use GET /__new_session for fresh ones.
  console.log(`[fixture] http://127.0.0.1:${PORT}/?t=${launch}`);
  console.log(`[fixture] ready on 127.0.0.1:${PORT}`);
});
