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

"""Parallel worker management for batch execution."""

from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import time
from pathlib import Path

import httpx

from ._common import (_PYTHON, init_config_defaults, is_sandbox_task,
                       load_task_yaml, log)
from .tool_injector import ToolInjector

PORT_STRIDE = 50  # port gap between adjacent worker slots


class MockServiceBootError(RuntimeError):
    """A mock service failed to become healthy within its ready_timeout."""


def setup_parallel_workers(
    task_yamls: list[str],
    parallel: int,
    config_path: str,
    sandbox_image: str | None = None,
) -> dict:
    """Pre-register tools and agents for parallel batch execution.

    Registers per-task MCP servers in mcp.servers and generates TOOLS.md
    + mcporter config per task.

    Every task gets a sandbox container slot; Docker containers themselves
    are started per-trial in batch_runner._execute_one so each trial gets a
    clean container.

    Returns a setup dict consumed by the parallel execution loop.
    """
    injector = ToolInjector(config_path)
    return injector.setup_parallel_workers(
        task_yamls, parallel, sandbox_image=sandbox_image,
    )


def start_mock_services_with_offset(task_yaml: str, task_dir: str, port_offset: int):
    """Start mock services for a task with the given port offset."""
    task = load_task_yaml(task_yaml)
    project_root = os.path.abspath(os.path.join(task_dir, "..", ".."))
    services = task.get("services", [])

    for svc in services:
        name = svc["name"]
        port = svc["port"] + port_offset
        health_check = svc.get("health_check", "")
        health_method = svc.get("health_check_method", "POST")

        # Apply port offset to health_check URL
        if health_check and port_offset:
            health_check = re.sub(
                r"localhost:(\d+)",
                lambda m: f"localhost:{int(m.group(1)) + port_offset}",
                health_check,
            )

        # Check if already running
        try:
            if health_method == "POST":
                r = httpx.post(health_check, json={}, timeout=3)
            else:
                r = httpx.get(health_check, timeout=3)
            if r.status_code == 200:
                continue
        except Exception:
            pass

        # Start service
        cmd = svc["command"].split()
        env = os.environ.copy()
        env["no_proxy"] = "localhost,127.0.0.1"
        env["NO_PROXY"] = "localhost,127.0.0.1"
        env["PORT"] = str(port)
        for k, v in svc.get("env", {}).items():
            if v.startswith("tasks/"):
                v = os.path.join(project_root, v)
            env[k] = v

        subprocess.Popen(
            cmd, env=env, cwd=project_root,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=lambda: resource.setrlimit(
                resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024)),
        )

        # Wait for health
        timeout_s = svc.get("ready_timeout", 15)
        deadline = time.monotonic() + timeout_s
        healthy = False
        while time.monotonic() < deadline:
            try:
                if health_method == "POST":
                    r = httpx.post(health_check, json={}, timeout=2)
                else:
                    r = httpx.get(health_check, timeout=2)
                if r.status_code == 200:
                    healthy = True
                    break
            except Exception:
                pass
            time.sleep(0.5)

        if health_check and not healthy:
            # A service whose command crashed at boot would previously be
            # waited out silently; its trials then ran against a dead
            # endpoint and were graded as genuine FAILs, polluting
            # avg_score/pass@1 instead of being counted errored. Fail
            # loudly so the batch never misgrades this task.
            log(f"[ERROR] mock service '{name}' failed to become healthy "
                f"on {health_check} within {timeout_s}s "
                f"(port={port}, command={' '.join(cmd)})")
            raise MockServiceBootError(
                f"mock service '{name}' (port={port}) did not become healthy "
                f"within {timeout_s}s: {health_check}")


def reset_services_with_offset(task_yaml: str, port_offset: int):
    """Reset services for a task with port offset."""
    task = load_task_yaml(task_yaml)
    for svc in task.get("services", []):
        name = svc["name"]
        reset_ep = svc.get("reset_endpoint", "")
        if reset_ep and port_offset:
            reset_ep = re.sub(
                r"localhost:(\d+)",
                lambda m: f"localhost:{int(m.group(1)) + port_offset}",
                reset_ep,
            )
        if reset_ep:
            try:
                r = httpx.post(reset_ep, json={}, timeout=5)
                if r.status_code >= 400:
                    log(f"  [WARNING] reset {name} returned {r.status_code}")
            except httpx.ConnectError:
                # Expected on cold start: services are killed before reset
                # runs, so the endpoint is unreachable. The subsequent
                # start_mock_services() call will bring it up fresh.
                log(f"  [INFO] reset {name} skipped (service not running yet — cold start, normal)")
            except Exception as exc:
                log(f"  [WARNING] reset {name} failed: {exc}")


def cleanup_parallel_workers(config_path: str, setup_info: dict, skip_dirs: bool = False):
    """Remove worker agents, MCP servers, and stop sandbox containers."""
    injector = ToolInjector(config_path)
    injector.cleanup_parallel(setup_info, skip_dirs=skip_dirs)
