import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_tasks  # noqa: E402


@contextmanager
def identity_env(*, user="charlie", agent="", session_id="", session_pid=None):
    values = {
        "USER": user,
        "GROGU_AGENT": agent,
        "GROGU_SESSION_ID": session_id,
        "GROGU_SESSION_PID": str(session_pid if session_pid is not None else os.getpid()),
    }
    with mock.patch.dict(os.environ, values, clear=False):
        yield


class GroguTaskStoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.store = grogu_tasks.TaskStore(self.root)

    def _write_legacy_task(self) -> Path:
        task_path = self.store.task_path("t-legacy")
        task_path.parent.mkdir(parents=True, exist_ok=True)
        task_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "id": "t-legacy",
                    "title": "Legacy task",
                    "body": "old format",
                    "status": grogu_tasks.ACTIVE,
                    "priority": "normal",
                    "labels": ["legacy"],
                    "issue": None,
                    "assignee": "charlie",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "created_by": "charlie",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "revision": 1,
                    "log": [
                        {
                            "at": "2026-01-01T00:00:00+00:00",
                            "by": "charlie",
                            "event": "created",
                            "host": "legacy-host",
                        }
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf8",
        )
        return task_path

    def test_legacy_task_migrates_and_exposes_provenance(self):
        self._write_legacy_task()

        view = self.store.view("t-legacy")

        self.assertEqual(view["schema_version"], grogu_tasks.SCHEMA_VERSION)
        self.assertEqual(view["ownership"]["created_by"]["owner"], "charlie")
        self.assertEqual(view["ownership"]["created_by"]["agent"], "")
        self.assertEqual(view["ownership"]["assignee"]["owner"], "charlie")
        self.assertEqual(view["log"][0]["by"], "charlie")
        self.assertIn("agent", view["log"][0])
        self.assertIn("session_id", view["log"][0])
        self.assertNotIn("host", view["log"][0])

        self.store.update("t-legacy", note="touched")
        raw = json.loads(self.store.task_path("t-legacy").read_text(encoding="utf8"))
        self.assertEqual(raw["schema_version"], grogu_tasks.SCHEMA_VERSION)
        self.assertIn("created_by_agent", raw)
        self.assertIn("assignee_session_id", raw)
        self.assertNotIn("created_by_host", raw)
        self.assertNotIn("assignee_host", raw)
        self.assertNotIn("host", raw["log"][0])

    def test_claim_records_agent_and_groups_by_agent_session(self):
        task_a = self.store.create("Task A")
        task_b = self.store.create("Task B")

        with identity_env(agent="alpha-agent", session_id="session-a"):
            claimed_a = self.store.claim(task_a["id"])
        with identity_env(agent="beta-agent", session_id="session-b"):
            claimed_b = self.store.claim(task_b["id"])

        self.assertEqual(claimed_a["lease"]["agent"], "alpha-agent")
        self.assertEqual(claimed_a["lease"]["session_id"], "session-a")
        self.assertEqual(
            self.store.view(task_a["id"])["ownership"]["assignee"]["agent"], "alpha-agent"
        )

        grouped = self.store.grouped_tasks()
        groups = {
            (
                group["assignee"]["agent"],
                group["assignee"]["session_id"],
            ): group
            for group in grouped["groups"]
        }
        self.assertIn(("alpha-agent", "session-a"), groups)
        self.assertIn(("beta-agent", "session-b"), groups)
        self.assertEqual(groups[("alpha-agent", "session-a")]["by_status"][grogu_tasks.ACTIVE], 1)
        self.assertEqual(groups[("beta-agent", "session-b")]["by_status"][grogu_tasks.ACTIVE], 1)
        self.assertEqual(
            {item["id"] for item in groups[("alpha-agent", "session-a")]["tasks"]},
            {task_a["id"]},
        )
        self.assertEqual(
            {item["id"] for item in groups[("beta-agent", "session-b")]["tasks"]},
            {task_b["id"]},
        )

    def test_finalize_closes_stale_grogu_children_without_touching_user_tasks(self):
        with identity_env(agent="parent-agent", session_id="parent-session"):
            parent = self.store.create("Parent task")
            child_grogu = self.store.create(
                "Grogu child", parent_task_id=parent["id"], body="subordinate"
            )
        with identity_env(agent="", session_id=""):
            child_user = self.store.create("User child", parent_task_id=parent["id"])
            unrelated = self.store.create("Unrelated task")

        self.store.update(parent["id"], status=grogu_tasks.DONE, note="finished")
        self.store.release(parent["id"], status=grogu_tasks.DONE, note="already finished")

        parent_view = self.store.view(parent["id"])
        grogu_child_view = self.store.view(child_grogu["id"])
        user_child_view = self.store.view(child_user["id"])
        unrelated_view = self.store.view(unrelated["id"])

        self.assertEqual(parent_view["status"], grogu_tasks.DONE)
        self.assertEqual(grogu_child_view["status"], grogu_tasks.CANCELLED)
        self.assertEqual(user_child_view["status"], grogu_tasks.OPEN)
        self.assertEqual(unrelated_view["status"], grogu_tasks.OPEN)
        self.assertEqual(
            len([entry for entry in grogu_child_view["log"] if entry["event"] == "cleanup"]),
            1,
        )

    def test_finalize_cleanup_is_idempotent(self):
        with identity_env(agent="parent-agent", session_id="parent-session"):
            parent = self.store.create("Parent task")
            child = self.store.create("Child task", parent_task_id=parent["id"])

        self.store.release(parent["id"], status=grogu_tasks.DONE, note="first")
        first = self.store.view(child["id"])
        self.store.release(parent["id"], status=grogu_tasks.DONE, note="second")
        second = self.store.view(child["id"])

        self.assertEqual(first["status"], grogu_tasks.CANCELLED)
        self.assertEqual(second["status"], grogu_tasks.CANCELLED)
        self.assertEqual(
            len([entry for entry in second["log"] if entry["event"] == "cleanup"]),
            1,
        )

    def test_expired_grogu_child_is_cancelled_after_parent_closed(self):
        with identity_env(agent="parent-agent", session_id="parent-session"):
            parent = self.store.create("Parent task")
            child = self.store.create("Child task", parent_task_id=parent["id"])
            self.store.claim(child["id"], ttl=3600)

        self.store.release(parent["id"], status=grogu_tasks.DONE, note="closed")

        lease = self.store.lease(child["id"])
        lease["expires_at"] = "2026-01-01T00:00:00+00:00"
        self.store._write_json(self.store.lease_path(child["id"]), lease)

        self.assertIn(child["id"], self.store.collect_expired())
        self.assertEqual(self.store.view(child["id"])["status"], grogu_tasks.CANCELLED)


if __name__ == "__main__":
    unittest.main()
