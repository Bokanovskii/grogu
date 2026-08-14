"""The skill store: proposing, echoing, refusing and installing lessons.

The behaviour worth pinning here is mostly refusal. A skill that gets written
is easy; what keeps the store from becoming a landfill is that near-duplicates
merge, hollow bodies bounce, an already-answered lesson comes back with the
answer, and an agent cannot install one at all.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_skills  # noqa: E402

BODY = """Run the narrowest command that proves the behaviour, then the suite.

1. Run the one module you touched.
2. Only if that passes, run everything.
3. Paste the failing assertion, not a summary of it.

The full suite hides which change broke what.
"""

OTHER_BODY = """Record one real upstream response and commit it as the fixture.

1. Fetch the feed once, by hand, and save the body verbatim.
2. Trim nothing: real responses carry nulls and stations reporting no value.
3. Assert against the shape only in the unit that owns it.

A hand-written fixture encodes what you expected, which is the assumption the
test exists to check.
"""


class SkillStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.repo = tempfile.TemporaryDirectory()
        self.previous = os.environ.get("GROGU_HOME")
        os.environ["GROGU_HOME"] = self.home.name

    def tearDown(self) -> None:
        if self.previous is None:
            os.environ.pop("GROGU_HOME", None)
        else:
            os.environ["GROGU_HOME"] = self.previous
        self.home.cleanup()
        self.repo.cleanup()

    def propose(self, name, description, body=BODY, **kwargs):
        return grogu_skills.propose(
            name,
            description=description,
            body=body,
            installed=grogu_skills.installed_skills(Path(self.repo.name)),
            **kwargs,
        )

    def test_a_hollow_body_is_refused(self) -> None:
        with self.assertRaises(grogu_skills.SkillError) as caught:
            self.propose("thin", "Do the thing properly.", body="do the thing")
        self.assertIn("procedure", str(caught.exception))

    def test_a_name_that_is_not_a_directory_is_refused(self) -> None:
        with self.assertRaises(grogu_skills.SkillError):
            self.propose("Narrow Tests!", "Prove it narrowly.")

    def test_two_agents_reaching_one_lesson_make_one_proposal(self) -> None:
        first = self.propose(
            "narrow-first",
            "Prove a change with the narrowest test before running the suite.",
            role="engineer",
        )
        second = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the whole suite.",
            role="tester",
        )
        self.assertEqual(first["seq"], second["seq"])
        self.assertEqual(len(grogu_skills.proposals()), 1)
        self.assertEqual(len(second["echoes"]), 1)

    def test_the_echo_keeps_its_own_wording(self) -> None:
        """A wrong merge must not silently destroy the second agent's lesson."""
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        merged = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
            role="tester",
        )
        echo = merged["echoes"][0]
        self.assertEqual(echo["name"], "narrow-tests-first")
        self.assertIn("narrowest", echo["description"])

    def test_a_different_lesson_stays_a_different_proposal(self) -> None:
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        self.propose(
            "recorded-fixtures",
            "Build fixtures from a recorded upstream response, never by hand.",
            body=OTHER_BODY,
        )
        self.assertEqual(len(grogu_skills.proposals()), 2)

    def test_the_why_does_not_pull_one_lesson_into_two(self) -> None:
        """`why` is a different incident every time, by construction."""
        self.propose(
            "narrow-first",
            "Prove a change with the narrowest test first.",
            why="ran the full suite six times chasing one assertion",
        )
        merged = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
            why="the suite hid which of four changes broke the build",
        )
        self.assertEqual(merged["seq"], 1)

    def test_a_declined_lesson_comes_back_with_the_reason(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(entry["seq"], note="the verify skill already covers it")
        with self.assertRaises(grogu_skills.AlreadyDeclined) as caught:
            self.propose(
                "narrow-tests-first",
                "Run the narrowest test that proves the change before the suite.",
            )
        self.assertIn("verify skill already covers it", str(caught.exception))

    def test_a_declined_lesson_still_counts_the_agents_that_re_derive_it(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(entry["seq"], note="not general enough")
        for _ in range(2):
            with self.assertRaises(grogu_skills.AlreadyDeclined):
                self.propose(
                    "narrow-tests-first",
                    "Run the narrowest test that proves the change before the suite.",
                )
        declined = [
            item
            for item in grogu_skills.proposals(include_decided=True)
            if item["status"] == grogu_skills.DECLINED
        ]
        self.assertEqual(len(declined[0]["echoes"]), 2)

    def test_a_live_proposal_beats_an_older_decline(self) -> None:
        first = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(first["seq"], note="too vague")
        reopened = grogu_skills.contest(
            first["seq"], note="three agents hit it since; here is the exact command", role="tester"
        )
        self.assertEqual(reopened["status"], grogu_skills.PENDING)
        again = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
        )
        self.assertEqual(again["seq"], reopened["seq"])

    def test_contesting_needs_an_argument(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(entry["seq"], note="too vague")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.contest(entry["seq"], note="  ")

    def test_only_a_declined_proposal_can_be_contested(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.contest(entry["seq"], note="I disagree")

    def test_contesting_keeps_the_reason_it_was_declined_for(self) -> None:
        """Whoever declined it has to see their own reason next to the rebuttal."""
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(entry["seq"], note="the verify skill already covers it")
        contested = grogu_skills.contest(
            entry["seq"], note="verify says nothing about which test is narrowest", role="engineer"
        )
        self.assertEqual(
            contested["contested"]["declined_for"], "the verify skill already covers it"
        )
        self.assertEqual(contested["contested"]["role"], "engineer")

    def test_contesting_does_not_install_anything(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.decline(entry["seq"], note="too vague")
        grogu_skills.contest(entry["seq"], note="it is not vague, here is the command")
        self.assertFalse((Path(self.repo.name) / ".github/skills").exists())

    def test_declining_without_a_reason_is_refused(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.decline(entry["seq"], note="   ")

    def test_accept_writes_a_skill_the_repository_can_review(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        manifest = Path(self.repo.name) / ".github/skills/narrow-first/SKILL.md"
        self.assertTrue(manifest.is_file())
        text = manifest.read_text(encoding="utf8")
        self.assertTrue(text.startswith("---\nname: narrow-first\n"))
        self.assertIn("narrowest command", text)
        self.assertEqual(
            [skill["name"] for skill in grogu_skills.installed_skills(Path(self.repo.name))],
            ["narrow-first"],
        )

    def test_a_tampered_name_cannot_write_outside_the_repository(self) -> None:
        """The name is validated at propose time and then sits in a JSON file."""
        import json

        entry = self.propose("legit", "A real lesson about proving changes narrowly.")
        store = Path(self.home.name) / "skills.json"
        payload = json.loads(store.read_text(encoding="utf8"))
        payload["entries"][0]["name"] = "../../../../escaped"
        store.write_text(json.dumps(payload), encoding="utf8")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        self.assertFalse((Path(self.repo.name).parent / "escaped").exists())

    def test_accepting_twice_is_refused(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.accept(entry["seq"], root=Path(self.repo.name))

    def test_a_lesson_the_repository_already_has_sends_you_to_read_it(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        with self.assertRaises(grogu_skills.AlreadyKnown) as caught:
            self.propose(
                "narrow-tests-first",
                "Run the narrowest test that proves the change before the suite.",
            )
        self.assertIn("narrow-first", str(caught.exception))

    def test_amending_an_installed_skill_is_allowed_and_marked(self) -> None:
        entry = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        amendment = self.propose(
            "narrow-first",
            "Prove a change with the narrowest test, and name the module you ran.",
        )
        self.assertTrue(amendment["amends"])
        grogu_skills.accept(amendment["seq"], root=Path(self.repo.name))
        manifest = Path(self.repo.name) / ".github/skills/narrow-first/SKILL.md"
        self.assertIn("name the module", manifest.read_text(encoding="utf8"))

    def test_a_body_is_redacted_on_the_way_into_a_pooled_store(self) -> None:
        entry = self.propose(
            "token-safety",
            "Fetch the feed with the key from the environment.",
            body=BODY + "\nRun: curl -H 'Authorization: Bearer ghp_" + "a" * 36 + "' https://x",
        )
        self.assertNotIn("ghp_" + "a" * 36, entry["body"])

    def test_ripeness_is_repetition_not_volume(self) -> None:
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        self.propose(
            "recorded-fixtures",
            "Build fixtures from a recorded upstream response, never by hand.",
            body=OTHER_BODY,
        )
        self.assertEqual(grogu_skills.ripe(), [])
        self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
        )
        self.assertEqual([entry["name"] for entry in grogu_skills.ripe()], ["narrow-first"])

    def test_unwritten_lessons_skip_what_somebody_already_proposed(self) -> None:
        clusters = [
            {
                "id": "f1",
                "title": "I keep having to work out which narrowest test proves a change",
                "count": 3,
                "repositories": ["a", "b"],
            },
            {
                "id": "f2",
                "title": "The worktree helper leaves stale branches behind every run",
                "count": 2,
                "repositories": ["a"],
            },
        ]
        self.assertEqual(len(grogu_skills.unwritten_lessons(clusters)), 2)
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        remaining = grogu_skills.unwritten_lessons(clusters)
        self.assertEqual([lesson["id"] for lesson in remaining], ["f2"])

    def test_a_single_complaint_is_not_an_unwritten_lesson(self) -> None:
        clusters = [{"id": "f1", "title": "this one command confused me once", "count": 1}]
        self.assertEqual(grogu_skills.unwritten_lessons(clusters), [])


class SkillCommandTests(unittest.TestCase):
    """The half of the contract that lives in exit codes."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.repo = tempfile.TemporaryDirectory()
        subprocess.run(["git", "init", "-q", "."], cwd=self.repo.name, check=True)
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"],
            cwd=self.repo.name,
            check=True,
            env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    def tearDown(self) -> None:
        self.home.cleanup()
        self.repo.cleanup()

    def run_cli(self, *arguments, role=""):
        environment = {**os.environ, "GROGU_HOME": self.home.name}
        environment.pop("GROGU_ROLE", None)
        if role:
            environment["GROGU_ROLE"] = role
        return subprocess.run(
            [sys.executable, str(ROOT / "src" / "grogu_cli.py"), "skill", *arguments],
            cwd=self.repo.name,
            env=environment,
            capture_output=True,
            text=True,
        )

    def propose(self, role="engineer"):
        return self.run_cli(
            "propose",
            "narrow-first",
            "--description",
            "Prove a change with the narrowest test before running the suite.",
            "--body",
            BODY,
            role=role,
        )

    def test_a_pipeline_role_may_propose_but_not_install(self) -> None:
        self.assertEqual(self.propose().returncode, 0)
        refused = self.run_cli("accept", "1", role="engineer")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("standing instruction", refused.stderr)
        self.assertFalse((Path(self.repo.name) / ".github/skills").exists())

    def test_the_supervisor_may_install(self) -> None:
        self.propose()
        accepted = self.run_cli("accept", "1", role="supervisor")
        self.assertEqual(accepted.returncode, 0)
        self.assertTrue(
            (Path(self.repo.name) / ".github/skills/narrow-first/SKILL.md").is_file()
        )

    def test_the_user_may_install(self) -> None:
        self.propose()
        self.assertEqual(self.run_cli("accept", "1").returncode, 0)

    def test_skills_resolve_to_this_working_tree(self) -> None:
        """Not the primary worktree: a skill is a file on a branch."""
        self.propose()
        self.run_cli("accept", "1", role="supervisor")
        listed = self.run_cli("list")
        self.assertIn("narrow-first", listed.stdout)


if __name__ == "__main__":
    unittest.main()
