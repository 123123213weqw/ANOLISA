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

"""ClawEval runner — agent evaluation orchestration."""

__version__ = "1.0.0"


def main():
    """Delegate to the real CLI entry point.

    The agent runtime (run_task and its Docker/OpenAI/MCP/platform
    imports) is loaded only when an evaluation is actually invoked, so
    importing ``ce_runner`` for metadata (the version probe, the
    ``ce-runner`` console script target) stays lightweight. The
    delegation, SystemExit and error propagation of the real entry
    point are preserved unchanged.
    """
    from .run_task import main as _run_task_main

    return _run_task_main()
