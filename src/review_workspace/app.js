// The review workspace controller. No inline script and no inline event
// handler exists anywhere; the CSP forbids both, so every listener is attached
// here.

import { offsetsFromSelection, highlight } from "/static/anchors.js";
import { renderDiagram, elementForNode, elementForEdge } from "/static/mermaid_anchors.js";

const STAGE_ORDER = ["design", "implementation", "testing", "evaluation"];
const STAGE_LABEL = {
  design: "Design",
  implementation: "Implementation",
  testing: "Testing",
  evaluation: "Evaluation",
};
const MAX_COMMENT_CHARS = 8000;
const POLL_MS = 5000;

const state = {
  data: null,
  stage: "",
  role: "reviewer",
  failures: 0,
  ended: false,
  reanchorSummary: "",
  pendingRevision: null,
  composer: null,
};

const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const headers = Object.assign({ "X-Grogu-Review": "1" }, options.headers || {});
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, Object.assign({}, options, { headers }));
  const text = await response.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch (error) {
    payload = {};
  }
  if (!response.ok) {
    const error = new Error(payload.error || `request failed (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload;
}

// -- loading and top-level state ---------------------------------------------

async function loadPlan() {
  try {
    const data = await api("/api/plan");
    state.failures = 0;
    applyData(data);
  } catch (error) {
    state.failures += 1;
    if (state.failures >= 2) {
      endSession();
    } else {
      renderLoadError();
    }
  }
}

function applyData(data) {
  const previous = state.data;
  state.data = data;
  state.role = data.role;
  if (!state.stage) {
    const requested = data.default_stage;
    const readableWritten = data.stages.find(
      (s) => s.readable && s.written && (!requested || s.stage === requested)
    );
    state.stage =
      (requested && data.stages.some((s) => s.stage === requested && s.readable && s.written)
        ? requested
        : (readableWritten ? readableWritten.stage : data.readable_stages[0])) ||
      "design";
  }
  // Detect a revision change while the workspace is open.
  if (previous) {
    const before = stageRevisions(previous);
    const after = stageRevisions(data);
    for (const stage of Object.keys(after)) {
      if (before[stage] !== undefined && before[stage] !== after[stage]) {
        state.pendingRevision = { stage, revision: after[stage] };
      }
    }
  }
  renderHeader();
  renderTabs();
  renderRevisionBanner();
  renderStage();
  renderRail();
}

function stageRevisions(data) {
  const out = {};
  for (const s of data.stages) {
    if (typeof s.revision === "number") out[s.stage] = s.revision;
  }
  return out;
}

function currentStage() {
  return (state.data.stages || []).find((s) => s.stage === state.stage);
}

function threadsForStage(stage) {
  return (state.data.threads || []).filter((t) => t.stage === stage);
}

// -- header ------------------------------------------------------------------

function renderHeader() {
  const d = state.data;
  $("header-plan-id").textContent = d.plan;
  $("header-title").textContent = d.title;
  const roleEl = $("header-role");
  if (d.role !== "reviewer") {
    roleEl.textContent = `reading as ${d.role}`;
    roleEl.hidden = false;
  } else {
    roleEl.hidden = true;
  }
  renderRoundChip();
  renderActions();
  const privacy = $("privacy-line");
  privacy.textContent =
    `Local only. Comments are saved to .grogu/plans/${d.plan}/review.json, ` +
    `which is git-ignored and is never sent anywhere.`;
}

function renderRoundChip() {
  const chip = $("round-chip");
  const summary = state.data.summary || {};
  chip.textContent = "";
  if (!summary.threads) {
    chip.textContent = "no comments";
    return;
  }
  const parts = [`round ${summary.round || 1}`];
  if (summary.open) parts.push(`${summary.open} open`);
  chip.append(document.createTextNode(parts.join(" · ")));
  if (summary.orphaned) {
    const span = document.createElement("span");
    span.className = "orphaned-count";
    span.textContent = ` · ${summary.orphaned} orphaned`;
    chip.append(span);
  }
  const round = state.data.round;
  const line = $("round-line");
  if (round && round.state === "changes_requested") {
    line.hidden = false;
    line.textContent = `Round ${round.number} is with the architect. Add a comment to open round ${round.number + 1}.`;
  } else {
    line.hidden = true;
  }
}

function renderActions() {
  const d = state.data;
  const approveBtn = $("approve-button");
  const rcBtn = $("request-changes-button");
  const approved = $("header-approved");
  const refusal = $("header-refusal");
  approved.hidden = true;
  refusal.hidden = true;
  approveBtn.hidden = false;
  rcBtn.hidden = false;
  if (d.approved_at) {
    approveBtn.hidden = true;
    rcBtn.hidden = true;
    approved.hidden = false;
    approved.textContent = `approved · ${formatStamp(d.approved_at)}`;
    return;
  }
  if (d.role !== "reviewer") {
    approveBtn.hidden = true;
    refusal.hidden = false;
    refusal.textContent =
      "Approval is the user's. This workspace is reading as the " +
      d.role + ", so it can read and comment only.";
  }
  const summary = d.summary || {};
  const round = d.round;
  if (round && round.state === "changes_requested") {
    rcBtn.disabled = true;
    rcBtn.title = "No open comments to send.";
  } else if (!summary.open) {
    rcBtn.disabled = true;
    rcBtn.title = "No open comments to send.";
    rcBtn.setAttribute("aria-describedby", "");
  } else {
    rcBtn.disabled = false;
    rcBtn.title = "";
  }
}

// -- tabs --------------------------------------------------------------------

function renderTabs() {
  const list = $("tablist");
  list.replaceChildren();
  const select = $("stage-select");
  select.replaceChildren();
  for (const stage of STAGE_ORDER) {
    const info = state.data.stages.find((s) => s.stage === stage) || { stage };
    const tab = document.createElement("button");
    tab.className = "tab";
    tab.type = "button";
    tab.setAttribute("role", "tab");
    tab.setAttribute("aria-selected", stage === state.stage ? "true" : "false");
    tab.tabIndex = stage === state.stage ? 0 : -1;
    const count = threadsForStage(stage).filter((t) => t.status === "open").length;
    tab.textContent = STAGE_LABEL[stage] + (count ? `·${count}` : "");
    tab.addEventListener("click", () => switchStage(stage));
    list.append(tab);

    const option = document.createElement("option");
    option.value = stage;
    option.textContent = STAGE_LABEL[stage];
    option.selected = stage === state.stage;
    select.append(option);
  }
  select.onchange = () => switchStage(select.value);
}

function switchStage(stage) {
  if (!state.data.stages.some((s) => s.stage === stage)) return;
  state.stage = stage;
  closeComposer(true);
  renderTabs();
  renderStage();
  renderRail();
}

// -- stage body --------------------------------------------------------------

function renderStage() {
  const doc = $("doc");
  const meta = $("stage-meta");
  const info = currentStage();
  doc.replaceChildren();
  if (!info) return;
  if (!info.readable) {
    meta.textContent = "";
    doc.append(stateBlock("Sealed",
      "The testing plan is written for the tester and is not readable by the engineer. " +
      "An implementation written against its own tests only proves the tests were satisfiable."));
    return;
  }
  if (info.state === "pending") {
    meta.textContent = "";
    doc.append(stateBlock("Not written yet",
      "The architect writes the implementation plan before this stage can be reviewed."));
    return;
  }
  if (info.state === "declined") {
    meta.textContent = "";
    doc.append(stateBlock("Not needed",
      "The architect recorded that no evaluation stage is warranted for this plan."));
    return;
  }
  const kb = ((info.markdown || "").length / 1024).toFixed(1);
  meta.textContent = `${info.stage} · revision ${info.revision} · ${kb} KB`;
  doc.innerHTML = info.html || "";
  renderDiagrams(info);
  applyMarks();
}

function stateBlock(title, body) {
  const wrap = document.createElement("div");
  wrap.className = "state-block";
  const h = document.createElement("h2");
  h.textContent = title;
  const p = document.createElement("p");
  p.textContent = body;
  wrap.append(h, p);
  return wrap;
}

async function renderDiagrams(info) {
  const doc = $("doc");
  const hasAsset = state.data.assets && state.data.assets.mermaid;
  for (const block of info.mermaid || []) {
    const holder = doc.querySelector(`[data-mermaid-index="${block.index}"]`);
    if (!holder) continue;
    const wrap = document.createElement("div");
    wrap.className = "diagram";
    wrap.dataset.blockDigest = block.digest || "";
    holder.replaceWith(wrap);
    let rendered = false;
    if (hasAsset) {
      rendered = await renderDiagram(wrap, block.source, block.index);
      if (rendered) decorateDiagram(wrap, block);
    }
    if (!rendered) renderDegraded(wrap, block);
  }
}

function renderDegraded(wrap, block) {
  wrap.replaceChildren();
  const notice = document.createElement("div");
  notice.className = "diagram-notice";
  notice.append(document.createTextNode(
    "Mermaid is not installed, so this diagram is shown as source. Every node and " +
    "edge below is still commentable."));
  const code = document.createElement("code");
  code.textContent = "grogu review assets --install";
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "btn-text copy-button";
  copy.textContent = "Copy";
  copy.addEventListener("click", () => copyText("grogu review assets --install", copy));
  code.append(copy);
  notice.append(code);
  wrap.append(notice);

  const pre = document.createElement("pre");
  pre.textContent = block.source;
  wrap.append(pre);

  const parsed = block.parsed || {};
  chipRow(wrap, "Nodes", (parsed.nodes || []).map((n) => ({
    label: n.label || n.id,
    target: "node", node_id: n.id,
  })), block);
  chipRow(wrap, "Edges", (parsed.edges || []).map((e) => ({
    label: `${labelOf(parsed, e.from)} → ${labelOf(parsed, e.to)}`,
    target: "edge", edge: e,
  })), block);
  chipRow(wrap, "Subgraphs", (parsed.subgraphs || []).map((g) => ({
    label: g.title || g.id,
    target: "subgraph", node_id: g.id,
  })), block);
}

function labelOf(parsed, id) {
  const node = (parsed.nodes || []).find((n) => n.id === id);
  return node ? (node.label || node.id) : id;
}

function chipRow(wrap, heading, items, block) {
  if (!items.length) return;
  const row = document.createElement("div");
  row.className = "chip-row";
  const label = document.createElement("span");
  label.className = "chip-row-label";
  label.textContent = heading;
  row.append(label);
  for (const item of items) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    if (chipHasThread(block, item)) chip.classList.add("chip-threaded");
    chip.textContent = item.label;
    chip.addEventListener("click", () => openDiagramComposer(block, item));
    row.append(chip);
  }
  wrap.append(row);
}

function chipHasThread(block, item) {
  return threadsForStage(state.stage).some((t) => {
    const a = t.anchor || {};
    if (a.kind !== "mermaid" || a.block_index !== block.index) return false;
    if (item.target === "node") return a.node_id === item.node_id;
    if (item.target === "edge" && a.edge) {
      return a.edge.from === item.edge.from && a.edge.to === item.edge.to;
    }
    return a.target === item.target && a.node_id === item.node_id;
  });
}

function decorateDiagram(wrap, block) {
  const svg = wrap.querySelector("svg");
  if (!svg) return;
  const unresolved = [];
  for (const thread of threadsForStage(state.stage)) {
    const a = thread.anchor || {};
    if (a.kind !== "mermaid" || a.block_index !== block.index) continue;
    let el = null;
    if (a.target === "node") el = elementForNode(svg, a.node_id);
    else if (a.target === "edge" && a.edge) el = elementForEdge(svg, a.edge);
    if (el) {
      if (a.target === "edge") el.classList.add("diagram-edge-threaded");
      else markNodeThreaded(el);
    } else {
      unresolved.push(a.label || a.node_id || "target");
    }
  }
  attachDiagramHandlers(svg, block);
  if (unresolved.length) {
    const notice = document.createElement("div");
    notice.className = "unresolved-notice";
    notice.textContent =
      "Unresolved anchors — the diagram no longer draws these, so comment on them here.";
    wrap.append(notice);
  }
}

function markNodeThreaded(el) {
  const box = el.getBBox ? tryBBox(el) : null;
  const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  dot.setAttribute("r", "4");
  if (box) {
    dot.setAttribute("cx", String(box.x + box.width));
    dot.setAttribute("cy", String(box.y));
  }
  dot.setAttribute("fill", "var(--color-accent)");
  el.append(dot);
}

function tryBBox(el) {
  try { return el.getBBox(); } catch (e) { return null; }
}

function attachDiagramHandlers(svg, block) {
  const parsed = block.parsed || {};
  for (const node of parsed.nodes || []) {
    const el = elementForNode(svg, node.id);
    if (!el) continue;
    el.setAttribute("tabindex", "0");
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", `node ${node.label || node.id}, comment`);
    el.addEventListener("mouseenter", () => el.classList.add("diagram-node-hover"));
    el.addEventListener("mouseleave", () => el.classList.remove("diagram-node-hover"));
    const open = () => openDiagramComposer(block, { label: node.label || node.id, target: "node", node_id: node.id });
    el.addEventListener("click", open);
    el.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === "c") { e.preventDefault(); open(); } });
  }
}

// -- marks -------------------------------------------------------------------

function applyMarks() {
  const doc = $("doc");
  const threads = threadsForStage(state.stage)
    .filter((t) => t.status === "open" && t.anchor && t.anchor.kind === "text")
    .sort((a, b) => a.id.localeCompare(b.id));
  for (const thread of threads) {
    const marks = highlight(doc, thread);
    for (const mark of marks) {
      mark.addEventListener("click", () => focusThread(thread.id));
      mark.addEventListener("keydown", (e) => {
        if (e.key === "Enter") { e.preventDefault(); focusThread(thread.id); }
      });
    }
  }
}

// -- rail --------------------------------------------------------------------

function renderRail() {
  const rail = $("rail");
  rail.replaceChildren();
  const info = currentStage();
  if (!info || !info.readable || info.state === "pending" || info.state === "declined") {
    return;
  }
  if (state.reanchorSummary) {
    const line = document.createElement("p");
    line.className = "orphaned-explain";
    line.setAttribute("aria-live", "polite");
    line.textContent = state.reanchorSummary;
    rail.append(line);
  }
  const threads = threadsForStage(state.stage);
  const open = threads.filter((t) => t.status === "open");
  const orphaned = open.filter((t) => t.anchor_state === "orphaned");
  const placed = open.filter((t) => t.anchor_state !== "orphaned");
  const resolved = threads.filter((t) => t.status === "resolved");

  if (!state.data.threads.length) {
    rail.append(emptyStateAll());
    return;
  }
  if (!threads.length) {
    rail.append(emptyStateStage());
  }

  if (orphaned.length) {
    const section = document.createElement("div");
    section.className = "orphaned-section";
    const header = document.createElement("h2");
    header.className = "rail-section-header";
    header.textContent = `Orphaned — ${orphaned.length}`;
    section.append(header);
    const explain = document.createElement("p");
    explain.className = "orphaned-explain";
    explain.textContent =
      "The plan was rewritten and this text is no longer in it. Your comments are kept " +
      "with the words you quoted, and nothing was deleted.";
    section.append(explain);
    for (const thread of orphaned) section.append(threadCard(thread, true));
    rail.append(section);
  }

  for (const thread of placed) rail.append(threadCard(thread, false));

  if (resolved.length) {
    const details = document.createElement("details");
    details.className = "resolved-disclosure";
    const summary = document.createElement("summary");
    summary.textContent = `Resolved — ${resolved.length}`;
    details.append(summary);
    for (const thread of resolved) details.append(threadCard(thread, false));
    rail.append(details);
  }

  if (state.pendingRevision && orphaned.length) {
    const first = rail.querySelector(".orphaned-section .card");
    if (first) first.scrollIntoView({ block: "nearest" });
  }
}

function emptyStateAll() {
  const wrap = document.createElement("div");
  wrap.className = "empty-state";
  const h = document.createElement("h2");
  h.textContent = "No comments yet";
  const p = document.createElement("p");
  p.textContent =
    "Select any text in the plan to comment on it. In a diagram, click a node or an " +
    "edge. Press ? for the keyboard shortcuts.";
  wrap.append(h, p);
  return wrap;
}

function emptyStateStage() {
  const wrap = document.createElement("div");
  wrap.className = "empty-state";
  const p = document.createElement("p");
  p.textContent = "No comments on this stage.";
  wrap.append(p);
  const list = document.createElement("ul");
  list.className = "empty-stage-list";
  for (const stage of STAGE_ORDER) {
    const count = threadsForStage(stage).length;
    if (!count || stage === state.stage) continue;
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "btn-text";
    btn.textContent = `${STAGE_LABEL[stage]} — ${count}`;
    btn.addEventListener("click", () => switchStage(stage));
    li.append(btn);
    list.append(li);
  }
  if (list.children.length) wrap.append(list);
  return wrap;
}

function threadCard(thread, orphaned) {
  const card = document.createElement("article");
  card.className = "card card-collapsed";
  card.dataset.thread = thread.id;
  if (orphaned) card.classList.add("card-orphaned");
  const stateLabel = orphaned ? "orphaned" : thread.status === "resolved" ? "resolved" : thread.anchor_state === "shifted" ? "moved" : "";
  card.setAttribute("aria-label", `Comment ${thread.id}${stateLabel ? ", " + stateLabel : ""}`);
  card.tabIndex = 0;

  if (stateLabel) {
    const badge = document.createElement("span");
    badge.className = "card-state" + (stateLabel === "orphaned" ? " state-orphaned" : "");
    badge.textContent = stateLabel;
    card.append(badge);
  }

  const anchor = thread.anchor || {};
  if (anchor.kind === "mermaid") {
    const quote = document.createElement("div");
    quote.className = "card-quote";
    quote.textContent = diagramHeader(anchor);
    card.append(quote);
  } else if (anchor.exact) {
    const quote = document.createElement("div");
    quote.className = "card-quote";
    quote.textContent = `"${anchor.exact}"`;
    card.append(quote);
  }

  const revLine = revisionLine(thread, orphaned);
  if (revLine) {
    const rev = document.createElement("div");
    rev.className = "card-revision";
    rev.textContent = revLine;
    card.append(rev);
  }

  for (const comment of thread.comments || []) {
    const body = document.createElement("div");
    body.className = "card-comment";
    body.textContent = comment.body;
    card.append(body);
    const meta = document.createElement("div");
    meta.className = "card-meta";
    meta.textContent = `${comment.author || "you"} · ${formatRelative(comment.at)}`;
    meta.title = formatStamp(comment.at);
    card.append(meta);
  }

  const actions = document.createElement("div");
  actions.className = "card-actions";
  if (thread.status === "resolved") {
    actions.append(textButton("Reopen", () => reopenThread(thread.id)));
  } else {
    actions.append(textButton("Reply", () => openReply(card, thread.id)));
    actions.append(textButton("Resolve", () => resolveThread(thread.id)));
  }
  card.append(actions);

  card.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    focusThread(thread.id);
  });
  if (orphaned || (state.focusedThread === thread.id)) card.classList.remove("card-collapsed");
  if (state.focusedThread === thread.id) card.classList.add("card-focused");
  return card;
}

function diagramHeader(anchor) {
  if (anchor.target === "node") return `node · ${anchor.label || anchor.node_id}`;
  if (anchor.target === "edge" && anchor.edge) {
    const base = `edge · ${anchor.edge.from} → ${anchor.edge.to}`;
    return anchor.label ? `${base} ("${anchor.label}")` : base;
  }
  if (anchor.target === "subgraph") return `subgraph · ${anchor.label || anchor.node_id}`;
  return "diagram";
}

function revisionLine(thread, orphaned) {
  if (orphaned) {
    return `orphaned since revision ${thread.anchor_revision || "?"}`;
  }
  if (thread.anchor_state === "shifted") {
    const history = thread.anchor_history || [];
    const last = history[history.length - 1] || {};
    const base = `moved · revision ${last.from_revision || "?"} to ${last.to_revision || thread.anchor_revision || "?"}`;
    return (thread.anchor_confidence || 1) < 0.95 ? base + " · close match" : base;
  }
  return "";
}

// -- focus and navigation ----------------------------------------------------

function focusThread(id) {
  state.focusedThread = id;
  renderStage();
  renderRail();
  const card = document.querySelector(`.card[data-thread="${id}"]`);
  if (card) {
    card.classList.remove("card-collapsed");
    card.classList.add("card-focused");
    card.focus();
  }
  const mark = document.querySelector(`mark[data-thread="${id}"]`);
  if (mark) mark.classList.add("mark-focused");
}

// -- composer ----------------------------------------------------------------

function openComposer(anchor, header) {
  closeComposer(true);
  const rail = $("rail");
  const composer = document.createElement("div");
  composer.className = "composer";
  const head = document.createElement("div");
  head.className = "composer-header";
  head.textContent = header;
  composer.append(head);
  const textarea = document.createElement("textarea");
  textarea.rows = 3;
  composer.append(textarea);
  const counter = document.createElement("div");
  counter.className = "composer-counter";
  counter.hidden = true;
  composer.append(counter);
  const hint = document.createElement("div");
  hint.className = "composer-hint";
  hint.textContent = "Ctrl+Enter saves";
  composer.append(hint);
  const failure = document.createElement("div");
  failure.className = "composer-failure";
  failure.hidden = true;
  composer.append(failure);
  const actions = document.createElement("div");
  actions.className = "composer-actions";
  const cancel = textButton("Cancel", () => closeComposer());
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "btn btn-primary";
  submit.textContent = "Comment";
  actions.append(cancel, submit);
  composer.append(actions);

  textarea.addEventListener("input", () => {
    const len = textarea.value.length;
    if (len >= 7500) {
      counter.hidden = false;
      if (len >= MAX_COMMENT_CHARS) {
        counter.textContent = "Comments are limited to 8,000 characters.";
        submit.disabled = true;
      } else {
        counter.textContent = `${len.toLocaleString()} / 8,000`;
        submit.disabled = false;
      }
    } else {
      counter.hidden = true;
      submit.disabled = false;
    }
  });
  textarea.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { e.preventDefault(); doSubmit(); }
  });
  submit.addEventListener("click", doSubmit);

  async function doSubmit() {
    const body = textarea.value.trim();
    if (!body) return;
    submit.disabled = true;
    submit.textContent = "Saving";
    failure.hidden = true;
    try {
      await api("/api/threads", {
        method: "POST",
        body: JSON.stringify({ stage: state.stage, anchor, body }),
      });
      state.composer = null;
      clearSelection();
      await loadPlan();
    } catch (error) {
      submit.disabled = false;
      submit.textContent = "Comment";
      failure.hidden = false;
      failure.textContent = "Could not save. Your text is still here.";
    }
  }

  state.composer = { element: composer, textarea };
  rail.prepend(composer);
  textarea.focus();
}

function openDiagramComposer(block, item) {
  const anchor = {
    kind: "mermaid",
    stage: state.stage,
    block_index: block.index,
    target: item.target,
  };
  if (item.node_id) anchor.node_id = item.node_id;
  if (item.edge) anchor.edge = { from: item.edge.from, to: item.edge.to, pair_ordinal: item.edge.pair_ordinal || 0, edge_index: item.edge.edge_index };
  if (item.label) anchor.label = item.label;
  openComposer(anchor, item.target === "edge"
    ? `edge · ${item.label}`
    : `${item.target} · ${item.label}`);
}

function commentOnSelection() {
  const selection = window.getSelection();
  const offsets = offsetsFromSelection($("doc"), selection);
  if (!offsets) return;
  const info = currentStage();
  const md = info.markdown || "";
  const exact = md.slice(offsets.start, offsets.end);
  const anchor = {
    kind: "text",
    stage: state.stage,
    start: offsets.start,
    end: offsets.end,
    exact,
    prefix: md.slice(Math.max(0, offsets.start - 32), offsets.start),
    suffix: md.slice(offsets.end, offsets.end + 32),
  };
  openComposer(anchor, `"${exact.slice(0, 80)}"`);
}

function closeComposer(silent) {
  if (!state.composer) return;
  const textarea = state.composer.textarea;
  if (!silent && textarea && textarea.value.trim()) {
    openDiscardDialog(textarea.value.trim().length, () => reallyCloseComposer());
    return;
  }
  reallyCloseComposer();
}

function reallyCloseComposer() {
  if (state.composer && state.composer.element) state.composer.element.remove();
  state.composer = null;
  clearSelection();
  $("doc-region").focus();
}

function openReply(card, threadId) {
  card.classList.remove("card-collapsed");
  if (card.querySelector(".composer")) return;
  const composer = document.createElement("div");
  composer.className = "composer";
  const textarea = document.createElement("textarea");
  textarea.rows = 2;
  composer.append(textarea);
  const failure = document.createElement("div");
  failure.className = "composer-failure";
  failure.hidden = true;
  composer.append(failure);
  const actions = document.createElement("div");
  actions.className = "composer-actions";
  const cancel = textButton("Cancel", () => composer.remove());
  const submit = document.createElement("button");
  submit.type = "button";
  submit.className = "btn btn-primary";
  submit.textContent = "Reply";
  actions.append(cancel, submit);
  composer.append(actions);
  submit.addEventListener("click", async () => {
    const body = textarea.value.trim();
    if (!body) return;
    submit.disabled = true;
    submit.textContent = "Saving";
    try {
      await api(`/api/threads/${threadId}/comments`, {
        method: "POST",
        body: JSON.stringify({ body }),
      });
      await loadPlan();
    } catch (error) {
      submit.disabled = false;
      submit.textContent = "Reply";
      failure.hidden = false;
      failure.textContent = "Could not save. Your text is still here.";
    }
  });
  card.append(composer);
  textarea.focus();
}

async function resolveThread(id) {
  try {
    await api(`/api/threads/${id}/resolve`, { method: "POST", body: JSON.stringify({ note: "" }) });
    await loadPlan();
  } catch (error) { /* keep state; a failed resolve is non-destructive */ }
}

async function reopenThread(id) {
  try {
    await api(`/api/threads/${id}/reopen`, { method: "POST", body: JSON.stringify({}) });
    await loadPlan();
  } catch (error) { /* non-destructive */ }
}

// -- dialogs -----------------------------------------------------------------

function openDiscardDialog(count, onConfirm) {
  const dialog = $("dialog-discard");
  $("discard-body").textContent = `You have written ${count} characters that have not been saved.`;
  dialog.querySelector("[data-discard-keep]").onclick = () => dialog.close();
  dialog.querySelector("[data-discard-confirm]").onclick = () => { dialog.close(); onConfirm(); };
  dialog.showModal();
}

function openRequestChangesDialog() {
  const dialog = $("dialog-request-changes");
  const open = (state.data.summary || {}).open || 0;
  $("rc-summary").textContent =
    `${open} open comments go to the architect as one steering note. The plan moves to ` +
    "needs_review and work stays blocked until the architect rewrites it.";
  $("rc-note").value = "";
  $("rc-error").hidden = true;
  dialog.showModal();
}

function openApproveDialog() {
  const dialog = $("dialog-approve");
  const open = (state.data.summary || {}).open || 0;
  const plan = state.data.plan;
  $("approve-note").value = "";
  $("approve-error").hidden = true;
  const body2 = $("approve-body-2");
  const confirm = $("approve-confirm");
  if (open > 0) {
    $("approve-heading").textContent = `Approve with ${open} open comments?`;
    $("approve-body").textContent =
      "These comments stay open and nobody is required to answer them. Request changes " +
      "instead if you want them addressed first.";
    body2.hidden = false;
    body2.textContent = `Approval releases the engineer and the tester to build ${plan}.`;
    confirm.textContent = "Approve anyway";
    dialog.dataset.confirmOpen = "1";
  } else {
    $("approve-heading").textContent = "Approve this plan";
    $("approve-body").textContent =
      `Approval releases the engineer and the tester to build ${plan}. It is ` +
      "recorded against your name and this workspace cannot undo it.";
    body2.hidden = true;
    confirm.textContent = "Approve";
    dialog.dataset.confirmOpen = "0";
  }
  dialog.showModal();
}

function wireDialogs() {
  for (const dialog of document.querySelectorAll("dialog")) {
    for (const close of dialog.querySelectorAll("[data-close]")) {
      close.addEventListener("click", () => dialog.close());
    }
  }
  $("dialog-request-changes").querySelector("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const note = $("rc-note").value;
    try {
      await api("/api/request-changes", { method: "POST", body: JSON.stringify({ note }) });
      $("dialog-request-changes").close();
      await loadPlan();
    } catch (error) {
      $("rc-error").hidden = false;
      $("rc-error").textContent = error.message;
    }
  });
  $("dialog-approve").querySelector("form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const note = $("approve-note").value;
    const confirmOpen = $("dialog-approve").dataset.confirmOpen === "1";
    try {
      await api("/api/approve", { method: "POST", body: JSON.stringify({ confirm_open: confirmOpen, note }) });
      $("dialog-approve").close();
      await loadPlan();
    } catch (error) {
      $("approve-error").hidden = false;
      $("approve-error").textContent = "Could not approve: " + error.message;
    }
  });
}

// -- revision banner and session end -----------------------------------------

function renderRevisionBanner() {
  const banner = $("revision-banner");
  if (!state.pendingRevision) {
    banner.hidden = true;
    return;
  }
  const { stage, revision } = state.pendingRevision;
  banner.hidden = false;
  banner.replaceChildren();
  const text = document.createElement("span");
  text.textContent = `The architect rewrote the ${stage} plan — revision ${revision}.`;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "btn";
  button.textContent = "Reload the plan";
  button.addEventListener("click", () => {
    state.reanchorSummary = summariseReanchor();
    state.pendingRevision = null;
    banner.hidden = true;
    renderStage();
    renderRail();
  });
  banner.append(text, button);
}

function summariseReanchor() {
  const open = threadsForStage(state.stage).filter((t) => t.status === "open");
  const moved = open.filter((t) => t.anchor_state === "shifted").length;
  const orphaned = open.filter((t) => t.anchor_state === "orphaned").length;
  if (!moved && !orphaned) return "";
  const parts = [];
  if (moved) parts.push(`${moved} comment${moved === 1 ? "" : "s"} moved`);
  if (orphaned) parts.push(`${orphaned} orphaned`);
  return `${parts.join(" and ")} when the plan changed.`;
}

function renderLoadError() {
  const doc = $("doc");
  doc.replaceChildren();
  const card = document.createElement("div");
  card.className = "error-card alert";
  card.setAttribute("role", "alert");
  const h = document.createElement("h2");
  h.textContent = "Could not reach the review server";
  const p = document.createElement("p");
  p.textContent = "It may have stopped, or the idle timeout expired.";
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "btn";
  retry.textContent = "Retry";
  retry.addEventListener("click", loadPlan);
  card.append(h, p, retry);
  doc.append(card);
}

function endSession() {
  if (state.ended) return;
  state.ended = true;
  const plan = state.data ? state.data.plan : "";
  document.body.replaceChildren();
  const wrap = document.createElement("div");
  wrap.className = "session-ended";
  const h = document.createElement("h1");
  h.textContent = "This review session has ended";
  const p = document.createElement("p");
  p.textContent =
    "The local server has stopped. Every comment you wrote was saved as it was written.";
  const p2 = document.createElement("p");
  p2.textContent = "Reopen the workspace with:";
  const code = document.createElement("code");
  code.textContent = `grogu review ${plan}`;
  wrap.append(h, p, p2, code);
  document.body.append(wrap);
}

// -- small helpers -----------------------------------------------------------

function textButton(label, handler) {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "btn-text";
  btn.textContent = label;
  btn.addEventListener("click", handler);
  return btn;
}

function clearSelection() {
  const selection = window.getSelection();
  if (selection) selection.removeAllRanges();
}

async function copyText(text, button) {
  try {
    await navigator.clipboard.writeText(text);
    const was = button.textContent;
    button.textContent = "Copied";
    setTimeout(() => { button.textContent = was; }, 1500);
  } catch (error) { /* clipboard denied; the reader can still select the text */ }
}

function formatStamp(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false })
    .replace(",", "");
}

function formatRelative(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return iso;
  const secs = Math.floor((Date.now() - then) / 1000);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return formatStamp(iso);
}

// -- selection button --------------------------------------------------------

function setupSelectionButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "selection-button";
  button.textContent = "Comment";
  button.hidden = true;
  button.addEventListener("click", () => { button.hidden = true; commentOnSelection(); });
  document.body.append(button);

  document.addEventListener("selectionchange", () => {
    const selection = window.getSelection();
    const offsets = offsetsFromSelection($("doc"), selection);
    if (!offsets || state.composer) { button.hidden = true; return; }
    const range = selection.getRangeAt(0);
    const rects = range.getClientRects();
    if (!rects.length) { button.hidden = true; return; }
    const last = rects[rects.length - 1];
    button.style.top = `${window.scrollY + last.bottom + 8}px`;
    button.style.left = `${window.scrollX + range.getBoundingClientRect().left}px`;
    button.hidden = false;
  });
}

// -- keyboard ----------------------------------------------------------------

function orderedThreads() {
  return threadsForStage(state.stage).filter((t) => t.status === "open");
}

function moveFocus(delta, orphanOnly) {
  let list = orderedThreads();
  if (orphanOnly) list = list.filter((t) => t.anchor_state === "orphaned");
  if (!list.length) return;
  const idx = list.findIndex((t) => t.id === state.focusedThread);
  const next = list[(idx + delta + list.length) % list.length] || list[0];
  focusThread(next.id);
}

function setupKeyboard() {
  document.addEventListener("keydown", (e) => {
    if (e.target.matches("textarea, input, select")) return;
    const tag = e.key;
    if (tag === "?") { e.preventDefault(); $("dialog-keyboard").showModal(); return; }
    if (tag === "Escape") {
      if (state.composer) { closeComposer(); return; }
    }
    if (tag === "c") { e.preventDefault(); commentOnSelection(); return; }
    if (tag === "j") { e.preventDefault(); moveFocus(1, false); return; }
    if (tag === "k") { e.preventDefault(); moveFocus(-1, false); return; }
    if (tag === "o") { e.preventDefault(); moveFocus(1, true); return; }
    if (tag === "[") { e.preventDefault(); stepStage(-1); return; }
    if (tag === "]") { e.preventDefault(); stepStage(1); return; }
    if (["1", "2", "3", "4"].includes(tag)) {
      e.preventDefault();
      switchStage(STAGE_ORDER[Number(tag) - 1]);
      return;
    }
    if (tag === "r" && state.focusedThread) { e.preventDefault(); const card = document.querySelector(`.card[data-thread="${state.focusedThread}"]`); if (card) openReply(card, state.focusedThread); return; }
    if (tag === "e" && state.focusedThread) { e.preventDefault(); resolveThread(state.focusedThread); return; }
  });
}

function stepStage(delta) {
  const idx = STAGE_ORDER.indexOf(state.stage);
  const next = STAGE_ORDER[(idx + delta + STAGE_ORDER.length) % STAGE_ORDER.length];
  switchStage(next);
}

// -- polling -----------------------------------------------------------------

function setupPolling() {
  setInterval(() => {
    if (document.hidden || state.ended) return;
    loadPlan();
  }, POLL_MS);
}

// -- boot --------------------------------------------------------------------

function boot() {
  $("request-changes-button").addEventListener("click", openRequestChangesDialog);
  $("approve-button").addEventListener("click", openApproveDialog);
  $("end-session-button").addEventListener("click", async () => {
    try { await api("/api/shutdown", { method: "POST", body: JSON.stringify({}) }); } catch (e) {}
    endSession();
  });
  const drawer = $("drawer-toggle");
  drawer.addEventListener("click", () => $("rail").classList.toggle("rail-open"));
  wireDialogs();
  setupSelectionButton();
  setupKeyboard();
  setupPolling();
  loadPlan();
}

boot();
