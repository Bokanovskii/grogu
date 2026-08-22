"""Test-suite setup that runs before any test module is imported.

The suite was writing into the developer's real `~/.grogu`. A quarter of the
activity feed behind `grogu watch` was test runs in temp directories, which is
both noise on a board meant for supervising live agents and the same class of
mistake as an agent writing into the personal taste store: tests reaching into
state that belongs to the person, not the run.

`GROGU_HOME` is redirected here rather than in each test, because the leak came
from the tests that never thought about it. Subprocesses inherit it, so the CLI
invocations are covered too. Individual tests are free to point it somewhere
more specific; this only guarantees it is never the real one.
"""

import atexit
import os
import shutil
import tempfile
from pathlib import Path

# Captured before anything is redirected, so a test can assert it is not here.
REAL_HOME = os.path.expanduser("~")

SANDBOX = tempfile.mkdtemp(prefix="grogu-tests-home-")
_SANDBOX = SANDBOX
os.environ["GROGU_HOME"] = _SANDBOX
# `HOME` as well, because `GROGU_HOME` is only the override: every path that
# resolves it falls back to `Path.home() / ".grogu"`, so a single test that
# unsets the override -- several do, deliberately, to exercise the default --
# puts the rest of the suite back on the developer's real state. Covering the
# override and leaving the fallback open is not covering anything.
os.environ["HOME"] = _SANDBOX
if os.name == "nt":
    # pathlib/ntpath resolves ~ from USERPROFILE before HOME.
    os.environ["USERPROFILE"] = _SANDBOX
    drive, tail = os.path.splitdrive(_SANDBOX)
    os.environ["HOMEDRIVE"] = drive
    os.environ["HOMEPATH"] = tail

# User Git settings must not decide which branch a temporary test repository
# starts on. The suite expects and documents `main` explicitly.
_gitconfig = Path(_SANDBOX) / "gitconfig"
_gitconfig.write_text("[init]\n\tdefaultBranch = main\n", encoding="utf8")
os.environ["GIT_CONFIG_GLOBAL"] = str(_gitconfig)
# A stray role or plan in the developer's shell changes what the CLI does.
for _leaked in ("GROGU_ROLE", "GROGU_PLAN", "GROGU_AGENT"):
    os.environ.pop(_leaked, None)

atexit.register(shutil.rmtree, _SANDBOX, True)
