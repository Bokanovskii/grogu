// The single HTTP boundary. The application makes no other network call. Every
// mutating request carries the CSRF token in X-Grogu-Token (double-submit with
// a readable cookie, or a bootstrap meta tag) and same-origin credentials; the
// server also checks the HttpOnly session cookie and the Origin. No absolute
// URLs are ever constructed, so nothing can address a non-loopback origin.

import type {
  AgentDrillResponse,
  ApiError,
  ControlResponse,
  DocResponse,
  FeedbackRecord,
  FeedbackResponse,
  FeedbackScope,
  HealthResponse,
  Impact,
  LayoutRequest,
  LayoutResponse,
  PatchOk,
  PatchRequest,
  ProjectionResponse,
  Proposal,
  ProposalPreview,
  RevisionEnvelope,
  Selector,
  Thread,
} from "./types";

export class ApiFailure extends Error {
  status: number;
  body: ApiError | null;
  constructor(status: number, body: ApiError | null) {
    super(body?.message || body?.error || `HTTP ${status}`);
    this.name = "ApiFailure";
    this.status = status;
    this.body = body;
  }
}

/** A 409 the caller rebases from. */
export class StaleError extends Error {
  revision: string;
  opsSince: PatchRequest["ops"];
  constructor(revision: string, opsSince: PatchRequest["ops"]) {
    super(`stale; server at ${revision}`);
    this.name = "StaleError";
    this.revision = revision;
    this.opsSince = opsSince;
  }
}

function readCsrfToken(): string {
  const meta = document.querySelector('meta[name="grogu-csrf"]');
  const fromMeta = meta?.getAttribute("content");
  if (fromMeta) return fromMeta;
  const m = document.cookie.match(/(?:^|;\s*)grogu_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]!) : "";
}

const BASE = "/api";
const REQUEST_TIMEOUT_MS = 30_000;

async function request<T>(
  path: string,
  opts: { method?: string; body?: unknown; signal?: AbortSignal } = {},
): Promise<T> {
  const method = opts.method ?? "GET";
  const headers: Record<string, string> = { Accept: "application/json" };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && method !== "HEAD") {
    headers["X-Grogu-Token"] = readCsrfToken();
  }
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort();
  opts.signal?.addEventListener("abort", abortFromCaller, { once: true });
  const timeout = window.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  let resp: Response;
  try {
    resp = await fetch(BASE + path, {
      method,
      headers,
      credentials: "same-origin",
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      signal: controller.signal,
    });
  } catch (e) {
    throw new ApiFailure(0, {
      error: timedOut ? "timeout" : "network",
      message: timedOut
        ? "The local Grogu server did not respond within 30 seconds."
        : String(e),
    });
  } finally {
    window.clearTimeout(timeout);
    opts.signal?.removeEventListener("abort", abortFromCaller);
  }
  const text = await resp.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }
  if (resp.status === 409 && parsed && (parsed as ApiError).error === "stale") {
    const b = parsed as { revision: string; ops_since: PatchRequest["ops"] };
    throw new StaleError(b.revision, b.ops_since ?? []);
  }
  if (!resp.ok) {
    throw new ApiFailure(resp.status, (parsed as ApiError) ?? null);
  }
  return parsed as T;
}

export const api = {
  health: () => request<HealthResponse>("/health"),

  doc: (stage?: string, include?: string) => {
    const q = new URLSearchParams();
    if (stage) q.set("stage", stage);
    if (include) q.set("include", include);
    const s = q.toString();
    return request<DocResponse>(`/doc${s ? "?" + s : ""}`);
  },

  projection: (params: {
    role: string;
    stage?: string;
    include?: string;
    budget?: number;
    format?: "md" | "json";
    since?: string;
  }) => {
    const q = new URLSearchParams();
    q.set("role", params.role);
    if (params.stage) q.set("stage", params.stage);
    if (params.include) q.set("include", params.include);
    if (params.budget != null) q.set("budget", String(params.budget));
    if (params.format) q.set("format", params.format);
    if (params.since) q.set("since", params.since);
    return request<ProjectionResponse>(`/projection?${q.toString()}`);
  },

  patch: (req: PatchRequest) =>
    request<PatchOk>("/patch", { method: "POST", body: req }),

  createThread: (selector: Selector, body: string, kind: string) =>
    request<{ thread: Thread }>("/threads", {
      method: "POST",
      body: { selector, body, kind },
    }),
  replyThread: (tid: string, body: string) =>
    request<{ thread: Thread }>(`/threads/${tid}/reply`, {
      method: "POST",
      body: { body },
    }),
  resolveThread: (tid: string, note: string) =>
    request<{ thread: Thread }>(`/threads/${tid}/resolve`, {
      method: "POST",
      body: { note },
    }),
  reopenThread: (tid: string) =>
    request<{ thread: Thread }>(`/threads/${tid}/reopen`, {
      method: "POST",
      body: {},
    }),
  reviseThread: (tid: string, instruction: string) =>
    request<{ request: FeedbackResponse }>(`/threads/${tid}/revise`, {
      method: "POST",
      body: { instruction },
    }),

  proposals: () => request<{ proposals: Proposal[] }>("/proposals"),
  createProposal: (ops: PatchRequest["ops"], why: string, fromThread: string | undefined, base: string) =>
    request<{ proposal: Proposal }>("/proposals", {
      method: "POST",
      body: { ops, why, from_thread: fromThread, base },
    }),
  restoreAsProposal: (revision: string, base: string) =>
    request<{ proposal: Proposal }>("/proposals", {
      method: "POST",
      body: { restore_from: revision, base, why: `Restore state of ${revision}` },
    }),
  proposalPreview: (pid: string) =>
    request<ProposalPreview>(`/proposals/${pid}/preview`),
  acceptProposal: (pid: string) =>
    request<{ revision: string }>(`/proposals/${pid}/accept`, {
      method: "POST",
      body: {},
    }),
  rejectProposal: (pid: string, whyNot: string) =>
    request<{ proposal: Proposal }>(`/proposals/${pid}/reject`, {
      method: "POST",
      body: { why_not: whyNot },
    }),

  impact: (selector: Selector, depth: number) =>
    request<Impact>("/impact", { method: "POST", body: { selector, depth } }),

  layout: (req: LayoutRequest) =>
    request<LayoutResponse>("/layout", { method: "POST", body: req }),

  revisions: () => request<{ revisions: RevisionEnvelope[] }>("/revisions"),
  diff: (from: string, to: string) =>
    request<{ from: string; to: string; before: string; after: string }>(
      `/diff?from=${from}&to=${to}`,
    ),

  control: (params: {
    scope?: "repository_program" | "current_plan";
    plan?: string;
    role?: string;
    workstream?: string;
    state?: string;
    window?: number;
  }) => {
    const q = new URLSearchParams();
    if (params.scope) q.set("scope", params.scope);
    if (params.plan) q.set("plan", params.plan);
    if (params.role) q.set("role", params.role);
    if (params.workstream) q.set("workstream", params.workstream);
    if (params.state) q.set("state", params.state);
    if (params.window != null) q.set("window", String(params.window));
    const s = q.toString();
    return request<ControlResponse>(`/control${s ? "?" + s : ""}`);
  },
  controlAgent: (agentKey: string) =>
    request<AgentDrillResponse>(`/control/${encodeURIComponent(agentKey)}`),
  activityForObject: (objectId: string, windowMinutes = 1440) =>
    request<{ events: import("./types").AuditEvent[] }>(
      `/activity?object=${encodeURIComponent(objectId)}&window=${windowMinutes}`,
    ),
  listFeedback: (plan?: string) => {
    const query = plan ? `?plan=${encodeURIComponent(plan)}` : "";
    return request<{ feedback: FeedbackRecord[] }>(`/feedback${query}`);
  },
  feedback: (
    agentKey: string,
    text: string,
    binding: boolean,
    scope: string,
    plan?: string,
  ) =>
    request<FeedbackResponse>(`/control/${encodeURIComponent(agentKey)}/feedback`, {
      method: "POST",
      body: { text, binding, scope, plan },
    }),
  sendScopedFeedback: (scope: FeedbackScope, text: string, binding: boolean) =>
    request<FeedbackResponse>("/feedback", {
      method: "POST",
      body: { scope, text, binding },
    }),
  withdrawFeedback: (id: string, plan?: string) =>
    request<{ ok: boolean }>(`/feedback/${encodeURIComponent(id)}/withdraw`, {
      method: "POST",
      body: { plan },
    }),

  requestChanges: (note: string) =>
    request<{ ok: boolean }>("/request-changes", { method: "POST", body: { note } }),
  approve: (confirmOpen: boolean, note: string) =>
    request<{ ok: boolean } | ApiError>("/approve", {
      method: "POST",
      body: { confirm_open: confirmOpen, note },
    }),
  shutdown: () => request<{ ok: boolean }>("/shutdown", { method: "POST", body: {} }),
};

export type SseHandler = (type: string, data: unknown) => void;

/** Connect to the SSE stream. Returns a disposer. Reconnect with visible
 * backoff is the caller's concern. */
export function connectEvents(onEvent: SseHandler, onError: () => void): () => void {
  const es = new EventSource(BASE + "/events", { withCredentials: true });
  const types = ["revision", "proposal", "thread", "control", "shutdown"];
  for (const t of types) {
    es.addEventListener(t, (ev) => {
      let data: unknown = null;
      try {
        data = (ev as MessageEvent).data ? JSON.parse((ev as MessageEvent).data) : null;
      } catch {
        data = null;
      }
      onEvent(t, data);
    });
  }
  es.onerror = () => onError();
  return () => es.close();
}
