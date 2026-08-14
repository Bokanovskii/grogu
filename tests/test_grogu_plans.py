import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import grogu_plans  # noqa: E402


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def plan(self, **kwargs):
        plan = self.store.create("Test plan", **kwargs)
        for stage in plan["stages"]:
            self.store.write_stage(
                plan["id"], stage, f"# {stage} body\n", role=grogu_plans.ARCHITECT
            )
        return plan["id"]

    # -- role isolation ----------------------------------------------------

    def test_engineer_reads_only_the_implementation_plan(self):
        plan_id = self.plan(evaluation=True)
        body = self.store.read_stage(
            plan_id, grogu_plans.IMPLEMENTATION, role=grogu_plans.ENGINEER
        )
        self.assertIn("implementation body", body)
        for stage in (grogu_plans.TESTING, grogu_plans.EVALUATION):
            with self.assertRaises(grogu_plans.PlanError):
                self.store.read_stage(plan_id, stage, role=grogu_plans.ENGINEER)

    def test_tester_reads_only_the_testing_and_evaluation_plans(self):
        plan_id = self.plan(evaluation=True)
        self.store.read_stage(plan_id, grogu_plans.TESTING, role=grogu_plans.TESTER)
        self.store.read_stage(plan_id, grogu_plans.EVALUATION, role=grogu_plans.TESTER)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.read_stage(
                plan_id, grogu_plans.IMPLEMENTATION, role=grogu_plans.TESTER
            )

    def test_refused_reads_are_recorded(self):
        plan_id = self.plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.read_stage(plan_id, grogu_plans.TESTING, role=grogu_plans.ENGINEER)
        refusals = [
            entry
            for entry in self.store.load(plan_id)["access_log"]
            if not entry["allowed"]
        ]
        self.assertEqual(len(refusals), 1)
        self.assertEqual(refusals[0]["role"], grogu_plans.ENGINEER)

    def test_sealed_stage_is_not_readable_as_plain_text(self):
        plan_id = self.plan()
        raw = (self.store.plan_dir(plan_id) / "testing.sealed").read_text()
        self.assertNotIn("testing body", raw)
        self.assertIn(grogu_plans.SEAL_HEADER, raw)

    def test_only_the_architect_writes_stages(self):
        plan_id = self.plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(
                plan_id, grogu_plans.IMPLEMENTATION, "hacked", role=grogu_plans.ENGINEER
            )

    # -- gates -------------------------------------------------------------

    def test_user_requested_plan_blocks_until_approved(self):
        plan_id = self.plan(review_required=True)
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("approve" in blocker for blocker in gate["blockers"]))
        self.store.approve(plan_id)
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)["allowed"])

    def test_unwritten_stage_blocks_the_gate(self):
        plan = self.store.create("Bare plan")
        gate = self.store.gate(plan["id"], grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])

    def test_test_gate_requires_implementation_complete(self):
        plan_id = self.plan()
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])

    # -- the architect loop ------------------------------------------------

    def test_amendment_requires_independent_verification(self):
        plan_id = self.plan()
        amendment = self.store.amend(plan_id, claim="cannot work", raised_by="engineer")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.resolve_amendment(
                plan_id,
                amendment["id"],
                outcome=grogu_plans.ACCEPTED,
                reason="engineer says so",
                verified=False,
                role=grogu_plans.ARCHITECT,
            )

    def test_only_the_architect_resolves_amendments(self):
        plan_id = self.plan()
        amendment = self.store.amend(plan_id, claim="cannot work", raised_by="engineer")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.resolve_amendment(
                plan_id,
                amendment["id"],
                outcome=grogu_plans.ACCEPTED,
                reason="self-approved",
                verified=True,
                role=grogu_plans.ENGINEER,
            )

    def test_pending_amendment_blocks_work(self):
        plan_id = self.plan()
        self.store.amend(plan_id, claim="wrong module", raised_by="engineer")
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)["allowed"])

    def test_amendment_rounds_are_capped_at_the_architect(self):
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_ROUNDS):
            amendment = self.store.amend(
                plan_id, claim=f"claim {index}", raised_by="engineer"
            )
            self.store.resolve_amendment(
                plan_id,
                amendment["id"],
                outcome=grogu_plans.REJECTED,
                reason="plan stands",
                verified=True,
                role=grogu_plans.ARCHITECT,
            )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.amend(plan_id, claim="one more", raised_by="engineer")
        self.assertIn("user", str(caught.exception))

    def test_guidance_reaches_engineer_and_tester_as_steering(self):
        plan_id = self.plan()
        amendment = self.store.amend(plan_id, claim="ambiguous", raised_by="engineer")
        self.store.resolve_amendment(
            plan_id,
            amendment["id"],
            outcome=grogu_plans.GUIDED,
            reason="use the existing helper",
            verified=True,
            role=grogu_plans.ARCHITECT,
        )
        for role in (grogu_plans.ENGINEER, grogu_plans.TESTER):
            notes = self.store.steering(role=role, plan_id=plan_id)["plan"]
            self.assertTrue(any("existing helper" in note["text"] for note in notes))

    # -- the tester loop ---------------------------------------------------

    def test_defect_routes_to_the_owning_role(self):
        plan_id = self.plan()
        owners = {
            grogu_plans.ROUTE_IMPLEMENTATION: grogu_plans.ENGINEER,
            grogu_plans.ROUTE_TEST: grogu_plans.TESTER,
            grogu_plans.ROUTE_PLAN: grogu_plans.ARCHITECT,
        }
        for route, owner in owners.items():
            defect = self.store.report_defect(
                plan_id, report=f"{route} failure", route=route, raised_by="tester"
            )
            self.assertEqual(defect["owner"], owner)

    def test_plan_route_defect_becomes_an_amendment(self):
        plan_id = self.plan()
        defect = self.store.report_defect(
            plan_id,
            report="acceptance criteria cannot be verified",
            route=grogu_plans.ROUTE_PLAN,
            raised_by="tester",
        )
        self.assertIn("amendment", defect)
        pending = self.store.summary(plan_id)["open_amendments"]
        self.assertEqual(len(pending), 1)

    def test_stalled_engineer_tester_loop_escalates_to_the_architect(self):
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS):
            defect = self.store.report_defect(
                plan_id,
                report=f"still failing {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION,
                raised_by="tester",
            )
            self.assertFalse(defect.get("escalated"))
        final = self.store.report_defect(
            plan_id,
            report="no convergence",
            route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        self.assertTrue(final["escalated"])
        manifest = self.store.load(plan_id)
        self.assertTrue(manifest["escalated"])
        escalations = [
            item
            for item in manifest["amendments"]
            if item["kind"] == grogu_plans.KIND_ESCALATION
        ]
        self.assertEqual(len(escalations), 1)
        gate = self.store.gate(plan_id, grogu_plans.GATE_TEST)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("architect" in blocker for blocker in gate["blockers"]))

    def test_escalation_does_not_consume_architect_amendment_rounds(self):
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 1):
            self.store.report_defect(
                plan_id,
                report=f"failure {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION,
                raised_by="tester",
            )
        self.assertEqual(self.store.load(plan_id)["rounds"], 0)

    def test_resolved_escalation_resets_the_loop_budget(self):
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 1):
            self.store.report_defect(
                plan_id,
                report=f"failure {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION,
                raised_by="tester",
            )
        escalation = self.store.summary(plan_id)["open_amendments"][0]
        self.store.resolve_amendment(
            plan_id,
            escalation["id"],
            outcome=grogu_plans.GUIDED,
            reason="the retry helper must be idempotent",
            verified=True,
            role=grogu_plans.ARCHITECT,
        )
        manifest = self.store.load(plan_id)
        self.assertEqual(manifest["defect_rounds"], 0)
        self.assertFalse(manifest["escalated"])

    # -- steering ----------------------------------------------------------

    def test_steering_is_scoped_by_role(self):
        plan_id = self.plan()
        self.store.steer("engineers only", plan_id=plan_id, role=grogu_plans.ENGINEER)
        self.store.steer("everyone", plan_id=plan_id, role="all")
        engineer = self.store.steering(role=grogu_plans.ENGINEER, plan_id=plan_id)["plan"]
        tester = self.store.steering(role=grogu_plans.TESTER, plan_id=plan_id)["plan"]
        self.assertEqual(len(engineer), 2)
        self.assertEqual([note["text"] for note in tester], ["everyone"])

    def test_repository_steering_outlives_a_plan(self):
        self.store.steer("always run the linter", role=grogu_plans.ENGINEER)
        plan_id = self.plan()
        visible = self.store.steering(role=grogu_plans.ENGINEER, plan_id=plan_id)
        self.assertEqual(len(visible["repository"]), 1)

    def test_late_spawned_agent_receives_standing_steering_in_its_brief(self):
        self.store.steer("prefer the ecosystem tools", role=grogu_plans.ENGINEER)
        plan_id = self.plan()
        self.store.steer("watch the lease timeout", plan_id=plan_id, role="all")
        # An agent spawned now has acked nothing, and the brief carries the full
        # standing set rather than an unread delta.
        brief = self.store.brief(grogu_plans.ENGINEER, plan_id=plan_id)
        texts = [
            note["text"]
            for note in brief["steering"]["repository"] + brief["steering"]["plan"]
        ]
        self.assertIn("prefer the ecosystem tools", texts)
        self.assertIn("watch the lease timeout", texts)

    def test_ack_clears_the_unread_delta(self):
        plan_id = self.plan()
        self.store.steer("first", plan_id=plan_id, role=grogu_plans.ENGINEER)
        unread = self.store.steering(
            role=grogu_plans.ENGINEER, plan_id=plan_id, unread=True
        )
        self.assertEqual(len(unread["plan"]), 1)
        self.store.ack_steering(role=grogu_plans.ENGINEER, plan_id=plan_id)
        unread = self.store.steering(
            role=grogu_plans.ENGINEER, plan_id=plan_id, unread=True
        )
        self.assertEqual(unread["plan"], [])

    def test_binding_steering_blocks_the_gates(self):
        plan_id = self.plan(review_required=False)
        self.store.approve(plan_id)
        self.store.steer(
            "the schema changed; replan",
            plan_id=plan_id,
            role="all",
            requires_replan=True,
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])

    def test_pending_banner_delivers_without_being_asked(self):
        plan_id = self.plan()
        self.store.steer("use the helper", plan_id=plan_id, role=grogu_plans.ENGINEER)
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_PLAN"] = plan_id
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        self.addCleanup(os.environ.pop, "GROGU_PLAN", None)
        banner = grogu_plans.pending_banner(self.root)
        self.assertIn("use the helper", banner)
        self.store.ack_steering(role=grogu_plans.ENGINEER, plan_id=plan_id)
        self.assertEqual(grogu_plans.pending_banner(self.root), "")

    def test_no_banner_without_a_declared_role(self):
        plan_id = self.plan()
        self.store.steer("anything", plan_id=plan_id, role="all")
        self.assertEqual(grogu_plans.pending_banner(self.root), "")

    # -- workstreams -------------------------------------------------------

    def test_overlapping_workstreams_are_reported(self):
        plan_id = self.plan()
        self.store.add_workstream(plan_id, name="api", paths=["src/api/**"])
        self.store.add_workstream(plan_id, name="store", paths=["src/store/**"])
        self.assertEqual(self.store.workstream_conflicts(plan_id), [])
        self.store.add_workstream(plan_id, name="wiring", paths=["src/api/**"])
        self.assertTrue(self.store.workstream_conflicts(plan_id))

    def test_dependent_workstreams_do_not_conflict(self):
        plan_id = self.plan()
        self.store.add_workstream(plan_id, name="schema", paths=["src/db/**"])
        self.store.add_workstream(
            plan_id, name="queries", paths=["src/db/**"], depends_on=["schema"]
        )
        self.assertEqual(self.store.workstream_conflicts(plan_id), [])

    def test_parallel_batches_follow_dependencies(self):
        plan_id = self.plan()
        self.store.add_workstream(plan_id, name="a", paths=["a/**"])
        self.store.add_workstream(plan_id, name="b", paths=["b/**"])
        self.store.add_workstream(
            plan_id, name="c", paths=["c/**"], depends_on=["a", "b"]
        )
        self.assertEqual(self.store.parallel_batches(plan_id), [["a", "b"], ["c"]])

    # -- triage ------------------------------------------------------------

    def test_questions_do_not_trigger_a_planning_cycle(self):
        for prompt in (
            "what does the memory index do?",
            "just tell me which file handles leases",
            "fix the typo in the README",
            "fyi the staging box is down",
        ):
            self.assertEqual(grogu_plans.triage(prompt)["decision"], "direct", prompt)

    def test_substantial_work_triggers_a_plan(self):
        for prompt in (
            "build a rate limiter for the API with retries and backoff, then wire "
            "it into the client and add configuration for it",
            "refactor the storage layer to support multiple backends",
            "please make me a plan for the new indexer",
        ):
            self.assertEqual(grogu_plans.triage(prompt)["decision"], "plan", prompt)

    # -- finalisation and improvement --------------------------------------

    def test_finalize_unseals_every_stage_for_review(self):
        plan_id = self.plan(evaluation=True)
        result = self.store.finalize(plan_id)
        self.assertEqual(len(result["emitted"]), 2)
        for stage in (grogu_plans.TESTING, grogu_plans.EVALUATION):
            path = self.store.plan_dir(plan_id) / f"{stage}.md"
            self.assertIn(f"{stage} body", path.read_text())
            self.assertFalse((self.store.plan_dir(plan_id) / f"{stage}.sealed").exists())

    def test_finalized_stages_stay_readable(self):
        plan_id = self.plan()
        self.store.finalize(plan_id)
        body = self.store.read_stage(plan_id, grogu_plans.TESTING, role=grogu_plans.TESTER)
        self.assertIn("testing body", body)

    def test_retro_attributes_findings_to_a_target(self):
        plan_id = self.plan()
        amendment = self.store.amend(plan_id, claim="module is wrong", raised_by="engineer")
        self.store.resolve_amendment(
            plan_id,
            amendment["id"],
            outcome=grogu_plans.ACCEPTED,
            reason="verified against the code",
            verified=True,
            role=grogu_plans.ARCHITECT,
        )
        report = self.store.retro(plan_id)
        self.assertFalse(report["clean"])
        targets = {finding["target"] for finding in report["findings"]}
        self.assertIn("architect_overlay", targets)

    def test_clean_plan_produces_no_findings(self):
        plan_id = self.plan()
        self.assertTrue(self.store.retro(plan_id)["clean"])

    def test_friction_recurring_across_plans_is_surfaced(self):
        for _ in range(2):
            plan_id = self.plan()
            amendment = self.store.amend(plan_id, claim="same gap", raised_by="engineer")
            self.store.resolve_amendment(
                plan_id,
                amendment["id"],
                outcome=grogu_plans.ACCEPTED,
                reason="verified",
                verified=True,
                role=grogu_plans.ARCHITECT,
            )
        report = self.store.friction()
        signals = {bucket["signal"] for bucket in report["recurring"]}
        self.assertIn("accepted_amendments", signals)

    def test_recorded_friction_notes_survive_for_review(self):
        plan_id = self.plan()
        self.store.note_friction(
            "the test fixtures take four minutes to build",
            plan_id=plan_id,
            role=grogu_plans.TESTER,
        )
        report = self.store.friction()
        self.assertEqual(len(report["notes"]), 1)
        self.store.resolve_friction(1, note="cached the fixtures")
        self.assertEqual(self.store.friction()["notes"], [])


if __name__ == "__main__":
    unittest.main()
