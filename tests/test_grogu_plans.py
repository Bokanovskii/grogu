import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import grogu_plans  # noqa: E402


def valid_design_spec() -> str:
    spec = grogu_plans.design_template("Test plan")
    return spec + "\n- the table renders with a 16px row gap\n"


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
            owner = sorted(grogu_plans.STAGE_WRITERS[stage])[0]
            body = (
                valid_design_spec()
                if stage == grogu_plans.DESIGN
                else f"# {stage} body\n"
            )
            self.store.write_stage(plan["id"], stage, body, role=owner)
        return plan["id"]

    def complete_all(self, plan_id):
        manifest = self.store.load(plan_id)
        for stage in manifest["stages"]:
            if stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING, grogu_plans.EVALUATION):
                self.store.set_stage_state(plan_id, stage, grogu_plans.COMPLETE)

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
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS - 1):
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
        self.complete_all(plan_id)
        result = self.store.finalize(plan_id)
        self.assertEqual(len(result["emitted"]), 2)
        for stage in (grogu_plans.TESTING, grogu_plans.EVALUATION):
            path = self.store.plan_dir(plan_id) / f"{stage}.md"
            self.assertIn(f"{stage} body", path.read_text())
            self.assertFalse((self.store.plan_dir(plan_id) / f"{stage}.sealed").exists())

    def test_finalize_refuses_while_work_is_open(self):
        plan_id = self.plan()
        self.store.report_defect(
            plan_id,
            report="still failing",
            route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.finalize(plan_id)
        result = self.store.finalize(plan_id, force=True)
        self.assertTrue(result["shipped_incomplete"])

    def test_accepted_amendment_blocks_until_the_stage_is_rewritten(self):
        plan_id = self.plan()
        self.store.approve(plan_id)
        amendment = self.store.amend(
            plan_id,
            claim="the plan names a module that does not exist",
            evidence="src/nope.py is absent",
            stage=grogu_plans.IMPLEMENTATION,
            raised_by="engineer",
        )
        self.store.resolve_amendment(
            plan_id,
            amendment["id"],
            outcome=grogu_plans.ACCEPTED,
            reason="confirmed against the tree",
            verified=True,
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])
        self.store.write_stage(
            plan_id,
            grogu_plans.IMPLEMENTATION,
            "# implementation body, corrected\n",
            role=grogu_plans.ARCHITECT,
        )
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)["allowed"])

    def test_an_agent_may_not_approve_for_the_user(self):
        plan_id = self.plan()
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.approve(plan_id)
        os.environ.pop("GROGU_ROLE")
        self.store.approve(plan_id)

    def test_personal_requests_never_reach_the_pipeline(self):
        for request in (
            "plan a trip to japan in april: flights from sfo, two weeks in "
            "kyoto and tokyo, with a daily itinerary and a budget",
            "help me plan my week: three deadlines, a dentist appointment and "
            "four trips to the gym",
            "research the best noise cancelling headphones under 400 dollars",
            "draft an email to my landlord about the broken dishwasher",
        ):
            result = grogu_plans.triage(request)
            self.assertEqual(result["decision"], "direct", request)
            self.assertFalse(result["software"], request)

    def test_software_requests_still_plan(self):
        result = grogu_plans.triage(
            "make me a plan for adding rate limiting to the api"
        )
        self.assertEqual(result["decision"], "plan")
        self.assertTrue(result["software"])

    def test_explicit_intent_overrides_triage_scoring(self):
        self.assertEqual(
            grogu_plans.triage(
                "just do it: build a settings page with dark mode and overrides"
            )["decision"],
            "direct",
        )
        self.assertEqual(
            grogu_plans.triage("please make me a plan for fixing the readme typo")[
                "decision"
            ],
            "plan",
        )

    def test_open_defects_block_the_test_gate(self):
        plan_id = self.plan()
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        defect = self.store.report_defect(
            plan_id,
            report="crashes on empty input",
            route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])
        self.store.resolve_defect(plan_id, defect["id"], note="guarded the empty case")
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])

    def test_brief_binds_the_session_so_steering_finds_it(self):
        plan_id = self.plan()
        self.store.brief(grogu_plans.ENGINEER, plan_id=plan_id)
        self.assertEqual(
            self.store.session_binding()["role"], grogu_plans.ENGINEER
        )

    def test_architect_assigns_model_and_review_per_workstream(self):
        plan_id = self.plan()
        stream = self.store.add_workstream(
            plan_id,
            name="api",
            paths=["src/api/**"],
            model="gpt-5.6-sol",
            review=grogu_plans.REVIEW_RUBBER_DUCK,
            brief="the rate limiter is the subtle part",
        )
        self.assertEqual(stream["model"], "gpt-5.6-sol")
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_TEST)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("review of: api" in blocker for blocker in gate["blockers"]))
        self.store.record_review(
            plan_id,
            "api",
            verdict=grogu_plans.PASS,
            model="claude-opus-5",
            findings="walked the token bucket refill and the 429 path",
        )
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])

    def test_a_bare_passing_review_is_refused(self):
        plan_id = self.plan()
        self.store.add_workstream(plan_id, name="api", paths=["src/api/**"])
        with self.assertRaises(grogu_plans.PlanError):
            self.store.record_review(plan_id, "api", verdict=grogu_plans.PASS)

    def test_unknown_review_kind_refused(self):
        plan_id = self.plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.add_workstream(
                plan_id, name="api", paths=["src/api/**"], review="vibes"
            )

    def test_finalized_stages_stay_readable(self):
        plan_id = self.plan()
        self.complete_all(plan_id)
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


    # -- design stage ------------------------------------------------------

    def test_designer_writes_the_design_stage_and_architect_may_not(self):
        plan = self.store.create("Surface work", design=True)
        self.assertEqual(plan["stages"][0], grogu_plans.DESIGN)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(
                plan["id"],
                grogu_plans.DESIGN,
                valid_design_spec(),
                role=grogu_plans.ARCHITECT,
            )
        self.store.write_stage(
            plan["id"],
            grogu_plans.DESIGN,
            valid_design_spec(),
            role=grogu_plans.DESIGNER,
        )

    def test_engineer_may_read_the_design_spec(self):
        plan_id = self.plan(design=True)
        body = self.store.read_stage(
            plan_id, grogu_plans.DESIGN, role=grogu_plans.ENGINEER
        )
        self.assertIn("## Acceptance criteria", body)

    def test_design_spec_missing_sections_refused(self):
        plan = self.store.create("Surface work", design=True)
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(
                plan["id"],
                grogu_plans.DESIGN,
                "# Spec\n\n## Surfaces\nOne page.\n",
                role=grogu_plans.DESIGNER,
            )
        self.assertIn("states", str(caught.exception))

    def test_design_spec_adjectives_refused(self):
        plan = self.store.create("Surface work", design=True)
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(
                plan["id"],
                grogu_plans.DESIGN,
                valid_design_spec() + "\nMake it clean and modern.\n",
                role=grogu_plans.DESIGNER,
            )
        self.assertIn("clean", str(caught.exception))

    def test_adjectives_allowed_inside_literal_output_blocks(self):
        plan = self.store.create("Surface work", design=True)
        body = valid_design_spec() + "\n```\n$ grogu clean --modern\n```\n"
        self.store.write_stage(
            plan["id"], grogu_plans.DESIGN, body, role=grogu_plans.DESIGNER
        )

    def test_test_gate_waits_for_the_designer_to_see_it_running(self):
        plan_id = self.plan(design=True)
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_TEST)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("signed off" in blocker for blocker in gate["blockers"]))
        self.store.design_review(
            plan_id,
            grogu_plans.PASS,
            evidence=["/tmp/empty.png"],
            role=grogu_plans.DESIGNER,
        )
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])

    def test_design_pass_requires_evidence(self):
        plan_id = self.plan(design=True)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.design_review(
                plan_id, grogu_plans.PASS, role=grogu_plans.DESIGNER
            )
        self.store.design_review(
            plan_id,
            grogu_plans.CHANGES,
            notes="row gap is 8, spec says 16",
            role=grogu_plans.DESIGNER,
        )

    def test_only_the_designer_signs_off(self):
        plan_id = self.plan(design=True)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.design_review(
                plan_id,
                grogu_plans.PASS,
                evidence=["/tmp/a.png"],
                role=grogu_plans.ENGINEER,
            )

    def test_design_defects_route_to_the_designer(self):
        plan_id = self.plan(design=True)
        defect = self.store.report_defect(
            plan_id,
            report="empty state copy is missing",
            route=grogu_plans.ROUTE_DESIGN,
            raised_by=grogu_plans.TESTER,
        )
        self.assertEqual(defect["owner"], grogu_plans.DESIGNER)

    def test_uncited_dependency_warns_but_does_not_block(self):
        plan = self.store.create("Rate limiting")
        manifest = self.store.write_stage(
            plan["id"],
            grogu_plans.IMPLEMENTATION,
            "# plan\n\nAdd a new dependency: pip install slowapi.\n",
            role=grogu_plans.ARCHITECT,
        )
        self.assertTrue(manifest["warnings"])

    def test_cited_dependency_does_not_warn(self):
        plan = self.store.create("Rate limiting")
        manifest = self.store.write_stage(
            plan["id"],
            grogu_plans.IMPLEMENTATION,
            "# plan\n\npip install slowapi (0.1.9, checked "
            "https://pypi.org/project/slowapi/ on 2026-08-14).\n",
            role=grogu_plans.ARCHITECT,
        )
        self.assertEqual(manifest["warnings"], [])

    def test_triage_flags_design_for_user_visible_work(self):
        visible = grogu_plans.triage(
            "build a settings page with a dark mode toggle and per-repo overrides "
            "that persist across sessions"
        )
        self.assertTrue(visible["design"])
        internal = grogu_plans.triage(
            "refactor the storage layer to support multiple backends"
        )
        self.assertFalse(internal["design"])


if __name__ == "__main__":
    unittest.main()
