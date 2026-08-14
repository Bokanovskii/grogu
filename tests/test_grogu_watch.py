"""Tests for the agent activity board."""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import grogu_watch  # noqa: E402


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        os.environ["GROGU_HOME"] = self.home.name
        self.addCleanup(os.environ.pop, "GROGU_HOME", None)

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
        os.environ["GROGU_HOME"] = "/dev/null/nope"
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
