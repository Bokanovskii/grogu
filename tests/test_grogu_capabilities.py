import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "grogu_cli.py"
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401
import grogu_capabilities  # noqa: E402
import grogu_cli  # noqa: E402


class CapabilityStoreTests(unittest.TestCase):
    def make_plugin(self, root: Path, name: str = "example") -> Path:
        root.mkdir()
        (root / "plugin.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "description": "Example personal capability.",
                }
            ),
            encoding="utf8",
        )
        return root

    def test_add_list_and_remove_use_a_personal_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            plugin = self.make_plugin(root / "plugin", "mail")
            store = grogu_capabilities.CapabilityStore(home)

            added = store.add(str(plugin))
            self.assertEqual(added["name"], "mail")
            self.assertEqual(store.plugin_paths(), [plugin.resolve()])
            self.assertEqual(store.add(str(plugin)), added)
            payload = json.loads(store.path.read_text(encoding="utf8"))
            self.assertEqual(payload["repositories"], [str(plugin.resolve())])
            if os.name != "nt":
                self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

            self.assertEqual(
                store.remove(str(plugin)),
                {"path": str(plugin.resolve()), "removed": True},
            )
            self.assertEqual(store.list(), [])

    def test_add_requires_a_copilot_plugin_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            store = grogu_capabilities.CapabilityStore(root / "home")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "has no plugin.json"
            ):
                store.add(str(repository))
            self.assertFalse(store.path.exists())

    def test_add_rejects_invalid_plugin_resource_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            (repository / "plugin.json").write_text(
                json.dumps({"name": "broken", "skills": 42}),
                encoding="utf8",
            )
            store = grogu_capabilities.CapabilityStore(root / "home")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "field 'skills'"
            ):
                store.add(str(repository))
            self.assertFalse(store.path.exists())

    def test_portable_plugin_rejects_schema_invalid_mcp_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            (repository / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": grogu_capabilities.AGENT_PLUGIN_SCHEMA,
                        "name": "broken",
                    }
                ),
                encoding="utf8",
            )
            (repository / "mcp.json").write_text(
                json.dumps(
                    {
                        "$schema": grogu_capabilities.AGENT_MCP_SCHEMA,
                        "mcpServers": {
                            "remote": {
                                "type": "streamable-http",
                                "url": "https://example.com/mcp",
                                "args": [],
                            }
                        },
                    }
                ),
                encoding="utf8",
            )
            store = grogu_capabilities.CapabilityStore(root / "home")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError,
                "unsupported remote fields",
            ):
                store.add(str(repository))

    def test_portable_plugin_rejects_non_object_extension_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            (repository / "plugin.json").write_text(
                json.dumps(
                    {
                        "$schema": grogu_capabilities.AGENT_PLUGIN_SCHEMA,
                        "name": "broken",
                        "extensions": {"com.example.client": "invalid"},
                    }
                ),
                encoding="utf8",
            )
            store = grogu_capabilities.CapabilityStore(root / "home")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError,
                "map namespaces to objects",
            ):
                store.add(str(repository))

    def test_legacy_plugin_accepts_direct_http_mcp_server_maps(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = root / "repository"
            repository.mkdir()
            (repository / "plugin.json").write_text(
                json.dumps({"name": "remote", "mcpServers": "mcp.json"}),
                encoding="utf8",
            )
            (repository / "mcp.json").write_text(
                json.dumps(
                    {
                        "remote": {
                            "type": "http",
                            "url": "https://example.com/mcp",
                        }
                    }
                ),
                encoding="utf8",
            )
            store = grogu_capabilities.CapabilityStore(root / "home")

            self.assertEqual(store.add(str(repository))["name"], "remote")

    def test_a_missing_configured_repository_is_not_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            plugin = self.make_plugin(root / "plugin")
            store = grogu_capabilities.CapabilityStore(home)
            store.add(str(plugin))
            plugin.rename(root / "moved")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "is unavailable"
            ):
                store.plugin_paths()

    def test_a_malformed_store_is_refused_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = grogu_capabilities.CapabilityStore(home)
            store.path.write_text("{broken", encoding="utf8")

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "is not valid JSON"
            ):
                store.list()
            self.assertEqual(store.path.read_text(encoding="utf8"), "{broken")

    def test_a_relative_stored_path_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = grogu_capabilities.CapabilityStore(home)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repositories": ["../moving-target"],
                    }
                ),
                encoding="utf8",
            )

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "canonical and absolute"
            ):
                store.list()

    def test_invalid_path_text_is_reported_as_a_capability_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = grogu_capabilities.CapabilityStore(home)
            store.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repositories": ["/tmp/\u0000invalid"],
                    }
                ),
                encoding="utf8",
            )

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError, "is unavailable"
            ):
                store.plugin_paths()

    def test_lock_failures_are_contextual_capability_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            home.mkdir()
            (home / "capabilities.lock").mkdir()
            plugin = self.make_plugin(root / "plugin")
            store = grogu_capabilities.CapabilityStore(home)

            with self.assertRaisesRegex(
                grogu_capabilities.CapabilityError,
                "cannot open capability repository lock",
            ):
                store.add(str(plugin))


class CapabilityCommandTests(unittest.TestCase):
    def run_cli(self, home: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), *arguments],
            cwd=ROOT,
            env={**os.environ, "GROGU_HOME": str(home)},
            capture_output=True,
            text=True,
            encoding="utf8",
        )

    def test_commands_manage_repository_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            plugin = root / "plugin"
            plugin.mkdir()
            (plugin / "plugin.json").write_text(
                json.dumps({"name": "calendar"}), encoding="utf8"
            )

            added = self.run_cli(home, "capability", "add", str(plugin))
            self.assertEqual(added.returncode, 0, added.stderr)
            self.assertEqual(json.loads(added.stdout)["name"], "calendar")

            listed = self.run_cli(home, "capability", "list")
            self.assertEqual(listed.returncode, 0, listed.stderr)
            self.assertEqual(
                json.loads(listed.stdout)["repositories"][0]["path"],
                str(plugin.resolve()),
            )

            removed = self.run_cli(home, "capability", "remove", str(plugin))
            self.assertEqual(removed.returncode, 0, removed.stderr)
            self.assertTrue(json.loads(removed.stdout)["removed"])

    def test_normal_launch_reports_store_errors_without_a_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home / "capabilities.json").write_text("{broken", encoding="utf8")
            stderr = io.StringIO()
            with (
                mock.patch.object(grogu_cli, "GROGU_HOME", home),
                mock.patch.object(grogu_cli.shutil, "which", return_value=sys.executable),
                contextlib.redirect_stderr(stderr),
            ):
                result = grogu_cli.main(["--model", "gpt-5.4"])

            self.assertEqual(result, 2)
            self.assertIn("is not valid JSON", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
