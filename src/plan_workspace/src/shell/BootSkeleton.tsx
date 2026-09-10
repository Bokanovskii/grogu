export function BootSkeleton() {
  // Top bar, mode tabs, and body scaffold at their real dimensions with muted
  // neutrals; no spinners. aria-busy on the main region.
  return (
    <div className="shell" aria-hidden={false}>
      <div className="topbar skeleton-block" />
      <div className="modebar skeleton-block" />
      <main className="shell-body" aria-busy="true" aria-label="Loading plan">
        <div className="panel-left skeleton-block" />
        <div className="workspace-body">
          <div className="skeleton-lines">
            <div className="skeleton-line" style={{ width: "40%" }} />
            <div className="skeleton-line" style={{ width: "80%" }} />
            <div className="skeleton-line" style={{ width: "70%" }} />
            <div className="skeleton-line" style={{ width: "90%" }} />
            <div className="skeleton-line" style={{ width: "55%" }} />
          </div>
        </div>
        <div className="panel-right skeleton-block" />
      </main>
      <div className="statusbar skeleton-block" />
    </div>
  );
}
