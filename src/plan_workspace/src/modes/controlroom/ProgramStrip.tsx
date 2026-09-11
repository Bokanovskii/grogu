import type { PlanProgramSummary } from "../../api/types";
import { gateText, stageSummary } from "./presentation";

export function ProgramStrip({
  plans,
  selected,
  onSelect,
}: {
  plans: Record<string, PlanProgramSummary>;
  selected: string | null;
  onSelect: (plan: string) => void;
}) {
  const entries = Object.entries(plans).sort(([a], [b]) => a.localeCompare(b));

  return (
    <section className="cr-program" aria-labelledby="cr-program-title">
      <h2 id="cr-program-title" className="sr-only">
        Repository program plans
      </h2>
      <div className="cr-program-strip">
        {entries.map(([id, plan]) => (
          <button
            key={id}
            type="button"
            className={`cr-plan-card${selected === id ? " is-selected" : ""}`}
            aria-pressed={selected === id}
            onClick={() => onSelect(id)}
          >
            <span className="cr-plan-head">
              <span className="cr-plan-title" title={`${plan.title} (${id})`}>
                {plan.title}
              </span>
            </span>
            <span className="cr-plan-id" title={id}>
              Plan {id.slice(-6)}
            </span>
            <span className="cr-plan-meta">
              {plan.status ?? "Status unavailable"}
              {plan.current_revision ? ` · ${plan.current_revision}` : " · revision unavailable"}
            </span>
            <span className="cr-plan-stages">{stageSummary(plan)}</span>
            <span className="cr-plan-gate">{gateText(plan)}</span>
            <span className="cr-plan-counts">
              {countLabel("Defects", plan.open_defects, "open")} ·{" "}
              {countLabel("Amendments", plan.open_amendments, "open")}
            </span>
            <span className="cr-plan-counts">
              {plan.completion
                ? `${plan.completion.complete} of ${plan.completion.total} stages complete`
                : "Stage completion unavailable"}{" "}
              · {plan.agents} agent{plan.agents === 1 ? "" : "s"} ·{" "}
              {plan.waiting_on_you == null
                ? "waiting count unavailable"
                : `${plan.waiting_on_you} waiting on you`}
            </span>
          </button>
        ))}
        {entries.length === 0 ? (
          <div className="cr-plan-unavailable" role="status">
            Plan summary unavailable.
          </div>
        ) : null}
      </div>
    </section>
  );
}

function countLabel(
  label: string,
  value: number | null | undefined,
  suffix: string,
): string {
  return value == null ? `${label} unavailable` : `${label} ${value} ${suffix}`;
}
