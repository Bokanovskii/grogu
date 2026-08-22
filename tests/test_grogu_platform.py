"""Cross-platform regression tests for Grogu's operating-system primitives."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import _sandbox  # noqa: E402,F401
import grogu_cli  # noqa: E402
import grogu_design  # noqa: E402
import grogu_platform  # noqa: E402


class PlatformPrimitiveTests(unittest.TestCase):
    def test_process_liveness_handles_current_and_missing_processes(self):
        self.assertTrue(grogu_platform.process_alive(os.getpid()))
        self.assertFalse(grogu_platform.process_alive(2**31 - 1))
        self.assertFalse(grogu_platform.process_alive(0))

    def test_windows_process_query_errors_are_conservative(self):
        self.assertFalse(grogu_platform._windows_open_failure_is_alive(87))
        self.assertTrue(grogu_platform._windows_open_failure_is_alive(5))
        self.assertTrue(grogu_platform._windows_open_failure_is_alive(8))

    def test_lock_is_released_after_an_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                with self.assertRaises(RuntimeError):
                    with grogu_platform.exclusive_lock(descriptor):
                        raise RuntimeError("probe")
                with grogu_platform.exclusive_lock(descriptor):
                    pass
            finally:
                os.close(descriptor)

    def test_lock_preserves_the_callers_file_position(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.write(descriptor, b"x")
                expected = os.lseek(descriptor, 0, os.SEEK_CUR)
                with grogu_platform.exclusive_lock(descriptor):
                    pass
                self.assertEqual(os.lseek(descriptor, 0, os.SEEK_CUR), expected)
            finally:
                os.close(descriptor)

    def test_a_competing_process_waits_for_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "store.lock"
            descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            child = None
            script = (
                "import os,sys;"
                f"sys.path.insert(0,{str(ROOT / 'src')!r});"
                "import grogu_platform;"
                "fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600);"
                "print('ready',flush=True);"
                "ctx=grogu_platform.exclusive_lock(fd);"
                "ctx.__enter__();"
                "print('acquired',flush=True);"
                "ctx.__exit__(None,None,None);"
                "os.close(fd)"
            )
            try:
                with grogu_platform.exclusive_lock(descriptor):
                    child = subprocess.Popen(
                        [sys.executable, "-c", script, str(path)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf8",
                    )
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    with self.assertRaises(subprocess.TimeoutExpired):
                        child.wait(timeout=0.25)
                stdout, stderr = child.communicate(timeout=10)
                self.assertEqual(child.returncode, 0, stderr)
                self.assertIn("acquired", stdout)
            finally:
                os.close(descriptor)
                if child is not None and child.poll() is None:
                    child.kill()
                    child.wait()

    def test_design_store_lock_remains_reentrant(self):
        with tempfile.TemporaryDirectory() as directory:
            store = grogu_design.DesignStore(Path(directory))
            with store.locked():
                with store.locked():
                    pass

    def test_windows_batch_forwarding_detection_reads_the_shim(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forwarding = root / "forwarding.cmd"
            forwarding.write_text(
                '@ECHO OFF\r\n"node.exe" "cli.js" %*\r\n',
                encoding="utf8",
            )
            simple = root / "simple.cmd"
            simple.write_text(
                "@ECHO OFF\r\nREM This wrapper does not forward all arguments.\r\n"
                "ECHO %%*\r\n"
                'IF "%~1"=="ok" EXIT /B 0\r\n',
                encoding="utf8",
            )

            self.assertTrue(
                grogu_cli._windows_batch_forwards_all_arguments(str(forwarding))
            )
            self.assertFalse(
                grogu_cli._windows_batch_forwards_all_arguments(str(simple))
            )

    @unittest.skipUnless(os.name == "nt", "Windows console behavior")
    def test_windows_npm_cmd_shim_preserves_percent_arguments_and_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "npm shim"
            root.mkdir()
            checker = root / "checker.py"
            arguments = [
                "two words",
                '"(foo|bar>baz|foz)"',
                "literal-%GROGU_PERCENT_EXPANSION_PROBE%-&-safe",
            ]
            checker.write_text(
                "import sys\n"
                f"raise SystemExit(7 if sys.argv[1:] == {arguments!r} else 9)\n",
                encoding="utf8",
            )
            shim = root / "copilot.cmd"
            shim.write_text(
                "@ECHO OFF\r\n"
                f'"{sys.executable}" "{checker}" %*\r\n',
                encoding="utf8",
            )

            environment = os.environ.copy()
            environment["GROGU_PERCENT_EXPANSION_PROBE"] = "expanded"
            result = grogu_cli._run_copilot(str(shim), arguments, environment)

        self.assertEqual(result, 7)

    @unittest.skipUnless(os.name == "nt", "Windows console behavior")
    def test_windows_simple_batch_wrapper_uses_one_escape_layer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "simple wrapper"
            root.mkdir()
            wrapper = root / "copilot.bat"
            wrapper.write_text(
                "@ECHO OFF\r\n"
                'IF "%~1"=="two words & safe" EXIT /B 7\r\n'
                "EXIT /B 9\r\n",
                encoding="utf8",
            )

            result = grogu_cli._run_copilot(
                str(wrapper), ["two words & safe"], os.environ.copy()
            )

        self.assertEqual(result, 7)

    @unittest.skipUnless(os.name == "nt", "Windows console behavior")
    def test_windows_native_executable_runs_directly(self):
        environment = os.environ.copy()
        completed = subprocess.CompletedProcess([], 7)
        with mock.patch.object(
            grogu_cli.subprocess, "run", return_value=completed
        ) as run:
            result = grogu_cli._run_copilot(
                r"C:\Program Files\GitHub\copilot.exe",
                ["--version"],
                environment,
            )

        self.assertEqual(result, 7)
        run.assert_called_once_with(
            [r"C:\Program Files\GitHub\copilot.exe", "--version"],
            env=environment,
        )


if __name__ == "__main__":
    unittest.main()
