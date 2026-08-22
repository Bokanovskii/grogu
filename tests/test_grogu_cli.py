import contextlib
import io
import json
import os
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "src" / "grogu_cli.py"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401  (redirects GROGU_HOME and HOME away from the real one)

import grogu_banner  # noqa: E402
import grogu_cli
import grogu_codemode  # noqa: E402
import grogu_context  # noqa: E402
import grogu_gmail  # noqa: E402
import grogu_imessage  # noqa: E402
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

    def test_bare_launch_defaults_to_autopilot(self):
        plugin = ["--plugin-dir", str(ROOT)]
        self.assertEqual(
            grogu_cli.copilot_arguments([]),
            [*plugin, "--autopilot"],
        )
        self.assertEqual(
            grogu_cli.copilot_arguments(["--model", "gpt-5.4"]),
            [*plugin, "--autopilot", "--model", "gpt-5.4"],
        )

    def test_launch_preserves_explicit_plugin_directories(self):
        self.assertEqual(
            grogu_cli.copilot_arguments(["--plugin-dir", "/user/plugin"]),
            [
                "--plugin-dir",
                str(ROOT),
                "--autopilot",
                "--plugin-dir",
                "/user/plugin",
            ],
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
            self.assertIn("work:t-demo", first["graph"]["nodes"])
            second = store.index()
            self.assertEqual(second["changed"], [])
            (root / "app.py").write_text("print('two')\n")
            third = store.index()
            self.assertEqual(third["changed"], ["app.py"])
            self.assertTrue((root / ".grogu/state/memory-cache.json").is_file())

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

    def test_imessage_drafts_require_confirmation(self):
        with tempfile.TemporaryDirectory() as home:
            store = grogu_imessage.DraftStore(Path(home))
            draft = store.create(
                grogu_imessage.Recipient("+15551234567"), "Hello"
            )
            adapter = grogu_imessage.MacOSIMessageAdapter()
            with self.assertRaises(grogu_imessage.ConfirmationRequiredError):
                adapter.send(draft.recipient, draft.body)

    def test_gmail_is_disabled_by_default_and_drafts_are_local(self):
        adapter = grogu_gmail.GmailAdapter(access_token="token", enabled=False)
        self.assertFalse(adapter.status()["enabled"])
        self.assertIn("GROGU_GMAIL_ACCESS_TOKEN", adapter.status()["reason"])
        self.assertIn("oauth_playground", adapter.status()["setup"])
        with self.assertRaises(grogu_gmail.GmailDisabledError):
            adapter.search("from:billing")
        with tempfile.TemporaryDirectory() as home:
            draft = grogu_gmail.DraftStore(Path(home)).create(
                "person@example.com", "Hello", "Message"
            )
            self.assertEqual(draft.status, "draft")


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
                source="gmail",
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
                    env=environment,
                )

            suggested = run(
                "personal", "suggest", "--type", "event", "--name", "Jamie Birthday",
                "--summary", "Mentioned in email", "--source", "gmail",
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
class ImessageSeaglassIntegrationTests(unittest.TestCase):
    """`MacOSIMessageAdapter.search()` should prefer a configured `seaglass`
    MCP server over the local SQL LIKE scan, and fall back transparently if
    the seaglass call fails. Uses tests/fixtures/mcp_seaglass_stub.py so
    this doesn't depend on a real seaglass installation.
    """

    def make_config(self):
        directory = tempfile.TemporaryDirectory()
        config_path = Path(directory.name) / "mcp-config.json"
        config_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "seaglass": {
                            "type": "local",
                            "command": sys.executable,
                            "args": [str(FIXTURES / "mcp_seaglass_stub.py")],
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

    def test_seaglass_available_reflects_mcp_config(self):
        self.assertTrue(grogu_imessage.seaglass_available())
        os.environ.pop("GROGU_MCP_CONFIG", None)
        # Regression test for IMPROVEMENT-13: previously this relied on
        # the real user's ~/.copilot/mcp-config.json happening to have no
        # seaglass entry once GROGU_MCP_CONFIG was popped -- fragile, and
        # in fact now false on this machine since a real seaglass entry
        # was registered as part of end-to-end verification. Point
        # COPILOT_HOME at an empty directory instead, so the "no config"
        # case is genuinely isolated from whatever the real environment
        # has configured.
        previous_home = os.environ.get("COPILOT_HOME")
        os.environ["COPILOT_HOME"] = str(Path(tempfile.mkdtemp()))
        try:
            self.assertFalse(grogu_imessage.seaglass_available())
        finally:
            if previous_home is None:
                os.environ.pop("COPILOT_HOME", None)
            else:
                os.environ["COPILOT_HOME"] = previous_home

    def test_search_via_seaglass_flattens_sessions(self):
        results = grogu_imessage.search_via_seaglass("dinner plans")
        # Both the reranked hit and its context_messages must be flattened
        # (IMPROVEMENT-8 fix): previously context_messages were silently
        # dropped even though seaglass's own recall@final metric counts
        # them as hits.
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0]["id"], 1001)
        self.assertIn("dinner plans", results[0]["text"])
        self.assertEqual(results[0]["handle"], "+15551234567")
        # Context trails *every* session's hits, not just its own.
        self.assertEqual(results[-1]["id"], 1000)
        self.assertEqual(results[-1]["text"], "context before the hit")
        # seaglass reports the user's own messages with a null sender, which
        # used to flatten to an empty handle -- indistinguishable from
        # "sender unknown".
        self.assertEqual(results[-1]["handle"], grogu_imessage.SELF_HANDLE)

    def test_flatten_puts_actual_matches_first(self):
        """seaglass returns a ~22-message window, so the message that
        actually matched can sit anywhere inside it. A caller reading the
        top few results would otherwise get its neighbours."""
        payload = {
            "sessions": [
                {
                    "messages": [
                        {"message_id": 1, "ts": 1.0, "text": "unrelated chatter", "sender": "A", "match_score": 0},
                        {"message_id": 2, "ts": 2.0, "text": "the classic one", "sender": "B", "match_score": 1},
                        {"message_id": 3, "ts": 3.0, "text": "more chatter", "sender": "C", "match_score": 0},
                    ],
                    "context_messages": [
                        {"message_id": 4, "ts": 0.5, "text": "before", "sender": "D", "match_score": 0},
                    ],
                }
            ]
        }
        ids = [row["id"] for row in grogu_imessage._flatten_seaglass_result(payload)]
        # Match first, its non-matching neighbours after in conversation
        # order, and context still trailing every hit.
        self.assertEqual(ids, [2, 1, 3, 4])

    def test_flatten_keeps_conversation_order_without_match_scores(self):
        """An older seaglass, or a query whose words are all stopwords,
        sends no scores -- conversation order is then the best available
        ordering and must not be disturbed."""
        payload = {
            "sessions": [
                {
                    "messages": [
                        {"message_id": 1, "ts": 1.0, "text": "first", "sender": "A"},
                        {"message_id": 2, "ts": 2.0, "text": "second", "sender": "B"},
                    ]
                }
            ]
        }
        ids = [row["id"] for row in grogu_imessage._flatten_seaglass_result(payload)]
        self.assertEqual(ids, [1, 2])

    def test_flatten_leaves_unknown_senders_blank(self):
        """Only `is_from_me` earns the "me" label; a missing sender that is
        not the user's own message stays empty rather than being
        misattributed to them."""
        payload = {
            "sessions": [
                {
                    "messages": [
                        {"message_id": 1, "ts": 1.0, "text": "a", "sender": None, "is_from_me": False},
                        {"message_id": 2, "ts": 2.0, "text": "b", "sender": None, "is_from_me": True},
                    ]
                }
            ]
        }
        handles = [row["handle"] for row in grogu_imessage._flatten_seaglass_result(payload)]
        self.assertEqual(handles, ["", grogu_imessage.SELF_HANDLE])

    def test_adapter_search_prefers_seaglass_when_configured(self):
        adapter = grogu_imessage.MacOSIMessageAdapter()
        results = adapter.search("dinner plans")
        self.assertEqual(len(results), 3)
        self.assertIn("dinner plans", results[0]["text"])

    def test_adapter_search_can_force_sql_like_path(self):
        # database_path deliberately points nowhere, so the SQL LIKE path
        # would raise -- this confirms use_seaglass=False actually bypasses
        # seaglass rather than silently still using it.
        adapter = grogu_imessage.MacOSIMessageAdapter(
            database_path=Path(self.directory.name) / "does-not-exist.db"
        )
        with self.assertRaises(grogu_imessage.IMessageError):
            adapter.search("dinner plans", use_seaglass=False)

    def test_sql_like_path_normalizes_date_to_unix_seconds(self):
        # Regression test for BUG-7: the SQL LIKE fallback used to return
        # message.date's raw Apple-epoch value (seconds OR nanoseconds,
        # depending on macOS version) verbatim, while seaglass's path
        # always returns unix seconds in the same "date" field -- silently
        # inconsistent depending on which backend answered. 700000000 is a
        # realistic Apple-epoch-seconds value (2023-03-11ish); 978307200
        # is the 2001-01-01 unix-seconds offset dates are relative to.
        chat_db = Path(self.directory.name) / "chat.db"
        connection = sqlite3.connect(chat_db)
        connection.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, handle_id INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            INSERT INTO handle (ROWID, id) VALUES (1, '+15559990000');
            INSERT INTO message (ROWID, text, date, handle_id)
                VALUES (1, 'dinner plans tonight', 700000000, 1);
            """
        )
        connection.commit()
        connection.close()
        adapter = grogu_imessage.MacOSIMessageAdapter(database_path=chat_db)
        results = adapter.search("dinner plans", use_seaglass=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["date"], 700000000 + 978307200)

    def test_adapter_search_falls_back_to_sql_like_on_seaglass_failure(self):
        # Force the fixture's search_messages tool to raise, and point the
        # adapter at a real sqlite file *with a matching row* (not an
        # empty one) so a non-empty result unambiguously proves the SQL
        # LIKE fallback actually ran, rather than being indistinguishable
        # from "seaglass succeeded with zero results" (IMPROVEMENT-13).
        chat_db = Path(self.directory.name) / "chat.db"
        connection = sqlite3.connect(chat_db)
        connection.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, handle_id INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            INSERT INTO handle (ROWID, id) VALUES (1, '+15559990000');
            INSERT INTO message (ROWID, text, date, handle_id)
                VALUES (1, 'contains the __fail__ sentinel text', 700000000, 1);
            """
        )
        connection.commit()
        connection.close()
        adapter = grogu_imessage.MacOSIMessageAdapter(database_path=chat_db)
        results = adapter.search("__fail__")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["text"], "contains the __fail__ sentinel text")
        self.assertEqual(results[0]["handle"], "+15559990000")

    def test_seaglass_failure_is_reported_on_stderr(self):
        """The fallback answers a different question than the query asked,
        so it must not happen silently: a stale index path in the MCP
        config degraded every search this way, invisibly."""
        chat_db = Path(self.directory.name) / "warn.db"
        connection = sqlite3.connect(chat_db)
        connection.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, handle_id INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            """
        )
        connection.commit()
        connection.close()
        adapter = grogu_imessage.MacOSIMessageAdapter(database_path=chat_db)
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            adapter.search("__fail__")
        self.assertIn("seaglass search failed", stderr.getvalue())
        self.assertIn("substring scan", stderr.getvalue())

    def test_status_reports_seaglass_configured(self):
        real_db = Path(self.directory.name) / "chat.db"
        connection = sqlite3.connect(real_db)
        connection.executescript(
            """
            CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, date INTEGER, handle_id INTEGER);
            CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
            """
        )
        connection.commit()
        connection.close()
        adapter = grogu_imessage.MacOSIMessageAdapter(database_path=real_db)
        status = adapter.status()
        self.assertTrue(status["available"])
        self.assertTrue(status["seaglass"])
        # Whether the index is current decides whether a search can answer
        # about the last hour at all, so status must say.
        self.assertTrue(status["seaglass_index"]["stale"])
        self.assertEqual(status["seaglass_index"]["n_messages_since_index"], 12)

    def test_a_stale_index_is_reported_on_stderr(self):
        # A stale result looks exactly like a complete one, so "what did
        # she just say" would answer with yesterday's conversation.
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            grogu_imessage.search_via_seaglass("__stale__")
        self.assertIn("12 message(s) behind", stderr.getvalue())
        self.assertIn("grogu imessage sync", stderr.getvalue())

    def test_no_warning_when_seaglass_served_the_gap_from_chat_db(self):
        """A filters-only query is answered from the live database, so the
        index being behind costs the answer nothing. Warning anyway would
        send the user to sync to fix a result that is already complete --
        and a warning that fires when nothing is wrong is one they learn
        to scroll past, which is how it gets missed when it matters."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            grogu_imessage.search_via_seaglass("__stale_but_covered__")
        self.assertEqual(stderr.getvalue(), "")

    def test_a_stale_index_still_warns_when_the_gap_was_not_covered(self):
        """The silence above must come from the coverage flag, not from
        having broken the warning outright."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            grogu_imessage.search_via_seaglass("__stale__")
        self.assertIn("12 message(s) behind", stderr.getvalue())

    def test_a_current_index_says_nothing(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            grogu_imessage.search_via_seaglass("anything")
        self.assertEqual(stderr.getvalue(), "")

    def test_a_recency_query_returns_the_newest_message_first(self):
        # "latest from Adrian" would otherwise return the oldest messages
        # of the newest day, because a session arrives in reading order.
        #
        # The newest message in the fixture is 2001 (Jan 2), in the
        # *second* session; 1001 is Jan 1. This assertion used to name
        # 1001, because sorting happened within a session and the sessions
        # were then concatenated in rank order -- so "the newest message"
        # only meant "the newest message of whichever session ranked
        # first". Ordering is global now, which is what a recency query
        # actually asks for.
        messages = grogu_imessage.search_via_seaglass("__recent__", limit=1)
        self.assertEqual(messages[0]["id"], 2001)

    def test_a_recency_query_orders_across_sessions_not_just_within_one(self):
        messages = grogu_imessage.search_via_seaglass("__recent__", limit=3)
        stamps = [m["date"] for m in messages]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual([m["id"] for m in messages], [2001, 1001, 999])

    def test_every_sessions_hits_come_before_any_sessions_context(self):
        # Otherwise the second session's actual matches sit below the first
        # session's surrounding chatter, and "the last thing Adrian sent"
        # answers with Adrian followed by the user's own message.
        messages = grogu_imessage.search_via_seaglass("anything")
        self.assertEqual([m["id"] for m in messages], [1001, 2001, 1000])

    def test_sync_runs_the_seaglass_tool(self):
        self.assertEqual(grogu_imessage.sync_seaglass_index(wait=True)["waited"], True)


@unittest.skipUnless(
    grogu_mcp.available(), "requires Python 3.10+ with the 'mcp' package installed"
)
class SeaglassFlatteningTests(unittest.TestCase):
    """What Grogu does to seaglass's ranked page before a caller sees it.

    Each of these is a defect seaglass's `behavior --compare` measured
    over 208 live queries: the app scored 1.00 on every property, Grogu
    failed 160 of the same queries purely in this flattening step.
    """

    @staticmethod
    def payload(sessions, ordering="relevance"):
        return {"ordering": ordering, "sessions": sessions}

    @staticmethod
    def session(hits=(), context=()):
        def rows(items, sender):
            return [
                {"message_id": i, "text": f"m{i}", "ts": float(i), "sender": sender}
                for i in items
            ]

        return {"messages": rows(hits, "them"), "context_messages": rows(context, "me")}

    def test_every_ranked_session_reaches_the_caller(self):
        # A 20-message limit used to be spent entirely inside session 0,
        # which alone holds 50+ messages once context is expanded -- so
        # seven of the eight sessions we paid to rerank never shipped.
        payload = self.payload([
            self.session(hits=range(1, 60), context=range(100, 150)),
            self.session(hits=range(200, 205)),
            self.session(hits=range(300, 305)),
        ])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual(len(rows), 20)
        self.assertTrue(any(r["id"] in range(200, 205) for r in rows))
        self.assertTrue(any(r["id"] in range(300, 305) for r in rows))

    def test_hits_are_spent_before_context(self):
        payload = self.payload([
            self.session(hits=[1, 2], context=[10, 11, 12]),
            self.session(hits=[3, 4], context=[13, 14, 15]),
        ])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=4)
        self.assertEqual({r["id"] for r in rows}, {1, 2, 3, 4})
        self.assertTrue(all(r["kind"] == "hit" for r in rows))

    def test_the_budget_is_spent_on_messages_that_actually_matched(self):
        # A session's `messages` are the whole matched stretch of
        # conversation and only some of them matched; `match_score` is 0
        # for the rest. In send order a small limit went to whatever the
        # session opened with -- "what did kaya say about the boat" led
        # with a winking emoji and pushed the boat below the cut.
        payload = self.payload([{
            "messages": [
                {"message_id": 1, "text": "hi", "ts": 1.0, "sender": "them", "match_score": 0},
                {"message_id": 2, "text": "the boat", "ts": 2.0, "sender": "them", "match_score": 1},
            ],
            "context_messages": [],
        }])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=1)
        self.assertEqual([r["id"] for r in rows], [2])

    def test_context_is_labelled_not_disguised_as_a_match(self):
        # Context is frequently from the *other* participant or from the
        # user. Unlabelled it read as a match and dropped sender purity
        # from 1.00 to 0.23.
        payload = self.payload([self.session(hits=[1], context=[2])])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual([r["kind"] for r in rows], ["hit", "context"])

    def test_a_declared_recent_ordering_is_honoured(self):
        # "recent messages from Kaya" is answered chronologically.
        # Emitting session by session scrambled it: recency order held
        # for 1% of the queries where it applied.
        payload = self.payload(
            [self.session(hits=[1, 5]), self.session(hits=[9, 3])],
            ordering="recent",
        )
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual([r["id"] for r in rows], [9, 5, 3, 1])

    def test_recency_returns_the_newest_run_not_a_sample_of_each_day(self):
        # Sessions are days under a "recent" ordering. Sharing the budget
        # across them returned two or three messages from each of eight
        # days instead of the newest twenty -- and Grogu cannot ask for a
        # second page, so the rest were unreachable.
        payload = self.payload(
            [self.session(hits=[9, 8, 7]), self.session(hits=[6, 5, 4]), self.session(hits=[3, 2, 1])],
            ordering="recent",
        )
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=4)
        self.assertEqual([r["id"] for r in rows], [9, 8, 7, 6])

    def test_recency_still_spends_hits_before_context(self):
        payload = self.payload(
            [self.session(hits=[1, 2], context=[80, 90])], ordering="recent"
        )
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=2)
        self.assertEqual([r["id"] for r in rows], [2, 1])

    def test_a_chronological_answer_is_asked_again_wider(self):
        # Seaglass orders whole days, and eight of them do not hold the
        # twenty newest messages of a contact who texts in bursts: the
        # narrow ask reached 0.77 of the true newest twenty, dropping
        # matches that sat in days it never requested.
        calls = []

        def fake_call_tool(server, tool, **kwargs):
            calls.append((kwargs.get("max_sessions"), kwargs.get("offset", 0)))
            return {
                "ordering": "recent",
                "has_more": True,
                "next_offset": 8,
                "sessions": [{
                    "messages": [
                        {"message_id": i, "text": "m", "ts": float(i), "sender": "them"}
                        for i in range(30)
                    ],
                    "context_messages": [],
                }],
            }

        original = grogu_imessage.grogu_mcp.call_tool
        grogu_imessage.grogu_mcp.call_tool = fake_call_tool
        try:
            rows = grogu_imessage.search_via_seaglass("latest from sam", limit=20)
        finally:
            grogu_imessage.grogu_mcp.call_tool = original

        self.assertEqual(calls, [(8, 0), (20, 0)])
        self.assertEqual(len(rows), 20)
        self.assertEqual([r["id"] for r in rows], list(range(29, 9, -1)))

    def test_a_wide_answer_still_short_of_matches_takes_one_page(self):
        # The sparse-contact case: widening found everything there was on
        # the first screen and there is genuinely more behind it.
        calls = []

        def fake_call_tool(server, tool, **kwargs):
            offset = kwargs.get("offset", 0)
            calls.append((kwargs.get("max_sessions"), offset))
            base = 100 - offset
            return {
                "ordering": "recent",
                "has_more": True,
                "next_offset": offset + 20,
                "sessions": [{
                    "messages": [
                        {"message_id": base - i, "text": "m", "ts": float(base - i),
                         "sender": "them"}
                        for i in range(2)
                    ],
                    "context_messages": [],
                }],
            }

        original = grogu_imessage.grogu_mcp.call_tool
        grogu_imessage.grogu_mcp.call_tool = fake_call_tool
        try:
            rows = grogu_imessage.search_via_seaglass("latest from sam", limit=20)
        finally:
            grogu_imessage.grogu_mcp.call_tool = original

        # Widened, then one page -- and no further, because a caller
        # waiting on a search is not served by crawling back through the
        # year one screen at a time.
        self.assertEqual(calls, [(8, 0), (20, 0), (20, 20)])
        self.assertEqual([r["id"] for r in rows], [100, 99, 80, 79])

    def test_context_does_not_stop_the_paging_for_matches(self):
        # Page one held one match and plenty of surrounding context.
        # Counting context toward the limit declared the answer full and
        # left the matches on page two unread.
        calls = []

        def fake_call_tool(server, tool, **kwargs):
            offset = kwargs.get("offset", 0)
            calls.append(offset)
            if offset == 0:
                return {
                    "ordering": "recent",
                    "has_more": True,
                    "next_offset": 8,
                    "sessions": [{
                        "messages": [
                            {"message_id": 1, "text": "m", "ts": 1.0, "sender": "them"}
                        ],
                        "context_messages": [
                            {"message_id": 50 + i, "text": "c", "ts": 50.0 + i, "sender": "me"}
                            for i in range(5)
                        ],
                    }],
                }
            return {
                "ordering": "recent",
                "has_more": False,
                "sessions": [{
                    "messages": [
                        {"message_id": 10 + i, "text": "m", "ts": 10.0 + i, "sender": "them"}
                        for i in range(3)
                    ],
                    "context_messages": [],
                }],
            }

        original = grogu_imessage.grogu_mcp.call_tool
        grogu_imessage.grogu_mcp.call_tool = fake_call_tool
        try:
            rows = grogu_imessage.search_via_seaglass("latest from sam", limit=4)
        finally:
            grogu_imessage.grogu_mcp.call_tool = original

        self.assertEqual(calls, [0, 8])
        self.assertEqual([r["id"] for r in rows], [12, 11, 10, 1])
        self.assertTrue(all(r["kind"] == "hit" for r in rows))

    def test_context_still_tops_up_an_answer_short_of_matches(self):
        def fake_call_tool(server, tool, **kwargs):
            return {
                "ordering": "recent",
                "has_more": False,
                "sessions": [{
                    "messages": [{"message_id": 1, "text": "m", "ts": 1.0, "sender": "them"}],
                    "context_messages": [
                        {"message_id": 2, "text": "c", "ts": 2.0, "sender": "me"}
                    ],
                }],
            }

        original = grogu_imessage.grogu_mcp.call_tool
        grogu_imessage.grogu_mcp.call_tool = fake_call_tool
        try:
            rows = grogu_imessage.search_via_seaglass("latest from sam", limit=4)
        finally:
            grogu_imessage.grogu_mcp.call_tool = original

        self.assertEqual([(r["id"], r["kind"]) for r in rows], [(1, "hit"), (2, "context")])

    def test_a_relevance_answer_is_never_paged(self):
        # Page two is by definition less relevant than the page the
        # reranker already chose, and paging it costs a second search.
        calls = []

        def fake_call_tool(server, tool, **kwargs):
            calls.append(kwargs.get("offset", 0))
            return {
                "ordering": "relevance",
                "has_more": True,
                "next_offset": 8,
                "sessions": [{"messages": [
                    {"message_id": 1, "text": "m", "ts": 1.0, "sender": "them"}
                ], "context_messages": []}],
            }

        original = grogu_imessage.grogu_mcp.call_tool
        grogu_imessage.grogu_mcp.call_tool = fake_call_tool
        try:
            rows = grogu_imessage.search_via_seaglass("what did sam say", limit=20)
        finally:
            grogu_imessage.grogu_mcp.call_tool = original

        self.assertEqual(calls, [0])
        self.assertEqual(len(rows), 1)

    def test_relevance_ordering_keeps_sessions_together(self):
        payload = self.payload([self.session(hits=[1, 2]), self.session(hits=[3, 4])])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3, 4])

    def test_a_message_in_two_sessions_is_returned_once(self):
        # One live query returned 372 rows for 249 distinct messages.
        payload = self.payload([self.session(hits=[1, 2]), self.session(hits=[2, 3])])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])

    def test_a_match_is_not_demoted_by_being_context_elsewhere(self):
        # Message 5 is context in the first session and a match in the
        # second. Keeping whichever came first made it context, and since
        # hits are spent before context it then fell off the limit --
        # six of one contact's newest twenty vanished exactly this way.
        payload = self.payload([
            self.session(hits=[1], context=[5]),
            self.session(hits=[5]),
        ])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual([(r["id"], r["kind"]) for r in rows], [(1, "hit"), (5, "hit")])

    def test_no_limit_returns_everything_once(self):
        payload = self.payload([self.session(hits=[1, 2], context=[3])])
        rows = grogu_imessage._flatten_seaglass_result(payload)
        self.assertEqual([r["id"] for r in rows], [1, 2, 3])

    def test_context_never_outranks_another_sessions_hit(self):
        """Reading order, not just budget order.

        Grouping the output by session put session 1's context -- usually
        a message the user sent themselves, matching nothing -- above
        session 2's actual match, so "the last thing Adrian sent" answered
        with Adrian followed by the user's own chatter. The budget was
        already spent hits-first, which is why a set-comparison test
        passed while the order a caller actually reads was wrong.
        """
        payload = self.payload([
            self.session(hits=[1], context=[10]),
            self.session(hits=[2]),
        ])
        rows = grogu_imessage._flatten_seaglass_result(payload, limit=20)
        self.assertEqual(
            [(r["id"], r["kind"]) for r in rows],
            [(1, "hit"), (2, "hit"), (10, "context")],
        )

    def test_matches_rank_first_even_without_a_limit(self):
        """`match_score` ordering must not depend on `limit` being set.

        Ranking lived inside the budget-sharing path, so a caller that
        passed no limit got the session in the order it was sent -- the
        matching message buried among its neighbours, which is the exact
        failure the scoring exists to prevent.
        """
        session = {
            "messages": [
                {"message_id": 1, "ts": 1.0, "text": "chatter", "sender": "a", "match_score": 0},
                {"message_id": 2, "ts": 2.0, "text": "the match", "sender": "b", "match_score": 3},
                {"message_id": 3, "ts": 3.0, "text": "more chatter", "sender": "c", "match_score": 0},
            ],
            "context_messages": [],
        }
        rows = grogu_imessage._flatten_seaglass_result(self.payload([session]))
        self.assertEqual([r["id"] for r in rows], [2, 1, 3])
        self.assertEqual(
            [r["id"] for r in grogu_imessage._flatten_seaglass_result(
                self.payload([session]), limit=3)],
            [2, 1, 3],
            "ordering must not change with or without a limit",
        )


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
                env=environment, capture_output=True, text=True, check=True,
            )
            result = subprocess.run(
                [sys.executable, str(CLI), "design", "status"],
                env=environment, capture_output=True, text=True, check=True,
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
            cwd=self.repo, capture_output=True, text=True, env=environment,
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
            cwd=self.repo, capture_output=True, text=True, env=environment,
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
