import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "src" / "grogu_cli.py"
sys.path.insert(0, str(ROOT))

from tests import _sandbox  # noqa: E402,F401


class SkillDiscoveryTests(unittest.TestCase):
    def test_plugin_manifest_exposes_repository_skills(self):
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf8"))
        self.assertEqual(manifest["name"], "grogu")
        skills = ROOT / manifest["skills"]
        self.assertEqual(skills, ROOT / ".github" / "skills")
        self.assertTrue((skills / "grogu-pipeline" / "SKILL.md").is_file())

    @unittest.skipIf(os.name == "nt", "setup.sh requires a POSIX shell")
    def test_installed_launcher_keeps_the_skill_plugin_with_the_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            install_dir = base / "bin"
            home = base / "home"
            fake_bin = base / "fake-bin"
            outside = base / "outside"
            home.mkdir()
            fake_bin.mkdir()
            outside.mkdir()
            fake_copilot = fake_bin / "copilot"
            fake_copilot.write_text("#!/bin/sh\nexit 0\n", encoding="utf8")
            fake_copilot.chmod(0o755)
            environment = {
                **os.environ,
                "HOME": str(home),
                "GROGU_HOME": str(base / "grogu-home"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    str(ROOT / "setup.sh"),
                    "--install-dir",
                    str(install_dir),
                    "--no-path-update",
                ],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            launcher = install_dir / "grogu"
            self.assertTrue(launcher.is_symlink())
            self.assertEqual(launcher.resolve(), ROOT / "bin" / "grogu")
            self.assertTrue((launcher.resolve().parents[1] / "plugin.json").is_file())
            self.assertIn("Harness Copilot skills found", result.stdout)
            self.assertIn(
                "Platform-specific capabilities remain user-configured",
                result.stdout,
            )
            self.assertFalse((home / ".copilot").exists())

    def test_launcher_adds_the_plugin_without_replacing_user_directories(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            fake_bin = base / "bin"
            outside = base / "outside"
            capture = base / "capture.json"
            fake_bin.mkdir()
            outside.mkdir()
            capture_program = """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["CAPTURE"], "w", encoding="utf8") as handle:
    json.dump(
        {
            "arguments": sys.argv[1:],
            "instruction_dirs": os.environ.get(
                "COPILOT_CUSTOM_INSTRUCTIONS_DIRS", ""
            ),
        },
        handle,
    )
"""
            if os.name == "nt":
                capture_script = fake_bin / "capture_copilot.py"
                capture_script.write_text(capture_program, encoding="utf8")
                fake_copilot = fake_bin / "copilot.cmd"
                fake_copilot.write_text(
                    f'@echo off\r\n"{sys.executable}" '
                    '"%~dp0capture_copilot.py" %*\r\n',
                    encoding="utf8",
                )
            else:
                fake_copilot = fake_bin / "copilot"
                fake_copilot.write_text(capture_program, encoding="utf8")
                fake_copilot.chmod(0o755)
            copilot_home = base / "copilot-home"
            user_instructions = base / "user-instructions"
            user_plugin = base / "user-plugin"
            capability_plugin = base / "capability-plugin"
            capability_plugin.mkdir()
            (capability_plugin / "plugin.json").write_text(
                json.dumps({"name": "example-capability"}),
                encoding="utf8",
            )
            grogu_home = base / "grogu-home"
            grogu_home.mkdir()
            (grogu_home / "capabilities.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "repositories": [str(capability_plugin.resolve())],
                    }
                ),
                encoding="utf8",
            )
            environment = {
                **os.environ,
                "CAPTURE": str(capture),
                "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": str(user_instructions),
                "COPILOT_HOME": str(copilot_home),
                "GROGU_BANNER": "0",
                "GROGU_HOME": str(grogu_home),
                "GROGU_PRUNE_WORKTREES": "0",
                "GROGU_SYNC_MAIN": "0",
                "GROGU_TAB_COLOR": "0",
                "HOME": str(base / "home"),
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
            }

            result = subprocess.run(
                [
                    sys.executable,
                    str(CLI),
                    "--model",
                    "gpt-5.4",
                    "--plugin-dir",
                    str(user_plugin),
                ],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(capture.read_text(encoding="utf8"))
            self.assertEqual(
                payload["arguments"],
                [
                    "--plugin-dir",
                    str(ROOT),
                    "--plugin-dir",
                    str(capability_plugin.resolve()),
                    "--autopilot",
                    "--model",
                    "gpt-5.4",
                    "--plugin-dir",
                    str(user_plugin),
                ],
            )
            self.assertEqual(
                payload["instruction_dirs"],
                f"{user_instructions},{ROOT / '.github'}",
            )
            self.assertFalse((copilot_home / "settings.json").exists())

    @unittest.skipUnless(
        shutil.which("copilot")
        and os.environ.get("GROGU_RUN_COPILOT_E2E") == "1",
        "set GROGU_RUN_COPILOT_E2E=1 with Copilot CLI installed",
    )
    def test_copilot_discovers_grogu_pipeline_outside_the_source_repository(self):
        copilot = shutil.which("copilot")
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            outside = base / "outside"
            outside.mkdir()
            real_home = Path(_sandbox.REAL_HOME)
            environment = {
                **os.environ,
                "CI": "1",
                "COPILOT_AUTO_UPDATE": "false",
                "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": str(ROOT / ".github"),
                "COPILOT_HOME": str(real_home / ".copilot"),
                "GROGU_BANNER": "0",
                "GROGU_HOME": str(base / "grogu-home"),
                "GROGU_PRUNE_WORKTREES": "0",
                "GROGU_SYNC_MAIN": "0",
                "GROGU_TAB_COLOR": "0",
                "HOME": str(real_home),
            }

            missing = subprocess.run(
                [copilot, "skill", "list", "--json"],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(missing.returncode, 0, missing.stderr)
            self.assertFalse(
                any(
                    Path(entry["path"]).resolve()
                    == ROOT / ".github" / "skills" / "grogu-pipeline"
                    for entry in json.loads(missing.stdout)
                )
            )

            discovered = subprocess.run(
                [
                    str(ROOT / "bin" / "grogu"),
                    "-p",
                    "Use the /grogu-pipeline skill. Reply with only its first "
                    "section heading.",
                    "--allow-all-tools",
                    "--output-format",
                    "json",
                    "--model",
                    os.environ.get("GROGU_COPILOT_E2E_MODEL", "gpt-5-mini"),
                    "--no-auto-update",
                ],
                cwd=outside,
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(discovered.returncode, 0, discovered.stderr)
            events = [
                json.loads(line)
                for line in discovered.stdout.splitlines()
                if line.strip()
            ]
            loaded = next(
                event for event in events if event["type"] == "session.skills_loaded"
            )
            skills = {entry["name"]: entry for entry in loaded["data"]["skills"]}
            skill = skills["grogu-pipeline"]
            self.assertEqual(skill["source"], "plugin")
            self.assertTrue(skill["userInvocable"])
            self.assertEqual(
                Path(skill["path"]).resolve(),
                ROOT / ".github" / "skills" / "grogu-pipeline" / "SKILL.md",
            )
            invocation = next(
                event
                for event in events
                if event["type"] == "tool.execution_start"
                and event["data"].get("toolName") == "skill"
            )
            self.assertEqual(
                invocation["data"]["arguments"],
                {"skill": "grogu-pipeline"},
            )
            self.assertTrue(
                any(
                    event["type"] == "tool.execution_complete"
                    and event["data"].get("toolCallId")
                    == invocation["data"]["toolCallId"]
                    and event["data"].get("success")
                    for event in events
                )
            )
