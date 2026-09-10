import { stageLabel, type ApiError, type Role, type Stage } from "../api/types";

/** A centred state panel with a title and body. Used both full-screen (boot
 * failures) and inline (sealed/unwritten stages, empty states). Every literal
 * string here is fixed by the design. */
export function StatePanel({
  title,
  children,
  tone = "neutral",
  actions,
  full,
  role = "status",
}: {
  title: string;
  children?: React.ReactNode;
  tone?: "neutral" | "warn" | "danger";
  actions?: React.ReactNode;
  full?: boolean;
  role?: string;
}) {
  return (
    <div
      className={`state-panel ${full ? "state-panel-full" : ""} tone-${tone}`}
      role={role}
    >
      <div className="state-panel-inner">
        <h2 className="state-panel-title">{title}</h2>
        {children ? <div className="state-panel-body">{children}</div> : null}
        {actions ? <div className="state-panel-actions">{actions}</div> : null}
      </div>
    </div>
  );
}

export function Kbd({ children }: { children: React.ReactNode }) {
  return <code className="kbd-inline">{children}</code>;
}

const OWNER: Record<Stage, Role> = {
  design: "designer",
  implementation: "engineer",
  testing: "tester",
  evaluation: "tester",
};

export function BootError({ error }: { error: ApiError | null }) {
  const code = error?.error ?? "";
  if (code === "token_spent") {
    return (
      <StatePanel title="This session link has already been used." full tone="neutral">
        <p>
          Run <Kbd>grogu plan doc open</Kbd> again to get a fresh link. The
          previous session is safe.
        </p>
      </StatePanel>
    );
  }
  if (code === "permission" || error?.error === "forbidden") {
    return (
      <StatePanel title="This stage is out of your role's reach." full tone="warn">
        <p>{error?.message ?? "You do not have access to this plan as your current role."}</p>
      </StatePanel>
    );
  }
  if (code === "stale") {
    return (
      <StatePanel title="The plan moved on while this page loaded." full tone="warn">
        <p>Reload to catch up to the latest revision.</p>
      </StatePanel>
    );
  }
  if (code === "invalid_package" || code === "ambiguous") {
    return (
      <StatePanel title="This plan is not in a state we recognise." full tone="danger">
        <p>
          {error?.message ??
            "Both an old directory and a new package exist. Delete the one you do not want, then reopen."}
        </p>
      </StatePanel>
    );
  }
  return (
    <StatePanel title="Could not open the plan." full tone="danger">
      <p>{error?.message ?? "The workspace server did not answer. Check "}</p>
      <p>
        Check <Kbd>grogu doctor</Kbd> and try again.
      </p>
    </StatePanel>
  );
}

export function SealedStagePanel({ stage }: { stage: Stage }) {
  const owner = OWNER[stage];
  return (
    <StatePanel title={`The ${stageLabel(stage)} stage is sealed.`} tone="neutral">
      <p>
        Only {owner} can unseal it. Ask for a review round to re-open.
      </p>
    </StatePanel>
  );
}

export function UnwrittenStagePanel({ stage }: { stage: Stage }) {
  const owner = OWNER[stage];
  return (
    <StatePanel title={`The ${stageLabel(stage)} stage has not been written yet.`} tone="neutral">
      <p>
        {owner} is responsible for writing it. When they publish, it will appear
        here.
      </p>
    </StatePanel>
  );
}

export function EmptyState({
  title,
  children,
}: {
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <StatePanel title={title} tone="neutral">
      {children}
    </StatePanel>
  );
}
