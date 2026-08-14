"""The skill store: proposing, echoing, refusing and installing lessons.

The behaviour worth pinning here is mostly refusal. A skill that gets written
is easy; what keeps the store from becoming a landfill is that near-duplicates
merge, hollow bodies bounce, an already-answered lesson comes back with the
answer, and an agent cannot install one at all.
"""

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
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

LICENSE_BODY = """Audit every new dependency licence before it reaches main.

1. List dependencies added since the last release.
2. Resolve each licence from the package metadata, not from the README.
3. Reject anything copyleft; record an exception with the reason if kept.
4. Commit the resulting inventory alongside the lockfile change.

An unaudited transitive dependency is how a copyleft licence enters a product.
"""

MASTERING_BODY = """Master every episode to the same loudness before publishing.

1. Normalise the mix to -16 LUFS integrated for stereo.
2. Keep true peak under -1 dBTP so lossy encoders do not clip.
3. Listen to the first and last minute on phone speakers.
4. Export and check the loudness measurement again after encoding.

Episodes mastered by ear drift louder over a season until listeners turn it off.
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

    def test_two_agents_reaching_one_lesson_are_linked_not_merged(self) -> None:
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
        self.assertNotEqual(first["seq"], second["seq"])
        self.assertEqual(second["related_to"], [first["seq"]])
        self.assertEqual(len(grogu_skills.proposals()), 2)

    def test_the_link_is_recorded_on_both_sides(self) -> None:
        """Whoever reads the older one has to be told a second exists."""
        first = self.propose("narrow-first", "Prove a change with the narrowest test first.")
        second = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
        )
        stored = {entry["seq"]: entry for entry in grogu_skills.proposals()}
        self.assertEqual(stored[first["seq"]]["related_to"], [second["seq"]])

    def test_an_unrelated_lesson_under_a_shared_headline_is_not_swallowed(self) -> None:
        """Both agents wrote something true; neither said anything about the other.

        This is the failure a merge could not survive: an adversarial probe
        filed a dependency-license audit and a podcast mastering procedure
        under one honest description and the second lesson vanished.
        """
        shared = "Run narrow validation before reporting success."
        self.propose("dependency-license-audit", shared, body=LICENSE_BODY)
        second = self.propose("podcast-audio-mastering", shared, body=MASTERING_BODY)
        self.assertEqual(len(grogu_skills.proposals()), 2)
        bodies = [entry["body"] for entry in grogu_skills.proposals()]
        self.assertTrue(any("loudness" in body for body in bodies))
        # Still linked, because the headline really is identical -- linking is
        # a note to the reader, not a claim, so a false one costs a glance.
        self.assertTrue(second["related_to"])

    def test_a_different_lesson_stays_a_different_proposal(self) -> None:
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        self.propose(
            "recorded-fixtures",
            "Build fixtures from a recorded upstream response, never by hand.",
            body=OTHER_BODY,
        )
        self.assertEqual(len(grogu_skills.proposals()), 2)

    def test_the_why_does_not_push_one_lesson_apart(self) -> None:
        """`why` is a different incident every time, by construction."""
        self.propose(
            "narrow-first",
            "Prove a change with the narrowest test first.",
            why="ran the full suite six times chasing one assertion",
        )
        second = self.propose(
            "narrow-tests-first",
            "Run the narrowest test that proves the change before the suite.",
            why="the suite hid which of four changes broke the build",
        )
        self.assertEqual(second["related_to"], [1])

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
        self.assertEqual(again["related_to"], [reopened["seq"]])

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
        self.assertEqual(
            sorted(entry["name"] for entry in grogu_skills.ripe()),
            ["narrow-first", "narrow-tests-first"],
        )

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

    def test_a_corrupt_store_refuses_rather_than_reading_as_empty(self) -> None:
        """Reading nothing and reading a damaged file are different facts."""
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        store = Path(self.home.name) / "skills.json"
        store.write_text("{not json", encoding="utf8")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.proposals()
        with self.assertRaises(grogu_skills.SkillError):
            self.propose("something-else", "An unrelated lesson entirely.", body=OTHER_BODY)
        self.assertEqual(store.read_text(encoding="utf8"), "{not json")

    def test_an_empty_store_file_does_not_erase_what_was_there(self) -> None:
        self.propose("narrow-first", "Prove a change with the narrowest test first.")
        store = Path(self.home.name) / "skills.json"
        store.write_text("", encoding="utf8")
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.proposals()

    def test_a_body_too_large_to_be_a_procedure_is_refused(self) -> None:
        """A five megabyte paste used to look like a hang rather than a mistake."""
        with self.assertRaises(grogu_skills.SkillError) as caught:
            self.propose("huge", "A lesson with the whole log pasted in.", body="x " * 60000)
        self.assertIn("procedure, not the material", str(caught.exception))

    def test_a_lesson_sharing_only_a_headline_with_an_installed_skill_still_files(self) -> None:
        """Redirecting to an unrelated skill ends with the lesson never written."""
        shared = "Run narrow validation before reporting success."
        entry = self.propose("dependency-license-audit", shared, body=LICENSE_BODY)
        grogu_skills.accept(entry["seq"], root=Path(self.repo.name))
        filed = self.propose("podcast-audio-mastering", shared, body=MASTERING_BODY)
        self.assertEqual(filed["name"], "podcast-audio-mastering")

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

    def test_an_agent_that_drops_its_role_is_still_refused(self) -> None:
        """The session binding remembers who is working in this directory."""
        self.propose()
        bind = subprocess.run(
            [sys.executable, str(ROOT / "src" / "grogu_cli.py"), "plan", "brief", "--role", "engineer"],
            cwd=self.repo.name,
            env={**os.environ, "GROGU_HOME": self.home.name, "GROGU_ROLE": "engineer"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(bind.returncode, 0, bind.stderr)
        refused = self.run_cli("accept", "1")
        self.assertEqual(refused.returncode, 3)
        self.assertIn("engineer", refused.stderr)
        self.assertFalse((Path(self.repo.name) / ".github/skills").exists())

    def test_the_supervisor_may_decide_in_a_directory_an_engineer_works_in(self) -> None:
        self.propose()
        subprocess.run(
            [sys.executable, str(ROOT / "src" / "grogu_cli.py"), "plan", "brief", "--role", "engineer"],
            cwd=self.repo.name,
            env={**os.environ, "GROGU_HOME": self.home.name, "GROGU_ROLE": "engineer"},
            capture_output=True,
            text=True,
        )
        accepted = self.run_cli("accept", "1", role="supervisor")
        self.assertEqual(accepted.returncode, 0, accepted.stderr)

    def test_skills_resolve_to_this_working_tree(self) -> None:
        """Not the primary worktree: a skill is a file on a branch."""
        self.propose()
        self.run_cli("accept", "1", role="supervisor")
        listed = self.run_cli("list")
        self.assertIn("narrow-first", listed.stdout)


if __name__ == "__main__":
    unittest.main()


class OverrideTests(unittest.TestCase):
    """The escape hatches from a refusal that is wrong.

    Both refusals are word counts, so both are sometimes wrong, and a wrong one
    is worse than a wrong acceptance: the lesson is never written at all. Each
    one names a flag that gets past it, and these pin that the flag works and
    that the override is recorded rather than swallowed.
    """

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        os.environ["GROGU_HOME"] = self.home.name

    def tearDown(self) -> None:
        self.home.cleanup()

    def test_an_agent_that_has_read_the_installed_skill_may_still_propose(self) -> None:
        installed = [
            {
                "name": "narrow-first",
                "description": "Prove a change with the narrowest test first.",
                "body": BODY,
                "path": ".github/skills/narrow-first/SKILL.md",
            }
        ]
        with self.assertRaises(grogu_skills.AlreadyKnown) as refused:
            grogu_skills.propose(
                "narrowest-test-first",
                description="Prove a change with the narrowest test first.",
                body=BODY,
                installed=installed,
            )
        self.assertIn("--not-the-same narrow-first", str(refused.exception))
        entry = grogu_skills.propose(
            "narrowest-test-first",
            description="Prove a change with the narrowest test first.",
            body=BODY,
            installed=installed,
            overrode=["narrow-first"],
        )
        self.assertEqual(entry["overrode"], ["narrow-first"])

    def test_an_agent_may_override_a_replayed_decline(self) -> None:
        first = grogu_skills.propose(
            "narrow-first", description="Narrowest test first.", body=BODY
        )
        grogu_skills.decline(first["seq"], note="we already do this")
        with self.assertRaises(grogu_skills.AlreadyDeclined) as refused:
            grogu_skills.propose(
                "narrow-first", description="Narrowest test first.", body=BODY
            )
        self.assertIn(f"--not-the-same {first['seq']}", str(refused.exception))
        entry = grogu_skills.propose(
            "narrow-first",
            description="Narrowest test first.",
            body=BODY,
            overrode=[str(first["seq"])],
        )
        self.assertEqual(entry["status"], grogu_skills.PENDING)

    def test_an_oversized_body_is_refused_before_it_is_scanned(self) -> None:
        # The cap exists because redaction reads every byte. Applying it after
        # the scan meant the caller waited for the cost the cap was there to
        # avoid, then got told the input was too big.
        huge = "x" * (grogu_skills.MAX_SKILL_CHARS + 1)
        started = time.time()
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.propose("big-one", description="A very large lesson.", body=huge)
        self.assertLess(time.time() - started, 1.0)

    def test_the_closest_proposals_are_named_even_when_nothing_links(self) -> None:
        # Word counting cannot see a paraphrase; the agent that wrote the
        # lesson can. It gets told what is nearest rather than nothing.
        grogu_skills.propose(
            "narrow-first", description="Narrowest test first.", body=BODY
        )
        entry = grogu_skills.propose(
            "audit-licences",
            description="Audit every new dependency licence.",
            body=LICENSE_BODY,
        )
        self.assertEqual(entry["related_to"], [])
        self.assertEqual(entry["nearby"], [1])

    def test_an_agent_may_link_a_paraphrase_the_harness_cannot_see(self) -> None:
        grogu_skills.propose(
            "narrow-first", description="Narrowest test first.", body=BODY
        )
        entry = grogu_skills.propose(
            "audit-licences",
            description="Audit every new dependency licence.",
            body=LICENSE_BODY,
            liked=[1],
        )
        self.assertEqual(entry["related_to"], [1])


class DecideGateTests(unittest.TestCase):
    """Who the harness believes is typing, when nobody says.

    Role is trust-on-assert, so the only case worth enforcing is the one that
    actually happens: an agent that already declared itself and then ran a
    command without the variable. That has to survive `cd` into a subdirectory
    -- and it has to stop mattering once the agent is long gone, or the user is
    locked out of his own checkout by a shell that no longer exists.
    """

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        self.repo = tempfile.TemporaryDirectory()
        for command in (["git", "init", "-q", "."],
                        ["git", "commit", "-q", "--allow-empty", "-m", "init"]):
            subprocess.run(
                command,
                cwd=self.repo.name,
                check=True,
                env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                     "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"},
            )
        self.nested = Path(self.repo.name) / "src" / "deep"
        self.nested.mkdir(parents=True)

    def tearDown(self) -> None:
        self.home.cleanup()
        self.repo.cleanup()

    def bind(self, role: str, when: str, where: Path) -> None:
        state = Path(self.repo.name) / ".grogu" / "state"
        state.mkdir(parents=True, exist_ok=True)
        (state / "session-roles.json").write_text(
            json.dumps({str(where.resolve()): {"role": role, "at": when}}),
            encoding="utf8",
        )

    def run_cli(self, *arguments, cwd=None, role=""):
        environment = {**os.environ, "GROGU_HOME": self.home.name}
        environment.pop("GROGU_ROLE", None)
        if role:
            environment["GROGU_ROLE"] = role
        return subprocess.run(
            [sys.executable, str(ROOT / "src" / "grogu_cli.py"), "skill", *arguments],
            cwd=str(cwd or self.repo.name),
            env=environment,
            capture_output=True,
            text=True,
        )

    def propose(self) -> None:
        result = self.run_cli(
            "propose",
            "narrow-first",
            "--description",
            "Prove a change with the narrowest test before running the suite.",
            "--body",
            BODY,
            role="engineer",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def now(self, hours_ago: float) -> str:
        moment = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours_ago)
        return moment.isoformat()

    def test_an_engineer_cannot_escape_the_gate_by_changing_directory(self) -> None:
        self.propose()
        self.bind("engineer", self.now(0.1), Path(self.repo.name))
        result = self.run_cli("accept", "1", cwd=self.nested)
        self.assertEqual(result.returncode, 3)
        self.assertIn("an engineer declared itself", result.stderr)

    def test_a_binding_from_last_week_does_not_lock_the_user_out(self) -> None:
        self.propose()
        self.bind("engineer", self.now(72), Path(self.repo.name))
        result = self.run_cli("accept", "1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_supervisor_may_say_so_with_the_flag_the_refusal_names(self) -> None:
        self.propose()
        self.bind("engineer", self.now(0.1), Path(self.repo.name))
        refused = self.run_cli("accept", "1")
        self.assertIn("--role supervisor", refused.stderr)
        result = self.run_cli("accept", "1", "--role", "supervisor")
        self.assertEqual(result.returncode, 0, result.stderr)


class HandEditedStoreTests(unittest.TestCase):
    """What happens when the store is not what `propose` would have written.

    It is a plain JSON file in the user's home. It gets hand-edited, restored
    from a backup, merged badly, or half-written. Every one of these was found
    by a probe doing exactly that, and each ended either in a traceback in
    front of the person reading the listing or in the harness corrupting a file
    that was fine when it arrived.
    """

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        os.environ["GROGU_HOME"] = self.home.name
        self.path = Path(self.home.name) / "skills.json"

    def tearDown(self) -> None:
        self.home.cleanup()

    def store(self, *entries) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"entries": list(entries)}), encoding="utf8")

    def entry(self, **overrides) -> dict:
        base = {
            "seq": 1,
            "name": "narrow-first",
            "description": "Narrowest test first.",
            "body": BODY,
            "status": grogu_skills.PENDING,
        }
        base.update(overrides)
        return base

    def test_a_gap_in_the_numbering_does_not_make_the_harness_reuse_one(self) -> None:
        # This is the one that did real damage: a store whose only entry was #2
        # got a second #2 written over the top of it, and the next read refused
        # the file the harness had just corrupted.
        self.store(self.entry(seq=2, name="existing-two"))
        created = grogu_skills.propose(
            "audit-licences",
            description="Audit every new dependency licence.",
            body=LICENSE_BODY,
        )
        self.assertEqual(created["seq"], 3)
        self.assertEqual(len(grogu_skills.proposals()), 2)

    def test_a_status_nobody_recognises_is_refused_not_hidden(self) -> None:
        # It vanished from the queue instead: not listed, so never decided, and
        # not an error either, so nobody knew to look.
        self.store(self.entry(status="teleported"))
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.proposals()

    def test_a_link_holding_something_that_is_not_a_number_is_refused(self) -> None:
        self.store(self.entry(related_to=[{}]))
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.proposals()

    def test_echoes_that_are_not_records_are_refused(self) -> None:
        self.store(self.entry(echoes="oops"))
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.proposals()

    def test_a_seq_that_is_not_a_counting_number_is_refused(self) -> None:
        for bad in (True, 0, -5):
            with self.subTest(seq=bad):
                self.store(self.entry(seq=bad))
                with self.assertRaises(grogu_skills.SkillError):
                    grogu_skills.proposals()

    def test_an_empty_body_cannot_be_installed_however_it_got_there(self) -> None:
        # It installed as a skill file with nothing under the front matter: a
        # standing instruction saying nothing, read by every agent after it.
        self.store(self.entry(body="", description=""))
        repo = tempfile.TemporaryDirectory()
        self.addCleanup(repo.cleanup)
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.accept(1, root=Path(repo.name))


class LinkAndOverrideTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        os.environ["GROGU_HOME"] = self.home.name

    def tearDown(self) -> None:
        self.home.cleanup()

    def two(self) -> None:
        grogu_skills.propose(
            "narrow-first", description="Narrowest test first.", body=BODY
        )
        grogu_skills.propose(
            "audit-licences",
            description="Audit every new dependency licence.",
            body=LICENSE_BODY,
        )

    def test_linking_amends_both_rather_than_filing_a_third(self) -> None:
        self.two()
        grogu_skills.link(2, 1)
        entries = grogu_skills.proposals()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["related_to"], [2])
        self.assertEqual(entries[1]["related_to"], [1])

    def test_linking_twice_does_not_say_it_twice(self) -> None:
        self.two()
        grogu_skills.link(2, 1)
        grogu_skills.link(2, 1)
        self.assertEqual(grogu_skills.proposals()[1]["related_to"], [1])

    def test_a_proposal_cannot_be_the_same_lesson_as_itself(self) -> None:
        self.two()
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.link(1, 1)

    def test_linking_to_a_proposal_that_is_not_there_is_refused(self) -> None:
        self.two()
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.link(1, 999)

    def test_an_override_has_to_name_something_the_agent_was_shown(self) -> None:
        # Unvalidated this was free text on an audit record, and a probe put a
        # credential in it, in a store that is pooled and reviewed in public.
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.propose(
                "narrow-first",
                description="Narrowest test first.",
                body=BODY,
                overrode=["ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890"],
            )
        self.assertEqual(grogu_skills.proposals(), [])

    def test_a_skill_name_can_never_be_read_as_a_proposal_number(self) -> None:
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.propose("1", description="A number.", body=BODY)


class RoundFourTests(unittest.TestCase):
    """Findings from the probe round that said no-go to the merge."""

    def setUp(self) -> None:
        self.home = tempfile.TemporaryDirectory()
        os.environ["GROGU_HOME"] = self.home.name
        self.repo = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.home.cleanup()
        self.repo.cleanup()

    def install(self, name: str) -> None:
        directory = Path(self.repo.name) / ".github" / "skills" / name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: A thing.\n---\n{BODY}", encoding="utf8"
        )

    def test_a_directory_that_is_not_a_skill_name_is_not_a_skill(self) -> None:
        # A probe made `.github/skills/ghp_<token>/` and cited it, which put
        # the token-shaped string intact into a store that is pooled across
        # repositories and reviewed in public.
        self.install("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890")
        self.install("narrow-first")
        names = [
            skill["name"]
            for skill in grogu_skills.installed_skills(Path(self.repo.name))
        ]
        self.assertEqual(names, ["narrow-first"])

    def test_an_accepted_proposal_cannot_start_a_link_either(self) -> None:
        for name in ("alpha-lesson", "beta-lesson"):
            grogu_skills.propose(name, description=f"The {name}.", body=BODY)
        grogu_skills.accept(1, root=Path(self.repo.name))
        with self.assertRaises(grogu_skills.SkillError):
            grogu_skills.link(1, 2)

    def test_junk_in_contested_or_overrode_is_refused_not_raised(self) -> None:
        grogu_skills.propose("alpha-lesson", description="The alpha.", body=BODY)
        path = Path(self.home.name) / "skills.json"
        for field, value in (("contested", "oops"), ("overrode", [{}])):
            with self.subTest(field=field):
                payload = json.loads(path.read_text(encoding="utf8"))
                payload["entries"][0][field] = value
                path.write_text(json.dumps(payload), encoding="utf8")
                with self.assertRaises(grogu_skills.SkillError):
                    grogu_skills.proposals()


class BindingTimestampTests(unittest.TestCase):
    def test_an_unreadable_timestamp_cannot_claim_to_be_the_newest(self) -> None:
        # It sorted above every real one as a string, so any junk in that field
        # masked the live agent and reopened the bypass from the other side.
        import grogu_plans

        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
            root = Path(repo).resolve()
            state = root / ".grogu" / "state"
            state.mkdir(parents=True)
            now = dt.datetime.now(dt.timezone.utc)
            (state / "session-roles.json").write_text(
                json.dumps({
                    str(root): {
                        "role": "engineer",
                        "at": (now - dt.timedelta(minutes=5)).isoformat(),
                    },
                    str(root / "sub"): {"role": "tester", "at": "zzzz"},
                }),
                encoding="utf8",
            )
            store = grogu_plans.PlanStore(root)
            self.assertEqual(store.binding_covering(root).get("role"), "engineer")
