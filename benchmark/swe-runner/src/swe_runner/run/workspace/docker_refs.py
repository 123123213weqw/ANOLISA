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

"""Docker naming and image reference helpers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from swe_runner.common.models import SWEInstance

# Length of the disambiguation digest appended to lossily-sanitized names.
_DIGEST_CHARS = 12


def _disambiguate_if_lossy(safe: str, original: str) -> str:
    """Append a digest of *original* when sanitization changed it.

    Character substitution is lossy — ``a/b`` and ``a-b`` sanitize to the
    same string — so distinct ids could collide on one docker container
    name / workspace path.  A deterministic digest keeps the sanitized
    prefix readable while making the mapping injective in practice;
    ids that sanitize to themselves keep their exact stable name.
    """
    if safe == original:
        return safe
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{safe}-{digest}"


def safe_docker_name(name: str) -> str:
    """Sanitize *name* so it is a valid Docker container name.

    Docker container names must match ``[a-zA-Z0-9][a-zA-Z0-9_.-]*``.
    When sanitization rewrites *name*, a digest suffix keeps distinct ids
    from colliding on one container name (see ``_disambiguate_if_lossy``).
    """
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", name)
    if safe and not safe[0].isalnum():
        safe = "s" + safe
    return _disambiguate_if_lossy(safe, name)


def default_work_dir(instance_id: str) -> Path:
    """Return the default host work directory for an instance.

    Distinct instance ids get distinct directories even when their ids
    differ only in characters the path sanitization replaces.
    """
    safe_instance_id = re.sub(r"[^A-Za-z0-9._-]", "_", instance_id)
    safe_instance_id = _disambiguate_if_lossy(safe_instance_id, instance_id)
    return Path(f"/tmp/swebench_work_{safe_instance_id}")


def default_workspace_root(instance_id: str) -> Path:
    """Return the default host workspace root for an instance."""
    return default_work_dir(instance_id)


def get_docker_image_name(instance: SWEInstance) -> str:
    """Derive the Docker image name for a SWE-bench instance."""
    if instance.image_name:
        return instance.image_name
    if instance.docker_image:
        return instance.docker_image
    docker_compatible_id = instance.instance_id.replace("__", "_1776_")
    return f"swebench/sweb.eval.x86_64.{docker_compatible_id}:latest".lower()


def image_has_registry(image_name: str) -> bool:
    """Return whether an image name starts with an explicit registry component."""
    first_component = image_name.split("/", 1)[0]
    return "." in first_component or ":" in first_component or first_component == "localhost"


def build_pull_image_name(image_name: str, pull_registry: str | None) -> str:
    """Return the image reference used for docker pull."""
    if not pull_registry:
        return image_name

    normalized_registry = pull_registry.rstrip("/")
    repository = image_name.split("/", 1)[1] if image_has_registry(image_name) and "/" in image_name else image_name
    return f"{normalized_registry}/{repository}"
