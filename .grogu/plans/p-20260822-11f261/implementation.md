# Implementation plan

1. Add one PlanStore authorization primitive that resolves the current agent identity from an explicit `GROGU_AGENT` first and the persisted session binding second. For a plan-scoped role claim, compare that identity with prior `agents_seen` and the current plan binding, reject any conflicting role, and record an accepted identity/role association so later calls cannot change it.
2. Apply the primitive at store boundaries that consume a caller role, especially stage reads, writes, completion, finalization, and role briefs. Keep target-role commands such as steering/commission outside caller authorization, and let explicit human `--as-user` operations bypass agent-role binding.
3. Replace the CLI-only environment comparison with store-backed validation so fresh subprocess shells remain protected while distinct `GROGU_AGENT` values may legitimately take tester or architect roles in the same checkout.
4. Update the pipeline contract and skill language to describe identity binding, its non-cryptographic scope, fresh-shell recovery, distinct agents, and `--as-user` behavior.
