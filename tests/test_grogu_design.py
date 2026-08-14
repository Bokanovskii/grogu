import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import grogu_design  # noqa: E402


class DesignStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = grogu_design.DesignStore(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_remember_and_recall(self) -> None:
        self.store.remember("Use one accent colour", scope="web", rationale="focus")
        recalled = self.store.recall(scope="web")
        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0]["status"], grogu_design.CONFIRMED)

    def test_recall_includes_scope_all(self) -> None:
        self.store.remember("Restraint over decoration", scope="all")
        self.store.remember("Right-align numeric columns", scope="cli")
        self.assertEqual(len(self.store.recall(scope="web")), 1)
        self.assertEqual(len(self.store.recall(scope="cli")), 2)

    def test_suggestion_does_not_apply_until_confirmed(self) -> None:
        candidate = self.store.suggest("Prefers quieter empty states", scope="web")
        self.assertEqual(self.store.recall(scope="web"), [])
        self.store.confirm(candidate["id"])
        self.assertEqual(len(self.store.recall(scope="web")), 1)

    def test_rejected_suggestion_is_gone(self) -> None:
        candidate = self.store.suggest("Likes dense tables")
        self.assertTrue(self.store.reject(candidate["id"]))
        self.assertEqual(self.store.review(), [])
        self.assertEqual(self.store.recall(), [])

    def test_unknown_scope_refused(self) -> None:
        with self.assertRaises(grogu_design.DesignError):
            self.store.remember("Something", scope="hologram")

    def test_empty_statement_refused(self) -> None:
        with self.assertRaises(grogu_design.DesignError):
            self.store.remember("   ")

    def test_seed_is_idempotent(self) -> None:
        first = self.store.seed_apple()
        self.assertTrue(first)
        self.assertEqual(self.store.seed_apple(), [])
        self.assertEqual(len(self.store.recall(limit=100)), len(first))

    def test_forget_removes_principle(self) -> None:
        principle = self.store.remember("Temporary")
        self.assertTrue(self.store.forget(principle["id"]))
        self.assertFalse(self.store.forget(principle["id"]))


class DesignCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ, GROGU_HOME=self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(ROOT / "src" / "grogu_cli.py"), *args],
            capture_output=True,
            text=True,
            env=self.env,
            cwd=str(ROOT),
        )

    def test_seed_then_recall_json(self) -> None:
        self.assertEqual(self.run_cli("design", "seed", "--apple").returncode, 0)
        result = self.run_cli("design", "recall", "--scope", "cli", "--json")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout))

    def test_template_contains_required_sections(self) -> None:
        result = self.run_cli("design", "template", "Thing")
        for section in ("## States", "## Tokens", "## Acceptance criteria"):
            self.assertIn(section, result.stdout)

    def test_bad_scope_exits_two(self) -> None:
        result = self.run_cli("design", "remember", "x", "--scope", "hologram")
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()


class ProvenanceTests(unittest.TestCase):
    """A store of seeds should not read as a store of the user's corrections."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_design.DesignStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)

    def test_adopted_principles_are_reported_as_adopted_not_as_missing(self):
        self.store.seed_apple()
        status = self.store.status()
        self.assertEqual(status["stated"], 0)
        self.assertEqual(status["adopted"], status["principles"])
        self.assertEqual(status["sets"], ["Apple's Human Interface Guidelines"])

    def test_something_the_user_said_counts_as_learned(self):
        self.store.seed_apple()
        self.store.remember("no green on the success path", scope="cli")
        self.assertEqual(self.store.status()["stated"], 1)


class AgentsMayNotAssertTasteTests(unittest.TestCase):
    """A dogfooding agent wrote "the user said this" about a fake project."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = grogu_design.DesignStore(Path(self.temporary.name))
        self.addCleanup(self.temporary.cleanup)
        os.environ.pop("GROGU_ROLE", None)

    def tearDown(self):
        os.environ.pop("GROGU_ROLE", None)

    def test_a_designer_cannot_record_a_principle_as_the_users_own(self):
        os.environ["GROGU_ROLE"] = "designer"
        with self.assertRaises(grogu_design.DesignError):
            self.store.remember("no green on the success path", scope="cli")

    def test_a_designer_may_still_suggest(self):
        os.environ["GROGU_ROLE"] = "designer"
        self.store.suggest("no green on the success path", scope="cli")
        self.assertEqual(len(self.store._pending()["candidates"]), 1)
        self.assertEqual(self.store.status()["principles"], 0)

    def test_the_user_may_record_their_own_taste(self):
        self.store.remember("no green on the success path", scope="cli")
        self.assertEqual(self.store.status()["stated"], 1)
