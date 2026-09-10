import React from "react";

export function Button({
  children,
  variant = "default",
  onClick,
  disabled,
  type = "button",
  title,
  className,
  ...rest
}: {
  children: React.ReactNode;
  variant?: "default" | "primary" | "text" | "danger" | "warn";
  onClick?: () => void;
  disabled?: boolean;
  type?: "button" | "submit";
  title?: string;
  className?: string;
} & React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type={type}
      className={`btn btn-${variant}${className ? " " + className : ""}`}
      onClick={onClick}
      disabled={disabled}
      {...(title ? { title, "aria-label": rest["aria-label"] ?? title } : {})}
      {...rest}
    >
      {children}
    </button>
  );
}

export function IconButton({
  label,
  glyph,
  onClick,
  active,
  disabled,
}: {
  label: string;
  glyph: string;
  onClick?: () => void;
  active?: boolean;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className={`icon-btn${active ? " is-active" : ""}`}
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      aria-pressed={active}
      title={label}
    >
      <span aria-hidden="true">{glyph}</span>
    </button>
  );
}

export function SegmentedControl<T extends string>({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
  ariaLabel: string;
}) {
  return (
    <div className="segmented" role="tablist" aria-label={ariaLabel}>
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={value === o.value}
          className={`segmented-item${value === o.value ? " is-active" : ""}`}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Chip({
  children,
  tone = "neutral",
  glyph,
  onRemove,
  count,
  onClick,
  active,
}: {
  children: React.ReactNode;
  tone?: "neutral" | "accent" | "warn" | "danger" | "success";
  glyph?: string;
  onRemove?: () => void;
  count?: number;
  onClick?: () => void;
  active?: boolean;
}) {
  const content = (
    <>
      {glyph ? (
        <span className="chip-glyph" aria-hidden="true">
          {glyph}
        </span>
      ) : null}
      <span>{children}</span>
      {count != null ? <sup className="chip-count">{count}</sup> : null}
    </>
  );
  if (onClick) {
    return (
      <button
        type="button"
        className={`chip chip-${tone} chip-btn${active ? " is-active" : ""}`}
        onClick={onClick}
        aria-pressed={active}
      >
        {content}
      </button>
    );
  }
  return (
    <span className={`chip chip-${tone}`}>
      {content}
      {onRemove ? (
        <button type="button" className="chip-remove" aria-label="Remove filter" onClick={onRemove}>
          ×
        </button>
      ) : null}
    </span>
  );
}

/** A right-side or bottom drawer used by inspector/audit/cycles. */
export function Drawer({
  title,
  children,
  onClose,
  width,
}: {
  title: string;
  children: React.ReactNode;
  onClose?: () => void;
  width?: number;
}) {
  return (
    <aside className="drawer" style={width ? { width } : undefined} aria-label={title}>
      <div className="drawer-header">
        <h2 className="drawer-title">{title}</h2>
        {onClose ? (
          <IconButton label="Close" glyph="×" onClick={onClose} />
        ) : null}
      </div>
      <div className="drawer-body">{children}</div>
    </aside>
  );
}
