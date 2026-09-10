import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_design  # noqa: E402
import grogu_plans  # noqa: E402
import grogu_tasks  # noqa: E402


def valid_design_spec() -> str:
    """A spec with a decision under every heading.

    This used to be the template plus one line, which is exactly the artifact
    the validator now refuses: the skeleton satisfies every structural check by
    construction, so a fixture built from it proved nothing.
    """
    sections = [
        line[3:].strip()
        for line in grogu_plans.design_template("Test plan").splitlines()
        if line.startswith("## ")
    ]
    body = ["# Test plan — design spec", ""]
    for section in sections:
        body.append(f"## {section}")
        if section.startswith("Acceptance"):
            body.append("- the id column is 12 characters wide")
            body.append("- the row gap is 16px at every breakpoint")
        elif section.startswith("Layout"):
            body.append("```")
            body.append("  name       count  state")
            body.append("  widget         3  ready")
            body.append("```")
        else:
            body.append(
                f"Decided for {section.lower()}: the table renders with a 16px row "
                "gap, the id column is 12 characters wide, and the status column "
                "is left-aligned at 10 characters."
            )
        body.append("")
    return "\n".join(body)


class HtmlReportTemplateTests(unittest.TestCase):
    """The standing chrome for a standalone HTML report or guide."""

    def test_renders_with_no_leftover_placeholder_tokens(self):
        html = grogu_plans.html_report_template(
            "Sample Report",
            subtitle="System walkthrough",
            sections=["Overview", "Topology", "Storage"],
        )
        self.assertNotRegex(html, r"__[A-Z_]+__")
        self.assertIn("<!doctype html>", html)
        self.assertIn("Sample Report", html)

    def test_one_section_and_nav_entry_per_named_section(self):
        html = grogu_plans.html_report_template(
            "Sample Report", sections=["Overview", "Options", "Risks"]
        )
        self.assertEqual(html.count('<section id='), 3)
        self.assertIn('href="#overview"', html)
        self.assertIn('href="#options"', html)
        self.assertIn('href="#risks"', html)

    def test_duplicate_section_names_get_distinct_anchors(self):
        html = grogu_plans.html_report_template(
            "Sample Report", sections=["Overview", "Overview"]
        )
        self.assertIn('href="#overview"', html)
        self.assertIn('href="#overview-2"', html)

    def test_defaults_to_a_single_overview_section(self):
        html = grogu_plans.html_report_template("Sample Report")
        self.assertEqual(html.count('<section id='), 1)
        self.assertIn('id="overview"', html)

    def test_storage_key_is_scoped_to_the_title(self):
        html = grogu_plans.html_report_template("Sample Report")
        self.assertIn('"sample-report-theme"', html)

    def test_no_duplicate_ids_between_hero_and_first_section(self):
        html = grogu_plans.html_report_template(
            "Sample Report", sections=["Overview", "Topology"]
        )
        ids = re.findall(r'\sid="([^"]+)"', html)
        self.assertEqual(len(ids), len(set(ids)), f"duplicate ids found: {ids}")
        self.assertIn('id="top"', html)
        self.assertEqual(html.count('<section id='), 2)

    def test_hero_stats_are_omitted_by_default(self):
        html = grogu_plans.html_report_template("Sample Report")
        self.assertNotIn('<div class="hero-stats">', html)

    def test_hero_stats_render_when_provided(self):
        html = grogu_plans.html_report_template(
            "Sample Report",
            hero_stats=[("94.2%", "Hit rate"), ("18ms", "P50 on hit")],
        )
        self.assertIn('<div class="hero-stats">', html)
        self.assertIn("<strong>94.2%</strong><span>Hit rate</span>", html)
        self.assertIn("<strong>18ms</strong><span>P50 on hit</span>", html)

    def test_table_callout_and_diagram_primitives_are_styled(self):
        html = grogu_plans.html_report_template("Sample Report")
        self.assertIn("th, td {", html)
        self.assertIn(".callout.danger", html)
        self.assertIn(".callout.success", html)
        self.assertIn(".callout.security", html)
        self.assertIn(".diagram {", html)
        self.assertIn(".legend {", html)


class TaskStoreWorktreeTests(unittest.TestCase):
    def test_linked_worktree_uses_primary_task_store(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "primary"
            linked = Path(directory) / "feature"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test User"],
                check=True,
            )
            (root / "README.md").write_text("# Demo\n")
            subprocess.run(["git", "-C", str(root), "add", "README.md"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "initial"],
                check=True,
            )

            primary = grogu_tasks.TaskStore(root)
            task = primary.create("Shared task")
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "worktree",
                    "add",
                    "-qb",
                    "feature",
                    str(linked),
                ],
                check=True,
            )

            worktree = grogu_tasks.TaskStore(linked)

            self.assertEqual(worktree.root, root.resolve())
            self.assertEqual(worktree.load(task["id"])["title"], "Shared task")
            self.assertEqual(
                grogu_plans.PlanStore(linked).root,
                worktree.root,
            )


class PlanStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
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
            self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
            self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
            self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
        self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
        self.assertEqual(len(result["emitted"]), 2)  # sealed stages only
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
        self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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

    def test_legacy_review_manifest_migrates_to_required_reviews(self):
        plan_id = self.plan()
        self.store.add_workstream(
            plan_id, name="api", paths=["src/api/**"],
            review=grogu_plans.REVIEW_SECURITY,
        )
        path = self.store.manifest_path(plan_id)
        manifest = json.loads(path.read_text())
        stream = manifest["workstreams"][0]
        stream["review"] = stream.pop("required_reviews")[0]
        path.write_text(json.dumps(manifest))

        migrated = self.store.load(plan_id)["workstreams"][0]
        self.assertEqual(
            migrated["required_reviews"], [grogu_plans.REVIEW_SECURITY]
        )
        self.assertEqual(migrated["review"], grogu_plans.REVIEW_SECURITY)

    def test_every_required_review_kind_blocks_until_recorded(self):
        plan_id = self.plan()
        stream = self.store.add_workstream(
            plan_id,
            name="auth",
            paths=["src/auth/**"],
            required_reviews=[
                grogu_plans.REVIEW_SECURITY,
                grogu_plans.REVIEW_CODE,
            ],
        )
        self.assertEqual(
            stream["required_reviews"],
            [grogu_plans.REVIEW_CODE, grogu_plans.REVIEW_SECURITY],
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )
        self.store.record_review(
            plan_id, "auth", verdict=grogu_plans.PASS,
            kind=grogu_plans.REVIEW_CODE, findings="checked failure paths",
        )
        blockers = self.store.gate(plan_id, grogu_plans.GATE_TEST)["blockers"]
        self.assertTrue(any("auth (security-review)" in item for item in blockers))
        self.store.record_review(
            plan_id, "auth", verdict=grogu_plans.PASS,
            kind=grogu_plans.REVIEW_SECURITY, findings="checked trust boundaries",
        )
        self.assertTrue(self.store.gate(plan_id, grogu_plans.GATE_TEST)["allowed"])

    def test_duplicate_required_review_kinds_are_rejected(self):
        plan_id = self.plan()
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.add_workstream(
                plan_id,
                name="auth",
                paths=["src/auth/**"],
                required_reviews=[
                    grogu_plans.REVIEW_SECURITY,
                    grogu_plans.REVIEW_SECURITY,
                ],
            )
        self.assertIn("duplicate", str(caught.exception))

    def test_replacing_workstream_preserves_requirements_and_all_reviews(self):
        plan_id = self.plan()
        self.store.add_workstream(
            plan_id, name="api", paths=["src/api/**"],
            required_reviews=[grogu_plans.REVIEW_CODE, grogu_plans.REVIEW_SECURITY],
        )
        for kind in (grogu_plans.REVIEW_CODE, grogu_plans.REVIEW_SECURITY):
            self.store.record_review(
                plan_id, "api", verdict=grogu_plans.PASS, kind=kind,
                findings=f"completed {kind}",
            )
        replaced = self.store.add_workstream(
            plan_id, name="api", paths=["src/service/**"], replace=True
        )
        self.assertEqual(
            replaced["required_reviews"],
            [grogu_plans.REVIEW_CODE, grogu_plans.REVIEW_SECURITY],
        )
        self.assertEqual(len(replaced["reviews"]), 2)

    def test_harness_friction_pools_across_repositories(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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
        self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
            self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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
        self.store.read_stage(plan_id, grogu_plans.IMPLEMENTATION, role="architect")
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


class StaticHtmlDocumentTriageTests(unittest.TestCase):
    """A standalone HTML report or guide is a document, not a build to plan."""

    DIRECT = (
        "build me an html file for reviewing the architecture upgrade options",
        "write an html guide to the ivy architecture",
        "make a standalone html report comparing the two approaches",
        "generate an html write-up summarizing the migration options for review",
        "can you build me an html file for reviewing things and guides",
    )
    STILL_PLAN = (
        "build a settings page with dark mode and overrides",
        "add a login form to the html dashboard",
        "create an interactive html dashboard with a database-backed api",
        "build a review dashboard app with an api backend",
    )

    def test_static_html_report_requests_are_answered_directly(self):
        for prompt in self.DIRECT:
            with self.subTest(prompt=prompt):
                result = grogu_plans.triage(prompt)
                self.assertEqual(result["decision"], "direct", prompt)
                self.assertTrue(result["software"], prompt)
                self.assertFalse(result["design"], prompt)

    def test_interactive_surfaces_still_plan_even_when_html_is_mentioned(self):
        for prompt in self.STILL_PLAN:
            with self.subTest(prompt=prompt):
                self.assertEqual(grogu_plans.triage(prompt)["decision"], "plan", prompt)

    def test_explicit_plan_request_still_wins_over_the_static_document_carveout(self):
        result = grogu_plans.triage(
            "please make me a plan for an html guide to the new architecture"
        )
        self.assertEqual(result["decision"], "plan")


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
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
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


class PlanShapeTests(unittest.TestCase):
    """The architect is spawned onto a plan they did not create.

    Everything about the shape of a plan used to be fixed at creation, by
    whoever typed `plan new` -- which is exactly the moment nobody has read
    the request carefully yet.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _plan(self):
        return self.store.create("shape")["id"]

    def test_architect_adds_an_evaluation_stage_after_creation(self):
        plan = self._plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(plan, grogu_plans.EVALUATION, "body", role="architect")
        self.store.add_stage(plan, grogu_plans.EVALUATION, role="architect")
        self.store.write_stage(plan, grogu_plans.EVALUATION, "body", role="architect")
        summary = self.store.summary(plan)
        self.assertIn(grogu_plans.EVALUATION, summary["stages"])
        self.assertEqual(
            summary["stages"],
            [grogu_plans.IMPLEMENTATION, grogu_plans.TESTING, grogu_plans.EVALUATION],
        )

    def test_only_the_architect_may_reshape_a_plan(self):
        plan = self._plan()
        for role in ("engineer", "tester", "designer"):
            with self.assertRaises(grogu_plans.PlanError):
                self.store.add_stage(plan, grogu_plans.EVALUATION, role=role)
            with self.assertRaises(grogu_plans.PlanError):
                self.store.require_review(plan, role=role)

    def test_required_stages_are_not_optional(self):
        plan = self._plan()
        for stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING):
            with self.assertRaises(grogu_plans.PlanError):
                self.store.add_stage(plan, stage, role="architect")
            with self.assertRaises(grogu_plans.PlanError):
                self.store.decline_stage(plan, stage, "no", role="architect")

    def test_declining_a_stage_records_why_and_needs_a_reason(self):
        plan = self._plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.decline_stage(plan, grogu_plans.EVALUATION, "  ", role="architect")
        self.store.decline_stage(
            plan, grogu_plans.EVALUATION, "every assertion is exact", role="architect"
        )
        declined = self.store.summary(plan)["declined_stages"]
        self.assertEqual(declined[grogu_plans.EVALUATION]["why"], "every assertion is exact")

    def test_declining_a_stage_the_plan_has_is_refused(self):
        plan = self.store.create("shape", evaluation=True)["id"]
        with self.assertRaises(grogu_plans.PlanError):
            self.store.decline_stage(plan, grogu_plans.EVALUATION, "changed my mind", role="architect")

    def test_review_can_be_required_after_creation_and_closes_the_gate(self):
        plan = self._plan()
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "body", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.assertTrue(self.store.gate(plan, "implement")["allowed"])
        self.store.require_review(plan, role="architect")
        verdict = self.store.gate(plan, "implement")
        self.assertFalse(verdict["allowed"])
        self.store.approve(plan)
        self.assertTrue(self.store.gate(plan, "implement")["allowed"])

    def test_review_cannot_be_demanded_after_approval(self):
        plan = self._plan()
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "body", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.store.approve(plan)
        with self.assertRaises(grogu_plans.PlanError):
            self.store.require_review(plan, role="architect")

    def test_mistaken_review_hold_is_cleared_without_superseding_or_rewriting(self):
        plan = self._plan()
        self.store.write_stage(
            plan, grogu_plans.IMPLEMENTATION, "implementation body", role="architect"
        )
        self.store.write_stage(
            plan, grogu_plans.TESTING, "testing body", role="architect"
        )
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.IN_PROGRESS, role="engineer"
        )
        stage_paths = {
            stage: self.store.stage_path(plan, stage)
            for stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING)
        }
        stage_bytes = {stage: path.read_bytes() for stage, path in stage_paths.items()}
        stage_state = dict(self.store.load(plan)["stage_state"])

        self.store.require_review(plan, role="architect")
        self.assertFalse(self.store.gate(plan, grogu_plans.GATE_IMPLEMENT)["allowed"])
        with mock.patch.object(grogu_plans, "actor", return_value="architect@test"):
            manifest = self.store.clear_review_requirement(
                plan, "  hold targeted the wrong plan  ", role="architect"
            )

        self.assertTrue(self.store.gate(plan, grogu_plans.GATE_IMPLEMENT)["allowed"])
        self.assertEqual([item["id"] for item in self.store.list_plans()], [plan])
        self.assertEqual(manifest["status"], grogu_plans.DRAFT)
        self.assertFalse(manifest["review_required"])
        self.assertEqual(manifest["stage_state"], stage_state)
        self.assertEqual(
            {stage: path.read_bytes() for stage, path in stage_paths.items()},
            stage_bytes,
        )
        event = manifest["events"][-1]
        self.assertEqual(event["event"], "review_cleared")
        self.assertEqual(event["actor"], "architect@test")
        self.assertEqual(event["reason"], "hold targeted the wrong plan")
        self.assertEqual(event["role"], grogu_plans.ARCHITECT)
        self.assertFalse(event["as_user"])
        self.assertTrue(event["at"])

    def test_clearing_review_preserves_an_independent_needs_review_blocker(self):
        plan = self._plan()
        self.store.require_review(plan, role="architect")
        self.store.set_status(plan, grogu_plans.NEEDS_REVIEW)

        manifest = self.store.clear_review_requirement(
            plan, "review was requested for a different plan", role="architect"
        )

        self.assertEqual(manifest["status"], grogu_plans.NEEDS_REVIEW)
        self.assertFalse(manifest["review_required"])
        gate = self.store.gate(plan, grogu_plans.GATE_IMPLEMENT)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("steering requires" in item for item in gate["blockers"]))

    def test_clear_review_requires_an_architect_and_an_audited_reason(self):
        plan = self._plan()
        self.store.require_review(plan, role="architect")
        for role in grogu_plans.ROLES:
            if role != grogu_plans.ARCHITECT:
                with self.subTest(role=role), self.assertRaises(grogu_plans.PlanError):
                    self.store.clear_review_requirement(plan, "mistake", role=role)
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.clear_review_requirement(plan, "  ", role="architect")
        self.assertIn("reason", str(caught.exception))

    def test_clear_review_default_denies_an_undeclared_caller(self):
        plan = self._plan()
        self.store.require_review(plan, role="architect")

        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.clear_review_requirement(plan, "mistake")

        self.assertIn("--as-user", str(caught.exception))
        self.assertTrue(self.store.load(plan)["review_required"])

    def test_clear_review_allows_an_explicit_user_and_audits_authorization(self):
        plan = self._plan()
        self.store.require_review(plan, role="architect")

        with mock.patch.object(grogu_plans, "actor", return_value="human@test"):
            manifest = self.store.clear_review_requirement(
                plan, "  wrong plan  ", as_user=True
            )

        self.assertFalse(manifest["review_required"])
        event = manifest["events"][-1]
        self.assertEqual(event["event"], "review_cleared")
        self.assertEqual(event["actor"], "human@test")
        self.assertEqual(event["reason"], "wrong plan")
        self.assertEqual(event["role"], "user")
        self.assertTrue(event["as_user"])

    def test_role_bound_agent_cannot_clear_review_as_user(self):
        plan = self._plan()
        self.store.require_review(plan, role="architect")
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)

        with self.assertRaises(grogu_plans.PlanError):
            self.store.clear_review_requirement(
                plan, "mistake", as_user=True
            )

        self.assertTrue(self.store.load(plan)["review_required"])

    def test_identified_engineer_cannot_clear_review_as_architect(self):
        plan = self._plan()
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-one"
        self.store.require_review(plan, role=grogu_plans.ARCHITECT)

        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.brief(grogu_plans.ENGINEER, plan_id=plan)
        os.environ.pop("GROGU_ROLE")

        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.clear_review_requirement(
                plan, "wrong plan", role=grogu_plans.ARCHITECT
            )

        self.assertIn("already bound to the engineer", str(caught.exception))
        self.assertTrue(self.store.load(plan)["review_required"])

    def test_clear_review_refuses_missing_approved_and_invalid_holds(self):
        no_hold = self._plan()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.clear_review_requirement(no_hold, "mistake", role="architect")

        approved = self._plan()
        self.store.write_stage(
            approved, grogu_plans.IMPLEMENTATION, "body", role="architect"
        )
        self.store.write_stage(approved, grogu_plans.TESTING, "body", role="architect")
        self.store.require_review(approved, role="architect")
        self.store.approve(approved)
        for authorization in ({"role": "architect"}, {"as_user": True}):
            with self.subTest(approved_authorization=authorization):
                with self.assertRaises(grogu_plans.PlanError) as caught:
                    self.store.clear_review_requirement(
                        approved, "mistake", **authorization
                    )
                self.assertIn("already approved", str(caught.exception))

        for status in (
            grogu_plans.AMENDING,
            grogu_plans.SUPERSEDED,
            grogu_plans.COMPLETE,
            "future_state",
        ):
            with self.subTest(status=status):
                plan = self._plan()
                self.store.require_review(plan, role="architect")
                manifest = self.store.load(plan)
                manifest["status"] = status
                self.store._write_json(self.store.manifest_path(plan), manifest)
                for authorization in ({"role": "architect"}, {"as_user": True}):
                    with self.subTest(authorization=authorization):
                        with self.assertRaises(grogu_plans.PlanError):
                            self.store.clear_review_requirement(
                                plan, "mistake", **authorization
                            )
                self.assertTrue(self.store.load(plan)["review_required"])

    def test_an_empty_reference_resolves_to_the_ambient_plan(self):
        plan = self._plan()
        self.store.create("another")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.resolve("")
        os.environ["GROGU_PLAN"] = plan
        self.addCleanup(os.environ.pop, "GROGU_PLAN", None)
        self.assertEqual(self.store.resolve(""), plan)

    def test_status_distinguishes_written_from_unwritten_stages(self):
        plan = self._plan()
        self.assertFalse(self.store.summary(plan)["stage_written"][grogu_plans.TESTING])
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.assertTrue(self.store.summary(plan)["stage_written"][grogu_plans.TESTING])


class SteeringDeliveryVisibilityTests(unittest.TestCase):
    """"Did it reach them" is not the same question as "have I read it"."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _plan(self):
        return self.store.create("delivery")["id"]

    def test_the_users_own_read_does_not_count_as_delivery(self):
        plan = self._plan()
        self.store.steer("use minor units of zero for HUF", role="engineer", plan_id=plan)
        # The user looks at the note themselves, from their own shell.
        self.store.steering(role="engineer", plan_id=plan)
        undelivered = self.store.summary(plan)["steering_undelivered"]
        self.assertEqual([note["seq"] for note in undelivered], [1])

    def test_an_agent_reading_it_clears_the_undelivered_line(self):
        plan = self._plan()
        self.store.steer("use minor units of zero for HUF", role="engineer", plan_id=plan)
        self.store.ack_steering(role="engineer", plan_id=plan, agent="s1-engineer")
        self.assertEqual(self.store.summary(plan)["steering_undelivered"], [])

    def test_one_engineer_reading_does_not_mark_it_delivered_for_the_role(self):
        plan = self._plan()
        self.store.steer("stop using floats", role="all", plan_id=plan)
        self.store.ack_steering(role="engineer", plan_id=plan, agent="engineer-one")
        # The engineer has it; no other role has ever run here, so there is
        # nobody else we can honestly say is behind.
        self.assertEqual(self.store.summary(plan)["steering_undelivered"], [])
        # A second engineer has its own key and still gets the note.
        os.environ["GROGU_AGENT"] = "engineer-two"
        self.addCleanup(os.environ.pop, "GROGU_AGENT", None)
        self.assertEqual(self.store.summary(plan)["steering_pending"]["engineer"], 1)
        self.store.ack_steering(role="tester", plan_id=plan, agent="tester-one")
        self.store.steer("and another", role="all", plan_id=plan)
        unread = {
            tuple(note["unread_by"]) for note in self.store.summary(plan)["steering_undelivered"]
        }
        self.assertEqual(unread, {("engineer@engineer-one", "tester@tester-one")})


class PlanRevisionTests(unittest.TestCase):
    """A completed stage is a claim about a plan that still exists."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _ready(self):
        plan = self.store.create("revision")["id"]
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "first", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "first", role="architect")
        return plan

    def test_rewriting_a_stage_reopens_the_completion_it_invalidates(self):
        plan = self._ready()
        self.store.set_stage_state(plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer")
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "second", role="architect", replace=True)
        summary = self.store.summary(plan)
        self.assertEqual(
            summary["stage_state"][grogu_plans.IMPLEMENTATION], grogu_plans.PENDING
        )
        notes = self.store.steering(role="engineer", plan_id=plan)["plan"]
        self.assertTrue(any("re-read it" in note["text"] for note in notes))

    def test_rewriting_a_stage_with_identical_text_changes_nothing(self):
        plan = self._ready()
        self.store.set_stage_state(plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer")
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "first", role="architect")
        self.assertEqual(
            self.store.summary(plan)["stage_state"][grogu_plans.IMPLEMENTATION],
            grogu_plans.COMPLETE,
        )

    def test_rewriting_the_sealed_testing_plan_reopens_the_tester(self):
        plan = self._ready()
        self.store.set_stage_state(plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer")
        self.store.set_stage_state(plan, grogu_plans.TESTING, grogu_plans.COMPLETE, role="tester")
        self.store.write_stage(plan, grogu_plans.TESTING, "second", role="architect", replace=True)
        self.assertEqual(
            self.store.summary(plan)["stage_state"][grogu_plans.TESTING], grogu_plans.PENDING
        )
        notes = self.store.steering(role="tester", plan_id=plan)["plan"]
        self.assertTrue(any("re-read it" in note["text"] for note in notes))

    def test_the_architect_sees_steering_aimed_at_other_roles(self):
        plan = self._ready()
        self.store.steer("HUF has zero minor units", role="engineer", plan_id=plan)
        seen = self.store.steering(role="architect", plan_id=plan)["plan"]
        self.assertTrue(any("HUF" in note["text"] for note in seen))
        # ...and the engineer still does not see the tester's mail.
        self.store.steer("check the scale, not just the value", role="tester", plan_id=plan)
        engineer = self.store.steering(role="engineer", plan_id=plan)["plan"]
        self.assertFalse(any("scale" in note["text"] for note in engineer))


class PlanStageConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("concurrency")["id"]

    def test_stale_base_reports_the_intervening_writer(self):
        initial = self.store.stage_version(
            self.plan, grogu_plans.IMPLEMENTATION
        )["digest"]
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-one"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "first body", base=initial
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(
                self.plan, grogu_plans.IMPLEMENTATION, "stale body", base=initial
            )
        message = str(caught.exception)
        self.assertIn("architect-one", message)
        self.assertIn("current revision", message)
        self.assertIn("10 bytes", message)
        self.assertIn("sha256:", message)

    def test_only_one_racing_compare_and_swap_wins(self):
        base = self.store.stage_version(
            self.plan, grogu_plans.IMPLEMENTATION
        )["digest"]
        barrier = threading.Barrier(2)
        results = []

        def write(body):
            contender = grogu_plans.PlanStore(self.root)
            barrier.wait()
            try:
                contender.write_stage(
                    self.plan, grogu_plans.IMPLEMENTATION, body,
                    role=grogu_plans.ARCHITECT, base=base,
                )
                results.append("written")
            except grogu_plans.PlanError as error:
                results.append(str(error))

        threads = [
            threading.Thread(target=write, args=("body one",)),
            threading.Thread(target=write, args=("body two",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(results.count("written"), 1)
        self.assertEqual(sum("changed after base" in item for item in results), 1)

    def test_replacement_writer_permanently_revokes_the_stale_identity(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-old"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "old draft"
        )
        os.environ["GROGU_AGENT"] = "architect-new"
        self.store.supersede_stage_writer(
            self.plan, grogu_plans.IMPLEMENTATION
        )
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "replacement draft"
        )

        os.environ["GROGU_AGENT"] = "architect-old"
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(
                self.plan, grogu_plans.IMPLEMENTATION, "stale overwrite"
            )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.supersede_stage_writer(
                self.plan, grogu_plans.IMPLEMENTATION
            )

        manifest = self.store.load(self.plan)
        self.assertEqual(len(manifest["revisions"]), 2)
        self.assertIn("architect@architect-old", manifest["agents_seen"])
        self.assertIn("architect@architect-new", manifest["agents_seen"])

    def test_replace_allows_a_new_same_role_writer_to_take_over(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-old"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "old draft"
        )
        os.environ["GROGU_AGENT"] = "architect-new"
        self.store.write_stage(
            self.plan,
            grogu_plans.IMPLEMENTATION,
            "replacement draft",
            replace=True,
        )
        self.assertEqual(
            self.store.load(self.plan)["stage_writers"][
                grogu_plans.IMPLEMENTATION
            ]["agent"],
            "architect-new",
        )
        os.environ["GROGU_AGENT"] = "architect-old"
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(
                self.plan,
                grogu_plans.IMPLEMENTATION,
                "old writer returns",
                replace=True,
            )

    def test_reset_releases_the_active_stage_writer(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-old"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "old draft"
        )
        self.store.reset_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ARCHITECT
        )
        self.assertNotIn(
            grogu_plans.IMPLEMENTATION,
            self.store.load(self.plan)["stage_writers"],
        )
        os.environ["GROGU_AGENT"] = "architect-new"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "new draft"
        )

    def test_superseded_writer_cannot_reset_the_replacement_stage(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-old"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "old draft"
        )
        os.environ["GROGU_AGENT"] = "architect-new"
        self.store.write_stage(
            self.plan,
            grogu_plans.IMPLEMENTATION,
            "replacement draft",
            replace=True,
        )
        os.environ["GROGU_AGENT"] = "architect-old"
        with self.assertRaises(grogu_plans.PlanError):
            self.store.reset_stage(
                self.plan,
                grogu_plans.IMPLEMENTATION,
                role=grogu_plans.ARCHITECT,
            )

    def test_anonymous_write_cannot_bypass_an_active_writer(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-one"
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "owned draft"
        )
        os.environ.pop("GROGU_ROLE")
        os.environ.pop("GROGU_AGENT")
        with mock.patch.object(
            self.store, "_identified_agent", return_value=("", "")
        ):
            with self.assertRaises(grogu_plans.PlanError) as caught:
                self.store.write_stage(
                    self.plan,
                    grogu_plans.IMPLEMENTATION,
                    "anonymous overwrite",
                    role=grogu_plans.ARCHITECT,
                )
        self.assertIn("unidentified caller", str(caught.exception))


class PlanGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("governance")["id"]
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "implementation",
            role=grogu_plans.ARCHITECT,
        )
        self.store.write_stage(
            self.plan, grogu_plans.TESTING, "testing",
            role=grogu_plans.ARCHITECT,
        )

    def test_exceeded_budget_blocks_completion_until_checkpoint(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan,
            agent="engineer-one",
            role=grogu_plans.ENGINEER,
            tool_calls=5,
            elapsed_seconds=60,
            ai_credits=2,
            checkpoint_tool_calls=4,
        )
        self.store.record_agent_usage(
            self.plan,
            agent="engineer-one",
            tool_calls=6,
            elapsed_seconds=61,
            ai_credits=3,
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.set_stage_state(
                self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
            )
        self.assertIn("without a current checkpoint", str(caught.exception))
        status = self.store.summary(self.plan)["governance"]
        self.assertTrue(status["warnings"])
        self.assertTrue(status["blockers"])

        checkpoint = self.store.record_checkpoint(
            self.plan, note="saved a recoverable checkpoint"
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(
                self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
            )
        recovery = self.store.record_checkpoint_recovery(
            self.plan, checkpoint["id"], status="available"
        )
        self.assertEqual(recovery["status"], "available")
        self.store.set_stage_state(
            self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )

    def test_checkpoint_deadline_rearms_after_a_recovered_checkpoint(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan,
            agent="engineer-one",
            role=grogu_plans.ENGINEER,
            checkpoint_tool_calls=4,
        )
        self.store.record_agent_usage(
            self.plan, agent="engineer-one", tool_calls=4
        )
        checkpoint = self.store.record_checkpoint(self.plan)
        self.store.record_checkpoint_recovery(
            self.plan, checkpoint["id"], status="available"
        )
        self.store.record_agent_usage(
            self.plan, agent="engineer-one", tool_calls=8
        )
        status = self.store.governance_status(self.plan)
        self.assertTrue(status["agents"]["engineer-one"]["checkpoint_due"])
        self.assertTrue(status["blockers"])

    def test_partial_governance_update_preserves_other_limits(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan,
            agent="engineer-one",
            role=grogu_plans.ENGINEER,
            tool_calls=10,
            checkpoint_tool_calls=4,
        )
        updated = self.store.configure_agent_governance(
            self.plan, agent="engineer-one", tool_calls=8
        )
        self.assertEqual(updated["limits"]["tool_calls"], 8)
        self.assertEqual(updated["limits"]["checkpoint_tool_calls"], 4)

    def test_usage_counters_cannot_move_backwards(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan, agent="engineer-one", tool_calls=10
        )
        self.store.record_agent_usage(
            self.plan, agent="engineer-one", tool_calls=6
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.record_agent_usage(
                self.plan, agent="engineer-one", tool_calls=5
            )

    def test_another_agent_is_not_blocked_by_an_unscoped_peer_budget(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-governor"
        self.store.configure_agent_governance(
            self.plan,
            agent="engineer-one",
            role=grogu_plans.ENGINEER,
            tool_calls=5,
        )
        self.store.record_agent_usage(
            self.plan, agent="engineer-one", tool_calls=6
        )
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-two"
        self.store.set_stage_state(
            self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
        )

    def test_governance_role_survives_a_partial_privileged_update(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        os.environ["GROGU_AGENT"] = "architect-governor"
        self.store.configure_agent_governance(
            self.plan,
            agent="engineer-one",
            role=grogu_plans.ENGINEER,
            tool_calls=10,
            workstream="api",
        )
        updated = self.store.configure_agent_governance(
            self.plan, agent="engineer-one", tool_calls=8
        )
        self.assertEqual(updated["role"], grogu_plans.ENGINEER)
        self.assertEqual(updated["workstream"], "api")

    def test_bound_agent_cannot_escalate_governance_by_changing_role_env(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan, agent="engineer-one", tool_calls=10
        )
        os.environ["GROGU_ROLE"] = grogu_plans.ARCHITECT
        with self.assertRaises(grogu_plans.PlanError):
            self.store.configure_agent_governance(
                self.plan, agent="engineer-one", tool_calls=20
            )

    def test_lost_role_environment_still_honors_the_bound_agents_blocker(self):
        os.environ["GROGU_ROLE"] = grogu_plans.ENGINEER
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.configure_agent_governance(
            self.plan, agent="engineer-one", tool_calls=5
        )
        self.store.record_agent_usage(
            self.plan, agent="engineer-one", tool_calls=6
        )
        os.environ.pop("GROGU_ROLE")
        os.environ.pop("GROGU_AGENT")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(
                self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE
            )


class ParallelCompletionTests(unittest.TestCase):
    """Fanning out is only safe if "done" means "my part", not "all of it"."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT", "GROGU_WORKSTREAM"):
            os.environ.pop(variable, None)

    def _split(self):
        plan = self.store.create("parallel")["id"]
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "body", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.store.add_workstream(plan, name="parse", paths=["src/parse.py"])
        self.store.add_workstream(plan, name="render", paths=["src/render.py"])
        return plan

    def test_one_engineer_finishing_does_not_open_the_test_gate(self):
        plan = self._split()
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role="engineer", workstream="parse",
        )
        summary = self.store.summary(plan)
        self.assertEqual(
            summary["stage_state"][grogu_plans.IMPLEMENTATION], grogu_plans.PENDING
        )
        self.assertFalse(self.store.gate(plan, "test")["allowed"])
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role="engineer", workstream="render",
        )
        self.assertEqual(
            self.store.summary(plan)["stage_state"][grogu_plans.IMPLEMENTATION],
            grogu_plans.COMPLETE,
        )
        self.assertTrue(self.store.gate(plan, "test")["allowed"])

    def test_a_split_plan_refuses_an_unattributed_completion(self):
        plan = self._split()
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.set_stage_state(
                plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer"
            )
        self.assertIn("--workstream", str(caught.exception))
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(
                plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
                role="engineer", workstream="nope",
            )

    def test_the_workstream_comes_from_the_environment_too(self):
        plan = self._split()
        os.environ["GROGU_WORKSTREAM"] = "parse"
        self.addCleanup(os.environ.pop, "GROGU_WORKSTREAM", None)
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer"
        )
        states = {
            stream["name"]: stream["state"]
            for stream in self.store.summary(plan)["workstreams"]
        }
        self.assertEqual(states["parse"], grogu_plans.COMPLETE)
        self.assertEqual(states["render"], grogu_plans.PENDING)

    def test_a_single_workstream_plan_still_completes_plainly(self):
        plan = self.store.create("one")["id"]
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "body", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.store.add_workstream(plan, name="only", paths=["src/**"])
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer"
        )
        self.assertEqual(
            self.store.summary(plan)["stage_state"][grogu_plans.IMPLEMENTATION],
            grogu_plans.COMPLETE,
        )

    def test_overlapping_workstreams_block_the_implement_gate(self):
        plan = self.store.create("collide")["id"]
        self.store.write_stage(plan, grogu_plans.IMPLEMENTATION, "body", role="architect")
        self.store.write_stage(plan, grogu_plans.TESTING, "body", role="architect")
        self.store.add_workstream(plan, name="a", paths=["src/**"])
        self.store.add_workstream(plan, name="b", paths=["src/parse.py"])
        verdict = self.store.gate(plan, "implement")
        self.assertFalse(verdict["allowed"])
        self.assertTrue(any("both claim" in reason for reason in verdict["blockers"]))

    def test_a_workstream_can_be_corrected_but_not_after_it_is_finished(self):
        plan = self._split()
        with self.assertRaises(grogu_plans.PlanError):
            self.store.add_workstream(plan, name="parse", paths=["src/feed.py"])
        self.store.add_workstream(
            plan, name="parse", paths=["src/feed.py"], replace=True
        )
        paths = {
            stream["name"]: stream["paths"]
            for stream in self.store.summary(plan)["workstreams"]
        }
        self.assertEqual(paths["parse"], ["src/feed.py"])
        self.store.set_stage_state(
            plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE,
            role="engineer", workstream="parse",
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.add_workstream(
                plan, name="parse", paths=["src/other.py"], replace=True
            )


class AgentPresenceTests(unittest.TestCase):
    """Which of my two engineers has not seen the correction."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def test_an_agent_that_has_never_acked_is_still_known_to_be_behind(self):
        plan = self.store.create("presence")["id"]
        os.environ["GROGU_AGENT"] = "engineer-two"
        self.addCleanup(os.environ.pop, "GROGU_AGENT", None)
        self.store.note_agent_presence(plan, "engineer")
        os.environ["GROGU_AGENT"] = "engineer-one"
        self.store.note_agent_presence(plan, "engineer")
        self.store.steer("strip thousands separators", role="engineer", plan_id=plan)
        self.store.ack_steering(role="engineer", plan_id=plan, agent="engineer-one")
        undelivered = self.store.summary(plan)["steering_undelivered"]
        self.assertEqual(
            [note["unread_by"] for note in undelivered], [["engineer@engineer-two"]]
        )


class LateReviewHoldTests(unittest.TestCase):
    """A hold placed after the work landed does not un-land it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def test_requiring_review_over_completed_work_says_so(self):
        plan = self.store.create("late")["id"]
        self.store.write_stage(plan, "implementation", "do it", role="architect")
        self.store.write_stage(plan, "testing", "check it", role="architect")
        self.store.set_stage_state(plan, "implementation", "complete", role="engineer")
        manifest = self.store.require_review(plan, role="architect")
        self.assertTrue(
            any("already completed" in warning for warning in manifest.get("warnings", []))
        )

    def test_requiring_review_before_any_work_is_silent(self):
        plan = self.store.create("early")["id"]
        manifest = self.store.require_review(plan, role="architect")
        self.assertEqual(manifest.get("warnings", []), [])


class ReviewHoldEnforcementTests(unittest.TestCase):
    """The hold has to bind the held party, not just answer the gate."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("held", review_required=True)["id"]
        self.store.write_stage(self.plan, "implementation", "do it", role="architect")
        self.store.write_stage(self.plan, "testing", "check it", role="architect")

    def test_an_engineer_that_never_checked_the_gate_is_still_stopped(self):
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.set_stage_state(
                self.plan, "implementation", "complete", role="engineer"
            )
        self.assertIn("plan approve", str(caught.exception))

    def test_approval_releases_it(self):
        self.store.approve(self.plan)
        manifest = self.store.set_stage_state(
            self.plan, "implementation", "complete", role="engineer"
        )
        self.assertEqual(manifest["stage_state"]["implementation"], "complete")


class FrictionRoutingTests(unittest.TestCase):
    """Fifteen of nineteen notes were filed into the wrong bucket."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.home.mkdir()
        self.previous = os.environ.get("GROGU_HOME")
        os.environ["GROGU_HOME"] = str(self.home)
        self.store = grogu_plans.PlanStore(Path(self.temporary.name) / "repo")
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self._restore)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _restore(self):
        if self.previous is None:
            os.environ.pop("GROGU_HOME", None)
        else:
            os.environ["GROGU_HOME"] = self.previous

    def test_a_note_naming_a_grogu_command_is_pooled_without_the_flag(self):
        self.store.note_friction(
            "grogu plan status does not honour $GROGU_PLAN", role="architect"
        )
        pooled = grogu_plans.harness_friction()
        self.assertEqual(len(pooled), 1)
        self.assertEqual(self.store.friction()["notes"], [])

    def test_a_note_about_the_project_stays_in_the_project(self):
        self.store.note_friction(
            "the vendor CSV has ragged rows and no header", role="engineer"
        )
        self.assertEqual(grogu_plans.harness_friction(), [])
        self.assertEqual(len(self.store.friction()["notes"]), 1)

    def test_the_caller_can_still_insist_it_is_local(self):
        self.store.note_friction(
            "grogu plan new is fine, our wrapper script is not",
            role="engineer",
            target=grogu_plans.TARGET_REPO_ONLY,
        )
        self.assertEqual(grogu_plans.harness_friction(), [])
        self.assertEqual(len(self.store.friction()["notes"]), 1)


class FinalizeArtifactTests(unittest.TestCase):
    """The plans have to reach the pull request, not just the disk."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("ship it")["id"]
        self.store.write_stage(self.plan, "implementation", "build it", role="architect")
        self.store.write_stage(self.plan, "testing", "check it", role="architect")
        self.store.set_stage_state(self.plan, "implementation", "complete", role="engineer")
        self.store.set_stage_state(self.plan, "testing", "complete", role="tester")

    def _staged(self):
        listing = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf8",
        )
        return set(listing.stdout.split())

    def test_finalizing_stages_the_plans_it_unsealed(self):
        result = self.store.finalize(self.plan, role="architect")
        staged = self._staged()
        self.assertTrue(result["staged"])
        self.assertIn(f".grogu/plans/{self.plan}/testing.md", staged)
        self.assertIn(f".grogu/plans/{self.plan}/implementation.md", staged)

    def test_the_working_manifest_is_not_shipped(self):
        self.store.finalize(self.plan, role="architect")
        self.assertNotIn(f".grogu/plans/{self.plan}/manifest.json", self._staged())

    def test_the_record_carries_what_the_stage_files_do_not(self):
        self.store.decline_stage(self.plan, "evaluation", "no taste call here", role="architect")
        self.store.steer("use decimal", plan_id=self.plan, role="engineer")
        self.store.finalize(self.plan, role="architect")
        record = (self.root / ".grogu" / "plans" / self.plan / "record.md").read_text()
        self.assertIn("no taste call here", record)
        self.assertIn("use decimal", record)


class WorkstreamWorktreeStoreTests(unittest.TestCase):
    """Harness friction #40, at the `PlanStore` layer: a workstream's
    dedicated worktree has to be reachable through the same plan/workstream
    vocabulary architects and engineers already use, not just the lower-level
    `grogu_worktrees` functions."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.root, check=True)
        (self.root / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        self.plan = self.store.create("ship it")["id"]
        self.store.add_workstream(
            self.plan, name="scaffold", paths=["src/scaffold/**"]
        )
        self.store.add_workstream(
            self.plan, name="ingest", paths=["src/ingest/**"]
        )

    def test_workstream_worktree_is_outside_the_primary_checkout(self):
        result = self.store.workstream_worktree(self.plan, "scaffold")

        self.assertTrue(result["created"])
        self.assertNotEqual(Path(result["path"]).resolve(), self.root.resolve())
        self.assertTrue(Path(result["path"]).is_dir())

    def test_two_declared_workstreams_get_distinct_worktrees(self):
        scaffold = self.store.workstream_worktree(self.plan, "scaffold")
        ingest = self.store.workstream_worktree(self.plan, "ingest")

        self.assertNotEqual(scaffold["path"], ingest["path"])
        self.assertNotEqual(scaffold["branch"], ingest["branch"])

    def test_workstream_worktree_is_idempotent(self):
        first = self.store.workstream_worktree(self.plan, "scaffold")
        second = self.store.workstream_worktree(self.plan, "scaffold")

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["path"], second["path"])

    def test_undeclared_workstream_is_rejected(self):
        with self.assertRaises(grogu_plans.PlanError) as failure:
            self.store.workstream_worktree(self.plan, "no-such-workstream")
        self.assertIn("scaffold", str(failure.exception))
        self.assertIn("ingest", str(failure.exception))

    def test_list_workstream_worktrees_reports_declared_flag(self):
        self.store.workstream_worktree(self.plan, "scaffold")
        entries = {entry["name"]: entry for entry in self.store.list_workstream_worktrees(self.plan)}

        self.assertIn("scaffold", entries)
        self.assertTrue(entries["scaffold"]["declared"])

    def test_list_workstream_worktrees_flags_a_dropped_workstream(self):
        self.store.workstream_worktree(self.plan, "ingest")
        self.store.drop_workstream(self.plan, "ingest")

        entries = {entry["name"]: entry for entry in self.store.list_workstream_worktrees(self.plan)}

        self.assertIn("ingest", entries)
        self.assertFalse(entries["ingest"]["declared"])

    def test_remove_workstream_worktree_works_after_drop(self):
        result = self.store.workstream_worktree(self.plan, "ingest")
        self.store.drop_workstream(self.plan, "ingest")

        removed = self.store.remove_workstream_worktree(self.plan, "ingest")

        self.assertEqual(removed["removed"], result["path"])
        self.assertFalse(Path(result["path"]).exists())

    def test_remove_workstream_worktree_reports_nothing_to_remove(self):
        removed = self.store.remove_workstream_worktree(self.plan, "scaffold")
        self.assertEqual(removed["removed"], "")
        self.assertFalse(removed["branch_deleted"])

    def test_scratch_files_stay_confined_to_their_own_worktree(self):
        """The exact regression friction #40 describes: an untracked scratch
        file written while working on one workstream must not appear in
        another workstream's worktree, or in the shared primary checkout."""
        scaffold = self.store.workstream_worktree(self.plan, "scaffold")
        ingest = self.store.workstream_worktree(self.plan, "ingest")

        (Path(ingest["path"]) / ".listing.xml").write_text("scratch\n")

        self.assertFalse((Path(scaffold["path"]) / ".listing.xml").exists())
        self.assertFalse((self.root / ".listing.xml").exists())

    def test_multi_word_workstream_name_gets_its_own_worktree(self):
        """`workstream_branch` slugifies a name to build its Git ref, but
        `workstream_worktree` itself must still accept and honour the raw,
        unslugified name declared on the plan."""
        self.store.add_workstream(self.plan, name="api gateway", paths=["src/api/**"])

        result = self.store.workstream_worktree(self.plan, "api gateway")

        self.assertTrue(result["created"])
        self.assertNotEqual(Path(result["path"]).resolve(), self.root.resolve())
        self.assertTrue(Path(result["path"]).is_dir())

    def test_list_workstream_worktrees_recovers_the_raw_multi_word_name(self):
        """Regression: listing used to derive the name back from the
        slugified branch (`api-gateway`), which never matched the declared
        `api gateway` workstream and so always reported it as no longer
        declared, even right after it was created."""
        self.store.add_workstream(self.plan, name="api gateway", paths=["src/api/**"])
        self.store.workstream_worktree(self.plan, "api gateway")

        entries = {
            entry["name"]: entry for entry in self.store.list_workstream_worktrees(self.plan)
        }

        self.assertIn("api gateway", entries)
        self.assertNotIn("api-gateway", entries)
        self.assertTrue(entries["api gateway"]["declared"])

    def test_remove_workstream_worktree_works_for_a_multi_word_name(self):
        self.store.add_workstream(self.plan, name="api gateway", paths=["src/api/**"])
        created = self.store.workstream_worktree(self.plan, "api gateway")

        removed = self.store.remove_workstream_worktree(self.plan, "api gateway")

        self.assertEqual(removed["removed"], created["path"])
        self.assertFalse(Path(created["path"]).exists())


class CommissionTests(unittest.TestCase):
    """The architect could open a design stage it could not brief."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("statusline", design=True)["id"]

    def test_the_designer_brief_carries_the_statement_of_work(self):
        self.store.commission(
            self.plan, "designer", "decide what dies first at 40 columns", by="architect"
        )
        brief = self.store.brief("designer", plan_id=self.plan)
        self.assertIn("40 columns", brief["commission"]["brief"])

    def test_only_the_architect_commissions(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.commission(self.plan, "designer", "do it", by="engineer")

    def test_steering_records_who_wrote_it(self):
        note = self.store.steer("prefer decimal", plan_id=self.plan, role="engineer")
        self.assertEqual(note["from"], "user")
        os.environ["GROGU_ROLE"] = "architect"
        self.addCleanup(os.environ.pop, "GROGU_ROLE", None)
        note = self.store.steer("and no floats", plan_id=self.plan, role="engineer")
        self.assertEqual(note["from"], "architect")


class SteeringRetractionTests(unittest.TestCase):
    """Append-only steering meant noise could only grow."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("retract")["id"]

    def test_an_undelivered_note_stops_being_shown(self):
        note = self.store.steer("ignore this probe", plan_id=self.plan, role="tester")
        result = self.store.retract_steering(note["seq"], plan_id=self.plan)
        self.assertFalse(result["delivered"])
        self.assertEqual(self.store.steering(role="tester", plan_id=self.plan)["plan"], [])

    def test_a_delivered_note_says_it_was_already_read(self):
        note = self.store.steer("wrong note", plan_id=self.plan, role="tester")
        self.store.ack_steering(role="tester", plan_id=self.plan, agent="t1")
        result = self.store.retract_steering(note["seq"], plan_id=self.plan)
        self.assertTrue(result["delivered"])

    def test_retracting_a_note_that_never_existed_is_an_error(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.retract_steering(9, plan_id=self.plan)


class StageResetTests(unittest.TestCase):
    """There was no way back from a bad stage write."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("reset", design=True)["id"]

    def test_a_written_stage_can_be_returned_to_unwritten(self):
        self.store.write_stage(self.plan, "design", valid_design_spec(), role="designer")
        self.assertTrue(self.store.summary(self.plan)["stage_written"]["design"])
        self.store.reset_stage(self.plan, "design", role="architect")
        self.assertFalse(self.store.summary(self.plan)["stage_written"]["design"])

    def test_only_the_architect_resets(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.reset_stage(self.plan, "design", role="designer")


class TemplateSpecTests(unittest.TestCase):
    """The one artifact guaranteed to pass used to be the empty one."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("stub", design=True)["id"]

    def test_the_template_written_back_unchanged_is_refused(self):
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(
                self.plan, "design", grogu_plans.design_template("stub"), role="designer"
            )
        self.assertIn("template's own instructions", str(caught.exception))

    def test_a_spec_with_decisions_under_every_heading_is_accepted(self):
        self.store.write_stage(self.plan, "design", valid_design_spec(), role="designer")

    def test_the_designer_can_close_its_stage_before_the_user_approves(self):
        self.store.require_review(self.plan, role="architect")
        self.store.write_stage(self.plan, "design", valid_design_spec(), role="designer")
        manifest = self.store.set_stage_state(
            self.plan, "design", "complete", role="designer"
        )
        self.assertEqual(manifest["stage_state"]["design"], "complete")
        with self.assertRaises(grogu_plans.PlanError):
            self.store.set_stage_state(
                self.plan, "implementation", "complete", role="engineer"
            )


class HollowSpecTests(unittest.TestCase):
    """A spec can say nothing without using a single banned adjective."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("hollow", design=True)["id"]

    def _spec(self, sentence):
        sections = [
            line[3:].strip()
            for line in grogu_plans.design_template("hollow").splitlines()
            if line.startswith("## ")
        ]
        body = ["# hollow — design spec", ""]
        for section in sections:
            body.extend([f"## {section}", sentence, ""])
        return "\n".join(body)

    def test_one_hollow_sentence_per_heading_is_refused(self):
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(
                self.plan, "design", self._spec("It works."), role="designer"
            )
        self.assertIn("say nothing a tester could check", str(caught.exception))

    def test_a_layout_section_without_a_fenced_block_is_refused(self):
        spec = valid_design_spec().replace("```\n  name       count  state\n  widget         3  ready\n```", "It looks right.")
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(self.plan, "design", spec, role="designer")
        self.assertIn("Layout", str(caught.exception))

    def test_an_empty_section_is_named_as_empty_not_as_the_template(self):
        spec = valid_design_spec()
        spec = spec.replace(
            "## Flow\nDecided for flow:", "## Flow\n\n## Was flow:"
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.write_stage(self.plan, "design", spec, role="designer")
        self.assertIn("empty", str(caught.exception))


class AttachmentTests(unittest.TestCase):
    """The designer's proof script had nowhere to go but a scratch directory."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line", design=True)["id"]

    def test_an_attachment_reaches_the_next_role(self):
        self.store.attach(
            self.plan,
            "verify_spec.py",
            "print('derived every block')\n",
            stage="design",
            role="designer",
            note="proves the blocks are mutually derivable",
        )
        brief = self.store.brief("tester", plan_id=self.plan)
        names = [item["name"] for item in brief["attachments"]]
        self.assertEqual(names, ["verify_spec.py"])
        self.assertEqual(brief["attachments"][0]["role"], "designer")

    def test_the_same_name_replaces_rather_than_duplicates(self):
        self.store.attach(self.plan, "check.py", "one\n")
        result = self.store.attach(self.plan, "check.py", "two\n")
        self.assertTrue(result["replaced"])
        self.assertEqual(len(self.store.attachments(self.plan)), 1)

    def test_a_secret_does_not_ride_along_with_the_plan(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.attach(
                self.plan,
                "config.py",
                'AWS_SECRET_ACCESS_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"\n',  # grogu-allow-secret: AWS's own published example key
            )

    def test_an_empty_artifact_is_refused(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.attach(self.plan, "empty.py", "   \n")

    def test_a_path_cannot_escape_the_plan_directory(self):
        self.store.attach(self.plan, "../../escape.py", "print(1)\n")
        self.assertTrue(
            (self.store.plan_dir(self.plan) / "attachments" / "escape.py").is_file()
        )


class UserCommandPresenceTests(unittest.TestCase):
    """Approving is not agent work, whatever role last read a brief here."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line", review_required=True)["id"]

    def test_a_bound_role_does_not_make_the_user_an_agent(self):
        self.store.brief("tester", plan_id=self.plan)
        grogu_plans.pending_banner(
            Path(self.temporary.name), plan_hint=self.plan, user_command=True
        )
        seen = self.store.load(self.plan).get("agents_seen", {})
        self.assertEqual(seen, {})

    def test_an_agent_command_still_records_presence(self):
        self.store.brief("tester", plan_id=self.plan)
        grogu_plans.pending_banner(Path(self.temporary.name), plan_hint=self.plan)
        seen = self.store.load(self.plan).get("agents_seen", {})
        self.assertTrue(any(key.startswith("tester") for key in seen))


class DestructiveWriteTests(unittest.TestCase):
    """One command silently lost work no other role could recover."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line")["id"]
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "the real plan\n" * 200, role="architect"
        )

    def test_rewriting_a_complete_stage_needs_saying_so(self):
        self.store.set_stage_state(
            self.plan, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, role="engineer"
        )
        with self.assertRaises(grogu_plans.PlanError):
            self.store.write_stage(
                self.plan, grogu_plans.IMPLEMENTATION, "probe", role="architect"
            )

    def test_the_replaced_text_is_recoverable(self):
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "probe", role="architect"
        )
        latest = self.store.revisions(self.plan, grogu_plans.IMPLEMENTATION)[-1]
        recovered = self.store.revision_body(
            self.plan, grogu_plans.IMPLEMENTATION, latest["revision"]
        )
        self.assertIn("the real plan", recovered)

    def test_losing_most_of_a_plan_says_so(self):
        result = self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "probe", role="architect"
        )
        self.assertTrue(
            any("bytes to" in warning for warning in result.get("warnings", []))
        )

    def test_a_sealed_stage_is_recoverable_as_plain_text(self):
        self.store.write_stage(
            self.plan, grogu_plans.TESTING, "the real test plan", role="architect"
        )
        self.store.write_stage(
            self.plan, grogu_plans.TESTING, "replaced", role="architect"
        )
        latest = self.store.revisions(self.plan, grogu_plans.TESTING)[-1]
        self.assertEqual(
            self.store.revision_body(
                self.plan, grogu_plans.TESTING, latest["revision"]
            ),
            "the real test plan",
        )


class VerifiedIsCheckedTests(unittest.TestCase):
    """`--verified` was an honour system and an architect walked through it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line")["id"]
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "the plan", role="architect"
        )
        self.amendment = self.store.amend(
            self.plan, claim="the seam cannot carry it", raised_by="engineer"
        )

    def _resolve(self):
        return self.store.resolve_amendment(
            self.plan,
            self.amendment["id"],
            outcome=grogu_plans.ACCEPTED,
            reason="checked it",
            verified=True,
            role=grogu_plans.ARCHITECT,
        )

    def test_an_architect_that_read_nothing_is_refused(self):
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self._resolve()
        self.assertIn("nothing records you reading", str(caught.exception))

    def test_reading_the_stage_is_what_makes_it_verified(self):
        self.store.read_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ARCHITECT
        )
        self.assertEqual(self._resolve()["status"], grogu_plans.ACCEPTED)

    def test_a_missing_amendment_says_so_before_demanding_verification(self):
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.resolve_amendment(
                self.plan,
                "a99",
                outcome=grogu_plans.ACCEPTED,
                reason="x",
                verified=False,
                role=grogu_plans.ARCHITECT,
            )
        self.assertIn("no amendment", str(caught.exception))

    def test_a_no_op_rewrite_does_not_answer_an_accepted_amendment(self):
        self.store.read_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ARCHITECT
        )
        self._resolve()
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "the plan", role="architect"
        )
        self.assertFalse(
            self.store.gate(self.plan, grogu_plans.GATE_IMPLEMENT)["allowed"]
        )


class RewriteReopensWorkTests(unittest.TestCase):
    """A rewritten plan left workstreams complete against text that was gone."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line")["id"]
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "the plan", role="architect"
        )
        self.store.add_workstream(self.plan, name="driver", paths=["src/driver.py"])
        self.store.add_workstream(self.plan, name="render", paths=["src/render.py"])

    def test_rewriting_the_plan_reopens_the_workstreams(self):
        for name in ("driver", "render"):
            self.store.set_stage_state(
                self.plan,
                grogu_plans.IMPLEMENTATION,
                grogu_plans.COMPLETE,
                role="engineer",
                workstream=name,
            )
        self.store.write_stage(
            self.plan,
            grogu_plans.IMPLEMENTATION,
            "the plan, corrected",
            role="architect",
            replace=True,
        )
        states = self.store.load(self.plan)["workstream_state"]
        self.assertEqual(set(states.values()), {grogu_plans.PENDING})


class VerifierTests(unittest.TestCase):
    """The harness carried the designer's check and never ran it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_plans.PlanStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("status line")["id"]
        for stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING):
            self.store.write_stage(self.plan, stage, "the plan", role="architect")

    def _attach(self, body):
        self.store.attach(
            self.plan, "check.py", body, role="designer", verifier=True
        )

    def test_a_passing_verifier_passes(self):
        self._attach("print('every block derives')\n")
        self.assertTrue(self.store.run_verifiers(self.plan)["passed"])

    def test_a_failing_verifier_blocks_the_test_gate(self):
        self._attach("raise SystemExit(1)\n")
        self.store.run_verifiers(self.plan)
        gate = self.store.gate(self.plan, grogu_plans.GATE_TEST)
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("verifier" in reason for reason in gate["blockers"]))

    def test_an_unrun_verifier_blocks_the_test_gate(self):
        self._attach("print('fine')\n")
        gate = self.store.gate(self.plan, grogu_plans.GATE_TEST)
        self.assertTrue(any("never been run" in reason for reason in gate["blockers"]))

    def test_a_plan_with_no_verifier_says_so(self):
        with self.assertRaises(grogu_plans.PlanError):
            self.store.run_verifiers(self.plan)


class WorkingStateStaysLocalTests(unittest.TestCase):
    """"The manifest stays local" was enforced by a comment, in a public repo."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.plan = self.store.create("a plan")["id"]

    def test_git_add_everything_does_not_sweep_in_the_manifest(self):
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf8",
        ).stdout
        self.assertNotIn("manifest.json", staged)
        self.assertIn("implementation.md", staged)

    def test_superseded_drafts_stay_local(self):
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "first draft", role="architect"
        )
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "second draft", role="architect"
        )
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf8",
        ).stdout
        self.assertNotIn("revisions/", staged)


class UnsealedStagesArePublishedTooTests(unittest.TestCase):
    """Finalize scanned only the sealed stages, so most of a plan shipped unread."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def _plan(self):
        plan = self.store.create("Test plan")
        for stage in plan["stages"]:
            owner = sorted(grogu_plans.STAGE_WRITERS[stage])[0]
            self.store.write_stage(plan["id"], stage, f"# {stage} body\n", role=owner)
        for stage in plan["stages"]:
            if stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING):
                self.store.set_stage_state(
                    plan["id"], stage, grogu_plans.COMPLETE, as_user=True
                )
        return plan["id"]

    def test_a_secret_in_the_implementation_plan_blocks_finalize(self):
        plan_id = self._plan()
        self.store.write_stage(
            plan_id,
            grogu_plans.IMPLEMENTATION,
            "# implementation body\n\nUse AKIA" + "IOSFODNN7EXAMPLE" + " for the run.\n",
            role=grogu_plans.ARCHITECT,
            replace=True,
        )
        self.store.set_stage_state(
            plan_id, grogu_plans.IMPLEMENTATION, grogu_plans.COMPLETE, as_user=True
        )
        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.finalize(plan_id, as_user=True)
        self.assertIn("implementation", str(caught.exception))


class TasteStaysOffTheInternetTests(unittest.TestCase):
    """A finalized plan is committed in plaintext to a public repository."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.addCleanup(self.temporary.cleanup)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def test_a_principle_the_user_stated_may_not_be_quoted_in_a_plan(self):
        private = "Never put a destructive action behind a modal dialogue box"
        with mock.patch.object(
            grogu_plans, "_private_design_statements", lambda: [private]
        ):
            plan = self.store.create("Test plan")
            for stage in plan["stages"]:
                owner = sorted(grogu_plans.STAGE_WRITERS[stage])[0]
                body = f"# {stage} body\n"
                if stage == grogu_plans.IMPLEMENTATION:
                    body += f"\nThe user's principle: {private}.\n"
                self.store.write_stage(plan["id"], stage, body, role=owner)
            for stage in plan["stages"]:
                if stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING):
                    self.store.set_stage_state(
                        plan["id"], stage, grogu_plans.COMPLETE, as_user=True
                    )
            with self.assertRaises(grogu_plans.PlanError) as caught:
                self.store.finalize(plan["id"], as_user=True)
        self.assertIn("verbatim", str(caught.exception))

    def test_an_adopted_public_set_is_not_private(self):
        store = grogu_design.DesignStore(self.root / "design")
        store.seed_apple()
        self.assertEqual(store.private_statements(), [])


class OneAgentIsOneIdentityTests(unittest.TestCase):
    """A subagent runs every command in a fresh shell; GROGU_AGENT gets dropped."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous_cwd)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("identity")["id"]

    def test_a_dropped_environment_variable_does_not_split_the_agent_in_two(self):
        os.environ["GROGU_AGENT"] = "aq-architect"
        self.store.bind_session(grogu_plans.ARCHITECT, self.plan)
        with_variable = self.store._ack_key(grogu_plans.ARCHITECT)
        del os.environ["GROGU_AGENT"]
        without = self.store._ack_key(grogu_plans.ARCHITECT)
        self.assertEqual(with_variable, without)
        self.assertEqual(without, "architect@aq-architect")

    def test_steering_read_under_one_shell_is_read_under_the_next(self):
        os.environ["GROGU_AGENT"] = "aq-architect"
        self.store.bind_session(grogu_plans.ARCHITECT, self.plan)
        self.store.steer("use the real feed", plan_id=self.plan, role=grogu_plans.ARCHITECT)
        self.store.ack_steering(role=grogu_plans.ARCHITECT, plan_id=self.plan)
        del os.environ["GROGU_AGENT"]
        pending = self.store.steering(
            role=grogu_plans.ARCHITECT, plan_id=self.plan, unread=True
        )
        self.assertEqual(pending["repository"] + pending["plan"], [])

    def test_an_explicit_name_still_wins(self):
        os.environ["GROGU_AGENT"] = "aq-architect"
        self.store.bind_session(grogu_plans.ARCHITECT, self.plan)
        self.assertEqual(
            self.store._ack_key(grogu_plans.ARCHITECT, "other-one"),
            "architect@other-one",
        )


class AgentRoleBindingTests(unittest.TestCase):
    """An identity may not become another pipeline role to cross a seal."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        self.previous_cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self.previous_cwd)
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)
        self.plan = self.store.create("role binding")["id"]
        self.store.write_stage(
            self.plan, grogu_plans.IMPLEMENTATION, "build from the contract", role="architect"
        )
        self.store.write_stage(
            self.plan, grogu_plans.TESTING, "sealed independent assertions", role="architect"
        )

    def _as(self, role: str, agent: str) -> None:
        os.environ["GROGU_ROLE"] = role
        os.environ["GROGU_AGENT"] = agent

    def test_an_identified_engineer_cannot_impersonate_a_reader_of_the_seal(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.read_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ENGINEER
        )
        os.environ.pop("GROGU_ROLE")

        for claimed in (grogu_plans.TESTER, grogu_plans.ARCHITECT):
            with self.subTest(claimed=claimed):
                with self.assertRaises(grogu_plans.PlanError) as caught:
                    self.store.read_stage(
                        self.plan, grogu_plans.TESTING, role=claimed
                    )
                self.assertIn(f"cannot act as the {claimed}", str(caught.exception))

        denied = [
            entry
            for entry in self.store.load(self.plan)["access_log"]
            if not entry["allowed"]
        ]
        self.assertEqual(len(denied), 2)

    def test_the_binding_survives_a_fresh_shell_that_lost_both_variables(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.brief(grogu_plans.ENGINEER, plan_id=self.plan)
        self.store.read_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ENGINEER
        )
        os.environ.pop("GROGU_ROLE")
        os.environ.pop("GROGU_AGENT")

        with self.assertRaises(grogu_plans.PlanError) as caught:
            self.store.read_stage(
                self.plan, grogu_plans.TESTING, role=grogu_plans.TESTER
            )
        self.assertIn("already bound to the engineer", str(caught.exception))

    def test_distinct_tester_and_architect_identities_keep_their_access(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.read_stage(
            self.plan, grogu_plans.IMPLEMENTATION, role=grogu_plans.ENGINEER
        )

        self._as(grogu_plans.TESTER, "tester-one")
        testing = self.store.read_stage(
            self.plan, grogu_plans.TESTING, role=grogu_plans.TESTER
        )
        self.assertIn("independent assertions", testing)

        self._as(grogu_plans.ARCHITECT, "architect-two")
        architect_copy = self.store.read_stage(
            self.plan, grogu_plans.TESTING, role=grogu_plans.ARCHITECT
        )
        self.assertEqual(architect_copy, testing)
        self.assertEqual(
            set(self.store.load(self.plan)["agents_seen"]),
            {
                "engineer@engineer-one",
                "tester@tester-one",
                "architect@architect-two",
            },
        )
        os.environ.pop("GROGU_ROLE")
        os.environ["GROGU_AGENT"] = "engineer-one"
        with self.assertRaises(grogu_plans.PlanError):
            self.store.read_stage(
                self.plan, grogu_plans.TESTING, role=grogu_plans.TESTER
            )

    def test_briefs_reject_role_switching_but_accept_a_distinct_agent(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.brief(grogu_plans.ENGINEER, plan_id=self.plan)
        os.environ.pop("GROGU_ROLE")

        with self.assertRaises(grogu_plans.PlanError):
            self.store.brief(grogu_plans.TESTER, plan_id=self.plan)

        self._as(grogu_plans.TESTER, "tester-one")
        brief = self.store.brief(grogu_plans.TESTER, plan_id=self.plan)
        self.assertEqual(brief["role"], grogu_plans.TESTER)

    def test_human_as_user_operations_ignore_an_agents_persisted_binding(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.brief(grogu_plans.ENGINEER, plan_id=self.plan)
        os.environ.pop("GROGU_ROLE")
        os.environ.pop("GROGU_AGENT")

        self.store.set_stage_state(
            self.plan,
            grogu_plans.IMPLEMENTATION,
            grogu_plans.COMPLETE,
            as_user=True,
        )
        self.store.set_stage_state(
            self.plan,
            grogu_plans.TESTING,
            grogu_plans.COMPLETE,
            as_user=True,
        )
        result = self.store.finalize(self.plan, as_user=True)
        self.assertEqual(result["status"], grogu_plans.COMPLETE)

    def test_steering_a_different_role_does_not_change_the_callers_binding(self):
        self._as(grogu_plans.ENGINEER, "engineer-one")
        self.store.brief(grogu_plans.ENGINEER, plan_id=self.plan)
        self.store.steer(
            "check the null branch",
            role=grogu_plans.TESTER,
            plan_id=self.plan,
        )
        os.environ.pop("GROGU_ROLE")
        os.environ.pop("GROGU_AGENT")

        binding = self.store.session_binding()
        self.assertEqual(binding["role"], grogu_plans.ENGINEER)
        self.assertEqual(binding["agent"], "engineer-one")


class PaddedStageTests(unittest.TestCase):
    """A sealed stage nobody else reads has to be worth reading.

    The first tester run in this pipeline's life was handed a testing plan that
    was one sentence repeated ninety times. It coped, and filed friction. The
    next one might have tested nothing and reported that it passed, and neither
    the architect nor the gate would have noticed.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.plan = self.store.create("padding", evaluation=True)["id"]

    def test_one_sentence_ninety_times_is_not_a_testing_plan(self):
        body = "A real sentence about how the tester checks a null AQI. " * 90
        with self.assertRaises(grogu_plans.PlanError) as refused:
            self.store.write_stage(self.plan, "testing", body, role="architect")
        self.assertIn("padding, not a plan", str(refused.exception))

    def test_a_terse_plan_is_still_a_plan(self):
        # The failure mode to avoid is a check that makes an architect pad to
        # get past it, which is the thing being caught.
        body = (
            "# Testing\n\n"
            "Load a file where every value is empty and assert nothing is zero.\n"
            "Load a file with a real zero and assert it still renders as zero.\n"
            "Kill the network mid-fetch and assert the cache shows its true age.\n"
            "Assert the renderer never shows a category for a null reading.\n"
            "Run the whole suite twice to catch order dependence between cases.\n"
        )
        self.store.write_stage(self.plan, "testing", body, role="architect")

    def test_a_long_real_plan_is_not_mistaken_for_padding(self):
        body = "\n".join(
            f"Step {index}: check the {word} path and record what came back, "
            f"then compare it against the recorded fixture for {word}."
            for index, word in enumerate(
                "parser renderer cache network clock storage retry timeout "
                "fallback alert nearest hourly".split()
            )
        )
        self.assertEqual(grogu_plans.padded_body(body), "")

    def test_a_repeated_boilerplate_line_does_not_condemn_a_real_plan(self):
        # A checklist repeats "Record the result." after every step; that is a
        # plan with a habit, not padding.
        body = "\n".join(
            f"Check that the {word} behaves as the contract says. Record the result."
            for word in (
                "parser renderer cache network clock storage retry timeout "
                "fallback alert nearest hourly reader writer"
            ).split()
        )
        self.assertEqual(grogu_plans.padded_body(body), "")
