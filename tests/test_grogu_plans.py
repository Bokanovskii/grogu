import json
import os
import subprocess
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

    def _evidence(self, name="shot.png"):
        """A real captured artifact: a design pass will not accept a filename."""
        path = self.root / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n captured")
        return str(path)

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
                self.store.set_stage_state(
                    plan_id, stage, grogu_plans.COMPLETE, as_user=True
                )

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

    def _bounce(self, plan_id, report):
        """One failed fix round: the engineer says it is fixed, it is not.

        Rounds count bounces, not bugs, so a round needs a resolution between
        the defects or it is still the same wave.
        """
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        return self.store.report_defect(
            plan_id, report=report, route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )

    def test_stalled_engineer_tester_loop_escalates_to_the_architect(self):
        plan_id = self.plan()
        self.store.report_defect(
            plan_id, report="first wave", route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS - 1):
            self.assertFalse(self._bounce(plan_id, f"still failing {index}").get("escalated"))
        final = self._bounce(plan_id, "no convergence")
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
        self.store.report_defect(
            plan_id, report="first", route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 1):
            self._bounce(plan_id, f"failure {index}")
        self.assertEqual(self.store.load(plan_id)["rounds"], 0)

    def test_a_first_test_pass_finding_several_bugs_does_not_escalate(self):
        """Rounds are bounces, not bugs. A healthy first pass that finds three
        real problems used to reach the architect before the engineer had been
        given a chance to fix any of them."""
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 2):
            defect = self.store.report_defect(
                plan_id, report=f"bug {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
            self.assertFalse(defect.get("escalated"))
        manifest = self.store.load(plan_id)
        self.assertFalse(manifest.get("escalated"))
        self.assertEqual(manifest["defect_rounds"], 0)

    def test_a_stalled_engineer_escalates_even_though_nothing_bounces(self):
        """Counting bounces has a blind spot: an engineer who never fixes
        anything never produces one, so failures could pile up on a route
        forever with the round count sitting at zero and nobody called in."""
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_PENDING_DEFECTS):
            self.store.report_defect(
                plan_id, report=f"still broken {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
        manifest = self.store.load(plan_id)
        self.assertTrue(manifest.get("escalated"))
        self.assertEqual(manifest["defect_rounds"], 0)
        claim = self.store.summary(plan_id)["open_amendments"][0]["claim"]
        self.assertIn("stalled rather than bounced", claim)

    def test_endless_one_more_bug_after_green_reaches_the_architect(self):
        """Every green pass reset the round count, so a plan that produced one
        fresh bug after every clean run could cycle forever without anyone
        asking whether the plan itself was the problem."""
        plan_id = self.plan()
        for cycle in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 2):
            self.store.report_defect(
                plan_id, report=f"late bug {cycle}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
            if self.store.load(plan_id).get("escalated"):
                break
            self.store.set_stage_state(
                plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
                role=grogu_plans.ENGINEER,
            )
            self.store.set_stage_state(
                plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE,
                role=grogu_plans.TESTER,
            )
        manifest = self.store.load(plan_id)
        self.assertTrue(manifest.get("escalated"))
        self.assertLess(
            manifest["defect_rounds"],
            grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS,
            "the bounce count resets at every green pass, which is the point",
        )
        claim = self.store.summary(plan_id)["open_amendments"][0]["claim"]
        self.assertIn("gone green and come back", claim)

    def test_a_stall_the_architect_has_seen_is_not_raised_again(self):
        """The engineer clears the pile by completing implementation. A tester
        filing before that happens should not call the architect straight back
        for a pile it has already ruled on."""
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_PENDING_DEFECTS):
            self.store.report_defect(
                plan_id, report=f"broken {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
        escalation = self.store.summary(plan_id)["open_amendments"][0]
        self.store.resolve_amendment(
            plan_id, escalation["id"], outcome=grogu_plans.GUIDED,
            reason="do it differently", verified=True,
            role=grogu_plans.ARCHITECT,
        )
        self.store.report_defect(
            plan_id, report="one more before the sweep",
            route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
        )
        self.assertFalse(self.store.load(plan_id).get("escalated"))
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        self.assertFalse(self.store.load(plan_id).get("stall_held"))
        for index in range(grogu_plans.DEFAULT_MAX_PENDING_DEFECTS):
            self.store.report_defect(
                plan_id, report=f"new pile {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
        self.assertTrue(
            self.store.load(plan_id).get("escalated"),
            "a genuinely new pile must still reach the architect",
        )

    def test_a_stall_split_across_routes_still_escalates(self):
        """Counted per route the backstop was evadable: five open
        implementation failures and five open test failures is a plan that has
        plainly stopped, and neither route reaches the cap alone."""
        plan_id = self.plan()
        routes = (grogu_plans.ROUTE_IMPLEMENTATION, grogu_plans.ROUTE_TEST)
        for index in range(grogu_plans.DEFAULT_MAX_PENDING_DEFECTS):
            self.store.report_defect(
                plan_id, report=f"still broken {index}",
                route=routes[index % 2], raised_by="tester",
            )
        self.assertTrue(self.store.load(plan_id).get("escalated"))

    def test_a_stall_escalation_does_not_strand_the_defects_it_names(self):
        """Auto-close is suspended while escalated so the architect keeps the
        evidence. That must not mean the extra defects are stranded once the
        escalation is settled."""
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_PENDING_DEFECTS):
            self.store.report_defect(
                plan_id, report=f"still broken {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by="tester",
            )
        escalation = self.store.summary(plan_id)["open_amendments"][0]
        self.store.resolve_amendment(
            plan_id, escalation["id"], outcome=grogu_plans.GUIDED,
            reason="the retry helper must be idempotent", verified=True,
            role=grogu_plans.ARCHITECT,
        )
        manifest = self.store.load(plan_id)
        self.assertFalse(manifest.get("escalated"))
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        manifest = self.store.load(plan_id)
        self.assertFalse(
            [d for d in manifest["defects"] if d["status"] == grogu_plans.PENDING]
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE,
            role=grogu_plans.TESTER,
        )

    def test_resolved_escalation_resets_the_loop_budget(self):
        plan_id = self.plan()
        self.store.report_defect(
            plan_id, report="first", route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by="tester",
        )
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS + 1):
            self._bounce(plan_id, f"failure {index}")
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
        result = self.store.finalize(plan_id, as_user=True)
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
            self.store.finalize(plan_id, as_user=True)
        result = self.store.finalize(plan_id, force=True, as_user=True)
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
        with self.assertRaises(grogu_plans.PlanError):
            self.store.record_review(
                plan_id, "api", verdict=grogu_plans.PASS,
                findings="looked at it",
            )
        self.store.record_review(
            plan_id,
            "api",
            verdict=grogu_plans.PASS,
            kind=grogu_plans.REVIEW_RUBBER_DUCK,
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

    def test_harness_friction_pools_across_repositories(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        second = tempfile.TemporaryDirectory()
        self.addCleanup(second.cleanup)
        other = grogu_plans.PlanStore(Path(second.name))

        self.store.note_friction(
            "no command to diff two plan stages",
            role=grogu_plans.ENGINEER,
            target=grogu_plans.TARGET_HARNESS,
        )
        other.note_friction(
            "plan status needs --json",
            role=grogu_plans.TESTER,
            target=grogu_plans.TARGET_HARNESS,
        )
        pooled = grogu_plans.harness_friction()
        self.assertEqual(len(pooled), 2)
        self.assertEqual(
            {entry["repository"] for entry in pooled},
            {self.root.name, Path(second.name).name},
        )
        # and it stays out of the repository's own report
        self.assertEqual(self.store.friction()["notes"], [])
        self.assertEqual(len(self.store.friction()["harness"]), 2)

    def test_harness_friction_resolves(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        self.store.note_friction(
            "a gap", role=grogu_plans.ENGINEER, target=grogu_plans.TARGET_HARNESS
        )
        self.assertTrue(
            grogu_plans.resolve_harness_friction(1, resolution="added the command")
        )
        self.assertEqual(grogu_plans.harness_friction(), [])
        self.assertFalse(grogu_plans.resolve_harness_friction(1, resolution="again"))

    def test_unknown_friction_target_refused(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.note_friction("a gap", target="everywhere")

    def test_user_session_is_reminded_of_harness_friction_once_a_day(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        for index in range(grogu_plans.HARNESS_FRICTION_THRESHOLD):
            self.store.note_friction(
                f"gap {index}",
                role=grogu_plans.ENGINEER,
                target=grogu_plans.TARGET_HARNESS,
            )
        banner = grogu_plans.pending_banner(self.root)
        self.assertIn("ready to fix in Grogu", banner)
        self.assertEqual(grogu_plans.pending_banner(self.root), "")

    def _harness_home(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        return home

    def test_the_same_complaint_worded_differently_forms_one_cluster(self):
        self._harness_home()
        self.store.note_friction(
            "no way to diff two plan stages without unsealing by hand",
            role=grogu_plans.ENGINEER,
            target=grogu_plans.TARGET_HARNESS,
        )
        self.store.note_friction(
            "diff two plan stages needed unsealing by hand again",
            role=grogu_plans.TESTER,
            target=grogu_plans.TARGET_HARNESS,
        )
        self.store.note_friction(
            "design recall has no scope for watch surfaces",
            role=grogu_plans.DESIGNER,
            target=grogu_plans.TARGET_HARNESS,
        )
        clusters = grogu_plans.cluster_harness_friction(grogu_plans.harness_friction())
        sizes = sorted(cluster["count"] for cluster in clusters)
        self.assertEqual(sizes, [1, 2])

    def test_a_complaint_from_two_repositories_is_ripe_before_the_third_hit(self):
        self._harness_home()
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        elsewhere = grogu_plans.PlanStore(Path(other.name))
        self.store.note_friction(
            "worktree setup takes four commands every time",
            role=grogu_plans.ENGINEER,
            target=grogu_plans.TARGET_HARNESS,
        )
        elsewhere.note_friction(
            "worktree setup takes four commands again here",
            role=grogu_plans.ENGINEER,
            target=grogu_plans.TARGET_HARNESS,
        )
        ripe = [
            cluster
            for cluster in grogu_plans.cluster_harness_friction(
                grogu_plans.harness_friction()
            )
            if cluster["ripe"]
        ]
        self.assertEqual(len(ripe), 1)
        self.assertIn("2 repositories", ripe[0]["reason"])

    def test_a_claimed_cluster_stops_being_proposed(self):
        self._harness_home()
        for index in range(grogu_plans.HARNESS_FRICTION_THRESHOLD):
            self.store.note_friction(
                f"plan stage diffing is manual, attempt {index}",
                role=grogu_plans.ENGINEER,
                target=grogu_plans.TARGET_HARNESS,
            )
        self.assertIn("ready to fix in Grogu", grogu_plans.pending_banner(self.root))
        self.assertTrue(
            grogu_plans.claim_harness_friction("f1", reference="pull/33")
        )
        clusters = grogu_plans.cluster_harness_friction(grogu_plans.harness_friction())
        self.assertEqual([c for c in clusters if c["ripe"] or c["stale"]], [])

    def test_the_banner_says_where_the_fix_belongs(self):
        self._harness_home()
        for index in range(grogu_plans.HARNESS_FRICTION_THRESHOLD):
            self.store.note_friction(
                f"steering delivery is invisible, case {index}",
                role=grogu_plans.ENGINEER,
                target=grogu_plans.TARGET_HARNESS,
            )
        outside = grogu_plans.harness_friction_banner(self.root)
        self.assertIn("fixed in the Grogu repository, not here", outside)
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src" / "grogu_cli.py").write_text("", encoding="utf8")
        (grogu_plans.harness_friction_path().with_name(".friction-reminded")).unlink()
        inside = grogu_plans.harness_friction_banner(self.root)
        self.assertIn("You are in the Grogu repository", inside)

    def test_below_the_threshold_nobody_is_nagged(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        self.store.note_friction(
            "one gap", role=grogu_plans.ENGINEER, target=grogu_plans.TARGET_HARNESS
        )
        self.assertEqual(grogu_plans.pending_banner(self.root), "")

    def test_finalized_stages_stay_readable(self):
        plan_id = self.plan()
        self.complete_all(plan_id)
        self.store.finalize(plan_id, as_user=True)
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
            evidence=[self._evidence()],
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


class LoopClosureTests(unittest.TestCase):
    """Regressions for the ways the pipeline used to jam and never recover.

    Each of these reproduces a state the pipeline could reach in an ordinary
    run and then never leave, which for an unsupervised system is worse than a
    crash: it burns tokens looking busy.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def _role(self, role):
        os.environ["GROGU_ROLE"] = role
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)

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

    def test_a_fixed_defect_reopens_the_test_gate(self):
        plan_id = self.plan()
        self.store.set_stage_state(plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE)
        self.store.report_defect(
            plan_id, report="the button does nothing", route="implementation", raised_by="tester"
        )
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])
        # the engineer fixes it and says so the only way it is told to
        self.store.set_stage_state(plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE)
        gate = self.store.gate(plan_id, grogu_plans.GATE_TEST)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_a_resolved_plan_defect_reopens_the_implement_gate(self):
        plan_id = self.plan()
        defect = self.store.report_defect(
            plan_id,
            report="the acceptance criteria cannot be verified",
            route="plan",
            raised_by="tester",
        )
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)["allowed"])
        self.store.resolve_amendment(
            plan_id,
            defect["amendment"],
            outcome=grogu_plans.ACCEPTED,
            reason="checked the code; the criteria were wrong",
            verified=True,
            role=grogu_plans.ARCHITECT,
        )
        self.store.write_stage(
            plan_id, grogu_plans.IMPLEMENTATION, "# revised\n", role=grogu_plans.ARCHITECT
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_one_repository_wide_note_does_not_freeze_every_future_plan(self):
        self.store.steer("always use the system font", requires_replan=True)
        plan_id = self.plan()
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_binding_repository_steering_blocks_only_until_it_is_folded_in(self):
        plan_id = self.plan()
        self.store.steer("switch to the new client", requires_replan=True)
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)["allowed"])
        self.store.write_stage(
            plan_id, grogu_plans.IMPLEMENTATION, "# folded in\n", role=grogu_plans.ARCHITECT
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_the_architect_revising_clears_needs_review_without_a_human(self):
        plan_id = self.plan()
        self.store.steer("use a sheet, not a dialog", plan_id=plan_id, requires_replan=True)
        self.assertEqual(self.store.load(plan_id)["status"], grogu_plans.NEEDS_REVIEW)
        self.store.write_stage(
            plan_id, grogu_plans.IMPLEMENTATION, "# revised\n", role=grogu_plans.ARCHITECT
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_a_plan_the_user_asked_for_still_returns_to_them_after_steering(self):
        plan_id = self.plan(review_required=True)
        self.store.approve(plan_id)
        self.store.steer("change the shape", plan_id=plan_id, requires_replan=True)
        self.store.write_stage(
            plan_id, grogu_plans.IMPLEMENTATION, "# revised\n", role=grogu_plans.ARCHITECT
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("approve" in blocker for blocker in gate["blockers"]))

    def test_the_engineer_cannot_unseal_the_test_plan_by_finishing_it(self):
        plan_id = self.plan()
        self._role(grogu_plans.ENGINEER)
        self.store.set_stage_state(plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.finalize(plan_id, force=True, as_user=True)
        self.assertTrue((self.store.plan_dir(plan_id) / "testing.sealed").exists())

    def test_an_undeclared_caller_may_not_complete_a_sealed_stage_or_finalize(self):
        """Default-deny: the role check was only as strong as the willingness
        to declare a role, and the one role it exists to stop is the one with a
        reason not to."""
        plan_id = self.plan()
        self.store.set_stage_state(plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.finalize(plan_id, force=True)
        self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, as_user=True
        )
        self.assertTrue(self.store.finalize(plan_id, force=True, as_user=True)["emitted"])

    def test_a_required_security_review_is_not_satisfied_by_any_other_review(self):
        plan_id = self.plan()
        self.store.add_workstream(
            plan_id, name="auth", paths=["src/auth/**"], review="security-review"
        )
        self.store.set_stage_state(plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE)
        self.store.record_review(
            plan_id, workstream="auth", kind="rubber-duck", verdict=grogu_plans.PASS, findings="fine"
        )
        self.assertFalse(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])
        self.store.record_review(
            plan_id, workstream="auth", kind="security-review", verdict=grogu_plans.PASS, findings="fine"
        )
        gate = self.store.gate(plan_id, grogu_plans.GATE_TEST)
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_several_fixed_bugs_do_not_escalate_a_healthy_loop(self):
        plan_id = self.plan()
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS):
            self.store.set_stage_state(
                plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
            )
            self.store.report_defect(
                plan_id, report=f"bug {index}", route="implementation", raised_by="tester"
            )
            self.store.set_stage_state(
                plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
            )
            self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, role=grogu_plans.TESTER
        )
        self.assertFalse(self.store.load(plan_id).get("escalated"))

    def test_agents_in_two_worktrees_share_one_plan_store(self):
        import subprocess

        def git(*arguments, cwd):
            subprocess.run(["git", *arguments], cwd=str(cwd), check=True,
                           capture_output=True)

        main = self.root / "main"
        main.mkdir()
        git("init", "-q", cwd=main)
        git("config", "user.email", "t@example.com", cwd=main)
        git("config", "user.name", "T", cwd=main)
        (main / "README").write_text("x", encoding="utf8")
        git("add", "-A", cwd=main)
        git("commit", "-qm", "init", cwd=main)
        linked = self.root / "wt"
        git("worktree", "add", "-q", str(linked), "-b", "feature", cwd=main)

        primary = grogu_plans.PlanStore(main)
        secondary = grogu_plans.PlanStore(linked)
        self.assertEqual(primary.root, secondary.root)
        plan = primary.create("Shared")
        self.assertTrue(any(p["id"] == plan["id"] for p in secondary.list_plans()))


class TriageScopeTests(unittest.TestCase):
    """Software work must not be mistaken for life admin, or vice versa."""

    SOFTWARE = (
        "message the architect about the plan",
        "add a purchase flow to the checkout page",
        "fix the text field validation",
        "reply to the review comment on the PR",
        "build an email service for notifications",
        "update the pricing page copy",
        "a plan for the new indexer",
    )
    PERSONAL = (
        "plan a trip to japan",
        "email my landlord about the rent",
        "check my inbox for the invoice",
        "remind my wife about dinner",
        "draft a note to the dentist",
    )

    def test_software_requests_reach_the_pipeline(self):
        for prompt in self.SOFTWARE:
            with self.subTest(prompt=prompt):
                self.assertTrue(grogu_plans.is_software_work(prompt))

    def test_personal_requests_bypass_the_pipeline(self):
        for prompt in self.PERSONAL:
            with self.subTest(prompt=prompt):
                self.assertFalse(grogu_plans.is_software_work(prompt))
                self.assertFalse(grogu_plans.triage(prompt)["software"])


class HarnessFrictionConcurrencyTests(unittest.TestCase):
    def test_concurrent_notes_are_not_lost(self):
        import subprocess

        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        script = (
            "import sys;"
            f"sys.path.insert(0, {str(ROOT / 'src')!r});"
            "import grogu_plans;"
            f"grogu_plans.PlanStore({repo.name!r}).note_friction("
            "sys.argv[1], role='engineer', target='harness')"
        )
        environment = dict(os.environ, GROGU_HOME=home.name)
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", script, f"note {index}"], env=environment
            )
            for index in range(16)
        ]
        for process in processes:
            self.assertEqual(process.wait(), 0)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)
        self.assertEqual(len(grogu_plans.harness_friction()), 16)


class DesignEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def _plan(self):
        plan = self.store.create("Test plan", design=True)
        for stage in plan["stages"]:
            owner = sorted(grogu_plans.STAGE_WRITERS[stage])[0]
            body = (
                valid_design_spec()
                if stage == grogu_plans.DESIGN
                else f"# {stage} body\n"
            )
            self.store.write_stage(plan["id"], stage, body, role=owner)
        self.store.set_stage_state(
            plan["id"], grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        return plan["id"]

    def test_a_pass_needs_evidence_that_exists(self):
        plan_id = self._plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.design_review(
                plan_id, grogu_plans.PASS,
                evidence=["never-captured.png"], role=grogu_plans.DESIGNER,
            )

    def test_an_empty_screenshot_is_not_evidence(self):
        plan_id = self._plan()
        shot = self.root / "shot.png"
        shot.write_bytes(b"")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.design_review(
                plan_id, grogu_plans.PASS, evidence=[str(shot)],
                role=grogu_plans.DESIGNER,
            )

    def test_a_real_capture_is_accepted(self):
        plan_id = self._plan()
        shot = self.root / "shot.png"
        shot.write_bytes(b"\x89PNG\r\n\x1a\n captured")
        self.store.design_review(
            plan_id, grogu_plans.PASS, evidence=[str(shot)],
            role=grogu_plans.DESIGNER,
        )
        review = self.store.load(plan_id)["design_review"]
        self.assertEqual(review["verdict"], grogu_plans.PASS)


class TriageCorpusTests(unittest.TestCase):
    """Triage runs on every request and is not a model call, so the only way to
    know a change to it is an improvement is to run it against things a user
    actually says."""

    def test_every_case_routes_the_way_a_person_would_route_it(self):
        from triage_cases import CASES

        wrong = [
            (want, grogu_plans.triage(text)["decision"], text)
            for text, want in CASES
            if grogu_plans.triage(text)["decision"] != want
        ]
        self.assertEqual(
            wrong,
            [],
            "\n".join(f"wanted {w}, got {g}: {t}" for w, g, t in wrong),
        )


class ParallelSteeringTests(unittest.TestCase):
    """Steering must reach every agent of a role, not the first one to poll."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.addCleanup(os.environ.pop, "GROGU_AGENT", None)

    def _as(self, agent):
        os.environ["GROGU_AGENT"] = agent

    def test_one_engineer_acking_does_not_hide_the_note_from_its_peers(self):
        self.store.steer("prefer the existing retry helper", role=grogu_plans.ENGINEER)
        self._as("worktree-api")
        self.assertEqual(
            len(self.store.steering(role=grogu_plans.ENGINEER, unread=True)["repository"]), 1
        )
        self.store.ack_steering(role=grogu_plans.ENGINEER)
        self.assertEqual(
            self.store.steering(role=grogu_plans.ENGINEER, unread=True)["repository"], []
        )
        self._as("worktree-store")
        self.assertEqual(
            len(self.store.steering(role=grogu_plans.ENGINEER, unread=True)["repository"]),
            1,
            "the second engineer never saw the user's steering",
        )

    def test_a_note_enters_an_agents_context_once(self):
        """The banner used to ride along with every command until the model
        remembered to ack, so one note could be paid for a dozen times."""
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        self._as("worktree-api")
        self.store.steer("prefer the existing retry helper", role=grogu_plans.ENGINEER)
        first = grogu_plans.pending_banner(self.root)
        self.assertIn("prefer the existing retry helper", first)
        grogu_plans.mark_delivered()
        self.assertEqual(
            grogu_plans.pending_banner(self.root),
            "",
            "the same note was loaded into the agent's context twice",
        )

    def test_an_undelivered_note_is_not_acked(self):
        """The ack happens when the text reaches a stream, so a command that
        composed a banner and then failed does not swallow the note."""
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        self._as("worktree-api")
        self.store.steer("prefer the existing retry helper", role=grogu_plans.ENGINEER)
        self.assertIn("retry helper", grogu_plans.pending_banner(self.root))
        grogu_plans._DELIVERING = None  # the command died before printing
        self.assertIn(
            "retry helper",
            grogu_plans.pending_banner(self.root),
            "a note that was never printed must still be pending",
        )

    def test_delivery_to_one_agent_does_not_consume_its_peers_copy(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        self.store.steer("prefer the existing retry helper", role=grogu_plans.ENGINEER)
        self._as("worktree-api")
        self.assertIn("retry helper", grogu_plans.pending_banner(self.root))
        grogu_plans.mark_delivered()
        self._as("worktree-store")
        self.assertIn(
            "retry helper",
            grogu_plans.pending_banner(self.root),
            "the second engineer never saw the note",
        )

    def test_plan_steering_reaches_an_agent_that_names_the_plan_on_argv(self):
        """The role prompts tell agents to pass the plan id as an argument, but
        the banner only ever read GROGU_PLAN — so an engineer following its own
        instructions was never handed plan-scoped steering at all."""
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        os.environ.pop("GROGU_PLAN", None)
        self._as("worktree-api")
        plan = self.store.create("Test plan")
        self.store.steer(
            "use the shared client",
            role=grogu_plans.ENGINEER,
            plan_id=plan["id"],
        )
        self.assertEqual(
            grogu_plans.pending_banner(self.root), "", "no plan is in scope yet"
        )
        self.assertIn(
            "use the shared client",
            grogu_plans.pending_banner(self.root, plan_hint=plan["id"]),
        )

    def test_acking_for_a_relayed_agent_stops_the_banner_repeating_it(self):
        """When the spawner pushes a note in with write_agent the note has
        arrived, but nothing else knows that, so the agent would be handed the
        same text again by its next command."""
        plan = self.store.create("Test plan")
        self.store.steer(
            "use the shared client",
            role=grogu_plans.ENGINEER,
            plan_id=plan["id"],
        )
        self.store.ack_steering(
            role=grogu_plans.ENGINEER, plan_id=plan["id"], agent="worktree-api"
        )
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        self._as("worktree-api")
        self.assertEqual(
            grogu_plans.pending_banner(self.root, plan_hint=plan["id"]), ""
        )
        self._as("worktree-store")
        self.assertIn(
            "use the shared client",
            grogu_plans.pending_banner(self.root, plan_hint=plan["id"]),
            "a peer that was not relayed to must still be told",
        )

    def test_a_binding_note_is_quoted_where_it_actually_bites(self):
        """Shown once means an agent can lose it. The gate is the moment it
        matters, so the refusal carries the text rather than a count."""
        plan = self.store.create("Test plan")
        self.store.write_stage(
            plan["id"], grogu_plans.IMPLEMENTATION, "# impl\n",
            role=grogu_plans.ARCHITECT,
        )
        self.store.steer(
            "must use the shared client",
            role=grogu_plans.ENGINEER,
            plan_id=plan["id"],
            requires_replan=True,
        )
        gate = self.store.gate(plan["id"], "implement")
        self.assertFalse(gate["allowed"])
        self.assertTrue(
            any("must use the shared client" in blocker for blocker in gate["blockers"]),
            gate["blockers"],
        )

    def test_the_summary_counts_unread_for_the_asking_agent(self):
        plan = self.store.create("Test plan")
        self.store.steer("watch the migration", role=grogu_plans.ENGINEER, plan_id=plan["id"])
        self._as("worktree-api")
        self.store.ack_steering(role=grogu_plans.ENGINEER, plan_id=plan["id"])
        self.assertEqual(
            self.store.summary(plan["id"])["steering_pending"][grogu_plans.ENGINEER], 0
        )
        self._as("worktree-store")
        self.assertEqual(
            self.store.summary(plan["id"])["steering_pending"][grogu_plans.ENGINEER], 1
        )

    def test_an_ack_written_before_per_agent_keys_still_counts(self):
        self.store.steer("older note", role=grogu_plans.ENGINEER)
        payload = json.loads(self.store.steering_path.read_text(encoding="utf8"))
        payload["acked"][grogu_plans.ENGINEER] = 1
        self.store.steering_path.write_text(json.dumps(payload), encoding="utf8")
        self._as("worktree-api")
        self.assertEqual(
            self.store.steering(role=grogu_plans.ENGINEER, unread=True)["repository"],
            [],
            "upgrading replayed every previously-read note",
        )


class ReAuditRegressionTests(unittest.TestCase):
    """The second review's findings, each reproduced before it was fixed."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _plan(self, **kwargs):
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

    def test_a_late_defect_invalidates_a_finished_test_run(self):
        """The auto-close fix opened a hole: a defect filed after testing was
        complete closed itself when the engineer re-completed implementation,
        and finalize then saw no open defects and a complete test stage."""
        plan_id = self._plan()
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, role=grogu_plans.TESTER
        )
        self.store.report_defect(
            plan_id, report="late regression in the retry path",
            route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by=grogu_plans.TESTER,
        )
        self.assertEqual(
            self.store.load(plan_id)["stage_state"][grogu_plans.TESTING],
            grogu_plans.PENDING,
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.finalize(plan_id, as_user=True)
        self.assertIn("testing", str(caught.exception))
        self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, role=grogu_plans.TESTER
        )
        self.assertTrue(self.store.finalize(plan_id, as_user=True)["emitted"])

    def test_a_design_defect_closes_when_the_design_is_revised(self):
        """`stage design complete` was the only closing edge, and nothing in
        the normal path runs it — so one design defect shut the test gate for
        the rest of the plan's life."""
        plan_id = self._plan(design=True)
        self.store.report_defect(
            plan_id, report="the spacing rule is ambiguous",
            route=grogu_plans.ROUTE_DESIGN, raised_by=grogu_plans.ENGINEER,
        )
        self.assertEqual(self.store.load(plan_id)["defects"][0]["status"], grogu_plans.PENDING)
        self.store.write_stage(
            plan_id, grogu_plans.DESIGN, valid_design_spec() + "\nrevised\n",
            role=grogu_plans.DESIGNER,
        )
        self.assertEqual(self.store.load(plan_id)["defects"][0]["status"], grogu_plans.RESOLVED)

    def test_a_required_review_kind_cannot_be_defaulted_into(self):
        """Defaulting the kind to the requirement meant a security review was a
        label you got for free."""
        plan_id = self._plan()
        self.store.add_workstream(
            plan_id, name="auth", paths=["src/auth/**"],
            review=grogu_plans.REVIEW_SECURITY,
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.record_review(
                plan_id, "auth", verdict=grogu_plans.PASS, findings="looked at it",
            )
        record = self.store.record_review(
            plan_id, "auth", verdict=grogu_plans.PASS,
            kind=grogu_plans.REVIEW_SECURITY,
            findings="walked the token verification and the session fixation path",
        )
        self.assertEqual(record["kind"], grogu_plans.REVIEW_SECURITY)

    def test_the_signed_off_commit_is_the_one_the_caller_is_working_on(self):
        """Plan state lives in the primary checkout so parallel worktrees share
        a lock, but the build a designer looked at is the one in *their*
        worktree."""
        repo = self.root / "checkout"
        repo.mkdir()
        for arguments in (
            ["init", "-q", "."],
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *arguments], cwd=str(repo), check=True,
                           capture_output=True)
        (repo / "a.txt").write_text("x", encoding="utf8")
        subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=str(repo), check=True,
                       capture_output=True)
        here = Path.cwd()
        os.chdir(repo)
        self.addCleanup(os.chdir, here)
        self.assertEqual(
            grogu_plans.working_head(), grogu_plans.head_commit(repo)
        )
        self.assertNotEqual(grogu_plans.working_head(), "")

    def test_testing_cannot_be_completed_over_an_open_defect(self):
        """The dual of the late-defect hole. Reopening testing when a defect
        arrives late does nothing for a pass recorded while one was already
        open — the defect then auto-closed on the next implementation
        completion and the plan shipped with no retest."""
        plan_id = self._plan()
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        self.store.report_defect(
            plan_id, report="the retry path drops the last attempt",
            route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by=grogu_plans.TESTER,
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.set_stage_state(
                plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE,
                role=grogu_plans.TESTER,
            )
        self.assertIn("while defect", str(caught.exception))
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, role=grogu_plans.TESTER
        )
        self.assertTrue(self.store.finalize(plan_id, as_user=True)["emitted"])

    def test_an_escalation_keeps_its_evidence(self):
        """Auto-closing defects while the architect is adjudicating left the
        escalation with nothing to look at."""
        plan_id = self._plan()
        self.store.report_defect(
            plan_id, report="first wave", route=grogu_plans.ROUTE_IMPLEMENTATION,
            raised_by=grogu_plans.TESTER,
        )
        for index in range(grogu_plans.DEFAULT_MAX_DEFECT_ROUNDS):
            self.store.set_stage_state(
                plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
                role=grogu_plans.ENGINEER,
            )
            self.store.report_defect(
                plan_id, report=f"still broken {index}",
                route=grogu_plans.ROUTE_IMPLEMENTATION, raised_by=grogu_plans.TESTER,
            )
        self.assertTrue(self.store.load(plan_id)["escalated"])
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role=grogu_plans.ENGINEER,
        )
        self.assertTrue(
            [
                defect
                for defect in self.store.load(plan_id)["defects"]
                if defect["status"] == grogu_plans.PENDING
            ],
            "the escalation lost the defects it was raised about",
        )

    def test_an_agent_cannot_claim_to_be_the_user(self):
        plan_id = self._plan()
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(
                plan_id, grogu_plans.TESTING, grogu_plans.COMPLETE, as_user=True
            )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.finalize(plan_id, force=True, as_user=True)
