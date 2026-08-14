"""Importing the suite as a package must isolate it too.

`unittest discover -s tests` imports these modules top-level and never runs
this file, which is why the isolation lives in `_sandbox` and every module
imports it directly. This exists so that `discover -t .`, which does import the
package, gets the same sandbox rather than a different one.
"""

from . import _sandbox  # noqa: F401
