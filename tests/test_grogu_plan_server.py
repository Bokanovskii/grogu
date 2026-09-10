import concurrent.futures
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401

import grogu_plan_server  # noqa: E402
import grogu_plandoc  # noqa: E402
import grogu_plandoc_patch  # noqa: E402
import grogu_plans  # noqa: E402


class PlanPackageIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)

    def package(self, *, design=True, evaluation=True):
        manifest = grogu_plans.PlanDocumentStore.create(
            self.store,
            "Typed plan",
            design=design,
            evaluation=evaluation,
        )
        return manifest["id"], grogu_plans.PlanDocumentStore.for_plan(
            self.store, manifest["id"]
        )

    def test_dual_layout_refuses_and_names_both_paths(self):
        plan_id, _documents = self.package()
        legacy = self.store.legacy_plan_dir(plan_id)
        legacy.mkdir()
        with self.assertRaises(grogu_plans.PlanError) as raised:
            self.store.plan_dir(plan_id)
        message = str(raised.exception)
        self.assertIn(str(legacy), message)
        self.assertIn(str(self.store.document_plan_dir(plan_id)), message)

    def test_direct_graph_edits_update_stage_written_state(self):
        plan_id, documents = self.package()
        document = documents.load(role="reviewer")
        criterion = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "Authentication compatibility",
            stage="testing",
            revision=document["revision"],
        )
        documents.patch(
            role="reviewer",
            base=document["revision"],
            operations=[
                {"op": "add", "path": "/nodes/crit-1", "value": criterion}
            ],
        )
        self.assertTrue(
            self.store.load(plan_id)["stage_written"]["testing"]
        )

        documents.patch(
            role="reviewer",
            base=documents.head(),
            operations=[{"op": "remove", "path": "/nodes/crit-1"}],
        )
        self.assertFalse(
            self.store.load(plan_id)["stage_written"]["testing"]
        )

    def test_client_counter_updates_are_atomic_with_new_objects(self):
        plan_id, documents = self.package()
        document = documents.load(role="reviewer")
        region = grogu_plandoc.make_node(
            "reg-1",
            "region",
            "Review region",
            stage="design",
            attrs={"shape": "rectangle", "anchor_state": "resolved"},
            geometry={"x": 40, "y": 80, "w": 240, "h": 120, "z": 2},
            revision=document["revision"],
        )
        documents.patch(
            role="reviewer",
            base=document["revision"],
            operations=[
                {"op": "add", "path": "/counters/reg", "value": 1},
                {"op": "add", "path": "/nodes/reg-1", "value": region},
            ],
        )
        manifest = self.store.load(plan_id)
        self.assertEqual(manifest["plandoc"]["counters"]["reg"], 1)

    def test_top_level_add_cannot_replace_an_existing_object(self):
        _plan_id, documents = self.package()
        document = documents.load(role="reviewer")
        original = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Original task",
            stage="implementation",
            revision=document["revision"],
        )
        documents.patch(
            role="reviewer",
            base=document["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": original}
            ],
        )
        replacement = dict(original)
        replacement["title"] = "Replacement task"
        with self.assertRaisesRegex(grogu_plans.PlanError, "already in use"):
            documents.patch(
                role="reviewer",
                base=documents.head(),
                operations=[
                    {
                        "op": "add",
                        "path": "/nodes/task-1",
                        "value": replacement,
                    }
                ],
            )
        self.assertEqual(
            documents.load(role="reviewer")["nodes"]["task-1"]["title"],
            "Original task",
        )

    def test_removed_dependency_marks_consequential_impact_destructive(self):
        _plan_id, documents = self.package()
        document = documents.load(role="reviewer")
        dependency = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Dependency",
            stage="implementation",
            revision=document["revision"],
        )
        dependent = grogu_plandoc.make_node(
            "task-2",
            "task",
            "Dependent",
            stage="implementation",
            revision=document["revision"],
        )
        edge = grogu_plandoc.make_edge(
            "edge-1",
            "depends_on",
            "task-2",
            "task-1",
            revision=document["revision"],
        )
        documents.patch(
            role="reviewer",
            base=document["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": dependency},
                {"op": "add", "path": "/nodes/task-2", "value": dependent},
            ],
        )
        documents.patch(
            role="reviewer",
            base=documents.head(),
            operations=[
                {"op": "add", "path": "/edges/edge-1", "value": edge},
            ],
        )
        proposal = documents.propose(
            role="reviewer",
            base=documents.head(),
            operations=[
                {"op": "remove", "path": "/edges/edge-1"},
                {"op": "remove", "path": "/nodes/task-1"},
            ],
            why="Remove the dependency",
        )
        preview = documents.proposal_preview(
            proposal["id"], role="reviewer"
        )
        impacted = {
            item["id"]: item for item in preview["impact"]["transitive"]
        }
        self.assertTrue(impacted["task-2"]["destructive"])

    def test_local_only_upgrade_is_additive(self):
        marker = self.root / ".grogu" / "plans" / ".gitignore"
        marker.parent.mkdir(parents=True)
        marker.write_text("manifest.json\n# keep me\n", encoding="utf8")
        with self.store.locked():
            pass
        value = marker.read_text(encoding="utf8")
        self.assertIn("# keep me", value)
        for entry in (
            "review.json",
            "HEAD",
            "graph/",
            "log/",
            "snapshots/",
            "proposals/",
            "projections/",
            "legacy/",
            "recovery/",
            "registrations/",
        ):
            self.assertIn(entry, value)

    def test_migration_preserves_legacy_copy_and_seals_graph_partitions(self):
        manifest = self.store.create("Legacy", evaluation=True)
        plan_id = manifest["id"]
        implementation = (
            "# Legacy — implementation plan\n\n"
            "## Work\n\nPreserve this exact prose.\n"
        )
        testing = "# Legacy — testing plan\n\n## Checks\n\nRun the check.\n"
        evaluation = "# Legacy — evaluation plan\n\n## Quality\n\nJudge it.\n"
        self.store.write_stage(
            plan_id, "implementation", implementation, role="architect"
        )
        self.store.write_stage(plan_id, "testing", testing, role="architect")
        self.store.write_stage(
            plan_id, "evaluation", evaluation, role="architect"
        )
        result = grogu_plans.PlanDocumentStore.migrate(
            self.store, plan_id, role="reviewer"
        )
        package = self.store.plan_dir(plan_id)
        self.assertTrue(package.name.endswith(".plan"))
        self.assertEqual(
            (
                package
                / "legacy"
                / "pre-migration"
                / "implementation.md"
            ).read_text(encoding="utf8"),
            implementation,
        )
        raw = (package / "graph" / "testing.sealed").read_text(encoding="utf8")
        self.assertIn(grogu_plans.SEAL_HEADER, raw)
        self.assertNotIn("Run the check", raw)
        self.assertTrue(all(item["equivalent"] for item in result["diagnostics"]))

    def test_forbidden_partition_path_is_rejected_before_apply(self):
        plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        hidden = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "Hidden criterion",
            stage="testing",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/crit-1", "value": hidden}
            ],
        )
        engineer = documents.load(role="engineer")
        with (
            mock.patch.object(
                grogu_plandoc_patch,
                "apply_patch",
                side_effect=AssertionError("apply must not run"),
            ),
            self.assertRaises(grogu_plandoc_patch.ForbiddenPatch),
        ):
            documents.patch(
                role="engineer",
                base=engineer["revision"],
                operations=[
                    {
                        "op": "replace",
                        "path": "/nodes/crit-1/title",
                        "value": "leak",
                    }
                ],
            )

    def test_partial_open_partition_reader_cannot_erase_hidden_implementation(self):
        _plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        implementation = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Implementation task",
            stage="implementation",
            revision=reviewer["revision"],
        )
        design = grogu_plandoc.make_node(
            "goal-1",
            "goal",
            "Design goal",
            stage="design",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": implementation},
                {"op": "add", "path": "/nodes/goal-1", "value": design},
            ],
        )
        tester = documents.load(role="tester")
        with self.assertRaisesRegex(
            grogu_plans.PlanError, "shared open graph partition"
        ):
            documents.patch(
                role="tester",
                base=tester["revision"],
                operations=[
                    {
                        "op": "replace",
                        "path": "/nodes/goal-1/title",
                        "value": "Changed design",
                    }
                ],
            )
        reviewer_after = documents.load(role="reviewer")
        self.assertIn("task-1", reviewer_after["nodes"])
        self.assertEqual(
            reviewer_after["nodes"]["task-1"]["title"],
            "Implementation task",
        )

    def test_partial_sealed_partition_reader_cannot_erase_cross_seal_edge(self):
        _plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        implementation = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Implementation task",
            stage="implementation",
            revision=reviewer["revision"],
        )
        first = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "First criterion",
            stage="testing",
            revision=reviewer["revision"],
        )
        second = grogu_plandoc.make_node(
            "crit-2",
            "criterion",
            "Second criterion",
            stage="testing",
            body="before",
            revision=reviewer["revision"],
        )
        cross_seal = grogu_plandoc.make_edge(
            "edge-1",
            "references",
            "crit-1",
            "task-1",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": implementation},
                {"op": "add", "path": "/nodes/crit-1", "value": first},
                {"op": "add", "path": "/nodes/crit-2", "value": second},
            ],
        )
        head = documents.head()
        documents.patch(
            role="reviewer",
            base=head,
            operations=[
                {"op": "add", "path": "/edges/edge-1", "value": cross_seal}
            ],
        )
        tester = documents.load(role="tester")
        self.assertNotIn("edge-1", tester["edges"])
        with self.assertRaisesRegex(
            grogu_plans.PlanError, "relationships outside"
        ):
            documents.patch(
                role="tester",
                base=tester["revision"],
                operations=[
                    {
                        "op": "replace",
                        "path": "/nodes/crit-2/body",
                        "value": "after",
                    }
                ],
            )
        reviewer_after = documents.load(role="reviewer")
        self.assertIn("edge-1", reviewer_after["edges"])
        self.assertEqual(
            reviewer_after["nodes"]["crit-2"]["body"], "before"
        )

    def test_open_endpoint_of_hidden_cross_seal_edge_cannot_be_removed(self):
        _plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        design = grogu_plandoc.make_node(
            "dec-1",
            "decision",
            "Visible design decision",
            stage="design",
            revision=reviewer["revision"],
        )
        testing = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "Hidden test criterion",
            stage="testing",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/dec-1", "value": design},
                {"op": "add", "path": "/nodes/crit-1", "value": testing},
            ],
        )
        edge = grogu_plandoc.make_edge(
            "edge-1",
            "references",
            "dec-1",
            "crit-1",
            revision=documents.head(),
        )
        documents.patch(
            role="reviewer",
            base=documents.head(),
            operations=[
                {"op": "add", "path": "/edges/edge-1", "value": edge}
            ],
        )
        engineer = documents.load(role="engineer")
        self.assertIn("dec-1", engineer["nodes"])
        self.assertNotIn("edge-1", engineer["edges"])
        with self.assertRaisesRegex(
            grogu_plans.PlanError, "referenced outside"
        ):
            documents.patch(
                role="engineer",
                base=engineer["revision"],
                operations=[
                    {"op": "remove", "path": "/nodes/dec-1"}
                ],
            )
        reviewer_after = documents.load(role="reviewer")
        self.assertIn("dec-1", reviewer_after["nodes"])
        self.assertIn("edge-1", reviewer_after["edges"])
        self.assertTrue(documents.verify(role="reviewer")["ok"])

    def test_warm_projection_cache_does_not_load_the_graph(self):
        _plan_id, documents = self.package()
        first = documents.projection(
            role="engineer", stages=["implementation"]
        )
        self.assertFalse(first["cached"])
        with mock.patch.object(
            documents,
            "load",
            side_effect=AssertionError("warm cache loaded graph"),
        ):
            second = documents.projection(
                role="engineer", stages=["implementation"]
            )
        self.assertTrue(second["cached"])
        self.assertEqual(first["etag"], second["etag"])

    def test_stale_base_returns_only_role_visible_operations(self):
        _plan_id, documents = self.package()
        first = documents.load(role="reviewer")
        testing = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "Secret test criterion",
            stage="testing",
            revision=first["revision"],
        )
        documents.patch(
            role="reviewer",
            base=first["revision"],
            operations=[
                {"op": "add", "path": "/nodes/crit-1", "value": testing}
            ],
        )
        engineer = documents.load(role="engineer")
        visible = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Visible task",
            stage="implementation",
            revision=engineer["revision"],
        )
        documents.patch(
            role="engineer",
            base=engineer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": visible}
            ],
        )
        with self.assertRaises(grogu_plandoc_patch.StaleRevision) as raised:
            documents.patch(
                role="engineer",
                base=engineer["revision"],
                operations=[],
            )
        serialized = json.dumps(getattr(raised.exception, "ops_since", []))
        self.assertNotIn("crit-1", serialized)
        self.assertNotIn("Secret test criterion", serialized)

    def test_sealed_id_collision_cannot_disclose_revision_ops_or_reset_counters(self):
        _plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        hidden = grogu_plandoc.make_node(
            "task-1",
            "task",
            "SECRET TEST TITLE",
            stage="testing",
            body="SECRET TEST BODY",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": hidden}
            ],
            intent="SECRET TEST INTENT",
        )
        engineer = documents.load(role="engineer")
        collision = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Open collision",
            stage="implementation",
            revision=engineer["revision"],
        )
        with self.assertRaisesRegex(
            grogu_plans.PlanError, "already in use"
        ):
            documents.patch(
                role="engineer",
                base=engineer["revision"],
                operations=[
                    {
                        "op": "add",
                        "path": "/nodes/task-1",
                        "value": collision,
                    }
                ],
            )
        note = grogu_plandoc.make_node(
            "note-1",
            "note",
            "Visible note",
            stage="implementation",
            revision=engineer["revision"],
        )
        documents.patch(
            role="engineer",
            base=engineer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/note-1", "value": note}
            ],
        )
        manifest = self.store.load(documents.plan_id)
        self.assertEqual(manifest["plandoc"]["counters"]["task"], 1)
        allocator = {
            "plandoc": {
                "counters": dict(manifest["plandoc"]["counters"])
            }
        }
        self.assertEqual(
            grogu_plandoc.allocate_id(
                allocator,
                "task",
                existing_ids=documents.load(role="engineer")["nodes"],
            ),
            "task-2",
        )
        history = json.dumps(documents.revisions(role="engineer"))
        self.assertNotIn("SECRET TEST TITLE", history)
        self.assertNotIn("SECRET TEST BODY", history)
        self.assertNotIn("SECRET TEST INTENT", history)
        changes = json.dumps(documents.ops_since("r0001", role="engineer"))
        self.assertNotIn("SECRET TEST TITLE", changes)
        self.assertNotIn("SECRET TEST BODY", changes)

    def test_head_recovers_to_newest_complete_generation(self):
        _plan_id, documents = self.package()
        (documents.package / "HEAD").write_text("r9999\n", encoding="ascii")
        self.assertEqual(documents.head(repair=True), "r0001")
        self.assertEqual(
            (documents.package / "HEAD").read_text(encoding="ascii"), "r0001\n"
        )

    def test_node_absent_does_not_affect_runtime_package_operations(self):
        plan_id, documents = self.package()
        with mock.patch.dict(os.environ, {"PATH": ""}):
            self.assertEqual(documents.load(role="reviewer")["plan_id"], plan_id)
            self.assertTrue(documents.verify(role="reviewer")["ok"])

    def test_concurrent_writers_commit_one_generation_and_refuse_the_stale_one(self):
        _plan_id, documents = self.package()
        base = documents.head()

        def write(node_id):
            node = grogu_plandoc.make_node(
                node_id,
                "note",
                node_id,
                stage="implementation",
                revision=base,
            )
            return documents.patch(
                role="reviewer",
                base=base,
                operations=[
                    {"op": "add", "path": f"/nodes/{node_id}", "value": node}
                ],
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(write, node_id) for node_id in ("note-1", "note-2")]
        successes, stale = [], []
        for future in futures:
            try:
                successes.append(future.result())
            except grogu_plandoc_patch.StaleRevision as error:
                stale.append(error)
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(stale), 1)
        self.assertTrue(documents.verify(role="reviewer")["ok"])
        graph = documents.load(role="reviewer")
        self.assertEqual(
            len({"note-1", "note-2"} & set(graph["nodes"])), 1
        )

    def test_feedback_records_are_visible_only_to_target_or_sender(self):
        _plan_id, documents = self.package()
        previous = os.environ.get("GROGU_AGENT")
        try:
            os.environ["GROGU_AGENT"] = "reviewer-sender"
            receipt = documents.route_feedback(
                role="reviewer",
                scope={
                    "kind": "role",
                    "role": "tester",
                    "label": "testers",
                },
                text="PRIVATE TESTER FEEDBACK",
                binding=False,
            )
            self.assertEqual(
                documents.feedback_record(
                    receipt["seq"], role="reviewer"
                )["text"],
                "PRIVATE TESTER FEEDBACK",
            )
            os.environ["GROGU_AGENT"] = "engineer-reader"
            self.assertEqual(
                documents.feedback_records(role="engineer"), []
            )
            os.environ["GROGU_AGENT"] = "tester-reader"
            tester = documents.feedback_records(role="tester")
            self.assertEqual(tester[0]["text"], "PRIVATE TESTER FEEDBACK")
        finally:
            if previous is None:
                os.environ.pop("GROGU_AGENT", None)
            else:
                os.environ["GROGU_AGENT"] = previous

    def test_sealed_proposals_are_not_listable_previewable_or_decidable(self):
        _plan_id, documents = self.package()
        reviewer = documents.load(role="reviewer")
        hidden = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "SECRET PROPOSAL TARGET",
            stage="testing",
            body="SECRET ORIGINAL",
            revision=reviewer["revision"],
        )
        documents.patch(
            role="reviewer",
            base=reviewer["revision"],
            operations=[
                {"op": "add", "path": "/nodes/crit-1", "value": hidden}
            ],
        )
        tester = documents.load(role="tester")
        proposal = documents.propose(
            role="tester",
            base=tester["revision"],
            operations=[
                {
                    "op": "replace",
                    "path": "/nodes/crit-1/body",
                    "value": "SECRET PROPOSED BODY",
                }
            ],
            why="SECRET PROPOSAL WHY",
        )
        self.assertEqual(documents.proposals(role="engineer"), [])
        for action in (
            lambda: documents.proposal_preview(
                proposal["id"], role="engineer"
            ),
            lambda: documents.accept_proposal(
                proposal["id"], role="engineer"
            ),
            lambda: documents.reject_proposal(
                proposal["id"],
                role="engineer",
                why_not="not mine",
            ),
        ):
            with self.assertRaisesRegex(
                grogu_plans.PlanError, "was not found"
            ) as raised:
                action()
            message = str(raised.exception)
            self.assertNotIn("crit-1", message)
            self.assertNotIn("SECRET", message)
        visible = json.dumps(documents.proposals(role="tester"))
        self.assertIn("SECRET PROPOSED BODY", visible)

    def test_patch_preserves_feedback_recorded_while_compilation_is_in_flight(self):
        _plan_id, documents = self.package()
        base = documents.head()
        node = grogu_plandoc.make_node(
            "note-1",
            "note",
            "Slow write",
            stage="implementation",
            revision=base,
        )
        entered = threading.Event()
        release = threading.Event()
        original = grogu_plans.grogu_plandoc_compile.compile_checked

        def delayed(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return original(*args, **kwargs)

        with mock.patch.object(
            grogu_plans.grogu_plandoc_compile,
            "compile_checked",
            side_effect=delayed,
        ):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    documents.patch,
                    role="engineer",
                    base=base,
                    operations=[
                        {
                            "op": "add",
                            "path": "/nodes/note-1",
                            "value": node,
                        }
                    ],
                    intent="slow patch",
                )
                self.assertTrue(entered.wait(timeout=5))
                receipt = documents.route_feedback(
                    role="reviewer",
                    scope={
                        "kind": "role",
                        "role": "engineer",
                        "label": "engineers",
                    },
                    text="Do not lose this binding feedback",
                    binding=True,
                )
                release.set()
                future.result(timeout=10)
        manifest = self.store.load(documents.plan_id)
        self.assertEqual(
            manifest["steering"][0]["feedback_id"], receipt["seq"]
        )
        gate = self.store.gate(
            documents.plan_id, grogu_plans.GATE_IMPLEMENT
        )
        self.assertTrue(
            any(receipt["seq"] in blocker for blocker in gate["blockers"])
        )


class SecuredPlanServerTests(unittest.TestCase):
    def setUp(self):
        self.previous_agent = os.environ.pop("GROGU_AGENT", None)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        manifest = grogu_plans.PlanDocumentStore.create(
            self.store, "Server plan", design=True, evaluation=True
        )
        self.plan_id = manifest["id"]
        self.documents = grogu_plans.PlanDocumentStore.for_plan(
            self.store, self.plan_id
        )
        graph = self.documents.load(role="reviewer")
        visible = grogu_plandoc.make_node(
            "task-1",
            "task",
            "Visible task",
            stage="implementation",
            body="Visible body",
            revision=graph["revision"],
        )
        hidden = grogu_plandoc.make_node(
            "crit-1",
            "criterion",
            "SEALED_SENTINEL_TITLE",
            stage="testing",
            body="SEALED_SENTINEL_BODY",
            revision=graph["revision"],
        )
        self.documents.patch(
            role="reviewer",
            base=graph["revision"],
            operations=[
                {"op": "add", "path": "/nodes/task-1", "value": visible},
                {"op": "add", "path": "/nodes/crit-1", "value": hidden},
            ],
        )
        self.info = grogu_plan_server.serve(
            self.plan_id,
            role="engineer",
            store=self.store,
            block=False,
            timeout=0,
        )
        self.port = self.info["port"]
        self.token = self.info["token"]  # grogu-allow-secret: ephemeral test token
        self.cookie = ""

    def tearDown(self):
        server = self.info.get("server")
        thread = self.info.get("thread")
        if server:
            server.context._shutdown = True
            server.shutdown()
        if thread:
            thread.join(timeout=3)
        if server:
            server.server_close()
        os.environ.pop("GROGU_AGENT", None)
        if self.previous_agent is not None:
            os.environ["GROGU_AGENT"] = self.previous_agent

    def request(self, method, path, body=None, *, cookie=True, origin=False, token=True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if cookie and self.cookie:
            headers["Cookie"] = self.cookie
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}"
        if token:
            headers["X-Grogu-Token"] = self.token
        encoded = None
        if body is not None:
            encoded = json.dumps(body)
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, response.getheaders(), payload)
        connection.close()
        return result

    def exchange(self):
        status, headers, _body = self.request(
            "GET", f"/?t={self.token}", cookie=False, token=False
        )
        self.assertEqual(status, 303)
        values = [
            value.split(";", 1)[0]
            for key, value in headers
            if key.lower() == "set-cookie"
        ]
        self.cookie = "; ".join(values)

    def test_single_use_token_is_burned_and_second_exchange_is_403(self):
        self.exchange()
        status, headers, body = self.request(
            "GET", f"/?t={self.token}", cookie=False, token=False
        )
        self.assertEqual(status, 403)
        self.assertIn(b"Grogu", body)
        self.assertTrue(
            any(
                key.lower() == "set-cookie"
                and "grogu_notice=token_spent" in value
                for key, value in headers
            )
        )

    def test_doc_response_is_role_bounded_and_has_security_headers(self):
        self.exchange()
        status, headers, body = self.request("GET", "/api/doc")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        serialized = json.dumps(payload)
        self.assertIn("Visible task", serialized)
        self.assertNotIn("SEALED_SENTINEL_TITLE", serialized)
        self.assertNotIn("SEALED_SENTINEL_BODY", serialized)
        header_map = {key.lower(): value for key, value in headers}
        self.assertEqual(header_map["x-content-type-options"], "nosniff")
        self.assertIn("default-src 'none'", header_map["content-security-policy"])
        self.assertNotIn("access-control-allow-origin", header_map)
        status, _headers, revision_body = self.request(
            "GET", "/api/revisions"
        )
        self.assertEqual(status, 200)
        self.assertNotIn(b"SEALED_SENTINEL_TITLE", revision_body)
        self.assertNotIn(b"SEALED_SENTINEL_BODY", revision_body)

    def test_mutation_requires_cookie_token_and_exact_origin(self):
        self.exchange()
        status, _headers, _body = self.request(
            "POST",
            "/api/patch",
            {"base": self.documents.head(), "ops": [], "intent": "noop"},
            origin=False,
        )
        self.assertEqual(status, 403)
        status, _headers, _body = self.request(
            "POST",
            "/api/patch",
            {"base": self.documents.head(), "ops": [], "intent": "noop"},
            origin=True,
            token=False,
        )
        self.assertEqual(status, 403)

    def test_patch_and_projection_etag_work_end_to_end(self):
        self.exchange()
        base = self.documents.head()
        status, _headers, body = self.request(
            "POST",
            "/api/patch",
            {
                "base": base,
                "ops": [
                    {
                        "op": "replace",
                        "path": "/nodes/task-1/title",
                        "value": "Renamed",
                    }
                ],
                "intent": "rename",
            },
            origin=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["revision"], "r0003")
        status, headers, _body = self.request(
            "GET",
            "/api/projection?role=engineer&stage=implementation&format=md",
        )
        self.assertEqual(status, 200)
        etag = dict(headers)["ETag"]
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=4)
        connection.request(
            "GET",
            "/api/projection?role=engineer&stage=implementation&format=md",
            headers={
                "Host": f"127.0.0.1:{self.port}",
                "Cookie": self.cookie,
                "If-None-Match": etag,
            },
        )
        response = connection.getresponse()
        response.read()
        connection.close()
        self.assertEqual(response.status, 304)

    def test_static_react_dist_is_served_without_node(self):
        self.exchange()
        with mock.patch.dict(os.environ, {"PATH": ""}):
            status, _headers, body = self.request("GET", "/app.js")
        self.assertEqual(status, 200)
        self.assertEqual(
            body,
            (ROOT / "src" / "plan_workspace" / "dist" / "app.js").read_bytes(),
        )

    def test_thread_revise_routes_a_binding_request_to_the_architect(self):
        thread = self.documents.add_thread(
            role="engineer",
            selector={"type": "node", "id": "task-1"},
            body="Please revise this task.",
        )
        self.exchange()
        status, _headers, body = self.request(
            "POST",
            f"/api/threads/{thread['id']}/revise",
            {"instruction": "Split this into two migration steps."},
            origin=True,
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertRegex(payload["request"]["seq"], r"^f-[0-9]+$")
        refreshed = self.documents.threads(role="engineer")
        revision_request = next(
            item for item in refreshed if item["id"] == thread["id"]
        )["revision_request"]
        self.assertEqual(
            revision_request["id"], payload["request"]["seq"]
        )
        self.assertEqual(revision_request["state"], "delivered")
        manifest = self.store.load(self.plan_id)
        note = manifest["steering"][-1]
        self.assertEqual(note["role"], "architect")
        self.assertTrue(note["binding_feedback"])
        self.assertIn(thread["id"], note["text"])
        self.assertEqual(self.documents.proposals(role="engineer"), [])

    def test_control_route_never_returns_adversarial_event_content(self):
        self.info["server"].context._shutdown = True
        self.info["server"].shutdown()
        self.info["thread"].join(timeout=3)
        self.info["server"].server_close()
        events = (
            ROOT
            / "tests"
            / "fixtures"
            / "controlroom"
            / "session-state"
            / "adv-session-001"
            / "events.jsonl"
        )
        self.documents.register_session(
            {
                "run_id": "run-adversarial",
                "repository": str(self.store.root),
                "plan": self.plan_id,
                "agent": "ingest",
                "role": "engineer",
                "workstream": "integration",
                "session_id": "adv-session-001",
                "agent_id": "agent-ada",
                "registered_at": "2026-09-05T08:59:59Z",
                "events_path": str(events),
            }
        )
        self.info = grogu_plan_server.serve(
            self.plan_id,
            role="engineer",
            store=self.store,
            block=False,
            timeout=0,
        )
        self.port = self.info["port"]
        self.token = self.info["token"]  # grogu-allow-secret: ephemeral test token
        self.cookie = ""
        self.exchange()
        status, _headers, body = self.request("GET", "/api/control")
        self.assertEqual(status, 200)
        text = body.decode("utf8")
        self.assertIn("ingest", text)
        for forbidden in (
            "FORBIDDEN_REASONING_TEXT",
            "FORBIDDEN_SHELL_COMMAND",
            "FORBIDDEN_TOOL_RESULT",
            "FORBIDDEN_ASSISTANT_MESSAGE_BODY",
            "payload_json",
            "reasoningOpaque",
        ):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
