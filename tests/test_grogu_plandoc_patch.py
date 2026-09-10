import copy
import concurrent.futures
import json
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plandoc_canon as canon  # noqa: E402
import grogu_plandoc_patch as patch  # noqa: E402
import grogu_plandoc_revision as revision  # noqa: E402
import grogu_plandoc_schema as schema  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "plandoc"


def graph_fixture() -> dict:
    return json.loads((FIXTURES / "graph.json").read_text(encoding="utf8"))


class ImmutablePatchTests(unittest.TestCase):
    def test_patch_is_all_or_nothing_and_does_not_mutate_inputs(self):
        graph = graph_fixture()
        original = copy.deepcopy(graph)
        operations = [
            {
                "op": "replace",
                "path": "/nodes/task-1/body",
                "value": "Updated body",
            },
            {
                "op": "replace",
                "path": "/nodes/task-1/updated_rev",
                "value": "r0008",
            },
        ]
        operations_before = copy.deepcopy(operations)
        updated = patch.apply_patch(graph, operations)
        self.assertEqual(updated["nodes"]["task-1"]["body"], "Updated body")
        self.assertEqual(graph, original)
        self.assertEqual(operations, operations_before)

        failing = operations + [
            {"op": "test", "path": "/revision", "value": "r9999"}
        ]
        with self.assertRaisesRegex(patch.PatchError, "test operation"):
            patch.apply_patch(graph, failing)
        self.assertEqual(graph, original)

    def test_test_operation_uses_json_type_identity(self):
        graph = graph_fixture()
        with self.assertRaises(patch.PatchError):
            patch.apply_patch(
                graph,
                [{"op": "test", "path": "/canvas/snap", "value": True}],
            )

    def test_stale_base_is_a_409_with_current_revision(self):
        with self.assertRaises(patch.StaleRevision) as caught:
            patch.apply_patch(
                graph_fixture(),
                [],
                base="r0006",
                current_revision="r0007",
            )
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.current_revision, "r0007")

    def test_patch_cannot_read_or_move_a_forbidden_stage(self):
        graph = graph_fixture()
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [{"op": "test", "path": "/nodes/crit-2/title", "value": "x"}],
                readable_stages={"design", "implementation"},
            )
        graph["edges"]["edge-6"] = {
            "id": "edge-6",
            "kind": "references",
            "from": "task-2",
            "to": "task-1",
            "attrs": {},
            "created_rev": "r0007",
        }
        outcomes = []
        for target in ("crit-2", "crit-999"):
            with self.assertRaises(patch.ForbiddenPatch) as caught:
                patch.apply_patch(
                    graph,
                    [
                        {
                            "op": "replace",
                            "path": "/edges/edge-6/to",
                            "value": target,
                        }
                    ],
                    readable_stages={"design", "implementation"},
                )
            outcomes.append((caught.exception.status, caught.exception.code))
        self.assertEqual(outcomes[0], outcomes[1])

        thread = copy.deepcopy(graph["nodes"]["thr-1"])
        thread["id"] = "thr-2"
        thread["attrs"]["selector"] = {"type": "node", "id": "crit-2"}
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [{"op": "add", "path": "/nodes/thr-2", "value": thread}],
                readable_stages={"design", "implementation"},
                authorized_new_ids={"thr-2"},
            )
        thread["attrs"]["selector"] = {"type": "node", "id": "task-1"}
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [{"op": "add", "path": "/nodes/thr-2", "value": thread}],
                readable_stages={"design", "implementation"},
            )
        added = patch.apply_patch(
            graph,
            [{"op": "add", "path": "/nodes/thr-2", "value": thread}],
            readable_stages={"design", "implementation"},
            authorized_new_ids={"thr-2"},
        )
        self.assertIn("thr-2", added["nodes"])
        probe_outcomes = []
        for pointer in (
            "/nodes/crit-2/title",
            "/nodes/crit-999/title",
            "/edges/edge-5",
            "/edges/edge-999",
        ):
            with self.assertRaises(patch.ForbiddenPatch) as caught:
                patch.apply_patch(
                    graph,
                    [{"op": "test", "path": pointer, "value": "probe"}],
                    readable_stages={"design", "implementation"},
                )
            probe_outcomes.append((caught.exception.status, caught.exception.code))
        self.assertEqual(len(set(probe_outcomes)), 1)
        from_outcomes = []
        for source in ("/nodes/crit-2/title", "/nodes/crit-999/title"):
            with self.assertRaises(patch.ForbiddenPatch) as caught:
                patch.apply_patch(
                    graph,
                    [{"op": "copy", "from": source, "path": "/title"}],
                    readable_stages={"design", "implementation"},
                )
            from_outcomes.append((caught.exception.status, caught.exception.code))
        self.assertEqual(from_outcomes[0], from_outcomes[1])
        move_outcomes = []
        for candidate in ("crit-2", "crit-999"):
            with self.assertRaises(patch.ForbiddenPatch) as caught:
                patch.apply_patch(
                    graph,
                    [
                        {
                            "op": "add",
                            "path": "/nodes/task-1/attrs/probe",
                            "value": candidate,
                        },
                        {
                            "op": "move",
                            "from": "/nodes/task-1/attrs/probe",
                            "path": "/edges/edge-1/to",
                        },
                    ],
                    readable_stages={"design", "implementation"},
                )
            move_outcomes.append((caught.exception.status, caught.exception.code))
        self.assertEqual(move_outcomes[0], move_outcomes[1])

        without_hidden = copy.deepcopy(graph)
        del without_hidden["edges"]["edge-5"]
        removal_outcomes = []
        for candidate in (graph, without_hidden):
            with self.assertRaises(patch.ForbiddenPatch) as caught:
                patch.apply_patch(
                    candidate,
                    [{"op": "remove", "path": "/nodes/task-1"}],
                    readable_stages={"design", "implementation"},
                )
            removal_outcomes.append((caught.exception.status, caught.exception.code))
        self.assertEqual(removal_outcomes[0], removal_outcomes[1])
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [{"op": "remove", "path": "/nodes/task-1"}],
                readable_stages={"design", "implementation"},
                authorized_remove_ids={"task-1"},
            )
        replacement_nodes = copy.deepcopy(graph["nodes"])
        del replacement_nodes["task-1"]
        replacement_nodes["task-99"] = {
            **copy.deepcopy(graph["nodes"]["task-2"]),
            "id": "task-99",
            "title": "Unreserved",
        }
        for pointer, value in (
            ("/nodes", replacement_nodes),
            (
                "",
                {
                    **copy.deepcopy(graph),
                    "nodes": replacement_nodes,
                },
            ),
        ):
            with self.assertRaises(patch.ForbiddenPatch):
                patch.apply_patch(
                    graph,
                    [{"op": "replace", "path": pointer, "value": value}],
                    readable_stages={"design", "implementation"},
                )
        proposed_edges = {
            "edge-99": {
                "id": "edge-99",
                "kind": "references",
                "from": "task-2",
                "to": "crit-999",
                "attrs": {},
                "created_rev": "r0007",
            }
        }
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [{"op": "replace", "path": "/edges", "value": proposed_edges}],
                readable_stages={"design", "implementation"},
            )
        removed = patch.apply_patch(
            without_hidden,
            [
                {"op": "remove", "path": "/edges/edge-1"},
                {"op": "remove", "path": "/edges/edge-2"},
                {"op": "remove", "path": "/edges/edge-6"},
                {"op": "remove", "path": "/nodes/task-1"},
            ],
            readable_stages={"design", "implementation"},
            authorized_remove_ids={"task-1"},
        )
        self.assertNotIn("task-1", removed["nodes"])
        with self.assertRaises(patch.ForbiddenPatch):
            patch.apply_patch(
                graph,
                [
                    {
                        "op": "replace",
                        "path": "/nodes/task-1/stage",
                        "value": "testing",
                    }
                ],
                readable_stages={"design", "implementation"},
            )

    def test_whole_graph_validation_rejects_a_cycle_after_valid_ops(self):
        graph = graph_fixture()
        edge = {
            "id": "edge-6",
            "kind": "depends_on",
            "from": "task-1",
            "to": "task-2",
            "attrs": {},
            "created_rev": "r0008",
        }
        with self.assertRaisesRegex(patch.PatchError, "depends_on cycle"):
            patch.apply_patch(
                graph,
                [{"op": "add", "path": "/edges/edge-6", "value": edge}],
            )
        self.assertNotIn("edge-6", graph["edges"])

    def test_deterministic_diff_round_trips(self):
        before = graph_fixture()
        after = copy.deepcopy(before)
        after["nodes"]["task-1"]["body"] = "Changed"
        after["nodes"]["task-1"]["updated_rev"] = "r0008"
        del after["nodes"]["note-1"]
        changes = patch.diff(before, after)
        self.assertEqual(patch.apply_patch(before, changes), after)
        self.assertEqual(changes, patch.diff(before, after))

    def test_namespaced_extension_data_survives_patching_unchanged(self):
        graph = graph_fixture()
        updated = patch.apply_patch(
            graph,
            [
                {
                    "op": "add",
                    "path": "/nodes/task-1/ext",
                    "value": {
                        "org.example.plugin": {
                            "nested": ["opaque", {"value": 7}]
                        }
                    },
                }
            ],
        )
        self.assertEqual(
            updated["nodes"]["task-1"]["ext"],
            {
                "org.example.plugin": {
                    "nested": ["opaque", {"value": 7}]
                }
            },
        )
        self.assertNotEqual(canon.digest(graph), canon.digest(updated))


class RevisionGenerationTests(unittest.TestCase):
    def setUp(self):
        self.directory = ROOT / "tests" / f".plandoc-runtime-{uuid.uuid4().hex}"
        self.directory.mkdir()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def _states(self):
        before = graph_fixture()
        after_one = patch.apply_patch(
            before,
            [
                {
                    "op": "replace",
                    "path": "/nodes/task-1/body",
                    "value": "Revision one",
                },
                {
                    "op": "replace",
                    "path": "/nodes/task-1/updated_rev",
                    "value": "r0008",
                },
            ],
        )
        after_two = patch.apply_patch(
            after_one,
            [
                {
                    "op": "replace",
                    "path": "/nodes/task-1/body",
                    "value": "Revision two",
                },
                {
                    "op": "replace",
                    "path": "/nodes/task-1/updated_rev",
                    "value": "r0009",
                },
            ],
        )
        return before, after_one, after_two

    def _envelope(self, before, after, seq, ops):
        return revision.make_revision(
            before,
            after,
            seq=seq,
            actor="engineer",
            role="engineer",
            agent="plan-v2-core-engineer",
            intent=f"revision {seq}",
            origin="workspace",
            ops=ops,
            at=f"2026-09-05T20:00:0{seq}Z",
        )

    def test_generation_writes_log_then_files_and_head_last(self):
        before, after_one, after_two = self._states()
        ops_one = patch.diff(before, after_one)
        first = self._envelope(before, after_one, 1, ops_one)
        revision.write_generation(
            self.directory,
            first,
            partitions={"graph/open.json": canon.dumpb(after_one)},
            artifacts={"implementation.md": b"compiled one\n"},
        )
        log_bytes = (self.directory / "log" / "000001.json").read_bytes()
        self.assertIn(b"\n  \"actor\":", log_bytes)
        self.assertEqual(canon.loads(log_bytes), first)
        self.assertEqual(revision.read_head(self.directory), "r0001")
        self.assertEqual(
            revision.verify_package(self.directory, materialized=after_one)["revisions"],
            1,
        )

        ops_two = patch.diff(after_one, after_two)
        second = self._envelope(after_one, after_two, 2, ops_two)

        def crash():
            raise RuntimeError("simulated crash before HEAD")

        with self.assertRaisesRegex(RuntimeError, "simulated crash"):
            revision.write_generation(
                self.directory,
                second,
                partitions={"graph/open.json": canon.dumpb(after_two)},
                artifacts={"implementation.md": b"compiled two\n"},
                before_head_replace=crash,
            )
        self.assertEqual(revision.read_head(self.directory), "r0001")
        self.assertTrue((self.directory / "log" / "000002.json").exists())
        self.assertEqual(
            revision.recover_head(self.directory, repair=True), "r0002"
        )
        self.assertEqual(revision.read_head(self.directory), "r0002")

        after_three = copy.deepcopy(after_two)
        after_three["title"] = "Recovered third revision"
        ops_three = patch.diff(after_two, after_three)
        third = self._envelope(after_two, after_three, 3, ops_three)
        revision.write_generation(
            self.directory,
            third,
            partitions={"graph/open.json": canon.dumpb(after_three)},
            artifacts={"implementation.md": b"compiled three\n"},
            base="r0002",
        )
        self.assertEqual(revision.read_head(self.directory), "r0003")

    def test_missing_head_target_recovers_to_newest_complete_log(self):
        before, after_one, after_two = self._states()
        for seq, left, right in (
            (1, before, after_one),
            (2, after_one, after_two),
        ):
            envelope = self._envelope(left, right, seq, patch.diff(left, right))
            revision.write_generation(
                self.directory,
                envelope,
                partitions={"graph/open.json": canon.dumpb(right)},
                artifacts={"implementation.md": f"compiled {seq}\n".encode()},
            )
        (self.directory / "HEAD").write_text("r9999\n", encoding="ascii")
        self.assertEqual(
            revision.recover_head(
                self.directory,
                is_complete=lambda item: item["revision"] == "r0001",
            ),
            "r0001",
        )
        self.assertEqual(revision.recover_head(self.directory), "r0002")
        self.assertEqual(
            revision.recover_head(self.directory, repair=True), "r0002"
        )
        self.assertEqual(revision.read_head(self.directory), "r0002")

    def test_generation_refuses_a_stale_parent_before_writing(self):
        before, after_one, after_two = self._states()
        first = self._envelope(before, after_one, 1, patch.diff(before, after_one))
        revision.write_generation(
            self.directory,
            first,
            partitions={"graph/open.json": canon.dumpb(after_one)},
            artifacts={},
        )
        stale = revision.make_revision(
            before,
            after_two,
            seq=2,
            actor="engineer",
            role="engineer",
            agent="plan-v2-core-engineer",
            intent="stale",
            origin="workspace",
            ops=patch.diff(before, after_two),
            at="2026-09-05T20:00:02Z",
        )
        with self.assertRaises(patch.StaleRevision):
            revision.write_generation(
                self.directory,
                stale,
                partitions={"graph/open.json": canon.dumpb(after_two)},
                artifacts={},
                base="r0000",
            )
        self.assertFalse((self.directory / "log" / "000002.json").exists())

    def test_failed_compiler_identity_writes_nothing(self):
        before, after_one, _after_two = self._states()
        envelope = self._envelope(
            before, after_one, 1, patch.diff(before, after_one)
        )

        def fail_identity():
            raise ValueError("identity 3 failed at nodes.task-1.body")

        with self.assertRaisesRegex(ValueError, "identity 3"):
            revision.write_generation(
                self.directory,
                envelope,
                partitions={"graph/open.json": canon.dumpb(after_one)},
                artifacts={"implementation.md": b"bad compile\n"},
                identity_checks=[fail_identity],
            )
        self.assertFalse((self.directory / "log").exists())
        self.assertFalse((self.directory / "HEAD").exists())

    def test_log_and_snapshot_files_are_immutable(self):
        before, after_one, _after_two = self._states()
        first = self._envelope(before, after_one, 1, patch.diff(before, after_one))
        revision.write_generation(
            self.directory,
            first,
            partitions={"graph/open.json": canon.dumpb(after_one)},
            artifacts={},
        )
        with self.assertRaises(revision.ImmutableFileError):
            revision.atomic_write(
                self.directory / "log" / "000001.json",
                b"different immutable bytes\n",
                immutable=True,
            )
        second = self._envelope(after_one, after_one, 2, [])
        with self.assertRaisesRegex(revision.RevisionError, "50-revision"):
            revision.write_generation(
                self.directory,
                second,
                partitions={"graph/open.json": canon.dumpb(after_one)},
                artifacts={},
                snapshot=after_one,
            )

    def test_replay_checks_every_before_and_after_digest(self):
        before, after_one, after_two = self._states()
        for seq, left, right in (
            (1, before, after_one),
            (2, after_one, after_two),
        ):
            revision.write_generation(
                self.directory,
                self._envelope(left, right, seq, patch.diff(left, right)),
                partitions={"graph/open.json": canon.dumpb(right)},
                artifacts={},
            )
        replayed = revision.replay(
            self.directory,
            initial=before,
            validator=schema.validate_document,
        )
        self.assertEqual(replayed, after_two)

    def test_snapshot_bounds_replay_to_fifty_or_fewer_patches(self):
        state = graph_fixture()
        state["revision"] = "r0000"
        initial = copy.deepcopy(state)
        for seq in range(1, 52):
            next_state = copy.deepcopy(state)
            next_state["revision"] = revision.revision_id(seq)
            next_state["nodes"]["task-1"]["body"] = f"Body at {seq}"
            next_state["nodes"]["task-1"]["updated_rev"] = revision.revision_id(seq)
            changes = patch.diff(state, next_state)
            envelope = revision.make_revision(
                state,
                next_state,
                seq=seq,
                actor="engineer",
                role="engineer",
                agent="plan-v2-core-engineer",
                intent=f"revision {seq}",
                origin="workspace",
                ops=changes,
                at=f"2026-09-05T20:{seq // 60:02d}:{seq % 60:02d}Z",
            )
            revision.write_generation(
                self.directory,
                envelope,
                partitions={"graph/open.json": canon.dumpb(next_state)},
                artifacts={},
                snapshot=next_state if seq == 50 else None,
            )
            state = next_state

        original_apply = revision.patch.apply_patch
        with mock.patch.object(
            revision.patch, "apply_patch", wraps=original_apply
        ) as apply_spy:
            replayed = revision.replay(
                self.directory,
                initial=initial,
                validator=schema.validate_document,
            )
        self.assertEqual(replayed, state)
        self.assertEqual(apply_spy.call_count, 1)

    @unittest.skipIf(os.name == "nt", "POSIX symlink containment")
    def test_generation_rejects_symlinked_package_components(self):
        before, after_one, _after_two = self._states()
        package = self.directory / "package"
        outside = self.directory / "outside"
        package.mkdir()
        outside.mkdir()
        (package / "graph").symlink_to(outside, target_is_directory=True)
        envelope = self._envelope(
            before, after_one, 1, patch.diff(before, after_one)
        )
        with self.assertRaisesRegex(revision.RevisionError, "symlink"):
            revision.write_generation(
                package,
                envelope,
                partitions={"graph/open.json": canon.dumpb(after_one)},
                artifacts={},
            )
        self.assertFalse((outside / "open.json").exists())
        self.assertFalse((package / "HEAD").exists())

    def test_generation_lock_allows_only_one_writer_for_the_same_head(self):
        before, after_one, _after_two = self._states()
        envelope = self._envelope(
            before, after_one, 1, patch.diff(before, after_one)
        )

        def write():
            revision.write_generation(
                self.directory,
                envelope,
                partitions={"graph/open.json": canon.dumpb(after_one)},
                artifacts={"implementation.md": b"compiled\n"},
            )
            return "written"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = []
            futures = [executor.submit(write) for _ in range(2)]
            for future in futures:
                try:
                    results.append(future.result())
                except patch.StaleRevision:
                    results.append("stale")
        self.assertCountEqual(results, ["written", "stale"])
        self.assertEqual(revision.read_head(self.directory), "r0001")
        self.assertEqual(len(revision.list_revisions(self.directory)), 1)

    def test_artifact_paths_cannot_alias_package_control_state(self):
        before, after_one, _after_two = self._states()
        envelope = self._envelope(
            before, after_one, 1, patch.diff(before, after_one)
        )
        for path in ("LOG/000001.json", "GRAPH/open.json", "head"):
            with self.subTest(path=path):
                with self.assertRaisesRegex(
                    revision.RevisionError, "control state|overlap"
                ):
                    revision.write_generation(
                        self.directory,
                        envelope,
                        partitions={"graph/open.json": canon.dumpb(after_one)},
                        artifacts={path: b"malicious alias\n"},
                    )
        with self.assertRaisesRegex(revision.RevisionError, "aliases"):
            revision.write_generation(
                self.directory,
                envelope,
                partitions={"graph/open.json": canon.dumpb(after_one)},
                artifacts={
                    "implementation.md": b"one\n",
                    "IMPLEMENTATION.MD": b"two\n",
                },
            )
        self.assertFalse((self.directory / "HEAD").exists())

    def test_package_paths_reject_posix_and_windows_escape_forms(self):
        for path in (
            "../victim",
            "graph/../HEAD",
            r"..\victim",
            r"graph\..\HEAD",
            r"C:\outside\victim",
            r"\\server\share\victim",
            "graph//open.json",
            "NUL.txt",
        ):
            with self.subTest(path=path):
                with self.assertRaises(revision.RevisionError):
                    revision._safe_relative(path)


if __name__ == "__main__":
    unittest.main()
