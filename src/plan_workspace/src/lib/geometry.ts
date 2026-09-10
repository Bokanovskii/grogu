import type { Geometry, PlanNode } from "../api/types";

// Canvas geometry is integers in canvas units (1 unit = 1 CSS px at 100% zoom).
// Every value that reaches a patch passes through here so nothing fractional is
// ever persisted.

export function ri(n: number): number {
  return Math.round(n);
}

export function intGeometry(g: Geometry): Geometry {
  return { x: ri(g.x), y: ri(g.y), w: ri(g.w), h: ri(g.h), z: ri(g.z) };
}

export interface Rect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export function nodeRect(n: PlanNode): Rect | null {
  if (!n.geometry) return null;
  return { x: n.geometry.x, y: n.geometry.y, w: n.geometry.w, h: n.geometry.h };
}

export function rectsIntersect(a: Rect, b: Rect): boolean {
  return (
    a.x < b.x + b.w &&
    a.x + a.w > b.x &&
    a.y < b.y + b.h &&
    a.y + a.h > b.y
  );
}

export function rectContains(outer: Rect, inner: Rect): boolean {
  return (
    inner.x >= outer.x &&
    inner.y >= outer.y &&
    inner.x + inner.w <= outer.x + outer.w &&
    inner.y + inner.h <= outer.y + outer.h
  );
}

export function boundingBox(rects: Rect[]): Rect | null {
  if (rects.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const r of rects) {
    minX = Math.min(minX, r.x);
    minY = Math.min(minY, r.y);
    maxX = Math.max(maxX, r.x + r.w);
    maxY = Math.max(maxY, r.y + r.h);
  }
  return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
}

/** Snap a value to the 8px grid. */
export function snapToGrid(v: number, grid = 8): number {
  return Math.round(v / grid) * grid;
}

/** Compute snap guides for a moving rect against sibling rects, within a
 * tolerance. Returns adjusted x/y and the guide lines to draw. */
export interface SnapGuide {
  orientation: "v" | "h";
  at: number;
  from: number;
  to: number;
  distanceLabel?: string;
}

export function computeSnap(
  moving: Rect,
  siblings: Rect[],
  tolerance: number,
): { dx: number; dy: number; guides: SnapGuide[] } {
  const guides: SnapGuide[] = [];
  let bestDx = 0;
  let bestDy = 0;
  let bestDxDist = tolerance + 1;
  let bestDyDist = tolerance + 1;

  const movingXs = [moving.x, moving.x + moving.w / 2, moving.x + moving.w];
  const movingYs = [moving.y, moving.y + moving.h / 2, moving.y + moving.h];

  for (const s of siblings) {
    const sXs = [s.x, s.x + s.w / 2, s.x + s.w];
    const sYs = [s.y, s.y + s.h / 2, s.y + s.h];
    for (const mx of movingXs) {
      for (const sx of sXs) {
        const d = sx - mx;
        if (Math.abs(d) <= tolerance && Math.abs(d) < Math.abs(bestDxDist)) {
          bestDxDist = d;
          bestDx = d;
        }
      }
    }
    for (const my of movingYs) {
      for (const sy of sYs) {
        const d = sy - my;
        if (Math.abs(d) <= tolerance && Math.abs(d) < Math.abs(bestDyDist)) {
          bestDyDist = d;
          bestDy = d;
        }
      }
    }
  }

  if (Math.abs(bestDxDist) <= tolerance) {
    const at = moving.x + bestDx;
    guides.push({ orientation: "v", at, from: moving.y - 40, to: moving.y + moving.h + 40 });
  }
  if (Math.abs(bestDyDist) <= tolerance) {
    const at = moving.y + bestDy;
    guides.push({ orientation: "h", at, from: moving.x - 40, to: moving.x + moving.w + 40 });
  }
  return { dx: guides.length ? bestDx : 0, dy: guides.some((g) => g.orientation === "h") ? bestDy : 0, guides };
}
