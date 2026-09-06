import { useMemo, useState } from "react";
import { useApp } from "../../state/store";
import type { ThreadKind } from "../../api/types";
import { deriveThreads, THREAD_KIND_META } from "./threads";
import { ThreadCard } from "./ThreadCard";
import { Chip } from "../../shell/ui";
import { EmptyState } from "../../shell/StatePanels";

type Filter = "open" | "resolved" | "orphaned" | "mine";

// The comment rail: round chip, filter chips, thread cards in document order.
// markOffsets maps a thread id to the vertical offset of its mark in the
// reading column; when provided, cards align to their mark with a minimum 8px
// gap between colliding cards (the p-20260904-6095a8 rule, defect d2).
export function CommentRail({
  markOffsets,
  round,
  changesRequested,
}: {
  markOffsets?: Record<string, number>;
  round?: number;
  changesRequested?: boolean;
}) {
  const state = useApp();
  const [filters, setFilters] = useState<Filter[]>([]);
  const [kinds, setKinds] = useState<ThreadKind[]>([]);

  const threads = useMemo(() => deriveThreads(state.nodes), [state.nodes]);
  const stageThreads = threads.filter((t) => {
    const anchor = t.anchorNode ? state.nodes[t.anchorNode] : undefined;
    return !anchor || anchor.stage === state.stage || anchor.stage === "";
  });

  const filtered = stageThreads.filter((t) => {
    if (filters.includes("open") && t.status !== "open") return false;
    if (filters.includes("resolved") && t.status !== "resolved") return false;
    if (filters.includes("orphaned") && t.anchorState !== "orphaned" && t.status !== "orphaned") return false;
    if (kinds.length && !kinds.includes(t.kind)) return false;
    return true;
  });

  const openCount = stageThreads.filter((t) => t.status === "open").length;
  const roundLabel =
    round != null
      ? changesRequested
        ? `Round ${round} · changes requested · ${openCount} open`
        : `Round ${round} · no changes requested`
      : null;

  // Collision-push layout: when we know mark offsets, place each card at
  // max(markTop, previousBottom + 8). Otherwise stack naturally.
  const positioned = useMemo(() => {
    if (!markOffsets) return null;
    const est = 140; // estimated card height; refined by the DOM in practice
    let prevBottom = 0;
    return filtered.map((t) => {
      const markTop = markOffsets[t.id] ?? prevBottom + 8;
      const top = Math.max(markTop, prevBottom + 8);
      prevBottom = top + est;
      return { thread: t, top };
    });
  }, [filtered, markOffsets]);

  const toggle = (f: Filter) => setFilters((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  const toggleKind = (k: ThreadKind) =>
    setKinds((cur) => (cur.includes(k) ? cur.filter((x) => x !== k) : [...cur, k]));

  return (
    <div className="comment-rail">
      {roundLabel ? (
        <div className="rail-round-chip" aria-live="polite">
          {roundLabel}
        </div>
      ) : null}

      <div className="rail-filters">
        {(["open", "resolved", "orphaned", "mine"] as Filter[]).map((f) => (
          <Chip key={f} onClick={() => toggle(f)} active={filters.includes(f)}>
            {f[0]!.toUpperCase() + f.slice(1)}
          </Chip>
        ))}
        {(Object.keys(THREAD_KIND_META) as ThreadKind[]).map((k) => (
          <Chip key={k} glyph={THREAD_KIND_META[k].glyph} onClick={() => toggleKind(k)} active={kinds.includes(k)}>
            {THREAD_KIND_META[k].label}
          </Chip>
        ))}
      </div>

      {filtered.length === 0 ? (
        <EmptyState title="No comments yet.">
          <p>Select text, an object, or a region and press C to start a conversation.</p>
        </EmptyState>
      ) : positioned ? (
        <div className="rail-positioned" style={{ position: "relative" }}>
          {positioned.map(({ thread, top }) => (
            <div key={thread.id} className="rail-card-slot" style={{ position: "absolute", top, left: 0, right: 0 }}>
              <ThreadCard thread={thread} active={state.selection.some((s) => s.id === thread.id)} onSelect={() => undefined} />
            </div>
          ))}
        </div>
      ) : (
        <div className="rail-stack">
          {filtered.map((thread) => (
            <ThreadCard
              key={thread.id}
              thread={thread}
              active={state.selection.some((s) => s.id === thread.id)}
              onSelect={() => undefined}
            />
          ))}
        </div>
      )}
    </div>
  );
}
