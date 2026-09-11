import contextlib
import hashlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "src" / "grogu_cli.py"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_banner  # noqa: E402
import grogu_cli
import grogu_codemode  # noqa: E402
import grogu_context  # noqa: E402
import grogu_mcp  # noqa: E402
import grogu_plans  # noqa: E402
import grogu_memory  # noqa: E402
import grogu_personal_memory  # noqa: E402
import grogu_telemetry  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class GroguCliTests(unittest.TestCase):
    def run_cli(self, *arguments, home=None):
        temporary_home = tempfile.TemporaryDirectory() if home is None else None
        try:
            environment = os.environ.copy()
            environment["GROGU_HOME"] = home or temporary_home.name
            return subprocess.run(
                [sys.executable, str(CLI), *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf8",
                env=environment,
            )
        finally:
            if temporary_home is not None:
                temporary_home.cleanup()

    def test_version(self):
        result = self.run_cli("--version")
        self.assertEqual(result.returncode, 0)
        self.assertIn("grogu 0.1.0", result.stdout)

    def test_doctor_reports_python_and_mcp_status(self):
        result = self.run_cli("doctor")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["python_min_required"], "3.10")
        self.assertEqual(
            payload["python_meets_minimum"], sys.version_info >= (3, 10)
        )
        self.assertIn("mcp_available", payload)

    def test_design_html_template_prints_a_populated_document(self):
        result = self.run_cli(
            "design",
            "html-template",
            "Sample",
            "Report",
            "--subtitle",
            "Review guide",
            "--section",
            "Overview",
            "--section",
            "Details",
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("<!doctype html>", result.stdout)
        self.assertIn("Sample Report", result.stdout)
        self.assertIn('href="#overview"', result.stdout)
        self.assertIn('href="#details"', result.stdout)
        self.assertNotRegex(result.stdout, r"__[A-Z_]+__")

    def test_bare_launch_defaults_to_autopilot(self):
        plugin = ["--plugin-dir", str(ROOT)]
        model_default = [
            "--model", grogu_cli.DEFAULT_MODEL,
            "--context", grogu_cli.DEFAULT_MODEL_CONTEXT,
            "--effort", grogu_cli.DEFAULT_MODEL_EFFORT,
        ]
        self.assertEqual(
            grogu_cli.copilot_arguments([]),
            [*plugin, *model_default, "--autopilot"],
        )
        self.assertEqual(
            grogu_cli.copilot_arguments(["--model", "gpt-5.4"]),
            [*plugin, "--autopilot", "--model", "gpt-5.4"],
        )

    def test_launch_preserves_explicit_plugin_directories(self):
        model_default = [
            "--model", grogu_cli.DEFAULT_MODEL,
            "--context", grogu_cli.DEFAULT_MODEL_CONTEXT,
            "--effort", grogu_cli.DEFAULT_MODEL_EFFORT,
        ]
        self.assertEqual(
            grogu_cli.copilot_arguments(["--plugin-dir", "/user/plugin"]),
            [
                "--plugin-dir",
                str(ROOT),
                *model_default,
                "--autopilot",
                "--plugin-dir",
                "/user/plugin",
            ],
        )

    def test_bare_launch_defaults_model_context_and_effort(self):
        self.assertEqual(
            grogu_cli.wants_model_default([]), True
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--model", "gpt-5.4"]), False
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--context", "default"]), False
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--effort", "low"]), False
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--resume"]), False
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--connect"]), False
        )
        self.assertEqual(
            grogu_cli.wants_model_default(["--continue"]), False
        )

    def test_model_default_disabled_by_environment(self):
        with mock.patch.dict(os.environ, {"GROGU_MODEL_DEFAULT": "0"}):
            self.assertEqual(grogu_cli.wants_model_default([]), False)
            self.assertEqual(
                grogu_cli.copilot_arguments([]),
                ["--plugin-dir", str(ROOT), "--autopilot"],
            )

    def test_trace_record_and_list(self):
        with tempfile.TemporaryDirectory() as home:
            recorded = self.run_cli(
                "trace",
                "record",
                "--kind",
                "test",
                "--provider",
                "copilot",
                "--status",
                "ok",
                "--payload",
                json.dumps({"redacted": True}),
                home=home,
            )
            self.assertEqual(recorded.returncode, 0)
            listed = self.run_cli("trace", "list", home=home)
            self.assertEqual(listed.returncode, 0)
            self.assertIn('"kind": "test"', listed.stdout)

    def test_project_init_writes_manifest(self):
        with tempfile.TemporaryDirectory() as home, tempfile.TemporaryDirectory() as project:
            environment = os.environ.copy()
            environment["GROGU_HOME"] = home
            result = subprocess.run(
                [sys.executable, str(CLI), "project", "init", "Example", "--path", project],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf8",
                env=environment,
            )
            self.assertEqual(result.returncode, 0)
            manifest = Path(project) / ".grogu" / "project.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual(json.loads(manifest.read_text())["slug"], "example")
            self.assertNotIn("repository_path", json.loads(manifest.read_text()))

    def test_project_relationship_catalog_is_separate_from_repo_context(self):
        with tempfile.TemporaryDirectory() as home:
            related = self.run_cli(
                "project",
                "relate",
                "frontend",
                "backend",
                "depends-on",
                "--evidence",
                '{"source":"service configuration"}',
                home=home,
            )
            self.assertEqual(related.returncode, 0)
            graph = self.run_cli("project", "graph", home=home)
            self.assertEqual(graph.returncode, 0)
            self.assertIn('"kind": "depends-on"', graph.stdout)
            self.assertIn("service configuration", graph.stdout)

    def test_session_new_adds_remote_flag(self):
        captured = []
        original = grogu_cli.launch_copilot
        grogu_cli.launch_copilot = lambda arguments: captured.append(arguments) or 0
        try:
            result = grogu_cli.main(["session", "new", "--", "--model", "gpt-5.4"])
        finally:
            grogu_cli.launch_copilot = original
        self.assertEqual(result, 0)
        self.assertEqual(captured, [["--remote", "--model", "gpt-5.4"]])

    def test_session_new_preserves_explicit_remote_choice(self):
        captured = []
        original = grogu_cli.launch_copilot
        grogu_cli.launch_copilot = lambda arguments: captured.append(arguments) or 0
        try:
            result = grogu_cli.main(["session", "new", "--no-remote"])
        finally:
            grogu_cli.launch_copilot = original
        self.assertEqual(result, 0)
        self.assertEqual(captured, [["--no-remote"]])

    def test_session_new_can_export_without_remote_control(self):
        captured = []
        original = grogu_cli.launch_copilot
        grogu_cli.launch_copilot = lambda arguments: captured.append(arguments) or 0
        try:
            result = grogu_cli.main(["session", "new", "--remote-export"])
        finally:
            grogu_cli.launch_copilot = original
        self.assertEqual(result, 0)
        self.assertEqual(captured, [["--remote-export"]])

    def test_memory_index_is_incremental_and_portable(self):
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("# Demo\n")
            (root / "app.py").write_text("print('one')\n")
            task_dir = root / ".grogu/tasks"
            task_dir.mkdir(parents=True)
            (task_dir / "t-demo.json").write_text(
                json.dumps(
                    {
                        "id": "t-demo",
                        "title": "Map the API",
                        "body": "Record the handler boundaries",
                        "status": "done",
                        "labels": ["architecture"],
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                )
            )
            store = grogu_memory.MemoryStore(root)
            first = store.index()
            self.assertEqual(first["index"]["summary"]["file_count"], 3)
            self.assertIn("app.py", first["changed"])
            self.assertNotIn("mtime_ns", first["index"]["files"]["app.py"])
            self.assertIn("file:app.py", first["graph"]["nodes"])
            self.assertIn("work:t-demo", first["graph"]["nodes"])
            second = store.index()
            self.assertEqual(second["changed"], [])
            (root / "app.py").write_text("print('two')\n")
            third = store.index()
            self.assertEqual(third["changed"], ["app.py"])
            self.assertTrue((root / ".grogu/state/memory-cache.json").is_file())

    def test_memory_state_and_identity_are_shared_across_linked_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "demo"
            linked = Path(directory) / "feature-worktree"
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(
                ["git", "-C", str(root), "config", "user.email", "test@example.com"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(root), "config", "user.name", "Test User"],
                check=True,
            )
            (root / "README.md").write_text("# Demo\n")
            (root / "app.py").write_text("print('one')\n")
            subprocess.run(["git", "-C", str(root), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-qm", "initial"],
                check=True,
            )

            primary = grogu_memory.MemoryStore(root)
            primary_result = primary.index()
            primary.remember(
                "convention",
                "python-style",
                "Keep Python explicit.",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(root),
                    "worktree",
                    "add",
                    "-qb",
                    "feature",
                    str(linked),
                ],
                check=True,
            )

            worktree = grogu_memory.MemoryStore(linked)
            result = worktree.index()

            self.assertEqual(worktree.state_root, root.resolve())
            self.assertEqual(
                result["manifest"]["repository_id"],
                primary_result["manifest"]["repository_id"],
            )
            self.assertEqual(result["manifest"]["name"], root.name)
            self.assertIn("convention:python-style", result["graph"]["nodes"])
            self.assertIn("file:app.py", result["graph"]["nodes"])
            worktree.link("convention:python-style", "file:app.py", "applies-to")
            self.assertFalse((linked / ".grogu/intelligence").exists())

    def test_memory_graph_traversal_returns_bounded_learning_context(self):
        with tempfile.TemporaryDirectory() as project:
            root = Path(project)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / "README.md").write_text("# Demo\n")
            store = grogu_memory.MemoryStore(root)
            store.index()
            store.remember(
                "architecture",
                "api",
                "HTTP handlers",
                paths=["src/api"],
                provenance={"kind": "verification"},
            )
            store.remember(
                "component",
                "routes",
                "Route definitions",
                paths=["src/api/routes.py"],
            )
            store.link("architecture:api", "component:routes", "contains")
            context = store.context(node_id="architecture:api", depth=1, limit=10)
            self.assertEqual(
                {node["id"] for node in context["nodes"]},
                {"architecture:api", "component:routes"},
            )
            self.assertNotIn("git", context)

    def test_non_repository_memory_does_not_scan_or_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = grogu_memory.MemoryStore(root)
            result = store.index()
            self.assertEqual(result["index"]["summary"]["file_count"], 0)
            self.assertFalse((root / ".grogu").exists())
            self.assertFalse(store.status()["initialized"])

    def test_telemetry_redacts_secret_values(self):
        with tempfile.TemporaryDirectory() as home:
            database = grogu_cli.connect(Path(home) / "traces.db")
            try:
                grogu_telemetry.initialize(database)
                event = grogu_telemetry.record(
                    database,
                    "verification",
                    outcome="passed",
                    payload={"token": "secret", "message": "ghp_example"},
                )
                self.assertEqual(event["payload"]["token"], "[REDACTED]")
                self.assertNotIn("ghp_example", json.dumps(event))
            finally:
                database.close()


class AggregateTests(unittest.TestCase):
    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "a@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "a"], check=True
        )
        (root / "app.py").write_text("print('one')\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True
        )
        return directory, root

    def run_cli(self, *arguments, home):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = home
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=environment,
        )

    def test_git_summary_is_bounded_and_has_no_diff_content(self):
        directory, root = self.make_repository()
        try:
            (root / "app.py").write_text("print('two')\n")
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "aggregate", "git", "--repo", str(root), home=home
                )
                self.assertEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["kind"], "grogu.context_summary")
                self.assertEqual(payload["op"], "git")
                data = payload["data"]
                self.assertEqual(data["branch"], "main")
                self.assertEqual(data["changed_files"], 1)
                self.assertEqual(data["changed_by_role"], {"source": 1})
                self.assertNotIn("print(", result.stdout)
        finally:
            directory.cleanup()

    def test_tasks_summary_omits_task_bodies(self):
        directory, root = self.make_repository()
        try:
            task_dir = root / ".grogu/tasks"
            task_dir.mkdir(parents=True)
            (task_dir / "t-demo.json").write_text(
                json.dumps(
                    {
                        "id": "t-demo",
                        "title": "Ship the thing",
                        "body": "a secret implementation detail",
                        "status": "open",
                        "labels": ["bug"],
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                )
            )
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "aggregate", "tasks", "--repo", str(root), home=home
                )
                self.assertEqual(result.returncode, 0)
                data = json.loads(result.stdout)["data"]
                self.assertEqual(data["total"], 1)
                self.assertEqual(data["by_status"], {"open": 1})
                self.assertEqual(data["by_label"], {"bug": 1})
                self.assertNotIn("secret implementation detail", result.stdout)
        finally:
            directory.cleanup()

    def test_signature_is_stable_for_identical_data(self):
        first = grogu_context._envelope("git", "repo", {"a": 1, "b": 2})
        second = grogu_context._envelope("git", "repo", {"b": 2, "a": 1})
        self.assertEqual(first["signature"], second["signature"])

    def test_unknown_operation_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                grogu_context.aggregate("not-a-real-op", Path(directory))


class CodemodeTests(unittest.TestCase):
    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "a@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "a"], check=True
        )
        (root / "app.py").write_text("print('one')\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True
        )
        return directory, root

    def test_execute_binds_tools_as_plain_function_calls(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(
                root,
                "created = task_create(title='Investigate flaky test', labels=['bug'])\n"
                "summary = tasks_summary()\n"
                "print(created['id'], summary['total'])\n",
            )
            self.assertEqual(result["returncode"], 0)
            self.assertFalse(result["timed_out"])
            output = result["stdout"].split()
            self.assertTrue(output[0].startswith("t-"))
            self.assertEqual(output[1], "1")
            self.assertTrue(Path(result["log_path"]).is_file())
        finally:
            directory.cleanup()

    def test_execute_truncates_large_output_but_keeps_full_log(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(root, "print('x' * 20000)")
            self.assertTrue(result["stdout_truncated"])
            self.assertLessEqual(
                len(result["stdout"].encode("utf8")), grogu_codemode.MAX_OUTPUT_BYTES
            )
            logged = json.loads(Path(result["log_path"]).read_text())
            self.assertEqual(len(logged["stdout"]), 20001)
        finally:
            directory.cleanup()

    def test_execute_reports_timeout(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(
                root, "import time; time.sleep(5)", timeout=1
            )
            self.assertTrue(result["timed_out"])
        finally:
            directory.cleanup()

    def test_search_tools_is_bounded_to_matching_names_and_summaries(self):
        matches = grogu_codemode.search_tools("task")
        names = {tool["name"] for tool in matches}
        self.assertIn("task_create", names)
        self.assertIn("tasks_summary", names)
        self.assertNotIn("git_summary", names)

    def test_generate_tool_tree_writes_one_file_per_tool(self):
        directory, root = self.make_repository()
        try:
            written = grogu_codemode.generate_tool_tree(root)
            files = {path.stem for path in written.glob("*.py")}
            self.assertEqual(files, set(grogu_codemode.TOOLS))
            content = (written / "git_summary.py").read_text()
            self.assertIn("git_summary() -> dict", content)
            self.assertIn("Branch, upstream drift", content)
        finally:
            directory.cleanup()

    def test_execute_surfaces_exceptions_as_nonzero_returncode(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(root, "raise ValueError('boom')")
            self.assertNotEqual(result["returncode"], 0)
            self.assertFalse(result["timed_out"])
            self.assertIn("ValueError: boom", result["stderr"])
        finally:
            directory.cleanup()

    def test_execute_reports_unknown_tool_as_name_error(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(root, "not_a_real_tool()")
            self.assertNotEqual(result["returncode"], 0)
            self.assertIn("NameError", result["stderr"])
        finally:
            directory.cleanup()

    def test_execute_truncates_large_stderr_but_keeps_full_log(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(
                root, "import sys; sys.stderr.write('e' * 20000)"
            )
            self.assertTrue(result["stderr_truncated"])
            self.assertLessEqual(
                len(result["stderr"].encode("utf8")), grogu_codemode.MAX_OUTPUT_BYTES
            )
            logged = json.loads(Path(result["log_path"]).read_text())
            self.assertEqual(len(logged["stderr"]), 20000)
        finally:
            directory.cleanup()

    def test_execute_clamps_timeout_to_configured_bounds(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(root, "print('ok')", timeout=0)
            self.assertEqual(result["returncode"], 0)
            result = grogu_codemode.execute(
                root, "print('ok')", timeout=10_000
            )
            self.assertEqual(result["returncode"], 0)
        finally:
            directory.cleanup()

    def test_task_create_persists_across_separate_executions(self):
        directory, root = self.make_repository()
        try:
            grogu_codemode.execute(
                root, "task_create(title='Persisted task')"
            )
            result = grogu_codemode.execute(root, "print(tasks_summary()['total'])")
            self.assertEqual(result["stdout"].strip(), "1")
        finally:
            directory.cleanup()

    def test_execute_accepts_positional_arguments_matching_documented_signature(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(
                root,
                "print(task_create('Positional title', 'body text')['title'])",
            )
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["stdout"].strip(), "Positional title")
        finally:
            directory.cleanup()

    def test_memory_remember_persists_a_graph_node(self):
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(
                root,
                "memory_remember('doc', 'README', 'top-level readme')",
            )
            self.assertEqual(result["returncode"], 0, result["stderr"])
            store = grogu_memory.MemoryStore(root)
            result = grogu_context.graph_context(store, query="README")
            names = [node["name"] for node in result["nodes"]]
            self.assertIn("README", names)
        finally:
            directory.cleanup()

    @unittest.skipIf(
        grogu_mcp.available(), "only meaningful when 'mcp' is NOT installed"
    )
    def test_mcp_functions_undefined_without_package(self):
        # No flag is needed to call mcp_call(); it's simply not bound into
        # the sandbox when the 'mcp' package isn't installed, so a script
        # that tries to use it gets an ordinary NameError, the same as
        # calling any other undefined name.
        directory, root = self.make_repository()
        try:
            result = grogu_codemode.execute(root, "mcp_call('x', 'y')")
            self.assertNotEqual(result["returncode"], 0)
            self.assertIn("NameError", result["stderr"])
        finally:
            directory.cleanup()


class CodemodeCliTests(unittest.TestCase):
    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "a@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "a"], check=True
        )
        (root / "app.py").write_text("print('one')\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True
        )
        return directory, root

    def run_cli(self, *arguments, home, input=None):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = home
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf8",
            input=input,
            env=environment,
        )

    def test_tools_and_search_subcommands(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.run_cli("codemode", "tools", home=home)
            self.assertEqual(result.returncode, 0)
            names = {tool["name"] for tool in json.loads(result.stdout)["tools"]}
            self.assertIn("git_summary", names)

            result = self.run_cli("codemode", "search", "task", home=home)
            self.assertEqual(result.returncode, 0)
            names = {tool["name"] for tool in json.loads(result.stdout)["tools"]}
            self.assertEqual(names, {"task_create", "tasks_summary"})

    def test_exec_with_inline_code_flag(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    "--code",
                    "print(git_summary()['branch'])",
                    home=home,
                )
                self.assertEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["stdout"].strip(), "main")
        finally:
            directory.cleanup()

    def test_exec_with_file_flag(self):
        directory, root = self.make_repository()
        try:
            script = root / "script.py"
            script.write_text("print(service_metadata())")
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    "--file",
                    str(script),
                    home=home,
                )
                self.assertEqual(result.returncode, 0)
                self.assertIn("manifests", json.loads(result.stdout)["stdout"])
        finally:
            directory.cleanup()

    def test_exec_reads_code_from_stdin_by_default(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    home=home,
                    input="print(tasks_summary()['total'])",
                )
                self.assertEqual(result.returncode, 0)
                self.assertEqual(json.loads(result.stdout)["stdout"].strip(), "0")
        finally:
            directory.cleanup()

    def test_exec_cli_exit_code_is_nonzero_on_script_failure(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    "--code",
                    "raise RuntimeError('nope')",
                    home=home,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RuntimeError", json.loads(result.stdout)["stderr"])
        finally:
            directory.cleanup()

    def test_exec_cli_exit_code_is_nonzero_on_timeout(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    "--timeout",
                    "1",
                    "--code",
                    "import time; time.sleep(5)",
                    home=home,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(json.loads(result.stdout)["timed_out"])
        finally:
            directory.cleanup()

    def test_generate_subcommand_writes_tool_tree(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode", "generate", "--repo", str(root), home=home
                )
                self.assertEqual(result.returncode, 0)
                directory_path = Path(json.loads(result.stdout)["tools_directory"])
                self.assertTrue(directory_path.is_dir())
                self.assertTrue((directory_path / "git_summary.py").is_file())
        finally:
            directory.cleanup()


class BannerArtTests(unittest.TestCase):
    def test_art_grid_is_rectangular_and_symmetric(self):
        for row in grogu_banner.ART_ROWS:
            self.assertEqual(len(row), grogu_banner.ART_WIDTH, row)
            mirrored = row[::-1]
            self.assertEqual(
                sorted(row), sorted(mirrored), f"row is not symmetric: {row}"
            )

    def test_every_frame_resolves_to_palette_colors(self):
        for frame in grogu_banner.EYE_FRAMES:
            for row in grogu_banner.frame_rows(frame):
                for pixel in row:
                    self.assertIn(pixel, grogu_banner.PALETTE, (frame, row))

    def test_frames_differ_only_in_the_eye_rows(self):
        base = grogu_banner.frame_rows("open")
        for frame in ("half", "closed"):
            other = grogu_banner.frame_rows(frame)
            differing = {i for i, (a, b) in enumerate(zip(base, other)) if a != b}
            self.assertTrue(differing, frame)
            self.assertLessEqual(differing, {4, 5}, frame)

    def test_rendered_mark_round_trips_back_to_the_pixel_grid(self):
        """The half blocks Copilot prints must reconstruct the source art."""
        for frame in grogu_banner.EYE_FRAMES:
            expected = [
                [grogu_banner.PALETTE[pixel] for pixel in row]
                for row in grogu_banner.frame_rows(frame)
            ]
            recovered = _decode_half_blocks(grogu_banner.render_mark(frame))
            self.assertEqual(recovered, expected, frame)

    def test_announcement_starts_on_its_own_line(self):
        payload = grogu_banner.announcement("open", "9.9.9")
        self.assertTrue(payload.startswith("\n"))
        self.assertIn("Grogu v9.9.9", payload)

    def test_announcement_uses_a_precomputed_check_phrase(self):
        payload = grogu_banner.announcement("open", check_text="Read the diff, you should.")
        self.assertIn("Read the diff, you should.", payload)

    def test_announcements_include_a_blink_frame(self):
        frames = grogu_banner.announcements("0.1.0")
        self.assertEqual(len(frames), 4)
        self.assertEqual(len(set(frames)), 2)
        self.assertTrue(
            any(f"  {sample}" in frames[0] for sample in grogu_banner.CHECK_THE_WORK_SAMPLES)
        )
        self.assertEqual(
            {
                sample
                for sample in grogu_banner.CHECK_THE_WORK_SAMPLES
                if f"  {sample}" in frames[0]
            },
            {
                sample
                for sample in grogu_banner.CHECK_THE_WORK_SAMPLES
                if f"  {sample}" in frames[3]
            },
        )

    def test_announcements_avoid_the_previous_phrase(self):
        previous = grogu_banner.CHECK_THE_WORK_SAMPLES[0]
        frames = grogu_banner.announcements("0.1.0", previous)
        self.assertNotIn(previous, frames[0])

    def test_announcement_avoids_sgr_copilot_strips(self):
        payload = grogu_banner.announcement()
        self.assertNotIn("\033[5m", payload)
        self.assertNotIn("\033[m", payload.replace("\033[0m", ""))

    def test_status_line_cycles_through_eye_states(self):
        cycle = grogu_banner.BLINK_CYCLE_SECONDS
        frames = {grogu_banner.status_line_frame(t / 4) for t in range(int(cycle) * 4)}
        self.assertEqual(len(frames), 3)


def _decode_half_blocks(lines):
    """Invert the half-block encoding back into two pixel rows per line."""
    import re

    token = re.compile(r"\033\[([0-9;]*)m")
    pixels = []
    for line in lines:
        foreground = background = None
        top, bottom = [], []
        position = 0
        for match in list(token.finditer(line)) + [None]:
            chunk = line[position : match.start()] if match else line[position:]
            for glyph in chunk:
                if glyph == "\u2588":
                    top.append(foreground)
                    bottom.append(foreground)
                elif glyph == "\u2580":
                    top.append(foreground)
                    bottom.append(background)
                elif glyph == "\u2584":
                    top.append(background)
                    bottom.append(foreground)
                else:
                    top.append(None)
                    bottom.append(None)
            if match is None:
                break
            position = match.end()
            params = [p for p in match.group(1).split(";") if p] or ["0"]
            index = 0
            while index < len(params):
                code = int(params[index])
                if code == 0:
                    foreground = background = None
                elif code == 38:
                    foreground = tuple(int(v) for v in params[index + 2 : index + 5])
                    index += 4
                elif code == 48:
                    background = tuple(int(v) for v in params[index + 2 : index + 5])
                    index += 4
                index += 1
        width = grogu_banner.ART_WIDTH
        pixels += [top[:width], bottom[:width]]
    return pixels


class BannerSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        base = Path(self.temporary.name)
        self.home = base / "copilot"
        self.state = base / "state"
        self.home.mkdir()
        self.settings = self.home / "settings.json"
        self.environment = {"COPILOT_HOME": str(self.home)}
        self.addCleanup(self.temporary.cleanup)

    def install(self, **kwargs):
        return grogu_banner.install(
            self.state, "0.1.0", self.environment, **kwargs
        )

    def test_install_adds_banner_keys_and_keeps_user_values(self):
        self.settings.write_text('{"model": "auto", "theme": "dim"}')
        self.assertTrue(self.install())
        settings = grogu_banner.load_settings(self.settings)
        self.assertEqual(settings["banner"], "always")
        self.assertEqual(settings["theme"], "dim")
        self.assertEqual(len(settings["companyAnnouncements"]), 4)

    def test_restore_returns_original_bytes_including_comments(self):
        original = '// mine\n{\n  "model": "auto", // pinned\n}\n'
        self.settings.write_text(original)
        self.install()
        self.assertNotEqual(self.settings.read_text(), original)
        self.assertTrue(grogu_banner.restore(self.state))
        self.assertEqual(self.settings.read_text(), original)

    def test_restore_removes_a_file_grogu_created(self):
        self.install()
        self.assertTrue(self.settings.exists())
        grogu_banner.restore(self.state)
        self.assertFalse(self.settings.exists())

    def test_restore_keeps_settings_changed_during_the_session(self):
        self.settings.write_text('{"banner": "once"}')
        self.install()
        live = grogu_banner.load_settings(self.settings)
        live["theme"] = "dim"
        grogu_banner.write_settings(self.settings, live)
        grogu_banner.restore(self.state)
        settings = grogu_banner.load_settings(self.settings)
        self.assertEqual(settings["theme"], "dim")
        self.assertEqual(settings["banner"], "once")
        self.assertNotIn("companyAnnouncements", settings)
        self.assertNotIn("statusLine", settings)

    def test_existing_status_line_is_never_replaced(self):
        self.settings.write_text('{"statusLine": {"command": "mine"}}')
        self.install()
        settings = grogu_banner.load_settings(self.settings)
        self.assertEqual(settings["statusLine"], {"command": "mine"})

    def test_status_line_can_be_disabled(self):
        self.install(status_line=False)
        self.assertNotIn("statusLine", grogu_banner.load_settings(self.settings))

    def test_a_second_session_defers_the_restore(self):
        self.settings.write_text('{"model": "auto"}')
        self.install()
        state_file = self.state / "banner-state.json"
        state = json.loads(state_file.read_text())
        state["holders"] = [os.getpid(), os.getpid()]
        state_file.write_text(json.dumps(state))
        self.assertFalse(grogu_banner.restore(self.state, pid=4242))
        self.assertIn("companyAnnouncements", grogu_banner.load_settings(self.settings))
        self.assertTrue(grogu_banner.restore(self.state))
        self.assertNotIn(
            "companyAnnouncements", grogu_banner.load_settings(self.settings)
        )

    def test_a_killed_session_is_repaired_on_the_next_launch(self):
        original = '{\n  "model": "auto"\n}\n'
        self.settings.write_text(original)
        self.install()
        state_file = self.state / "banner-state.json"
        state = json.loads(state_file.read_text())
        state["holders"] = [4242]
        state_file.write_text(json.dumps(state))
        self.install()
        grogu_banner.restore(self.state)
        self.assertEqual(self.settings.read_text(), original)

    def test_settings_writes_follow_a_symlink(self):
        target = Path(self.temporary.name) / "dotfiles-settings.json"
        target.write_text('{"model": "auto"}')
        self.settings.symlink_to(target)
        self.install()
        self.assertTrue(self.settings.is_symlink())
        self.assertIn("companyAnnouncements", json.loads(target.read_text()))
        grogu_banner.restore(self.state)
        self.assertTrue(self.settings.is_symlink())
        self.assertEqual(target.read_text(), '{"model": "auto"}')

    def test_strip_jsonc_leaves_string_contents_alone(self):
        text = '{"a": "http://x//y", /* c */ "b": 1} // tail'
        self.assertEqual(
            json.loads(grogu_banner.strip_jsonc(text)),
            {"a": "http://x//y", "b": 1},
        )


class BannerCommandTests(unittest.TestCase):
    def run_cli(self, *arguments):
        environment = os.environ.copy()
        with tempfile.TemporaryDirectory() as home:
            environment["GROGU_HOME"] = home
            return subprocess.run(
                [sys.executable, str(CLI), *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf8",
                env=environment,
            )

    def test_banner_show(self):
        result = self.run_cli("banner", "show")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Grogu v0.1.0", result.stdout)
        self.assertEqual(len(result.stdout.rstrip("\n").split("\n")), 7)

    def test_banner_status_line(self):
        result = self.run_cli("banner", "status-line")
        self.assertEqual(result.returncode, 0)
        self.assertIn("grogu", result.stdout)
        self.assertEqual(result.stdout.count("\n"), 1)


class PersonalMemoryTests(unittest.TestCase):
    def test_remember_is_confirmed_immediately_and_recall_is_bounded(self):
        with tempfile.TemporaryDirectory() as home:
            store = grogu_personal_memory.PersonalMemoryStore(Path(home))
            person = store.remember(
                "person", "Jamie", "Sister, lives in Denver", tags=["family"]
            )
            self.assertEqual(person["id"], "person:jamie")
            event = store.remember("event", "Denver Trip", "Visiting in March")
            store.link(person["id"], event["id"], "relates-to")
            context = store.recall(node_id=person["id"], depth=1, limit=10)
            self.assertEqual(
                {node["id"] for node in context["nodes"]},
                {"person:jamie", "event:denver-trip"},
            )
            listed = store.list(node_type="person")
            self.assertEqual([node["id"] for node in listed], ["person:jamie"])
            self.assertTrue(store.forget(person["id"]))
            self.assertEqual(store.list(node_type="person"), [])

    def test_unknown_node_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as home:
            store = grogu_personal_memory.PersonalMemoryStore(Path(home))
            with self.assertRaises(ValueError):
                store.remember("secret", "x", "y")

    def test_suggestions_require_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as home:
            store = grogu_personal_memory.PersonalMemoryStore(Path(home))
            candidate = store.suggest(
                "event",
                "Jamie Birthday",
                "Mentioned in an email thread",
                source="mail-plugin",
                confidence=0.4,
            )
            # A suggestion must never appear in the confirmed graph on its own.
            self.assertEqual(store.list(), [])
            pending = store.review()
            self.assertEqual([entry["id"] for entry in pending], [candidate["id"]])
            node = store.confirm(candidate["id"])
            self.assertEqual(node["id"], "event:jamie-birthday")
            self.assertEqual(node["provenance"][-1]["kind"], "confirmed-suggestion")
            self.assertEqual(store.review(), [])

    def test_reject_discards_without_persisting(self):
        with tempfile.TemporaryDirectory() as home:
            store = grogu_personal_memory.PersonalMemoryStore(Path(home))
            candidate = store.suggest(
                "fact", "Likes Coffee", "Mentioned liking coffee", source="conversation"
            )
            self.assertTrue(store.reject(candidate["id"]))
            self.assertEqual(store.review(), [])
            self.assertEqual(store.list(), [])
            self.assertFalse(store.reject(candidate["id"]))


class PersonalMemoryCommandTests(unittest.TestCase):
    def run_cli(self, *arguments):
        environment = os.environ.copy()
        with tempfile.TemporaryDirectory() as home:
            environment["GROGU_HOME"] = home
            return subprocess.run(
                [sys.executable, str(CLI), *arguments],
                cwd=ROOT,
                capture_output=True,
                text=True,
                encoding="utf8",
                env=environment,
            )

    def test_personal_remember_and_recall_round_trip(self):
        with tempfile.TemporaryDirectory() as home:
            environment = os.environ.copy()
            environment["GROGU_HOME"] = home

            def run(*arguments):
                return subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                    env=environment,
                )

            remembered = run(
                "personal", "remember", "--type", "person",
                "--name", "Jamie", "--summary", "Sister, lives in Denver",
            )
            self.assertEqual(remembered.returncode, 0)
            self.assertIn("person:jamie", remembered.stdout)

            recalled = run("personal", "recall", "--query", "denver")
            self.assertEqual(recalled.returncode, 0)
            self.assertIn("person:jamie", recalled.stdout)

    def test_personal_suggest_is_not_recalled_until_confirmed(self):
        with tempfile.TemporaryDirectory() as home:
            environment = os.environ.copy()
            environment["GROGU_HOME"] = home

            def run(*arguments):
                return subprocess.run(
                    [sys.executable, str(CLI), *arguments],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf8",
                    env=environment,
                )

            suggested = run(
                "personal", "suggest", "--type", "event", "--name", "Jamie Birthday",
                "--summary", "Mentioned in email", "--source", "mail-plugin",
            )
            self.assertEqual(suggested.returncode, 0)
            candidate_id = json.loads(suggested.stdout)["id"]

            recalled = run("personal", "recall")
            self.assertEqual(recalled.returncode, 0)
            self.assertNotIn("jamie-birthday", recalled.stdout)

            confirmed = run("personal", "confirm", candidate_id)
            self.assertEqual(confirmed.returncode, 0)
            self.assertIn("event:jamie-birthday", confirmed.stdout)

            recalled_after = run("personal", "recall")
            self.assertIn("event:jamie-birthday", recalled_after.stdout)


@unittest.skipUnless(
    grogu_mcp.available(), "requires Python 3.10+ with the 'mcp' package installed"
)
class GroguMcpTests(unittest.TestCase):
    """Exercises grogu_mcp.py against a tiny local stdio fixture server, so
    these tests don't depend on any real external MCP server being
    installed/configured on the machine. Skipped entirely under Grogu's
    default (older) interpreter, since it can't import ``mcp`` at all; run
    with a Python 3.10+ interpreter that has ``mcp`` installed to exercise
    them for real (e.g. ``python3.11 -m pytest tests/test_grogu_cli.py -k Mcp``).
    """

    def make_config(self):
        directory = tempfile.TemporaryDirectory()
        config_path = Path(directory.name) / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "echo": {
                            "type": "local",
                            "command": sys.executable,
                            "args": [str(FIXTURES / "mcp_echo_server.py")],
                        }
                    }
                }
            ),
            encoding="utf8",
        )
        return directory, config_path

    def setUp(self):
        self.directory, self.config_path = self.make_config()
        self._previous_config = os.environ.get("GROGU_MCP_CONFIG")
        os.environ["GROGU_MCP_CONFIG"] = str(self.config_path)

    def tearDown(self):
        grogu_mcp.close_all()
        if self._previous_config is None:
            os.environ.pop("GROGU_MCP_CONFIG", None)
        else:
            os.environ["GROGU_MCP_CONFIG"] = self._previous_config
        self.directory.cleanup()

    def test_list_servers_without_connecting(self):
        self.assertEqual(grogu_mcp.list_servers(), ["echo"])

    def test_list_tools_returns_schema(self):
        tools = {tool["name"]: tool for tool in grogu_mcp.list_tools("echo")}
        self.assertEqual(set(tools), {"echo", "add", "fail"})
        self.assertIn("properties", tools["echo"]["input_schema"])

    def test_call_tool_returns_result(self):
        self.assertEqual(grogu_mcp.call_tool("echo", "echo", text="hi"), "hi")
        self.assertEqual(grogu_mcp.call_tool("echo", "add", a=2, b=3), 5)

    def test_call_tool_propagates_errors(self):
        with self.assertRaises(RuntimeError):
            grogu_mcp.call_tool("echo", "fail")

    def test_unknown_tool_raises(self):
        with self.assertRaises(RuntimeError):
            grogu_mcp.call_tool("echo", "not_a_real_tool")

    def test_unknown_server_raises_value_error(self):
        with self.assertRaises(ValueError):
            grogu_mcp.call_tool("does-not-exist", "echo", text="hi")

    def test_session_is_reused_across_calls(self):
        # Two calls against the same server should reuse one bridge/session
        # rather than reconnecting, which matters for stateful servers.
        grogu_mcp.call_tool("echo", "echo", text="first")
        bridge_after_first = grogu_mcp._BRIDGES["echo"]
        grogu_mcp.call_tool("echo", "echo", text="second")
        self.assertIs(grogu_mcp._BRIDGES["echo"], bridge_after_first)

    def test_close_all_allows_reconnecting(self):
        grogu_mcp.call_tool("echo", "echo", text="one")
        grogu_mcp.close_all()
        self.assertEqual(grogu_mcp._BRIDGES, {})
        # A fresh call after close_all() should transparently reconnect.
        self.assertEqual(grogu_mcp.call_tool("echo", "echo", text="two"), "two")


@unittest.skipUnless(
    grogu_mcp.available(), "requires Python 3.10+ with the 'mcp' package installed"
)
class CodemodeMcpExecTests(unittest.TestCase):
    """Exercises `grogu codemode exec` calling MCP tools end to end via the
    CLI, using the same echo fixture server as GroguMcpTests. No flag is
    needed: mcp_call() etc. are always bound in when the 'mcp' package is
    importable, and simply undefined (a plain NameError) otherwise.
    """

    def make_repository(self):
        directory = tempfile.TemporaryDirectory()
        root = Path(directory.name)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "a@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "a"], check=True
        )
        (root / "app.py").write_text("print('one')\n")
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(root), "commit", "-q", "-m", "init"], check=True
        )
        return directory, root

    def setUp(self):
        self.config_directory = tempfile.TemporaryDirectory()
        self.config_path = Path(self.config_directory.name) / "mcp-config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "echo": {
                            "type": "local",
                            "command": sys.executable,
                            "args": [str(FIXTURES / "mcp_echo_server.py")],
                        }
                    }
                }
            ),
            encoding="utf8",
        )

    def tearDown(self):
        self.config_directory.cleanup()

    def run_cli(self, *arguments, home, input=None):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = home
        environment["GROGU_MCP_CONFIG"] = str(self.config_path)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf8",
            input=input,
            env=environment,
        )

    def test_exec_calls_configured_mcp_server_by_default(self):
        directory, root = self.make_repository()
        try:
            with tempfile.TemporaryDirectory() as home:
                result = self.run_cli(
                    "codemode",
                    "exec",
                    "--repo",
                    str(root),
                    "--code",
                    "print(mcp_servers()); print(mcp_call('echo', 'add', a=1, b=2))",
                    home=home,
                )
                self.assertEqual(result.returncode, 0)
                payload = json.loads(result.stdout)
                self.assertIn("echo", payload["stdout"])
                self.assertIn("3", payload["stdout"])
        finally:
            directory.cleanup()

    def test_mcp_servers_subcommand(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.run_cli("codemode", "mcp-servers", home=home)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(json.loads(result.stdout)["servers"], ["echo"])

    def test_mcp_tools_subcommand(self):
        with tempfile.TemporaryDirectory() as home:
            result = self.run_cli("codemode", "mcp-tools", "echo", home=home)
            self.assertEqual(result.returncode, 0)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["server"], "echo")
            names = {tool["name"] for tool in payload["tools"]}
            self.assertEqual(names, {"echo", "add", "fail"})


if __name__ == "__main__":
    unittest.main()


class PlanSteeringReadTests(unittest.TestCase):
    """Polling for steering must not cost the history every time."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def run_cli(self, *arguments, role=""):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = str(self.repo / "home")
        environment.pop("GROGU_AGENT", None)
        if role:
            environment["GROGU_ROLE"] = role
        else:
            environment.pop("GROGU_ROLE", None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments, "--repo", str(self.repo)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=environment,
        )

    def _plan(self):
        result = self.run_cli("plan", "new", "steering read")
        return result.stdout.strip()

    def test_a_role_scoped_read_shows_each_note_once(self):
        plan = self._plan()
        self.run_cli("plan", "steer", "--plan", plan, "--role", "engineer", "use zero for HUF")
        first = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer", role="engineer"
        )
        self.assertIn("use zero for HUF", first.stdout)
        second = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer", role="engineer"
        )
        self.assertNotIn("use zero for HUF", second.stdout)
        self.assertIn("no unread steering", second.stdout)
        replay = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer", "--all", role="engineer"
        )
        self.assertIn("use zero for HUF", replay.stdout)

    def test_the_user_looking_does_not_consume_the_note(self):
        """Checking that steering landed used to ack it for the agent."""
        plan = self._plan()
        self.run_cli("plan", "steer", "--plan", plan, "--role", "engineer", "use zero for HUF")
        for _ in range(2):
            peek = self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer")
            self.assertIn("use zero for HUF", peek.stdout)
            self.assertIn("you are looking, not consuming", peek.stdout)
        agent = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer", role="engineer"
        )
        self.assertIn("use zero for HUF", agent.stdout)

    def test_naming_a_role_to_inspect_does_not_put_the_user_on_the_board(self):
        """A person checking a note appeared as the agent they had steered."""
        plan = self._plan()
        self.run_cli("plan", "steer", "--plan", plan, "--role", "engineer", "use zero for HUF")
        self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer")
        feed = Path(self.repo) / "home" / "activity.jsonl"
        roles = {
            json.loads(line).get("role", "")
            for line in feed.read_text().splitlines()
            if line.strip()
        }
        self.assertEqual(roles, {""})

    def test_acking_for_another_agent_does_not_ack_for_this_one(self):
        plan = self._plan()
        self.run_cli("plan", "steer", "--plan", plan, "--role", "engineer", "use zero for HUF")
        proxy = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer",
            "--ack", "--agent", "s1-engineer",
        )
        self.assertIn("s1-engineer", proxy.stdout)
        status = self.run_cli("plan", "status", plan, "--json")
        payload = json.loads(status.stdout)
        self.assertEqual(payload["steering_undelivered"], [])

    def test_status_says_when_a_note_has_reached_nobody(self):
        plan = self._plan()
        self.run_cli("plan", "steer", "--plan", plan, "--role", "engineer", "use zero for HUF")
        status = self.run_cli("plan", "status", plan)
        self.assertIn("has not reached engineer", status.stdout)


class SteerNoteAliasTests(unittest.TestCase):
    """Two write commands took the note two different ways."""

    def _run(self, *arguments):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = self.home
        environment.pop("GROGU_ROLE", None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.project,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=environment,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = self.temporary.name
        self.project = self.temporary.name
        self.addCleanup(self.temporary.cleanup)

    def test_the_note_can_be_given_the_same_way_friction_takes_it(self):
        created = self._run("plan", "new", "alias")
        plan_id = created.stdout.split()[0]
        result = self._run("plan", "steer", "--plan", plan_id, "--note", "use decimal")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("recorded", result.stdout)

    def test_an_empty_note_is_refused_rather_than_recorded_blank(self):
        created = self._run("plan", "new", "alias")
        plan_id = created.stdout.split()[0]
        result = self._run("plan", "steer", "--plan", plan_id)
        self.assertEqual(result.returncode, 2)
        self.assertIn("nothing to steer", result.stderr)


class DefectResolveAliasTests(unittest.TestCase):
    """The command an engineer reaches for should be the command that works."""

    def _run(self, *arguments, role=""):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = self.home
        environment.pop("GROGU_ROLE", None)
        if role:
            environment["GROGU_ROLE"] = role
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.home,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=environment,
        )

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = self.temporary.name
        self.addCleanup(self.temporary.cleanup)
        self.plan = self._run("plan", "new", "defect alias").stdout.split()[0]

    def test_a_defect_can_be_closed_from_the_command_that_filed_it(self):
        filed = self._run(
            "plan", "defect", self.plan, "--report", "rounds the wrong way",
            "--route", "implementation", role="tester",
        )
        self.assertEqual(filed.returncode, 0, filed.stderr)
        closed = self._run(
            "plan", "defect", self.plan, "--resolve", "d1",
            "--note", "restored half-up", role="engineer",
        )
        self.assertEqual(closed.returncode, 0, closed.stderr)
        self.assertIn("d1 resolved", closed.stdout)

    def test_filing_without_a_route_says_what_is_missing(self):
        result = self._run(
            "plan", "defect", self.plan, "--report", "broken", role="tester"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--report and --route", result.stderr)


class SubcommandUsageTests(unittest.TestCase):
    """A mistyped flag printed the usage for the whole binary."""

    def test_the_usage_shown_is_the_subcommands_own(self):
        with tempfile.TemporaryDirectory() as home:
            environment = os.environ.copy()
            environment["GROGU_HOME"] = home
            result = subprocess.run(
                [sys.executable, str(CLI), "plan", "stage", "p-1",
                 "--stage", "implementation", "--state", "complete"],
                cwd=home,
                capture_output=True,
                text=True,
                encoding="utf8",
                env=environment,
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: grogu plan stage", result.stderr)
        self.assertNotIn("{doctor,", result.stderr)


class PlanIdFromEnvironmentTests(unittest.TestCase):
    """Every role prompt exports GROGU_PLAN; the commands ignored it."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.store = grogu_plans.PlanStore(self.root)
        self.plan = self.store.create("status line")["id"]
        for variable in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
            os.environ.pop(variable, None)

    def _run(self, arguments, environment=None):
        env = dict(os.environ, **(environment or {}))
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=env,
        )

    def test_a_gate_takes_its_plan_from_the_environment(self):
        result = self._run(
            ["plan", "gate", "--stage", "implement"], {"GROGU_PLAN": self.plan}
        )
        self.assertIn(self.plan, result.stdout)

    def test_a_gate_takes_the_stage_positionally(self):
        result = self._run(["plan", "gate", "implement"], {"GROGU_PLAN": self.plan})
        self.assertIn("implement", result.stdout)

    def test_a_missing_plan_says_so_rather_than_printing_usage(self):
        result = self._run(["plan", "gate", "--stage", "implement"])
        self.assertEqual(result.returncode, 2)
        self.assertIn("GROGU_PLAN", result.stderr)

    def test_a_brief_takes_its_plan_from_the_environment(self):
        result = self._run(
            ["plan", "brief", "--role", "engineer"], {"GROGU_PLAN": self.plan}
        )
        self.assertIn(self.plan, result.stdout)

    def test_the_plan_id_is_spelled_the_same_way_everywhere(self):
        # An architect's very first command failed because the spawn prompt
        # said --id and brief wanted --plan. A fresh agent with one instruction
        # and no context is the least recoverable place to be wrong, and there
        # is nothing to be gained by making it guess which of three spellings a
        # given subcommand happens to take.
        for command in (["plan", "brief", "--role", "engineer"], ["plan", "status"]):
            for spelling in (["--plan", self.plan], ["--id", self.plan], [self.plan]):
                with self.subTest(command=command[1], spelling=spelling[0]):
                    result = self._run(command + spelling)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(self.plan, result.stdout)

    def test_a_gate_takes_the_stage_and_the_plan_in_any_arrangement(self):
        # `plan gate test --plan <id>` is what a tester types, because the
        # brief teaches both halves, and it was answered with "two plans
        # given, 'test' and 'p-...'": the stage word landed in the id slot and
        # the conflict check fired before the gate could recognise it.
        for arguments in (
            ["plan", "gate", "test", "--plan", self.plan],
            ["plan", "gate", "test", self.plan],
            ["plan", "gate", "--stage", "test", self.plan],
            ["plan", "gate", self.plan, "--stage", "test"],
            ["plan", "gate", "implement", self.plan],
        ):
            with self.subTest(arguments=" ".join(arguments[2:])):
                result = self._run(arguments)
                # 0 allowed, 3 blocked; either means the command was
                # understood. 2 is the usage error this is about.
                self.assertIn(result.returncode, (0, 3), result.stderr)
                self.assertIn(self.plan, result.stdout)

    def test_naming_two_different_plans_is_refused_rather_than_guessed(self):
        result = self._run(
            ["plan", "brief", "--role", "engineer", "p-other", "--plan", self.plan]
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("name it once", result.stderr)


class AdoptedTasteIsNotReportedAsMissingTests(unittest.TestCase):
    """The user adopted Apple's set and was told nothing was learned."""

    def test_status_credits_the_set_the_user_chose(self):
        with tempfile.TemporaryDirectory() as home:
            environment = dict(os.environ, GROGU_HOME=home)
            environment.pop("GROGU_ROLE", None)
            subprocess.run(
                [sys.executable, str(CLI), "design", "seed", "--apple"],
                env=environment, capture_output=True, text=True, encoding="utf8",
                check=True,
            )
            result = subprocess.run(
                [sys.executable, str(CLI), "design", "status"],
                env=environment, capture_output=True, text=True, encoding="utf8",
                check=True,
            )
        self.assertIn("you adopted from Apple's Human Interface Guidelines", result.stdout)
        self.assertIn("nothing is owed", result.stdout)
        self.assertNotIn("seeded defaults", result.stdout)
        self.assertNotIn("learned from you", result.stdout)


class SteerTakesThePlanIdLikeEveryOtherCommandTests(unittest.TestCase):
    """`plan steer p-... --note x` recorded the plan id as the note."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def run_cli(self, *arguments):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = str(self.repo / "home")
        environment.pop("GROGU_ROLE", None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments, "--repo", str(self.repo)],
            cwd=self.repo, capture_output=True, text=True, encoding="utf8",
            env=environment,
        )

    def test_a_leading_plan_id_scopes_rather_than_becoming_the_note(self):
        plan = self.run_cli("plan", "new", "steer shape").stdout.strip()
        recorded = self.run_cli(
            "plan", "steer", plan, "--role", "engineer", "--note", "use the real feed"
        )
        self.assertIn(f"recorded for engineer on {plan}", recorded.stdout)
        shown = self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer", "--all")
        self.assertIn("use the real feed", shown.stdout)
        self.assertNotIn(f": {plan}", shown.stdout)

    def test_two_notes_at_once_is_refused_rather_than_one_dropped(self):
        plan = self.run_cli("plan", "new", "steer shape").stdout.strip()
        result = self.run_cli(
            "plan", "steer", plan, "positional note", "--note", "flag note"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("pass the note once", result.stderr)


class SupervisionIsNotWorkTests(unittest.TestCase):
    """The user steering appeared on the board as the agent they steered."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.home = str(self.repo / "home")

    def run_cli(self, *arguments, role=""):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = self.home
        environment.pop("GROGU_AGENT", None)
        if role:
            environment["GROGU_ROLE"] = role
        else:
            environment.pop("GROGU_ROLE", None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments, "--repo", str(self.repo)],
            cwd=self.repo, capture_output=True, text=True, encoding="utf8",
            env=environment,
        )

    def _roles(self):
        feed = Path(self.home) / "activity.jsonl"
        return [
            json.loads(line).get("role", "")
            for line in feed.read_text().splitlines()
            if line.strip()
        ]

    def test_a_bound_agent_does_not_lend_its_role_to_the_user(self):
        plan = self.run_cli("plan", "new", "binding").stdout.strip()
        # The engineer's brief binds the role to this working directory.
        self.run_cli("plan", "brief", "--role", "engineer", "--plan", plan, role="engineer")
        self.run_cli("plan", "steer", plan, "--note", "prefer the real feed")
        self.run_cli("watch")
        self.assertEqual(self._roles()[-2:], ["", ""])

    def test_an_agent_that_declares_itself_is_still_shown(self):
        plan = self.run_cli("plan", "new", "binding").stdout.strip()
        self.run_cli("plan", "steer", plan, "--note", "from the architect", role="architect")
        self.assertEqual(self._roles()[-1], "architect")


class ArchitectFrictionTests(unittest.TestCase):
    """Bugs a real architect hit while planning a real project.

    Every one of these was reported by an opus-5 architect agent that was
    asked to build a plan for a Washington air quality site and to treat
    anything that got in its way as a finding.
    """

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)

    def run_cli(self, *arguments, role="", agent=""):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = str(self.repo / "home")
        agent = agent or (f"{role}-agent" if role else "")
        for name, value in (("GROGU_ROLE", role), ("GROGU_AGENT", agent)):
            if value:
                environment[name] = value
            else:
                environment.pop(name, None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments, "--repo", str(self.repo)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            encoding="utf8",
            env=environment,
        )

    def _plan(self):
        return self.run_cli("plan", "new", "air quality").stdout.strip()

    def test_show_takes_the_stage_the_way_write_taught_it(self):
        """`plan write <id> testing` works, so `plan show <id> testing` must."""
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "implementation", "--body", "x" * 200, role="architect"
        )
        positional = self.run_cli("plan", "show", plan, "implementation", role="architect")
        self.assertEqual(positional.returncode, 0, positional.stderr)
        self.assertIn("x" * 20, positional.stdout)
        flagged = self.run_cli(
            "plan", "show", plan, "--stage", "implementation", role="architect"
        )
        self.assertEqual(positional.stdout, flagged.stdout)

    def test_show_names_a_bad_stage_instead_of_an_argparse_error(self):
        plan = self._plan()
        result = self.run_cli("plan", "show", plan, "implementaton", role="architect")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no stage called", result.stderr)
        self.assertIn("implementation", result.stderr)

    def test_shape_help_explains_the_audited_clear_review_inverse(self):
        result = self.run_cli("plan", "shape", "--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        help_text = " ".join(result.stdout.split())
        self.assertIn("--clear-review", help_text)
        self.assertIn("requires --why", help_text)
        self.assertIn("declared architect or --as-user", help_text)
        self.assertIn("permits --clear-review without an architect role", help_text)

    def test_mistaken_review_hold_is_recovered_on_the_same_plan(self):
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "implementation",
            "--body", "implementation body", role="architect",
        )
        self.run_cli(
            "plan", "write", plan, "testing",
            "--body", "testing body", role="architect",
        )
        self.run_cli(
            "plan", "stage", plan, "implementation", "in_progress", role="engineer"
        )
        store = grogu_plans.PlanStore(self.repo)
        stage_paths = {
            stage: store.stage_path(plan, stage)
            for stage in (grogu_plans.IMPLEMENTATION, grogu_plans.TESTING)
        }
        stage_bytes = {stage: path.read_bytes() for stage, path in stage_paths.items()}
        stage_state = dict(store.load(plan)["stage_state"])

        held = self.run_cli(
            "plan", "shape", plan, "--require-review", role="architect"
        )
        self.assertEqual(held.returncode, 0, held.stderr)
        blocked = self.run_cli("plan", "gate", plan, "--stage", "implement")
        self.assertEqual(blocked.returncode, 3)

        cleared = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why",
            "hold targeted the wrong plan", role="architect",
        )

        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        self.assertEqual(
            cleared.stdout.strip(),
            f"{plan}: cleared the unapproved review requirement; "
            "other plan state is unchanged",
        )
        self.assertEqual(
            self.run_cli("plan", "gate", plan, "--stage", "implement").returncode,
            0,
        )
        manifest = store.load(plan)
        self.assertEqual([item["id"] for item in store.list_plans()], [plan])
        self.assertEqual(manifest["stage_state"], stage_state)
        self.assertEqual(
            {stage: path.read_bytes() for stage, path in stage_paths.items()},
            stage_bytes,
        )
        event = manifest["events"][-1]
        self.assertEqual(event["event"], "review_cleared")
        self.assertEqual(event["reason"], "hold targeted the wrong plan")
        self.assertEqual(event["role"], grogu_plans.ARCHITECT)
        self.assertFalse(event["as_user"])
        self.assertTrue(event["actor"])
        self.assertTrue(event["at"])

    def test_clear_review_default_denies_but_explicit_user_succeeds(self):
        plan = self._plan()
        self.run_cli(
            "plan", "shape", plan, "--require-review", role="architect"
        )

        undeclared = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why", "wrong plan"
        )

        self.assertEqual(undeclared.returncode, 3)
        self.assertIn("--as-user", undeclared.stderr)
        store = grogu_plans.PlanStore(self.repo)
        self.assertTrue(store.load(plan)["review_required"])

        cleared = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why", "wrong plan",
            "--as-user",
        )

        self.assertEqual(cleared.returncode, 0, cleared.stderr)
        event = store.load(plan)["events"][-1]
        self.assertEqual(event["event"], "review_cleared")
        self.assertEqual(event["role"], "user")
        self.assertTrue(event["as_user"])

    def test_role_bound_agent_cannot_use_clear_review_as_user(self):
        plan = self._plan()
        self.run_cli(
            "plan", "shape", plan, "--require-review", role="architect"
        )

        refused = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why", "wrong plan",
            "--as-user", role="engineer",
        )

        self.assertEqual(refused.returncode, 3)
        self.assertIn("architect", refused.stderr)
        self.assertTrue(grogu_plans.PlanStore(self.repo).load(plan)["review_required"])

    def test_identified_engineer_cannot_clear_review_as_architect(self):
        plan = self._plan()
        self.run_cli(
            "plan", "shape", plan, "--require-review",
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )

        refused = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why", "wrong plan",
            "--role", "architect", agent="engineer-one",
        )

        self.assertEqual(refused.returncode, 3)
        self.assertIn("already bound to the engineer", refused.stderr)
        self.assertTrue(grogu_plans.PlanStore(self.repo).load(plan)["review_required"])

    def test_clear_review_refuses_missing_reason_and_non_architects(self):
        plan = self._plan()
        self.run_cli(
            "plan", "shape", plan, "--require-review", role="architect"
        )
        missing = self.run_cli(
            "plan", "shape", plan, "--clear-review", role="architect"
        )
        self.assertEqual(missing.returncode, 3)
        self.assertIn("needs a reason", missing.stderr)
        refused = self.run_cli(
            "plan", "shape", plan, "--clear-review", "--why", "mistake",
            role="engineer",
        )
        self.assertEqual(refused.returncode, 3)
        self.assertIn("architect", refused.stderr)
        self.assertTrue(grogu_plans.PlanStore(self.repo).load(plan)["review_required"])

    def test_shape_as_user_is_only_for_clear_review(self):
        plan = self._plan()
        result = self.run_cli(
            "plan", "shape", plan, "--require-review", "--as-user"
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("only valid with --clear-review", result.stderr)

    def test_two_stages_at_once_are_refused(self):
        plan = self._plan()
        result = self.run_cli(
            "plan", "show", plan, "testing", "--stage", "implementation", role="architect"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("two stages", result.stderr)

    def test_status_never_shows_an_agent_its_own_note_clipped(self):
        """The clipped supervision line is for the person, not the addressee.

        An architect read 87 characters of the note that invalidated its
        architecture through `plan status` and believed it had read the note.
        """
        plan = self._plan()
        note = "Late constraint: no build step. " + ("the rest matters too. " * 12)
        self.run_cli("plan", "steer", plan, "--role", "engineer", "--note", note)
        supervisor = self.run_cli("plan", "status", plan)
        self.assertIn("has not reached", supervisor.stdout)
        self.assertIn("...", supervisor.stdout)
        agent = self.run_cli("plan", "status", plan, role="engineer", agent="e1")
        self.assertNotIn("has not reached", agent.stdout)
        self.assertIn(note.strip(), agent.stdout)

    def test_replace_keeps_the_fields_it_was_not_given(self):
        plan = self._plan()
        self.run_cli(
            "plan", "workstream", plan, "--name", "ingest", "--path", "js/**",
            "--model", "claude-opus-5", "--review", "code-review",
            "--brief", "nulls are not zeroes",
        )
        result = self.run_cli(
            "plan", "workstream", plan, "--replace", "--name", "ingest", "--path", "js/data/**"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        streams = json.loads(
            self.run_cli("plan", "workstreams", plan, "--json").stdout
        )["workstreams"]
        ingest = next(stream for stream in streams if stream["name"] == "ingest")
        self.assertEqual(ingest["paths"], ["js/data/**"])
        self.assertEqual(ingest["model"], "claude-opus-5")
        self.assertEqual(ingest["review"], "code-review")
        self.assertEqual(ingest["brief"], "nulls are not zeroes")

    def test_replace_can_still_clear_a_field_on_purpose(self):
        plan = self._plan()
        self.run_cli(
            "plan", "workstream", plan, "--name", "ingest", "--path", "js/**",
            "--model", "claude-opus-5",
        )
        self.run_cli(
            "plan", "workstream", plan, "--replace", "--name", "ingest",
            "--path", "js/**", "--model", "",
        )
        streams = json.loads(
            self.run_cli("plan", "workstreams", plan, "--json").stdout
        )["workstreams"]
        self.assertEqual(streams[0]["model"], "")

    def test_a_workstream_can_be_withdrawn(self):
        plan = self._plan()
        self.run_cli("plan", "workstream", plan, "--name", "scaffold", "--path", "index.html")
        self.run_cli("plan", "workstream", plan, "--name", "web", "--path", "js/ui/**")
        dropped = self.run_cli("plan", "workstream", plan, "--drop", "--name", "web")
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        streams = json.loads(
            self.run_cli("plan", "workstreams", plan, "--json").stdout
        )["workstreams"]
        self.assertEqual([stream["name"] for stream in streams], ["scaffold"])

    def test_dropping_something_depended_on_is_refused(self):
        plan = self._plan()
        self.run_cli("plan", "workstream", plan, "--name", "scaffold", "--path", "index.html")
        self.run_cli(
            "plan", "workstream", plan, "--name", "web", "--path", "js/ui/**",
            "--depends-on", "scaffold",
        )
        result = self.run_cli("plan", "workstream", plan, "--drop", "--name", "scaffold")
        self.assertEqual(result.returncode, 3)
        self.assertIn("web depend", result.stderr)

    def test_dropping_an_unknown_workstream_lists_the_real_ones(self):
        plan = self._plan()
        self.run_cli("plan", "workstream", plan, "--name", "scaffold", "--path", "index.html")
        result = self.run_cli("plan", "workstream", plan, "--drop", "--name", "scafold")
        self.assertEqual(result.returncode, 3)
        self.assertIn("scaffold", result.stderr)

    def test_byte_counts_are_the_plan_as_a_person_reads_it(self):
        """A sealed stage is stored compressed; the counts must not be.

        The same write announced 13178 bytes and "replaced 379 bytes", and the
        truncation warning was comparing compression ratios.
        """
        plan = self._plan()
        first = "First testing plan. " * 400
        self.run_cli("plan", "write", plan, "testing", "--body", first, role="architect")
        second = "Second, much shorter. " * 20
        result = self.run_cli(
            "plan", "write", plan, "testing", "--body", second, "--replace", role="architect"
        )
        self.assertIn(f"({len(second)} bytes)", result.stdout)
        self.assertIn(f"replaced {len(first)} bytes", result.stdout)
        self.assertIn(f"went from {len(first)} bytes to {len(second)}", result.stderr)
        revisions = self.run_cli(
            "plan", "show", plan, "testing", "--revisions", role="architect"
        )
        self.assertIn(f"{len(first)} bytes", revisions.stdout)

    def test_a_declared_role_cannot_claim_another_one(self):
        """The seal was a norm because --role was a bare assertion."""
        plan = self._plan()
        self.run_cli("plan", "write", plan, "testing", "--body", "y" * 200, role="architect")
        impersonation = self.run_cli(
            "plan", "show", plan, "testing", "--role", "tester", role="engineer", agent="e1"
        )
        self.assertEqual(impersonation.returncode, 2)
        self.assertIn("cannot act as the tester", impersonation.stderr)
        self.assertNotIn("y" * 20, impersonation.stdout)

    def test_an_identified_agent_cannot_switch_roles_by_dropping_grogu_role(self):
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "testing", "--body", "sealed assertions " * 20,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )
        impersonation = self.run_cli(
            "plan", "show", plan, "testing", "--role", "tester",
            agent="engineer-one",
        )
        self.assertEqual(impersonation.returncode, 3)
        self.assertIn("already bound to the engineer", impersonation.stderr)
        self.assertNotIn("sealed assertions", impersonation.stdout)

    def test_role_binding_survives_a_fresh_cli_shell(self):
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "testing", "--body", "sealed assertions " * 20,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )
        impersonation = self.run_cli(
            "plan", "show", plan, "testing", "--role", "tester"
        )
        self.assertEqual(impersonation.returncode, 3)
        self.assertIn("already bound to the engineer", impersonation.stderr)
        self.assertNotIn("sealed assertions", impersonation.stdout)

    def test_impersonation_does_not_receive_the_other_roles_steering(self):
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "testing", "--body", "sealed assertions " * 20,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )
        self.run_cli(
            "plan", "steer", plan, "--role", "tester",
            "--note", "tester-only direction",
        )
        impersonation = self.run_cli(
            "plan", "show", plan, "testing",
            role="tester", agent="engineer-one",
        )
        self.assertEqual(impersonation.returncode, 3)
        self.assertNotIn(
            "tester-only direction",
            impersonation.stdout + impersonation.stderr,
        )

    def test_distinct_tester_and_architect_agents_keep_sealed_access(self):
        plan = self._plan()
        body = "sealed assertions " * 20
        self.run_cli(
            "plan", "write", plan, "testing", "--body", body,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )
        tester = self.run_cli(
            "plan", "show", plan, "testing", "--role", "tester",
            agent="tester-one",
        )
        architect = self.run_cli(
            "plan", "show", plan, "testing", "--role", "architect",
            agent="architect-two",
        )
        self.assertEqual(tester.returncode, 0, tester.stderr)
        self.assertEqual(architect.returncode, 0, architect.stderr)
        self.assertEqual(tester.stdout, body)
        self.assertEqual(architect.stdout, body)

    def test_human_as_user_still_works_after_an_engineer_brief(self):
        plan = self._plan()
        self.run_cli(
            "plan", "write", plan, "implementation", "--body", "implementation " * 20,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "write", plan, "testing", "--body", "assertions " * 20,
            role="architect", agent="architect-one",
        )
        self.run_cli(
            "plan", "brief", "--role", "engineer", "--plan", plan,
            role="engineer", agent="engineer-one",
        )
        implementation = self.run_cli(
            "plan", "stage", plan, "implementation", "complete", "--as-user"
        )
        testing = self.run_cli(
            "plan", "stage", plan, "testing", "complete", "--as-user"
        )
        finalized = self.run_cli("plan", "finalize", plan, "--as-user")
        self.assertEqual(implementation.returncode, 0, implementation.stderr)
        self.assertEqual(testing.returncode, 0, testing.stderr)
        self.assertEqual(finalized.returncode, 0, finalized.stderr)

    def test_steering_another_role_is_still_allowed(self):
        """--role names a subject on the steering commands, not the caller."""
        plan = self._plan()
        result = self.run_cli(
            "plan", "steer", plan, "--role", "tester", "--note", "check nulls",
            role="engineer", agent="e1",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_polling_for_steering_does_not_print_it_twice(self):
        """The banner delivers to commands that were about something else.

        `plan steering` already prints the notes, so the banner doubled them
        in one response -- on the single path built to be token-efficient.
        """
        plan = self._plan()
        self.run_cli("plan", "steer", plan, "--role", "engineer", "--note", "one note only")
        poll = self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer",
            role="engineer", agent="e1",
        )
        self.assertEqual(poll.stdout.count("one note only"), 1, poll.stdout)

    def test_status_does_not_tell_the_user_notes_are_unread_forever(self):
        """The pending count is per-caller; the user is not the addressee.

        `plan status` said "1 unread steering note(s) for the engineer" and the
        count never fell when the engineer read it, because what it actually
        measured was that the user's own shell had not.
        """
        plan = self._plan()
        self.run_cli("plan", "steer", plan, "--role", "engineer", "--note", "read me")
        before = self.run_cli("plan", "status", plan)
        self.assertIn("has not reached", before.stdout)
        self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "engineer",
            role="engineer", agent="e1",
        )
        after = self.run_cli("plan", "status", plan)
        self.assertNotIn("unread steering note", after.stdout)
        self.assertNotIn("has not reached", after.stdout)

    def test_orchestrators_can_still_read_the_pending_count(self):
        plan = self._plan()
        self.run_cli("plan", "steer", plan, "--role", "engineer", "--note", "read me")
        payload = json.loads(self.run_cli("plan", "status", plan, "--json").stdout)
        self.assertEqual(payload["steering_pending"]["engineer"], 1)


class DesignerFrictionTests(ArchitectFrictionTests):
    """Bugs a real designer hit while writing a real design spec."""

    def test_plan_can_be_named_with_a_flag_everywhere(self):
        """`--plan` is taught by brief/steering, then rejected by status/gate."""
        plan = self._plan()
        for command in (
            ["plan", "status", "--plan", plan],
            ["plan", "workstreams", "--plan", plan],
            ["plan", "gate", "--plan", plan, "--stage", "implement"],
        ):
            result = self.run_cli(*command)
            self.assertNotIn("unrecognized arguments", result.stderr, command)
            self.assertNotIn("usage:", result.stderr, command)

    def test_two_plans_at_once_are_refused(self):
        result = self.run_cli("plan", "status", "p-aaaaaaaa-aaaaaa", "--plan", "p-bbbbbbbb-bbbbbb")
        self.assertEqual(result.returncode, 2)
        self.assertIn("two plans", result.stderr)

    def test_complete_is_spelled_the_way_roles_reach_for_it(self):
        plan = self._plan()
        self.run_cli("plan", "write", plan, "implementation", "--body", "z" * 200, role="architect")
        result = self.run_cli("plan", "complete", plan, "implementation", role="engineer")
        self.assertEqual(result.returncode, 0, result.stderr)
        status = self.run_cli("plan", "status", plan)
        self.assertIn("implementation=complete", status.stdout)

    def test_complete_names_a_bad_stage(self):
        plan = self._plan()
        result = self.run_cli("plan", "complete", plan, "implementaton", role="engineer")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no stage called", result.stderr)

    def test_a_relayed_note_can_be_audited_without_consuming_it(self):
        """Being told "you were acked" is only useful if it is checkable."""
        plan = self._plan()
        self.run_cli("plan", "steer", plan, "--role", "designer", "--note", "relayed by hand")
        self.run_cli(
            "plan", "steering", "--plan", plan, "--role", "designer",
            "--ack", "--agent", "d1",
        )
        audit = self.run_cli(
            "plan", "steering", "--plan", plan, "--audit", "1", role="designer", agent="d1"
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.assertIn("designer@d1", audit.stdout)
        self.assertIn("read by", audit.stdout)
        second = self.run_cli(
            "plan", "steering", "--plan", plan, "--audit", "1", role="designer", agent="d1"
        )
        self.assertIn("designer@d1", second.stdout)

    def test_auditing_an_unknown_note_says_how_to_list_them(self):
        plan = self._plan()
        result = self.run_cli("plan", "steering", "--plan", plan, "--audit", "42")
        self.assertEqual(result.returncode, 3)
        self.assertIn("--all", result.stderr)

    def test_replacing_a_commission_reopens_work_done_against_the_old_one(self):
        plan = self._plan()
        self.run_cli("plan", "shape", plan, "--add", "design", role="architect")
        self.run_cli("plan", "commission", plan, "designer", "--brief", "first brief", role="architect")
        self.run_cli("plan", "write", plan, "design", "--file", self._spec(), role="designer")
        self.run_cli("plan", "complete", plan, "design", role="designer")
        result = self.run_cli(
            "plan", "commission", plan, "designer", "--replace",
            "--brief", "the map is out of scope now", role="architect",
        )
        self.assertIn("pending again", result.stderr)
        status = self.run_cli("plan", "status", plan)
        self.assertIn("design=pending", status.stdout)

    def test_recommissioning_the_same_brief_changes_nothing(self):
        plan = self._plan()
        self.run_cli("plan", "shape", plan, "--add", "design", role="architect")
        self.run_cli("plan", "commission", plan, "designer", "--brief", "same brief", role="architect")
        self.run_cli("plan", "write", plan, "design", "--file", self._spec(), role="designer")
        self.run_cli("plan", "complete", plan, "design", role="designer")
        result = self.run_cli(
            "plan", "commission", plan, "designer", "--replace",
            "--brief", "same brief", role="architect",
        )
        self.assertNotIn("pending again", result.stderr)
        self.assertIn("design=complete", self.run_cli("plan", "status", plan).stdout)

    def _spec(self):
        """A design spec concrete enough that the harness accepts it."""
        path = self.repo / "spec.md"
        path.write_text(
            "# Reading — design\n\n"
            "## Surfaces\n- #/ the current reading for one area, the whole "
            "product.\n- #/area/<slug> one area's detail with a 7-day series.\n"
            "- #/pick the area picker listing all 59 reporting areas.\n\n"
            "## Hierarchy\nPrimary action is the reading itself; the picker is "
            "secondary and sits below the fold. Nothing destructive.\n\n"
            "## States\nDefault shows the number. Empty reads 'No reading this "
            "hour'. Loading shows the last cached number. Error reads 'Could "
            "not reach the sensors'.\n\n"
            "## Flow\nOpen, read, optionally pick another area. Cancel returns "
            "to the reading; failure keeps the cached number on screen.\n\n"
            "## Copy\nHeading: 'Air quality'. Button: 'Choose an area'. Error: "
            "'Could not reach the sensors'. Voice is plain and unhurried: "
            "'Updated 4 hours ago', not 'Data staleness: 4h'.\n\n"
            "## Tokens\n--space-2: 8px and --space-4: 16px on a 4px scale. "
            "Type ramp 13px/17px/20px/128px, weights 400 and 700. Accent "
            "#0066CC means interaction. Radius 12px. Motion 200ms.\n\n"
            "## Accessibility\nNumber contrast 5.9:1 on #FFFFFF and 7.1:1 on "
            "#1C1C1E. Full keyboard path through the picker, focus order top to "
            "bottom, reduced-motion disables the 200ms fade, dynamic type wraps "
            "at 320px. The number carries an aria-label naming the category.\n\n"
            "## Layout\nAt 375x812, with 16px gutters:\n\n"
            "```\n"
            "+-----------------------------+\n"
            "|                             |\n"
            "|            142              |  128px/700, category colour\n"
            "|     Unhealthy for some      |  20px/400, neutral ink\n"
            "|   Seattle - Duwamish 3.2km  |  17px/400\n"
            "|      Updated 1 hour ago     |  13px/400, secondary ink\n"
            "|                             |\n"
            "|      [ Choose an area ]     |  44px tall, accent #0066CC\n"
            "+-----------------------------+\n"
            "```\n\n"
            "## Acceptance criteria\n"
            "- A missing reading renders the em dash and 'No reading this "
            "hour', never the digit 0.\n"
            "- Every text colour measures at least 4.5:1 against its own "
            "background in both colour schemes.\n"
            "- The reading is visible at 375x812 without scrolling and without "
            "any network call beyond the first JSON fetch.\n"
            "- Every interactive target measures at least 44x44px.\n\n"
            "## Left to the engineer\nThe sparkline smoothing algorithm, the "
            "exact wrap breakpoint for dynamic type above 320px, the picker's "
            "search match strategy, and whether the cached reading is held in "
            "localStorage or in memory only. None of these change what the "
            "screen looks like, so they are implementation calls.\n",
            encoding="utf8",
        )
        return str(path)


class SupervisorRoleTests(ArchitectFrictionTests):
    """The role that coordinates the others, and what it may not do."""

    def test_the_supervisor_may_not_approve_for_the_user(self):
        plan = self._plan()
        result = self.run_cli("plan", "approve", plan, role="supervisor", agent="sup")
        self.assertEqual(result.returncode, 3)
        self.assertIn("approval is the user's alone", result.stderr)

    def test_the_supervisor_may_not_write_a_stage(self):
        plan = self._plan()
        result = self.run_cli(
            "plan", "write", plan, "implementation", "--body", "q" * 200,
            role="supervisor", agent="sup",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("may not write", result.stderr)

    def test_a_relayed_note_is_marked_as_the_users_words(self):
        plan = self._plan()
        result = self.run_cli(
            "plan", "steer", plan, "--role", "designer", "--relayed",
            "--note", "the user's actual words", role="supervisor", agent="sup",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("in the user's words", result.stdout)
        brief = self.run_cli(
            "plan", "brief", "--plan", plan, "--role", "designer",
            role="designer", agent="d1",
        )
        self.assertIn("(the user, relayed by the supervisor)", brief.stdout)

    def test_the_supervisors_own_note_is_attributed_to_the_supervisor(self):
        plan = self._plan()
        self.run_cli(
            "plan", "steer", plan, "--role", "designer", "--note", "my own opinion",
            role="supervisor", agent="sup",
        )
        brief = self.run_cli(
            "plan", "brief", "--plan", plan, "--role", "designer",
            role="designer", agent="d1",
        )
        self.assertIn("(the supervisor) my own opinion", brief.stdout)

    def test_only_the_supervisor_may_claim_a_relay(self):
        plan = self._plan()
        for role in ("", "engineer"):
            result = self.run_cli(
                "plan", "steer", plan, "--role", "designer", "--relayed",
                "--note", "not mine to relay", role=role,
            )
            self.assertEqual(result.returncode, 3, role)
            self.assertIn("only the supervisor relays", result.stderr)

    def test_the_audit_says_when_each_agent_was_last_seen(self):
        """Listing hour-old probes with no context read as a scoping bug."""
        plan = self._plan()
        self.run_cli("plan", "steer", plan, "--role", "engineer", "--note", "check me")
        self.run_cli("plan", "status", plan, role="engineer", agent="live")
        audit = self.run_cli("plan", "steering", "--plan", plan, "--audit", "1")
        self.assertIn("engineer@live", audit.stdout)
        self.assertIn("last seen", audit.stdout)


class PlanGovernanceCliTests(unittest.TestCase):
    def test_workstream_accepts_every_repeated_review(self):
        args = grogu_cli.build_parser().parse_args(
            [
                "plan",
                "workstream",
                "p-demo",
                "--name",
                "api",
                "--path",
                "src/api/**",
                "--review",
                "code-review",
                "--review",
                "security-review",
            ]
        )
        store = mock.Mock()
        store.resolve.return_value = "p-demo"
        store.add_workstream.return_value = {
            "name": "api",
            "paths": ["src/api/**"],
            "model": "",
            "required_reviews": ["code-review", "security-review"],
        }
        with (
            mock.patch.object(grogu_cli, "plan_store", return_value=store),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(args.handler(args), 0)
        self.assertEqual(
            store.add_workstream.call_args.kwargs["required_reviews"],
            ["code-review", "security-review"],
        )

    def test_plan_write_forwards_the_expected_base_digest(self):
        args = grogu_cli.build_parser().parse_args(
            [
                "plan",
                "write",
                "p-demo",
                "implementation",
                "--body",
                "# Implementation\n\nBuild it.\n",
                "--base",
                "sha256:abc",
            ]
        )
        store = mock.Mock()
        store.resolve.return_value = "p-demo"
        store.write_stage.return_value = {
            "warnings": [],
            "last_write": {"digest": "sha256:def"},
        }
        with (
            mock.patch.object(grogu_cli, "plan_store", return_value=store),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            self.assertEqual(args.handler(args), 0)
        self.assertEqual(
            store.write_stage.call_args.kwargs["base"], "sha256:abc"
        )

    def test_plan_write_dry_run_is_forwarded_and_reported(self):
        args = grogu_cli.build_parser().parse_args(
            [
                "plan",
                "write",
                "p-demo",
                "implementation",
                "--body",
                "# Implementation\n\nBuild it.\n",
                "--dry-run",
            ]
        )
        store = mock.Mock()
        store.resolve.return_value = "p-demo"
        store.is_document_plan.return_value = True
        store.write_stage.return_value = {
            "warnings": [],
            "dry_run": True,
            "document_write": {"dry_run": True},
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(grogu_cli, "plan_store", return_value=store),
            mock.patch("sys.stdout", stdout),
        ):
            self.assertEqual(args.handler(args), 0)
        self.assertTrue(store.write_stage.call_args.kwargs["dry_run"])
        self.assertIn("would write implementation plan", stdout.getvalue())
        self.assertIn("dry run: nothing was written", stdout.getvalue())

    def test_plan_write_dry_run_json_is_machine_readable(self):
        args = grogu_cli.build_parser().parse_args(
            [
                "plan",
                "write",
                "p-demo",
                "implementation",
                "--body",
                "# Implementation\n\nBuild it.\n",
                "--dry-run",
                "--json",
            ]
        )
        store = mock.Mock()
        store.resolve.return_value = "p-demo"
        store.is_document_plan.return_value = True
        store.write_stage.return_value = {
            "warnings": [],
            "dry_run": True,
            "document_write": {
                "base": "r0001",
                "changed": ["note-1"],
                "revision": "r0002",
                "stages": ["implementation"],
            },
            "last_write": {"digest": "sha256:preview"},
        }
        stdout = io.StringIO()
        with (
            mock.patch.object(grogu_cli, "plan_store", return_value=store),
            mock.patch("sys.stdout", stdout),
        ):
            self.assertEqual(args.handler(args), 0)
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["base"], "r0001")
        self.assertEqual(result["digest"], "sha256:preview")
        self.assertEqual(result["stage"], "implementation")

    def test_governance_commands_are_exposed(self):
        parser = grogu_cli.build_parser()
        cases = {
            "writer": ["plan", "writer", "p-demo", "implementation"],
            "agent-budget": [
                "plan",
                "agent-budget",
                "p-demo",
                "--tool-calls",
                "40",
            ],
            "agent-usage": [
                "plan",
                "agent-usage",
                "p-demo",
                "--tool-calls",
                "12",
            ],
            "checkpoint": ["plan", "checkpoint", "p-demo"],
            "checkpoint-recovery": [
                "plan",
                "checkpoint-recovery",
                "p-demo",
                "c1",
                "--status",
                "available",
            ],
            "governance": ["plan", "governance", "p-demo"],
        }
        for command, arguments in cases.items():
            with self.subTest(command=command):
                parsed = parser.parse_args(arguments)
                self.assertTrue(callable(parsed.handler))


class WorkstreamWorktreeCliTests(unittest.TestCase):
    """Harness friction #40, end to end: `grogu plan workstream-worktree`
    is the concrete replacement for "fan out into worktrees by convention",
    and its output has to be directly usable, not just informative."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=self.repo, check=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("hello\n")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=self.repo, check=True)

    def run_cli(self, *arguments, role=""):
        environment = os.environ.copy()
        environment["GROGU_HOME"] = str(self.repo / "home")
        if role:
            environment["GROGU_ROLE"] = role
        else:
            environment.pop("GROGU_ROLE", None)
        return subprocess.run(
            [sys.executable, str(CLI), *arguments, "--repo", str(self.repo)],
            cwd=self.repo,
            capture_output=True,
            text=True,
            env=environment,
        )

    def _plan_with_workstreams(self):
        plan = self.run_cli("plan", "new", "air quality").stdout.strip()
        self.run_cli(
            "plan", "workstream", plan, "--name", "scaffold", "--path", "src/scaffold/**",
            role="architect",
        )
        self.run_cli(
            "plan", "workstream", plan, "--name", "ingest", "--path", "src/ingest/**",
            role="architect",
        )
        return plan

    def test_creating_a_worktree_prints_a_directly_usable_cd_line(self):
        plan = self._plan_with_workstreams()
        result = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", role="engineer"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        last_line = result.stdout.strip().splitlines()[-1]
        self.assertTrue(last_line.startswith("cd "))
        path = Path(last_line[len("cd "):])
        self.assertTrue(path.is_dir())
        self.assertNotEqual(path.resolve(), self.repo.resolve())

    def test_json_output_reports_path_branch_and_created(self):
        plan = self._plan_with_workstreams()
        result = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
            role="engineer",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["name"], "scaffold")
        self.assertTrue(payload["created"])
        self.assertIn("workstream/", payload["branch"])
        self.assertTrue(Path(payload["path"]).is_dir())

    def test_two_workstreams_never_share_a_checkout(self):
        plan = self._plan_with_workstreams()
        scaffold = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
                role="engineer",
            ).stdout
        )
        ingest = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "ingest", "--json",
                role="engineer",
            ).stdout
        )
        self.assertNotEqual(scaffold["path"], ingest["path"])

        # The exact friction #40 scenario: a scratch file dropped by one
        # workstream must not appear in the other, or in the shared repo.
        Path(scaffold["path"], ".hourly_test.dat").write_text("scratch\n")
        self.assertFalse((Path(ingest["path"]) / ".hourly_test.dat").exists())
        self.assertFalse((self.repo / ".hourly_test.dat").exists())

    def test_repeat_call_reuses_the_same_worktree(self):
        plan = self._plan_with_workstreams()
        first = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
                role="engineer",
            ).stdout
        )
        second = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
                role="engineer",
            ).stdout
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["path"], second["path"])

    def test_undeclared_workstream_is_a_plan_error(self):
        plan = self.run_cli("plan", "new", "air quality").stdout.strip()
        result = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "no-such-workstream",
            role="engineer",
        )
        self.assertEqual(result.returncode, 3)
        self.assertIn("no workstream", result.stderr)

    def test_name_is_required_unless_listing(self):
        plan = self._plan_with_workstreams()
        result = self.run_cli("plan", "workstream-worktree", plan, role="engineer")
        self.assertEqual(result.returncode, 2)
        self.assertIn("--name", result.stderr)
        self.assertIn("--list", result.stderr)

    def test_list_reports_every_worktree_created_so_far(self):
        plan = self._plan_with_workstreams()
        self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", role="engineer"
        )
        result = self.run_cli("plan", "workstream-worktree", plan, "--list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        names = {entry["name"] for entry in payload["worktrees"]}
        self.assertEqual(names, {"scaffold"})

    def test_remove_cleans_up_the_worktree(self):
        plan = self._plan_with_workstreams()
        created = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
                role="engineer",
            ).stdout
        )
        result = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", "--remove", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["removed"], created["path"])
        self.assertFalse(Path(created["path"]).exists())

    def test_remove_is_idempotent(self):
        plan = self._plan_with_workstreams()
        self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", role="engineer"
        )
        self.run_cli("plan", "workstream-worktree", plan, "--name", "scaffold", "--remove")
        second = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", "--remove",
        )
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("no worktree to remove", second.stdout)

    def test_plan_workstreams_hints_at_the_command_before_a_worktree_exists(self):
        plan = self._plan_with_workstreams()
        result = self.run_cli("plan", "workstreams", plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("not created yet -- grogu plan workstream-worktree", result.stdout)

    def test_plan_workstreams_shows_the_path_once_created(self):
        plan = self._plan_with_workstreams()
        created = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "scaffold", "--json",
                role="engineer",
            ).stdout
        )
        result = self.run_cli("plan", "workstreams", plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(created["path"], result.stdout)

    def test_plan_workstreams_json_includes_worktree_fields(self):
        plan = self._plan_with_workstreams()
        self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "scaffold", role="engineer"
        )
        payload = json.loads(self.run_cli("plan", "workstreams", plan, "--json").stdout)
        by_name = {stream["name"]: stream for stream in payload["workstreams"]}
        self.assertTrue(by_name["scaffold"]["worktree_ready"])
        self.assertFalse(by_name["ingest"]["worktree_ready"])
        self.assertTrue(by_name["scaffold"]["worktree"])

    def _plan_with_a_multi_word_workstream(self):
        plan = self.run_cli("plan", "new", "air quality").stdout.strip()
        self.run_cli(
            "plan", "workstream", plan, "--name", "api gateway", "--path", "src/api/**",
            role="architect",
        )
        return plan

    def test_multi_word_workstream_name_can_be_created_and_is_listed_by_its_raw_name(self):
        """Regression: a multi-word name used to round-trip through its
        slugified branch (`api-gateway`), so a freshly created worktree for
        it was reported as belonging to an undeclared workstream."""
        plan = self._plan_with_a_multi_word_workstream()
        created = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "api gateway", "--json",
                role="engineer",
            ).stdout
        )
        self.assertTrue(created["created"])
        self.assertEqual(created["name"], "api gateway")

        payload = json.loads(
            self.run_cli("plan", "workstream-worktree", plan, "--list", "--json").stdout
        )
        by_name = {entry["name"]: entry for entry in payload["worktrees"]}
        self.assertIn("api gateway", by_name)
        self.assertTrue(by_name["api gateway"]["declared"])

    def test_multi_word_workstream_name_list_human_output_is_not_flagged_undeclared(self):
        plan = self._plan_with_a_multi_word_workstream()
        self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "api gateway", role="engineer"
        )
        result = self.run_cli("plan", "workstream-worktree", plan, "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("api gateway:", result.stdout)
        self.assertNotIn("no longer declared", result.stdout)

    def test_plan_workstreams_worktree_ready_is_true_for_a_multi_word_name(self):
        plan = self._plan_with_a_multi_word_workstream()
        self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "api gateway", role="engineer"
        )
        payload = json.loads(self.run_cli("plan", "workstreams", plan, "--json").stdout)
        by_name = {stream["name"]: stream for stream in payload["workstreams"]}
        self.assertTrue(by_name["api gateway"]["worktree_ready"])
        self.assertTrue(by_name["api gateway"]["worktree"])

    def test_plan_workstreams_human_output_shows_the_path_for_a_multi_word_name(self):
        plan = self._plan_with_a_multi_word_workstream()
        created = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "api gateway", "--json",
                role="engineer",
            ).stdout
        )
        result = self.run_cli("plan", "workstreams", plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(created["path"], result.stdout)
        self.assertNotIn("not created yet", result.stdout)

    def test_plan_workstreams_human_hint_safely_quotes_a_multi_word_name(self):
        """The remediation line is meant to be copy-pasted into a shell; an
        unquoted multi-word name would be split into extra positional
        arguments there instead of being read as a single --name value."""
        plan = self._plan_with_a_multi_word_workstream()
        result = self.run_cli("plan", "workstreams", plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--name 'api gateway'", result.stdout)
        hint = next(
            line for line in result.stdout.splitlines() if "workstream-worktree" in line
        )
        self.assertEqual(shlex.split(hint)[-2:], ["--name", "api gateway"])

    def test_remove_cleans_up_the_worktree_for_a_multi_word_name(self):
        plan = self._plan_with_a_multi_word_workstream()
        created = json.loads(
            self.run_cli(
                "plan", "workstream-worktree", plan, "--name", "api gateway", "--json",
                role="engineer",
            ).stdout
        )
        result = self.run_cli(
            "plan", "workstream-worktree", plan, "--name", "api gateway", "--remove", "--json",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["removed"], created["path"])
        self.assertFalse(Path(created["path"]).exists())


class ReviewCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Review Tester"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=self.repo, check=True)
        self.home = str(self.repo / "home")

    def run_main(self, *arguments, role="", agent="", plan_env=""):
        if not agent:
            import secrets
            agent = f"cli-agent-{secrets.token_hex(4)}"
        env_patches = {
            "GROGU_HOME": self.home,
            "GROGU_AGENT": agent,
        }
        if role:
            env_patches["GROGU_ROLE"] = role
        else:
            env_patches["GROGU_ROLE"] = ""
        if plan_env:
            env_patches["GROGU_PLAN"] = plan_env
        else:
            env_patches["GROGU_PLAN"] = ""

        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, env_patches):
            for k in ("GROGU_ROLE", "GROGU_PLAN"):
                if not env_patches.get(k):
                    os.environ.pop(k, None)
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                try:
                    code = grogu_cli.main(list(arguments) + ["--repo", str(self.repo)])
                except SystemExit as exit_err:
                    code = exit_err.code if isinstance(exit_err.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def test_review_in_grogu_commands(self):
        self.assertIn("review", grogu_cli.GROGU_COMMANDS)

    def test_review_subcommands_parse_positional_and_flag_and_env(self):
        code, out, err = self.run_main("plan", "new", "CLI subcommands")
        self.assertEqual(code, 0, err)
        plan = out.strip()

        code, _, err = self.run_main("plan", "write", plan, "implementation", "--body", "# Implementation\nBody\n", role="architect", agent=f"arch-{plan}")
        self.assertEqual(code, 0, err)

        # Positional
        code, out_pos, err = self.run_main("review", "list", plan, "--json")
        self.assertEqual(code, 0, err)
        data_pos = json.loads(out_pos)
        self.assertEqual(data_pos["plan"], plan)

        # Flag --plan
        code, out_flag, err = self.run_main("review", "list", "--plan", plan, "--json")
        self.assertEqual(code, 0, err)
        data_flag = json.loads(out_flag)
        self.assertEqual(data_flag["plan"], plan)

        # Environment GROGU_PLAN
        code, out_env, err = self.run_main("review", "list", "--json", plan_env=plan)
        self.assertEqual(code, 0, err)
        data_env = json.loads(out_env)
        self.assertEqual(data_env["plan"], plan)

        # Status positional and flag
        code, out_st, err = self.run_main("review", "status", plan, "--json")
        self.assertEqual(code, 0, err)
        data_st = json.loads(out_st)
        self.assertEqual(data_st["plan"], plan)
        self.assertIn("summary", data_st)

    def test_review_errors_and_exit_codes(self):
        code, out, err = self.run_main("plan", "new", "CLI errors")
        plan = out.strip()
        self.run_main("plan", "write", plan, "implementation", "--body", "# Implementation\nFirst line with duplicate duplicate.\n", role="architect", agent=f"arch-{plan}")

        # Quote absent from stage exits non-zero (3 for ReviewError) and names stage
        code_absent, out, err_absent = self.run_main("review", "comment", plan, "--stage", "implementation", "--quote", "nonexistent text", "--body", "comment")
        self.assertEqual(code_absent, 3)
        self.assertIn("implementation", err_absent)

        # Quote occurring twice exits non-zero and names count 2
        code_dup, out, err_dup = self.run_main("review", "comment", plan, "--stage", "implementation", "--quote", "duplicate", "--body", "comment")
        self.assertEqual(code_dup, 3)
        self.assertIn("2", err_dup)

        # Usage error exits 2
        code_usage, out, err_usage = self.run_main("review", "comment", plan, "--invalid-flag")
        self.assertEqual(code_usage, 2)

    def test_plan_status_and_brief_integration_with_review(self):
        code, out, _ = self.run_main("plan", "new", "No review")
        plan_no_rev = out.strip()
        self.run_main("plan", "write", plan_no_rev, "implementation", "--body", "# Implementation\nBody\n", role="architect", agent=f"arch-{plan_no_rev}")
        _, status_no_rev, _ = self.run_main("plan", "status", plan_no_rev)
        self.assertNotIn("review:", status_no_rev)

        _, brief_no_rev, _ = self.run_main("plan", "brief", "--role", "architect", "--plan", plan_no_rev, role="architect", agent=f"arch-{plan_no_rev}")
        self.assertNotIn("[c1 implementation anchored]", brief_no_rev)

        # Plan with thread
        code, out, _ = self.run_main("plan", "new", "With review")
        plan_rev = out.strip()
        self.run_main("plan", "write", plan_rev, "implementation", "--body", "# Implementation\nUnique quote in body\n", role="architect", agent=f"arch-{plan_rev}")
        code_c, out_c, err_c = self.run_main("review", "comment", plan_rev, "--stage", "implementation", "--quote", "Unique quote", "--body", "Please fix")
        self.assertEqual(code_c, 0, err_c)

        _, status_rev, _ = self.run_main("plan", "status", plan_rev)
        self.assertIn("review:", status_rev)
        self.assertIn("round 1", status_rev)
        self.assertIn("1 open", status_rev)

        _, brief_rev, _ = self.run_main("plan", "brief", "--role", "architect", "--plan", plan_rev, role="architect", agent=f"arch-{plan_rev}")
        self.assertIn("[c1 implementation anchored]", brief_rev)

    def test_review_assets_cli(self):
        code, out, err = self.run_main("review", "assets")
        self.assertEqual(code, 0, err)

        fixture_file = self.repo / "test_mermaid.js"
        fixture_file.write_bytes(b"mermaid dummy bundle")

        # Wrong digest refuses
        with mock.patch("grogu_review_server.MERMAID_SHA256", "wrong_expected_digest"):
            code_bad, out, err_bad = self.run_main("review", "assets", "--install", "--from", str(fixture_file))
            self.assertNotEqual(code_bad, 0)

        # Correct digest installs
        expected_digest = hashlib.sha256(b"mermaid dummy bundle").hexdigest()
        with mock.patch("grogu_review_server.MERMAID_SHA256", expected_digest):
            code_good, out_good, err_good = self.run_main("review", "assets", "--install", "--from", str(fixture_file))
            self.assertEqual(code_good, 0, err_good)
            code_p, out_p, _ = self.run_main("review", "assets", "--json")
            data_present = json.loads(out_p)
            self.assertTrue(data_present.get("mermaid"))


class PlanDocumentCliTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        self.home = str(self.repo / "home")

    def run_main(self, *arguments, role="", agent=""):
        import secrets

        agent = agent or f"doc-cli-{secrets.token_hex(4)}"
        environment = {
            "GROGU_HOME": self.home,
            "GROGU_AGENT": agent,
            "GROGU_ROLE": role,
            "GROGU_PLAN": "",
        }
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment):
            if not role:
                os.environ.pop("GROGU_ROLE", None)
            with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
                try:
                    code = grogu_cli.main(
                        list(arguments) + ["--repo", str(self.repo)]
                    )
                except SystemExit as error:
                    code = error.code if isinstance(error.code, int) else 1
        return code, stdout.getvalue(), stderr.getvalue()

    def create(self):
        code, output, error = self.run_main(
            "plan", "doc", "create", "CLI document"
        )
        self.assertEqual(code, 0, error)
        return output.strip()

    def test_create_node_query_compile_and_export_surfaces(self):
        plan = self.create()
        code, output, error = self.run_main(
            "plan",
            "doc",
            "node",
            "add",
            plan,
            "--kind",
            "task",
            "--title",
            "First task",
            "--stage",
            "implementation",
            role="reviewer",
        )
        self.assertEqual(code, 0, error)
        self.assertIn("task-1", output)
        code, output, error = self.run_main(
            "plan",
            "doc",
            "query",
            plan,
            "--node",
            "task-1",
            "--json",
            role="reviewer",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["nodes"][0]["title"], "First task")
        code, _output, error = self.run_main(
            "plan", "doc", "compile", plan, "--check", role="reviewer"
        )
        self.assertEqual(code, 0, error)
        destination = self.repo / "projection.md"
        code, output, error = self.run_main(
            "plan",
            "doc",
            "export",
            plan,
            "--stage",
            "implementation",
            "--output",
            str(destination),
            role="reviewer",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(Path(output.strip()), destination.resolve())
        self.assertIn("First task", destination.read_text(encoding="utf8"))

    def test_migrate_dry_run_and_actual_are_explicit_and_lossless(self):
        code, output, error = self.run_main("plan", "new", "Legacy CLI")
        self.assertEqual(code, 0, error)
        plan = output.strip()
        self.run_main(
            "plan",
            "write",
            plan,
            "implementation",
            "--body",
            "# Legacy\n\nKeep this prose.\n",
            role="architect",
            agent="legacy-architect",
        )
        code, output, error = self.run_main(
            "plan", "doc", "migrate", plan, "--dry-run", "--json",
            role="reviewer",
        )
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["dry_run"])
        self.assertTrue((self.repo / ".grogu" / "plans" / plan).is_dir())
        code, output, error = self.run_main(
            "plan", "doc", "migrate", plan, "--json", role="reviewer"
        )
        self.assertEqual(code, 0, error)
        result = json.loads(output)
        self.assertFalse(result["dry_run"])
        package = self.repo / ".grogu" / "plans" / f"{plan}.plan"
        self.assertTrue(package.is_dir())
        self.assertIn(
            "Keep this prose",
            (
                package
                / "legacy"
                / "pre-migration"
                / "implementation.md"
            ).read_text(encoding="utf8"),
        )

    def test_plan_write_refuses_compiled_artifact_as_source(self):
        plan = self.create()
        artifact = (
            self.repo / ".grogu" / "plans" / f"{plan}.plan" / "implementation.md"
        )
        code, _output, error = self.run_main(
            "plan",
            "write",
            plan,
            "implementation",
            "--file",
            str(artifact),
            role="architect",
        )
        self.assertEqual(code, 3)
        self.assertIn("compiled output", error)

    def test_engineer_cannot_project_testing_partition(self):
        plan = self.create()
        code, _output, error = self.run_main(
            "plan",
            "doc",
            "projection",
            plan,
            "--stage",
            "testing",
            role="engineer",
        )
        self.assertEqual(code, 3)
        self.assertIn("may not compile", error)

    def test_review_open_uses_new_server_for_a_package(self):
        plan = self.create()

        def fake_serve(*args, **kwargs):
            kwargs["on_ready"](
                {
                    "url": "http://127.0.0.1:1234",
                    "host": "127.0.0.1",
                    "port": 1234,
                    "plan": plan,
                    "role": "reviewer",
                    "mode": "document",
                    "token": "token",
                }
            )
            return {}

        with mock.patch("grogu_plan_server.serve", side_effect=fake_serve):
            code, output, error = self.run_main(
                "review", "open", plan, "--no-open", role="reviewer"
            )
        self.assertEqual(code, 0, error)
        self.assertIn("/?t=token", output)
