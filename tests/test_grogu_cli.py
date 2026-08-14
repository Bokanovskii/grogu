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

import grogu_banner  # noqa: E402
import grogu_cli
import grogu_codemode  # noqa: E402
import grogu_context  # noqa: E402
import grogu_gmail  # noqa: E402
import grogu_imessage  # noqa: E402
import grogu_mcp  # noqa: E402
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
        self.assertEqual(grogu_cli.copilot_arguments([]), ["--autopilot"])
        self.assertEqual(
            grogu_cli.copilot_arguments(["--model", "gpt-5.4"]),
            ["--autopilot", "--model", "gpt-5.4"],
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
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["id"], 1001)
        self.assertIn("dinner plans", results[0]["text"])
        self.assertEqual(results[0]["handle"], "+15551234567")
        self.assertEqual(results[1]["id"], 1000)
        self.assertEqual(results[1]["text"], "context before the hit")

    def test_adapter_search_prefers_seaglass_when_configured(self):
        adapter = grogu_imessage.MacOSIMessageAdapter()
        results = adapter.search("dinner plans")
        self.assertEqual(len(results), 2)
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
        first = self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer")
        self.assertIn("use zero for HUF", first.stdout)
        second = self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer")
        self.assertNotIn("use zero for HUF", second.stdout)
        self.assertIn("no unread steering", second.stdout)
        replay = self.run_cli("plan", "steering", "--plan", plan, "--role", "engineer", "--all")
        self.assertIn("use zero for HUF", replay.stdout)

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
