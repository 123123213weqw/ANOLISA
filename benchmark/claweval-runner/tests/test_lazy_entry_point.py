# Copyright 2026 Alibaba Cloud
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Regression tests for the lazy ce_runner entry point (issue #5952).

Importing ``ce_runner`` used to import ``run_task`` (and with it the whole
agent runtime: Docker/OpenAI/MCP wiring and platform modules such as
``resource``), so metadata-only imports — the version probe, the console
script resolving ``ce_runner:main`` — depended on full runtime setup even
though no evaluation was being run. The package must stay importable with
a tiny boundary: ``__version__`` and a zero-argument ``main`` that defers
to the real entry point only when invoked, preserving delegation, exactly
one invocation, and SystemExit/error propagation.

Each check runs in an isolated real subprocess against the actual
package on ``src/``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

VERSION_PROBE = """
import sys


class _RuntimeImportGuard:
    # Reject every ce_runner submodule import: a metadata-only version
    # probe must never pull the agent runtime (run_task, sandbox, ...).

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "ce_runner" and fullname.startswith("ce_runner."):
            raise ImportError("runtime import rejected during version probe: " + fullname)
        return None


sys.meta_path.insert(0, _RuntimeImportGuard())

import ce_runner

print("VERSION:" + ce_runner.__version__)
print("MAIN:" + ce_runner.main.__name__)
"""

DELEGATION_PROBE = """
import json
import sys
import types

fake = types.ModuleType("ce_runner.run_task")
calls = []


def _fake_main():
    calls.append("invocation-%d" % (len(calls) + 1))
    return "DELEGATED-%d" % len(calls)


fake.main = _fake_main
sys.modules["ce_runner.run_task"] = fake

import ce_runner

first = ce_runner.main()
second = ce_runner.main()
print(json.dumps({"first": first, "second": second, "count": len(calls)}))
"""

EXIT_PROBE = """
import sys
import types

fake = types.ModuleType("ce_runner.run_task")


def _fake_main():
    raise SystemExit(7)


fake.main = _fake_main
sys.modules["ce_runner.run_task"] = fake

import ce_runner

ce_runner.main()
print("NOT-REACHED")
"""


def run_probe(script):
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(SRC) + (os.pathsep + existing if existing else "")
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, env=env, timeout=60,
    )


class TestVersionProbe:
    def test_version_probe_needs_no_runtime_imports(self):
        """Baseline failure on main: package import pulled run_task."""
        result = run_probe(VERSION_PROBE)
        assert result.returncode == 0, (
            "version probe must succeed without importing the agent runtime\n"
            "stdout: %s\nstderr: %s" % (result.stdout, result.stderr)
        )
        assert "VERSION:1.0.0" in result.stdout
        assert "MAIN:main" in result.stdout
        assert "runtime import rejected" not in result.stderr


class TestDelegation:
    def test_main_delegates_exactly_once_and_returns_result(self):
        """Control: one call to ce_runner.main() = exactly one invocation."""
        result = run_probe(DELEGATION_PROBE)
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["first"] == "DELEGATED-1"
        assert payload["second"] == "DELEGATED-2"
        assert payload["count"] == 2


class TestExitPropagation:
    def test_system_exit_code_is_preserved(self):
        """Control: SystemExit from the entry point passes through."""
        result = run_probe(EXIT_PROBE)
        assert result.returncode == 7, (
            "SystemExit(7) must propagate through ce_runner.main()\n"
            "stdout: %s\nstderr: %s" % (result.stdout, result.stderr)
        )
        assert "NOT-REACHED" not in result.stdout
