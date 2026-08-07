import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "src" / "grogu_cli.py"
sys.path.insert(0, str(ROOT / "src"))

import grogu_banner  # noqa: E402
import grogu_cli  # noqa: E402
import grogu_memory  # noqa: E402
import grogu_telemetry  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
