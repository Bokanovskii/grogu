// Small pure helpers for formatting observable facts. No side effects.

/** Relative "N ago" using observed millisecond timestamps. Deterministic given
 * (at, now). Buckets match the design's freshness rules. */
export function ago(atMs: number, nowMs: number): string {
  const s = Math.max(0, Math.floor((nowMs - atMs) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  return `${d}d ago`;
}

export type FreshnessBucket = "live" | "idle" | "stale" | "dead";

export function freshnessBucket(atMs: number, nowMs: number): FreshnessBucket {
  const s = (nowMs - atMs) / 1000;
  if (s < 60) return "live";
  if (s < 5 * 60) return "idle";
  if (s < 15 * 60) return "stale";
  return "dead";
}

export function freshnessLabel(atMs: number, nowMs: number): string {
  const bucket = freshnessBucket(atMs, nowMs);
  const label = ago(atMs, nowMs);
  const word = { live: "Live", idle: "Idle", stale: "Stale", dead: "Dead" }[bucket];
  return `${word} · ${label}`;
}

/** Duration in ms as a compact human string, for tool call durations. */
export function durationMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(s < 10 ? 1 : 0)} s`;
  const m = Math.floor(s / 60);
  return `${m}m ${Math.round(s % 60)}s`;
}

/** Short form of a sha256:... digest for headers. */
export function shortDigest(digest: string): string {
  const hex = digest.replace(/^sha256:/, "");
  return "sha256:" + hex.slice(0, 12);
}

/** Format a wall-clock timestamp (ms) as HH:MM:SS. */
export function clock(atMs: number): string {
  const d = new Date(atMs);
  const p = (n: number) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

/** Day key for grouping revisions in the timeline. */
export function dayKey(atMs: number): string {
  const d = new Date(atMs);
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}
