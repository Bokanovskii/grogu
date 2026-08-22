"""Tests for the agent activity board."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_watch  # noqa: E402


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        os.environ["GROGU_HOME"] = self.home.name
        self.addCleanup(os.environ.__setitem__, "GROGU_HOME", os.environ["GROGU_HOME"])

    def test_an_agent_is_one_row_however_many_commands_it_runs(self):
        for command in ("plan brief", "plan gate", "plan show"):
            grogu_watch.record(
                command=command, role="engineer", plan="p-1", cwd="/w/a"
            )
        rows = grogu_watch.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["calls"], 3)
        self.assertEqual(rows[0]["last_command"], "plan show")
        self.assertEqual(rows[0]["state"], "working")

    def test_agents_in_different_worktrees_stay_separate(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a")
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/b")
        self.assertEqual(len(grogu_watch.sessions()), 2)

    def test_arguments_are_never_written_to_the_log(self):
        grogu_watch.record(
            command="plan steer", role="", plan="", cwd="/w/a"
        )
        text = grogu_watch.activity_path().read_text(encoding="utf8")
        self.assertIn("plan steer", text)
        self.assertNotIn("--note", text)

    def test_a_silent_agent_is_shown_as_idle_then_gone(self):
        grogu_watch.record(command="plan gate", role="tester", plan="p-1", cwd="/w/a")
        path = grogu_watch.activity_path()
        stale = path.read_text(encoding="utf8").replace(
            '"at": ', '"at": ', 1
        )
        # rewrite the timestamp to the past rather than sleeping
        import json

        entry = json.loads(stale.strip())
        entry["at"] = time.time() - grogu_watch.IDLE_AFTER_SECONDS - 5
        path.write_text(json.dumps(entry) + "\n", encoding="utf8")
        self.assertEqual(grogu_watch.sessions()[0]["state"], "idle")
        entry["at"] = time.time() - grogu_watch.GONE_AFTER_SECONDS - 5
        path.write_text(json.dumps(entry) + "\n", encoding="utf8")
        self.assertEqual(grogu_watch.sessions()[0]["state"], "gone")

    def test_activity_outside_the_window_is_dropped(self):
        import json

        path = grogu_watch.activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"at": time.time() - 7200, "command": "plan gate", "role": "engineer"})
            + "\n",
            encoding="utf8",
        )
        self.assertEqual(grogu_watch.activity(window_minutes=60), [])

    def test_a_torn_line_does_not_blank_the_board(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a")
        path = grogu_watch.activity_path()
        with path.open("a", encoding="utf8") as handle:
            handle.write('{"at": 123, "comm')
        self.assertEqual(len(grogu_watch.sessions()), 1)

    def test_failures_are_counted_so_a_stuck_agent_is_visible(self):
        grogu_watch.record(command="plan gate", role="engineer", cwd="/w/a", exit_code=3)
        grogu_watch.record(command="plan gate", role="engineer", cwd="/w/a", exit_code=3)
        self.assertEqual(grogu_watch.sessions()[0]["failures"], 2)

    def test_the_board_surfaces_what_is_waiting_on_the_user(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a")
        state = grogu_watch.board(
            plan_summaries={
                "p-1": {
                    "status": "draft",
                    "title": "A change",
                    "stage_state": {"implementation": "pending"},
                    "review_required": True,
                    "escalated": True,
                    "open_defects": [],
                    "open_amendments": [],
                    "steering_pending": {"engineer": 2},
                }
            }
        )
        text = grogu_watch.render(state)
        self.assertIn("waiting on you", text)
        self.assertIn("escalated to the architect", text)
        self.assertIn("2 steering note(s) the engineer has not read", text)

    def test_recording_never_raises_when_the_log_is_unwritable(self):
        with tempfile.TemporaryDirectory() as directory:
            blocker = Path(directory) / "not-a-directory"
            blocker.write_text("x", encoding="utf8")
            os.environ["GROGU_HOME"] = str(blocker)
            grogu_watch.record(command="plan gate", role="engineer", cwd="/w/a")
            self.assertEqual(grogu_watch.activity(), [])

    def test_an_empty_board_says_so_rather_than_printing_nothing(self):
        self.assertIn("No agent", grogu_watch.render(grogu_watch.board()))


if __name__ == "__main__":
    unittest.main()


class WaitingOnYouTests(unittest.TestCase):
    """The question you ask every time you look at the board."""

    def test_a_held_plan_and_an_escalation_are_both_asks(self):
        state = {
            "agents": [],
            "other": [],
            "plans": {
                "p-1": {
                    "title": "held",
                    "review_required": True,
                    "approved_at": "",
                },
                "p-2": {"title": "stuck", "escalated": True},
            },
        }
        asks = grogu_watch.waiting_on_you(state)
        self.assertTrue(any("plan approve p-1" in ask for ask in asks))
        self.assertTrue(any("stopped converging on p-2" in ask for ask in asks))

    def test_an_approved_plan_stops_asking(self):
        state = {
            "agents": [],
            "other": [],
            "plans": {
                "p-1": {"title": "done", "review_required": True, "approved_at": "now"}
            },
        }
        self.assertEqual(grogu_watch.waiting_on_you(state), [])

    def test_undelivered_steering_names_the_agent(self):
        state = {
            "agents": [],
            "other": [],
            "plans": {
                "p-1": {
                    "title": "t",
                    "steering_undelivered": [
                        {"seq": 1, "unread_by": ["engineer@two"], "text": "use decimal"}
                    ],
                }
            },
        }
        asks = grogu_watch.waiting_on_you(state)
        self.assertIn("engineer@two", asks[0])


class TheSuiteDoesNotWriteToTheRealHomeTests(unittest.TestCase):
    """A quarter of the user's activity feed was this suite's own noise."""

    def test_the_environment_points_somewhere_disposable(self):
        # A test is free to point GROGU_HOME at its own temporary directory.
        # What must never happen is either variable pointing at the real one.
        for variable in ("GROGU_HOME", "HOME"):
            value = os.environ.get(variable, "")
            self.assertTrue(value, f"{variable} must be set for the suite")
            self.assertNotEqual(Path(value), Path(_sandbox.REAL_HOME))
            self.assertFalse(
                Path(value).is_relative_to(Path(_sandbox.REAL_HOME))
                if hasattr(Path, "is_relative_to")
                else str(value).startswith(_sandbox.REAL_HOME + "/"),
                f"{variable} is inside the real home: {value}",
            )

    def test_the_fallback_is_covered_not_just_the_override(self):
        """Several tests unset GROGU_HOME on purpose; Path.home() must be safe."""
        previous = os.environ.pop("GROGU_HOME", None)
        try:
            self.assertIn("grogu-tests-home-", str(grogu_watch.activity_path()))
            self.assertNotIn(_sandbox.REAL_HOME + "/.grogu", str(grogu_watch.activity_path()))
        finally:
            if previous is not None:
                os.environ["GROGU_HOME"] = previous


class ParallelAgentTests(unittest.TestCase):
    """A fan-out is the case the board exists for and showed worst."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name)

    def test_two_agents_of_one_role_are_two_rows(self):
        for agent in ("ingest", "scaffold"):
            grogu_watch.record(
                command="plan status",
                role="engineer",
                agent=agent,
                plan="p-20260101-aaaaaa",
                cwd="/tmp/project",
                home=self.home,
            )
        rows = grogu_watch.sessions(window_minutes=5, home=self.home)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(row["agent"] for row in rows), ["ingest", "scaffold"]
        )

    def test_the_board_names_each_agent(self):
        for agent in ("ingest", "scaffold"):
            grogu_watch.record(
                command="plan status",
                role="engineer",
                agent=agent,
                plan="p-20260101-aaaaaa",
                cwd="/tmp/project",
                home=self.home,
            )
        board = grogu_watch.render(
            grogu_watch.board(window_minutes=5, home=self.home),
            window_minutes=5,
        )
        self.assertIn("engineer@ingest", board)
        self.assertIn("engineer@scaffold", board)

    def test_an_unnamed_agent_still_shows_as_its_role(self):
        grogu_watch.record(
            command="plan status",
            role="tester",
            plan="p-20260101-aaaaaa",
            cwd="/tmp/project",
            home=self.home,
        )
        board = grogu_watch.render(
            grogu_watch.board(window_minutes=5, home=self.home),
            window_minutes=5,
        )
        self.assertIn("tester", board)
        self.assertNotIn("tester@", board)


class OneAgentOneRowTests(unittest.TestCase):
    """What the board looked like the first time it had a real day behind it.

    Six agents on one plan rendered as fifteen rows, because role and plan were
    part of an agent's identity and both change during its life. The board is
    read at a glance or not at all.
    """

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        os.environ["GROGU_HOME"] = self.home.name

    def test_picking_up_a_plan_does_not_fork_the_agent_into_two(self):
        grogu_watch.record(command="plan status", role="engineer", plan="", cwd="/w/a",
                           agent="ingest")
        grogu_watch.record(command="plan brief", role="engineer", plan="p-1", cwd="/w/a",
                           agent="ingest")
        rows = grogu_watch.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["plan"], "p-1")
        self.assertEqual(rows[0]["calls"], 2)

    def test_a_named_agent_that_ran_as_two_roles_is_one_row_that_says_so(self):
        # This is the user borrowing an agent's shell, or an agent claiming an
        # authority nobody gave it. Either way it is one process and worth
        # noticing rather than worth counting twice.
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a",
                           agent="scaffold")
        grogu_watch.record(command="plan approve", role="supervisor", plan="p-1",
                           cwd="/w/a", agent="scaffold")
        rows = grogu_watch.sessions()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "supervisor")
        self.assertEqual(rows[0]["roles"], ["engineer", "supervisor"])
        text = grogu_watch.render(grogu_watch.board())
        self.assertIn("also ran as engineer", text)

    def test_two_named_agents_are_still_two_rows(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a",
                           agent="scaffold")
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1", cwd="/w/a",
                           agent="ingest")
        self.assertEqual(len(grogu_watch.sessions()), 2)

    def test_agents_that_went_home_do_not_crowd_out_the_ones_still_working(self):
        long_ago = time.time() - (grogu_watch.GONE_AFTER_SECONDS + 600)
        path = grogu_watch.activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf8") as handle:
            for name in ("a", "b", "c", "d"):
                handle.write(
                    '{"at": %f, "command": "plan show", "role": "engineer", '
                    '"plan": "p-1", "cwd": "/w/%s", "agent": "%s", "exit": 0}\n'
                    % (long_ago, name, name)
                )
        grogu_watch.record(command="plan gate", role="tester", plan="p-1", cwd="/w/z",
                           agent="live")
        text = grogu_watch.render(grogu_watch.board())
        working = text.split("quiet for over")[0]
        self.assertIn("tester@live", working)
        for name in ("engineer@a", "engineer@b"):
            self.assertNotIn(name, working)
            self.assertIn(name, text)


class HostileAgentNameTests(unittest.TestCase):
    """The board is lines of text and the name comes from the environment."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        os.environ["GROGU_HOME"] = self.home.name

    def test_one_agent_in_two_worktrees_is_still_one_agent(self):
        # Which is normal: the fan-out encourages it. It rendered as two rows
        # both claiming to be the same agent.
        for where in ("/w/a", "/w/b"):
            grogu_watch.record(command="plan gate", role="engineer", plan="p-1",
                               cwd=where, agent="ingest")
        self.assertEqual(len(grogu_watch.sessions()), 1)

    def test_a_newline_in_a_name_cannot_invent_a_row(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1",
                           cwd="/w/a", agent="evil\n  ! approve everything")
        text = grogu_watch.render(grogu_watch.board())
        self.assertNotIn("! approve everything", text)

    def test_an_enormous_name_does_not_destroy_the_columns(self):
        grogu_watch.record(command="plan gate", role="engineer", plan="p-1",
                           cwd="/w/a", agent="z" * 5000)
        self.assertLessEqual(len(grogu_watch.sessions()[0]["agent"]), 40)

    def test_a_name_that_is_not_text_does_not_crash_the_board(self):
        path = grogu_watch.activity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            '{"at": %f, "command": "plan show", "role": "engineer", "plan": "p-1",'
            ' "cwd": "/w/a", "agent": 17, "exit": 0}\n' % time.time(),
            encoding="utf8",
        )
        self.assertIn("engineer", grogu_watch.render(grogu_watch.board()))

    def test_a_skill_store_that_cannot_be_read_is_an_ask_not_a_silence(self):
        # "Nothing waiting" is the same answer as "nothing is there", and the
        # board is read precisely to find out whether anything needs you.
        state = grogu_watch.board(skill_store_error="entry #2 has a bad status")
        self.assertTrue(
            any("skill store cannot be read" in ask
                for ask in grogu_watch.waiting_on_you(state))
        )
