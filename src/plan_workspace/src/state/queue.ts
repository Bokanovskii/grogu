import type { PatchOp } from "../api/types";

// The crash-recovery / offline queue. A queued item is an *intent* — the ops
// and the base revision the user last saw — never compiled output. Persisted to
// localStorage under a per-plan namespace so a closed tab can restore.

export interface QueuedIntent {
  id: string;
  ops: PatchOp[];
  base: string;
  intent: string;
  origin: string;
  at: number;
}

const PREFIX = "grogu.plan-workspace";

function key(plan: string): string {
  return `${PREFIX}.queue.${plan}`;
}

export function loadQueue(plan: string): QueuedIntent[] {
  if (!plan) return [];
  try {
    const raw = localStorage.getItem(key(plan));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? (parsed as QueuedIntent[]) : [];
  } catch {
    return [];
  }
}

export function saveQueue(plan: string, queue: QueuedIntent[]): void {
  if (!plan) return;
  try {
    if (queue.length === 0) localStorage.removeItem(key(plan));
    else localStorage.setItem(key(plan), JSON.stringify(queue));
  } catch {
    /* storage full or unavailable — the queue stays in memory only */
  }
}

// Panel widths, collapse state, density, and theme persist per-plan too.
export function loadPref<T>(name: string, plan: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(`${PREFIX}.${name}.${plan || "_"}`);
    if (raw == null) return fallback;
    return JSON.parse(raw) as T;
  } catch {
    return fallback;
  }
}

export function savePref<T>(name: string, plan: string, value: T): void {
  try {
    localStorage.setItem(`${PREFIX}.${name}.${plan || "_"}`, JSON.stringify(value));
  } catch {
    /* ignore */
  }
}
