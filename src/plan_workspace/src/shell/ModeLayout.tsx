import React, { useCallback, useRef } from "react";
import { useActions, useApp } from "../state/store";

// The left/right resizable, collapsible panel frame shared by every plan mode.
// Left: default 280 (min 240, max 400). Right: default 360 (min 320, max 520).
// Each collapses to a 40px rail with a chevron; state persists per plan.

function ResizeGutter({
  side,
  width,
  min,
  max,
  onResize,
}: {
  side: "left" | "right";
  width: number;
  min: number;
  max: number;
  onResize: (w: number) => void;
}) {
  const startX = useRef(0);
  const startW = useRef(0);
  const onDown = useCallback(
    (e: React.PointerEvent) => {
      startX.current = e.clientX;
      startW.current = width;
      (e.target as HTMLElement).setPointerCapture(e.pointerId);
      const move = (ev: PointerEvent) => {
        const dx = ev.clientX - startX.current;
        onResize(side === "left" ? startW.current + dx : startW.current - dx);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    },
    [onResize, side, width],
  );
  return (
    <div
      className={`panel-gutter panel-gutter-${side}`}
      role="separator"
      aria-orientation="vertical"
      aria-label={`Resize ${side} panel`}
      aria-valuenow={Math.round(width)}
      aria-valuemin={min}
      aria-valuemax={max}
      onPointerDown={onDown}
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "ArrowLeft") onResize(side === "left" ? width - 16 : width + 16);
        if (e.key === "ArrowRight") onResize(side === "left" ? width + 16 : width - 16);
      }}
    />
  );
}

export function ModeLayout({
  left,
  leftTitle,
  right,
  rightTitle,
  compact = false,
  leftCollapsed: leftCollapsedOverride,
  rightCollapsed: rightCollapsedOverride,
  onLeftCollapsedChange,
  onRightCollapsedChange,
  children,
}: {
  left: React.ReactNode;
  leftTitle: string;
  right: React.ReactNode;
  rightTitle: string;
  compact?: boolean;
  leftCollapsed?: boolean;
  rightCollapsed?: boolean;
  onLeftCollapsedChange?: (collapsed: boolean) => void;
  onRightCollapsedChange?: (collapsed: boolean) => void;
  children: React.ReactNode;
}) {
  const { panels } = useApp();
  const actions = useActions();
  const leftRange = compact ? { min: 216, max: 256 } : { min: 240, max: 400 };
  const rightRange = compact ? { min: 216, max: 288 } : { min: 320, max: 520 };
  const clampLeft = (w: number) => Math.max(leftRange.min, Math.min(leftRange.max, w));
  const clampRight = (w: number) => Math.max(rightRange.min, Math.min(rightRange.max, w));
  const leftWidth = clampLeft(panels.leftWidth);
  const rightWidth = clampRight(panels.rightWidth);
  const leftCollapsed = leftCollapsedOverride ?? panels.leftCollapsed;
  const rightCollapsed = rightCollapsedOverride ?? panels.rightCollapsed;
  const setLeftCollapsed = (collapsed: boolean) => {
    if (onLeftCollapsedChange) onLeftCollapsedChange(collapsed);
    else actions.patchPanels({ leftCollapsed: collapsed });
  };
  const setRightCollapsed = (collapsed: boolean) => {
    if (onRightCollapsedChange) onRightCollapsedChange(collapsed);
    else actions.patchPanels({ rightCollapsed: collapsed });
  };

  return (
    <div className={`mode-layout${compact ? " mode-layout-compact" : ""}`}>
      {leftCollapsed ? (
        <div className="panel-rail panel-rail-left">
          <button
            type="button"
            className="rail-expand"
            aria-label={`Expand ${leftTitle} panel`}
            title={`Expand ${leftTitle}`}
            onClick={() => setLeftCollapsed(false)}
          >
            ›
          </button>
          <span className="rail-label">{leftTitle}</span>
        </div>
      ) : (
        <>
          <section
            className="panel-left"
            style={{ width: leftWidth }}
            aria-label={leftTitle}
          >
            <div className="panel-head">
              <span className="panel-head-title">{leftTitle}</span>
              <button
                type="button"
                className="panel-collapse"
                aria-label={`Collapse ${leftTitle} panel`}
                title="Collapse"
                onClick={() => setLeftCollapsed(true)}
              >
                ‹
              </button>
            </div>
            <div className="panel-scroll">{left}</div>
          </section>
          <ResizeGutter
            side="left"
            width={leftWidth}
            min={leftRange.min}
            max={leftRange.max}
            onResize={(w) => actions.patchPanels({ leftWidth: clampLeft(w) })}
          />
        </>
      )}

      <main className="workspace-body" id="workspace-body" tabIndex={-1}>
        {children}
      </main>

      {rightCollapsed ? (
        <div className="panel-rail panel-rail-right">
          <button
            type="button"
            className="rail-expand"
            aria-label={`Expand ${rightTitle} panel`}
            title={`Expand ${rightTitle}`}
            onClick={() => setRightCollapsed(false)}
          >
            ‹
          </button>
          <span className="rail-label">{rightTitle}</span>
        </div>
      ) : (
        <>
          <ResizeGutter
            side="right"
            width={rightWidth}
            min={rightRange.min}
            max={rightRange.max}
            onResize={(w) => actions.patchPanels({ rightWidth: clampRight(w) })}
          />
          <section
            className="panel-right"
            style={{ width: rightWidth }}
            aria-label={rightTitle}
          >
            <div className="panel-head">
              <button
                type="button"
                className="panel-collapse"
                aria-label={`Collapse ${rightTitle} panel`}
                title="Collapse"
                onClick={() => setRightCollapsed(true)}
              >
                ›
              </button>
              <span className="panel-head-title">{rightTitle}</span>
            </div>
            <div className="panel-scroll">{right}</div>
          </section>
        </>
      )}
    </div>
  );
}
