import { useReactFlow, useViewport } from "@xyflow/react";
import { useState } from "react";

// Custom zoom control: − 100% + Fit. The percentage is an editable numeric
// input. All controls are keyboard-focusable.
export function ZoomControl() {
  const { zoomIn, zoomOut, zoomTo, fitView } = useReactFlow();
  const { zoom } = useViewport();
  const [draft, setDraft] = useState<string | null>(null);
  const pct = Math.round(zoom * 100);

  return (
    <div className="cv-zoom" role="group" aria-label="Zoom">
      <button type="button" className="cv-zoom-btn" aria-label="Zoom out" onClick={() => zoomOut()}>
        −
      </button>
      <input
        className="cv-zoom-input"
        aria-label="Zoom percentage"
        value={draft ?? String(pct)}
        onChange={(e) => setDraft(e.target.value.replace(/[^\d]/g, ""))}
        onBlur={() => {
          if (draft) {
            const v = Math.max(10, Math.min(400, Number(draft)));
            zoomTo(v / 100);
          }
          setDraft(null);
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter") (e.target as HTMLInputElement).blur();
        }}
      />
      <span className="cv-zoom-pct" aria-hidden="true">
        %
      </span>
      <button type="button" className="cv-zoom-btn" aria-label="Zoom in" onClick={() => zoomIn()}>
        +
      </button>
      <button type="button" className="cv-zoom-fit" onClick={() => fitView({ duration: 200, padding: 0.2 })}>
        Fit
      </button>
    </div>
  );
}
