import React, { useEffect, useRef } from "react";

// A modal overlay above the shell. Focus is trapped; Escape closes; the initial
// focus lands on the first focusable element. Background is inert to pointer.
export function Modal({
  title,
  onClose,
  children,
  width = 720,
  labelledBy,
  className,
}: {
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  width?: number;
  labelledBy?: string;
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const focusables = node.querySelectorAll<HTMLElement>(
      'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
    );
    focusables[0]?.focus();

    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key === "Tab") {
        const list = node!.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        );
        if (list.length === 0) return;
        const first = list[0]!;
        const last = list[list.length - 1]!;
        if (e.shiftKey && document.activeElement === first) {
          e.preventDefault();
          last.focus();
        } else if (!e.shiftKey && document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }
    node.addEventListener("keydown", onKey);
    return () => node.removeEventListener("keydown", onKey);
  }, [onClose]);

  return (
    <div className="modal-scrim" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div
        ref={ref}
        className={`modal${className ? " " + className : ""}`}
        role="dialog"
        aria-modal="true"
        aria-label={labelledBy ? undefined : title}
        aria-labelledby={labelledBy}
        style={{ width, maxWidth: "calc(100vw - 48px)" }}
      >
        {children}
      </div>
    </div>
  );
}

export function ModalHeader({ children }: { children: React.ReactNode }) {
  return <header className="modal-header">{children}</header>;
}

export function ModalTitle({ children, id }: { children: React.ReactNode; id?: string }) {
  return (
    <h2 className="modal-title" id={id}>
      {children}
    </h2>
  );
}

export function ModalBody({ children }: { children: React.ReactNode }) {
  return <div className="modal-body">{children}</div>;
}

export function ModalFooter({ children }: { children: React.ReactNode }) {
  return <footer className="modal-footer">{children}</footer>;
}
