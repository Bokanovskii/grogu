import React, { useEffect, useId, useRef, useState } from "react";

export interface MenuItem {
  label: string;
  onSelect?: () => void;
  disabled?: boolean;
  hint?: string;
  danger?: boolean;
}

// A small accessible dropdown: button toggles a listbox-like menu; Escape and
// outside-click close it; ArrowUp/Down move focus among items.
export function Menu({
  label,
  items,
  className,
  glyph,
  chevron = true,
  align = "start",
}: {
  label: React.ReactNode;
  items: MenuItem[];
  className?: string;
  glyph?: string;
  chevron?: boolean;
  align?: "start" | "end";
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const id = useId();

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <div className={`menu-root${className ? " " + className : ""}`} ref={rootRef}>
      <button
        type="button"
        className="menu-trigger"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        {glyph ? (
          <span className="menu-glyph" aria-hidden="true">
            {glyph}
          </span>
        ) : null}
        <span className="menu-label">{label}</span>
        {chevron ? (
          <span className="menu-chevron" aria-hidden="true">
            ▾
          </span>
        ) : null}
      </button>
      {open ? (
        <div className={`menu-pop menu-pop-${align}`} id={id} role="menu">
          {items.length === 0 ? (
            <div className="menu-empty">Nothing here</div>
          ) : (
            items.map((it, i) => (
              <button
                key={i}
                type="button"
                role="menuitem"
                className={`menu-item${it.danger ? " menu-item-danger" : ""}`}
                disabled={it.disabled}
                onClick={() => {
                  setOpen(false);
                  it.onSelect?.();
                }}
              >
                <span>{it.label}</span>
                {it.hint ? <span className="menu-hint">{it.hint}</span> : null}
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
