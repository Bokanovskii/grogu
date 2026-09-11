import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401
import grogu_cli  # noqa: E402
import grogu_plans  # noqa: E402
import grogu_privacy  # noqa: E402
import grogu_tasks  # noqa: E402
from tests.test_grogu_plans import valid_design_spec


class FrictionSweepTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        for key in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(key, None)

    def test_dependency_warning_is_negation_aware_and_accepts_bare_domains(self):
        self.assertEqual(
            grogu_plans.uncited_dependencies(
                "No new dependency, no new package is needed."
            ),
            [],
        )
        self.assertEqual(
            grogu_plans.uncited_dependencies(
                "Add a new dependency: slowapi. Checked pypi.org/project/slowapi."
            ),
            [],
        )
        self.assertTrue(
            grogu_plans.uncited_dependencies("Add a new dependency: slowapi.")
        )

    def test_secret_scan_ignores_type_identifiers_and_oauth_placeholders(self):
        text = (
            "const fencingTokenSchema = FencingToken\n"  # grogu-allow-secret
            'GROGU_GMAIL_TOKEN = "your-oauth-access-token"\n'  # grogu-allow-secret
        )
        self.assertEqual(grogu_privacy.scan(text, personal=False), [])

    def test_current_task_is_the_sessions_one_live_lease(self):
        tasks = grogu_tasks.TaskStore(self.root)
        task = tasks.create("Current work")
        with mock.patch.dict(
            os.environ,
            {
                "GROGU_AGENT": "friction-engineer",
                "GROGU_SESSION_ID": "session-one",
                "GROGU_SESSION_PID": "0",
            },
        ):
            tasks.claim(task["id"])
            self.assertEqual(tasks.current_task(), task["id"])

    def test_binary_attachments_are_preserved_and_brief_names_real_path(self):
        plan = self.store.create("Screenshots")["id"]
        payload = b"\x89PNG\r\n\x1a\n\x00\xff"
        result = self.store.attach(plan, "review.png", payload, role="designer")
        self.assertEqual(Path(result["path"]).read_bytes(), payload)
        brief = self.store.brief("engineer", plan_id=plan)
        self.assertEqual(
            brief["attachment_path"],
            str(self.store.plan_dir(plan) / "attachments"),
        )

    def test_design_changes_reach_the_engineer_brief(self):
        plan = self.store.create("Review", design=True)["id"]
        self.store.write_stage(
            plan, grogu_plans.DESIGN, valid_design_spec(), role="designer"
        )
        self.store.design_review(
            plan,
            grogu_plans.CHANGES,
            notes="Reduce the toolbar to one primary action.",
            role="designer",
        )
        brief = self.store.brief("engineer", plan_id=plan)
        self.assertEqual(brief["design_review"]["verdict"], grogu_plans.CHANGES)

    def test_design_pass_deadlock_names_the_exact_unblock_command(self):
        plan = self.store.create("Review", design=True)["id"]
        evidence = self.root / "shot.png"
        evidence.write_bytes(b"\x89PNG\r\n\x1a\n")
        with self.assertRaisesRegex(
            grogu_plans.PlanError,
            rf"grogu plan complete {plan} implementation",
        ):
            self.store.design_review(
                plan,
                grogu_plans.PASS,
                evidence=[str(evidence)],
                role="designer",
            )

    def test_completed_workstream_review_requirement_can_change_before_review(self):
        plan = self.store.create("Review routing")["id"]
        self.store.add_workstream(
            plan, name="api", paths=["src/api/**"], review="code-review"
        )
        manifest = self.store.load(plan)
        manifest.setdefault("workstream_state", {})["api"] = grogu_plans.COMPLETE
        self.store._write_json(self.store.manifest_path(plan), manifest)
        stream = self.store.set_workstream_reviews(
            plan, "api", ["security-review"]
        )
        self.assertEqual(stream["required_reviews"], ["security-review"])

    def test_parallel_workstreams_scale_amendment_rounds_and_deduplicate(self):
        plan = self.store.create("Parallel")["id"]
        for name in ("api", "ui", "data"):
            self.store.add_workstream(plan, name=name, paths=[f"{name}/**"])
        first = self.store.amend(plan, claim="Shared lockfile ownership is wrong")
        duplicate = self.store.amend(
            plan, claim="  shared LOCKFILE ownership is wrong  "
        )
        self.assertEqual(first["id"], duplicate["id"])
        for index in range(5):
            self.store.amend(plan, claim=f"Distinct issue {index}")

    def test_typed_lint_reports_every_partition_digest(self):
        plan = grogu_plans.PlanDocumentStore.create(
            self.store, "Typed", design=True, evaluation=True
        )["id"]
        documents = grogu_plans.PlanDocumentStore.for_plan(self.store, plan)
        result = documents.verify(role="designer")
        self.assertEqual(
            set(result["partition_digests"]),
            {"graph/open.json", "graph/testing.sealed", "graph/evaluation.sealed"},
        )

    def test_record_attributes_steering_to_its_actual_source(self):
        plan = self.store.create("Attribution")["id"]
        manifest = self.store.load(plan)
        manifest["steering"] = [
            {"text": "Re-run the contract check.", "from": "engineer"}
        ]
        record = self.store._plan_record(manifest)
        self.assertIn("## Steering", record)
        self.assertIn("From the engineer", record)
        self.assertNotIn("Corrections from the user", record)

    def test_finalize_stages_artifacts_in_the_calling_worktree(self):
        main = self.root / "main"
        main.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=main, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=main,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=main, check=True
        )
        (main / "README").write_text("main\n", encoding="utf8")
        subprocess.run(["git", "add", "README"], cwd=main, check=True)
        subprocess.run(["git", "commit", "-qm", "initial"], cwd=main, check=True)
        linked = self.root / "linked"
        subprocess.run(
            ["git", "worktree", "add", "-q", "-b", "feature", str(linked)],
            cwd=main,
            check=True,
        )
        store = grogu_plans.PlanStore(linked)
        relative = ".grogu/plans/p-test/record.md"
        source = main / relative
        source.parent.mkdir(parents=True)
        source.write_text("review record\n", encoding="utf8")
        self.assertEqual(store._stage_for_review([relative]), [relative])
        self.assertEqual((linked / relative).read_text(), "review record\n")
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=linked,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn(relative, staged)

    def test_new_cli_flags_parse_without_private_api_workarounds(self):
        parser = grogu_cli.build_parser()
        cases = (
            ["plan", "gate", "implement", "--role", "engineer"],
            ["plan", "steer", "--file", "-"],
            ["plan", "commission", "p-20260910-abcdef", "designer", "--file", "-"],
            ["plan", "attach", "p-20260910-abcdef", "--list", "--json"],
            [
                "plan",
                "doc",
                "node",
                "add",
                "p-20260910-abcdef",
                "--kind",
                "note",
                "--title",
                "Draft",
                "--body-file",
                "-",
                "--base",
                "r0001",
            ],
            ["design", "lint", "--file", "-", "--json"],
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                parser.parse_args(arguments)

    def test_independent_command_modules_register_without_editing_grogu_cli(self):
        extensions = self.root / "commands"
        extensions.mkdir()
        (extensions / "grogu_command_example.py").write_text(
            "COMMANDS = ('example',)\n"
            "def register(subparsers):\n"
            "    parser = subparsers.add_parser('example')\n"
            "    parser.set_defaults(handler=lambda args: 0)\n",
            encoding="utf8",
        )
        with mock.patch.object(grogu_cli, "COMMAND_MODULE_DIR", extensions):
            with mock.patch.object(grogu_cli, "_COMMAND_EXTENSIONS", None):
                self.assertEqual(grogu_cli._extension_command_names(), {"example"})
                parsed = grogu_cli.build_parser().parse_args(["example"])
                self.assertEqual(parsed.handler(parsed), 0)


if __name__ == "__main__":
    unittest.main()
