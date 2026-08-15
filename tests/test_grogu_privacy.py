"""Tests for the egress guard.

The precision tests matter as much as the detection ones. A scanner that
reports placeholders trains everybody to pass `--no-verify`, and a guard that
is routinely overridden is worse than none, because it also carries assurance.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_privacy  # noqa: E402
import grogu_tasks  # noqa: E402


class DetectionTests(unittest.TestCase):
    def assertCaught(self, text, label=""):
        findings = grogu_privacy.scan(text)
        self.assertTrue(findings, f"missed: {text}")
        if label:
            self.assertTrue(
                any(label in finding.label for finding in findings),
                f"expected {label!r}, got {[f.label for f in findings]}",
            )

    def assertClean(self, text):
        self.assertEqual(
            [finding.label for finding in grogu_privacy.scan(text)], [], text
        )

    def test_vendor_credentials_are_caught(self):
        self.assertCaught("token = ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "GitHub")  # grogu-allow-secret
        self.assertCaught("key: AKIAIOSFODNN7EXAMPLE", "AWS")  # grogu-allow-secret
        self.assertCaught("-----BEGIN OPENSSH PRIVATE KEY-----", "private key")  # grogu-allow-secret
        self.assertCaught("xoxb-1234567890-abcdefghij", "Slack")  # grogu-allow-secret
        self.assertCaught("use sk-ant-api03-abcdefghijklmnopqrstuvwxyz01", "Anthropic")  # grogu-allow-secret

    def test_a_connection_string_with_a_password_is_a_secret(self):
        self.assertCaught(
            "DATABASE_URL=postgres://app:hunter2iswrong@db.internal:5432/prod",  # grogu-allow-secret
            "connection string",
        )

    def test_hard_coded_assignments_are_caught(self):
        self.assertCaught(
            "SESSION_SECRET = 'k9Xq2mZp7Lw4Rt8Nv6Bd3Fy1Hs5Jc0A'", "SESSION_SECRET"  # grogu-allow-secret
        )

    def test_the_correct_way_to_write_it_is_not_flagged(self):
        self.assertClean("api_key = os.environ['API_KEY']")
        self.assertClean('token = process.env.GITHUB_TOKEN')
        self.assertClean("password: ${DB_PASSWORD}")
        self.assertClean("SECRET_KEY = config('SECRET_KEY')")

    def test_placeholders_are_not_reported(self):
        for value in (
            "API_KEY = 'your-api-key-here'",
            "SECRET = 'changeme'",
            "token = '<insert token>'",
            "PASSWORD = 'xxxxxxxxxxxx'",
            "client_secret: replace-with-your-secret",
        ):
            with self.subTest(value=value):
                self.assertClean(value)

    def test_personal_data_is_detected_but_kept_separate(self):
        findings = grogu_privacy.scan("write to charlie@somewhere.io or 415-555-0132")  # grogu-allow-secret
        self.assertEqual({finding.kind for finding in findings}, {grogu_privacy.PERSONAL})

    def test_example_and_noreply_addresses_are_ordinary(self):
        self.assertClean("see user@example.com")
        self.assertClean("Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>")

    def test_numbers_that_merely_look_like_cards_are_left_alone(self):
        self.assertClean("const id = 1234567890123456")
        self.assertClean("timeout after 400 ms, retry 3 times")

    def test_a_real_card_number_is_caught(self):
        self.assertCaught("card 4111 1111 1111 1111", "payment card")  # grogu-allow-secret

    def test_secrets_block_everywhere_and_personal_data_only_in_public(self):
        findings = grogu_privacy.scan("mail charlie@somewhere.io")  # grogu-allow-secret
        self.assertEqual(
            grogu_privacy.blocking(findings, destination=grogu_privacy.REPOSITORY), []
        )
        self.assertEqual(
            len(grogu_privacy.blocking(findings, destination=grogu_privacy.PUBLISHED)), 1
        )

    def test_a_marked_line_is_deliberately_exempt(self):
        secret = "token = ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"  # grogu-allow-secret
        self.assertTrue(grogu_privacy.scan(secret))
        self.assertEqual(grogu_privacy.scan(secret + "  # grogu-allow-secret"), [])

    def test_the_report_never_prints_the_whole_secret(self):
        secret = "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"  # grogu-allow-secret
        text = grogu_privacy.report(grogu_privacy.scan(f"token = {secret}"))
        self.assertNotIn(secret, text)

    def test_redaction_removes_the_value_and_keeps_the_sentence(self):
        redacted = grogu_privacy.redact(
            "it failed with token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789 for charlie@somewhere.io"  # grogu-allow-secret
        )
        self.assertNotIn("ghp_", redacted)
        self.assertNotIn("charlie@somewhere.io", redacted)  # grogu-allow-secret
        self.assertIn("it failed with token", redacted)


class DangerousPathTests(unittest.TestCase):
    def test_credential_files_are_recognised(self):
        for path in (
            ".env", "app/.env.production", "config/id_rsa", "certs/server.pem",
            ".npmrc", "secrets.yaml", "gcp-service_account.json",
        ):
            with self.subTest(path=path):
                self.assertTrue(grogu_privacy.dangerous_path(path))

    def test_example_environment_files_are_fine(self):
        for path in (".env.example", ".env.sample", "config/.env.template"):
            with self.subTest(path=path):
                self.assertFalse(grogu_privacy.dangerous_path(path))


class StagedScanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self._git("init", "-q", ".")
        self._git("config", "user.email", "t@example.com")
        self._git("config", "user.name", "T")

    def _git(self, *arguments):
        return subprocess.run(
            ["git", *arguments], cwd=str(self.repo), capture_output=True, text=True,
            check=False,
        )

    def test_a_staged_secret_is_found(self):
        (self.repo / "settings.py").write_text(
            "SESSION_SECRET = 'k9Xq2mZp7Lw4Rt8Nv6Bd3Fy1Hs5Jc0A'\n", encoding="utf8"  # grogu-allow-secret
        )
        self._git("add", "-A")
        findings = grogu_privacy.scan_staged(self.repo)
        self.assertTrue(findings)

    def test_a_staged_env_file_is_found_by_name(self):
        (self.repo / ".env").write_text("NOTHING=here\n", encoding="utf8")
        self._git("add", "-f", ".env")
        labels = [finding.label for finding in grogu_privacy.scan_staged(self.repo)]
        self.assertIn("credential file staged for commit", labels)

    def test_removing_a_leaked_secret_is_never_blocked(self):
        leaked = self.repo / "settings.py"
        leaked.write_text(
            "SESSION_SECRET = 'k9Xq2mZp7Lw4Rt8Nv6Bd3Fy1Hs5Jc0A'\n", encoding="utf8"  # grogu-allow-secret
        )
        self._git("add", "-A")
        self._git("commit", "-qm", "oops")
        leaked.write_text("SESSION_SECRET = os.environ['SESSION_SECRET']\n", encoding="utf8")
        self._git("add", "-A")
        self.assertEqual(grogu_privacy.scan_staged(self.repo), [])

    def test_the_hook_blocks_a_commit_and_lets_the_fix_through(self):
        grogu_privacy.install_hook(
            self.repo, python=sys.executable, script=str(ROOT / "src" / "grogu_cli.py")
        )
        target = self.repo / "settings.py"
        target.write_text(
            "AWS_SECRET_ACCESS_KEY = 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'\n",  # grogu-allow-secret
            encoding="utf8",
        )
        self._git("add", "-A")
        blocked = self._git("commit", "-m", "add settings")
        self.assertNotEqual(blocked.returncode, 0, blocked.stdout + blocked.stderr)
        target.write_text(
            "AWS_SECRET_ACCESS_KEY = os.environ['AWS_SECRET_ACCESS_KEY']\n",
            encoding="utf8",
        )
        self._git("add", "-A")
        allowed = self._git("commit", "-m", "add settings")
        self.assertEqual(allowed.returncode, 0, allowed.stdout + allowed.stderr)

    def test_hook_arguments_survive_windows_paths_and_spaces(self):
        python = r"C:\Program Files\Python\python.exe"
        script = r"C:\work trees\grogu\grogu_cli.py"
        path = grogu_privacy.install_hook(
            self.repo, python=python, script=script
        )
        command = path.read_text(encoding="utf8").splitlines()[-1]
        self.assertEqual(
            shlex.split(command),
            ["exec", python, script, "guard", "staged", "--quiet"],
        )


class PipelineEgressTests(unittest.TestCase):
    """The pipeline's own publishing paths."""

    def setUp(self):
        sys.path.insert(0, str(ROOT / "src"))
        import grogu_plans

        self.grogu_plans = grogu_plans
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = grogu_plans.PlanStore(self.root)
        for variable in ("GROGU_ROLE", "GROGU_PLAN"):
            os.environ.pop(variable, None)

    def test_finalize_refuses_to_publish_a_plan_containing_a_secret(self):
        plans = self.grogu_plans
        plan = self.store.create("Test plan")
        self.store.write_stage(
            plan["id"], plans.IMPLEMENTATION, "# do the thing\n", role=plans.ARCHITECT
        )
        self.store.write_stage(
            plan["id"],
            plans.TESTING,
            "# run it with token ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789\n",  # grogu-allow-secret
            role=plans.ARCHITECT,
        )
        with self.assertRaises(plans.PlanError) as caught:
            self.store.finalize(plan["id"], force=True, as_user=True)
        self.assertIn("must not be published", str(caught.exception))

    def test_harness_friction_is_redacted_before_it_leaves_the_repository(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        os.environ["GROGU_HOME"] = home.name
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])
        self.store.note_friction(
            "grogu auth failed with ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789",  # grogu-allow-secret
            role="engineer",
            target=self.grogu_plans.TARGET_HARNESS,
        )
        note = self.grogu_plans.harness_friction()[0]["note"]
        self.assertNotIn("ghp_", note)
        self.assertIn("grogu auth failed", note)


if __name__ == "__main__":
    unittest.main()


class WorktreeHookTests(unittest.TestCase):
    """`.git` is a file in a linked worktree, and hooks are shared."""

    def test_the_hook_installs_into_the_shared_hooks_directory(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        repo = base / "main"
        repo.mkdir()

        def git(*arguments, cwd=None):
            return subprocess.run(
                ["git", *arguments], cwd=str(cwd or repo), check=True,
                capture_output=True, text=True,
            )

        git("init", "-q", "-b", "main", ".")
        git("config", "user.email", "t@example.com")
        git("config", "user.name", "T")
        (repo / "a.txt").write_text("x", encoding="utf8")
        git("add", "-A")
        git("commit", "-qm", "init")
        linked = base / "linked"
        git("worktree", "add", "-q", str(linked), "-b", "side")
        self.assertTrue((linked / ".git").is_file(), "expected a linked worktree")

        installed = grogu_privacy.install_hook(
            linked, python=sys.executable, script=str(ROOT / "src" / "grogu_cli.py")
        )
        self.assertTrue(installed.exists())
        self.assertEqual(
            installed.resolve(), (repo / ".git" / "hooks" / "pre-commit").resolve(),
            "the hook must land in the shared hooks directory, not a per-worktree one",
        )


class JsonNoticeTests(unittest.TestCase):
    """An agent that only calls --json commands has no other channel."""

    def test_a_pending_notice_rides_inside_the_json_payload(self):
        sys.path.insert(0, str(ROOT / "src"))
        import io
        import contextlib

        import grogu_cli

        original = grogu_cli._PENDING_NOTICE
        self.addCleanup(setattr, grogu_cli, "_PENDING_NOTICE", original)
        grogu_cli._PENDING_NOTICE = "steering: prefer the existing helper"
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            grogu_cli.print_json({"allowed": True})
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["grogu_notice"], "steering: prefer the existing helper")
        self.assertEqual(
            grogu_cli._PENDING_NOTICE, "", "a delivered notice must not print twice"
        )

    def test_a_list_payload_leaves_the_notice_for_the_trailer(self):
        sys.path.insert(0, str(ROOT / "src"))
        import io
        import contextlib

        import grogu_cli

        original = grogu_cli._PENDING_NOTICE
        self.addCleanup(setattr, grogu_cli, "_PENDING_NOTICE", original)
        grogu_cli._PENDING_NOTICE = "steering: something"
        with contextlib.redirect_stdout(io.StringIO()):
            grogu_cli.print_json([1, 2, 3])
        self.assertEqual(grogu_cli._PENDING_NOTICE, "steering: something")


class ActorAndDestinationTests(unittest.TestCase):
    """The guard let a machine hostname into a public commit. Twice."""

    def test_the_recorded_actor_carries_no_hostname(self):
        environment = dict(os.environ)
        environment.pop("GROGU_ACTOR", None)
        environment["USER"] = "charlie"
        with mock.patch.dict(os.environ, environment, clear=True):
            self.assertEqual(grogu_tasks.actor(), "charlie")

    def test_a_secret_with_one_english_word_in_it_is_still_a_secret(self):
        # A token was waved through because it contained "secret".
        for value in ("xoxb-secret-9f3ad21b7c", "my-real-key-9f3a2b1c8d"):  # grogu-allow-secret
            self.assertTrue(
                grogu_privacy._looks_like_a_real_secret(value), value
            )

    def test_obvious_placeholders_are_still_ignored(self):
        for value in ("your-api-key-here", "CHANGEME_token", "replace-me-please"):
            self.assertFalse(
                grogu_privacy._looks_like_a_real_secret(value), value
            )


class CardFalsePositiveTests(unittest.TestCase):
    """A plan id blocked its own plan from being finalized."""

    def test_a_plan_id_is_not_a_card_number(self):
        findings = grogu_privacy.scan("Plan `p-20260814-619058` finalized.", path="record")
        self.assertEqual(
            [finding for finding in findings if "card" in finding.label], []
        )

    def test_a_real_card_number_is_still_caught(self):
        card = "4111 1111 1111 1111"  # grogu-allow-secret: the published test card
        findings = grogu_privacy.scan(f"charge {card} today", path="notes")
        self.assertTrue(any("card" in finding.label for finding in findings))
