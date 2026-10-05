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

"""Tests for stable ID-based dataset sharding."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from swe_runner.common.models import DatasetConfig, SWEInstance
from swe_runner.run.dataset import filter_instances, load_dataset

SHARD_ROWS = [
    {
        "instance_id": f"repo__proj-{index:03d}",
        "repo": "repo/proj",
        "version": "1.0",
        "base_commit": f"abc{index}",
        "problem_statement": f"Bug {index}",
        "patch": "",
        "test_patch": "",
    }
    for index in range(24)
]


def _shard_instances() -> list[SWEInstance]:
    return [SWEInstance.from_dataset_row(row) for row in SHARD_ROWS]


def _ids(instances: list[SWEInstance]) -> list[str]:
    return [i.instance_id for i in instances]


class TestShardPartition:
    def test_all_shards_cover_unique_ids_without_overlap(self):
        instances = _shard_instances()
        shards = [
            _ids(filter_instances(instances, DatasetConfig(num_shards=4, shard_index=index))) for index in range(4)
        ]
        union = [item for shard in shards for item in shard]
        assert sorted(union) == sorted(_ids(instances))
        assert len(union) == len(set(union))
        for shard in shards:
            assert shard

    def test_shard_preserves_local_row_order(self):
        instances = _shard_instances()
        source_order = _ids(instances)
        for index in range(4):
            shard = _ids(filter_instances(instances, DatasetConfig(num_shards=4, shard_index=index)))
            positions = [source_order.index(item) for item in shard]
            assert positions == sorted(positions)

    def test_default_single_shard_preserves_selection_and_order(self):
        instances = _shard_instances()
        default = filter_instances(instances, DatasetConfig())
        assert _ids(default) == _ids(instances)
        explicit = filter_instances(instances, DatasetConfig(num_shards=1, shard_index=0))
        assert _ids(explicit) == _ids(instances)


class TestShardStability:
    def test_membership_stable_after_reorder_and_extension(self):
        instances = _shard_instances()
        baseline = [
            set(_ids(filter_instances(instances, DatasetConfig(num_shards=3, shard_index=index)))) for index in range(3)
        ]

        reordered = list(reversed(instances))
        reordered_membership = [
            set(_ids(filter_instances(reordered, DatasetConfig(num_shards=3, shard_index=index)))) for index in range(3)
        ]
        assert reordered_membership == baseline

        extra_row = dict(SHARD_ROWS[0])
        extra_row["instance_id"] = "repo__proj-999"
        extended = instances + [SWEInstance.from_dataset_row(extra_row)]
        extended_membership = [
            set(_ids(filter_instances(extended, DatasetConfig(num_shards=3, shard_index=index)))) for index in range(3)
        ]
        for index in range(3):
            assert baseline[index] <= extended_membership[index]

    def test_membership_stable_across_python_hash_seeds(self, tmp_path):
        rows_file = tmp_path / "rows.json"
        rows_file.write_text(json.dumps(SHARD_ROWS))
        memberships = []
        for seed in ("0", "1", "12345"):
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    _HASH_SEED_PROBE,
                    str(rows_file),
                ],
                capture_output=True,
                text=True,
                env={"PYTHONHASHSEED": seed, "PYTHONPATH": _repo_src_path()},
                check=True,
            )
            memberships.append(proc.stdout.strip())
        assert len(set(memberships)) == 1


class TestShardComposition:
    def test_sharding_applies_after_id_and_regex_filters_before_slice(self):
        instances = _shard_instances()
        wanted = _ids(filter_instances(instances, DatasetConfig(num_shards=2, shard_index=0)))
        regex_pattern = re.compile(r"proj-0\d\d")
        regex_only = [i for i in wanted if regex_pattern.search(i)]
        composed = filter_instances(
            instances,
            DatasetConfig(
                filter_regex=r"proj-0\d\d",
                num_shards=2,
                shard_index=0,
                slice_range="1:",
            ),
        )
        assert _ids(composed) == regex_only[1:]
        assert all(i.instance_id in wanted for i in composed)

    def test_instance_ids_filter_composes_with_sharding(self):
        instances = _shard_instances()
        selected = _ids(instances)[:10]
        shard_zero = set(_ids(filter_instances(instances, DatasetConfig(num_shards=2, shard_index=0))))
        expected = [i for i in selected if i in shard_zero]
        composed = filter_instances(
            instances,
            DatasetConfig(instance_ids=selected, num_shards=2, shard_index=0),
        )
        assert _ids(composed) == expected


class TestShardValidation:
    def test_num_shards_must_be_positive(self):
        with pytest.raises(ValidationError):
            DatasetConfig(num_shards=0)

    def test_shard_index_must_not_be_negative(self):
        with pytest.raises(ValidationError):
            DatasetConfig(num_shards=2, shard_index=-1)

    def test_shard_index_must_be_below_num_shards(self):
        with pytest.raises(ValidationError):
            DatasetConfig(num_shards=2, shard_index=2)

    def test_valid_shard_parameters_are_accepted(self):
        config = DatasetConfig(num_shards=4, shard_index=3)
        assert config.num_shards == 4
        assert config.shard_index == 3


class TestShardDatasetLoading:
    def test_load_dataset_applies_sharding(self):
        with patch("swe_runner.run.dataset.hf_datasets.load_dataset") as mock_load:
            mock_load.return_value = SHARD_ROWS
            whole = load_dataset(DatasetConfig())
            assert len(whole) == 24
            shards = [
                {i.instance_id for i in load_dataset(DatasetConfig(num_shards=3, shard_index=index))}
                for index in range(3)
            ]
        union = set.union(*shards)
        assert union == {i.instance_id for i in whole}
        assert sum(len(s) for s in shards) == 24
        for left in range(3):
            for right in range(left + 1, 3):
                assert not shards[left] & shards[right]


def _repo_src_path() -> str:
    """PYTHONPATH entry exposing swe_runner plus this test module to subprocesses."""
    test_file = Path(__file__).resolve()
    package_root = test_file.parents[3]
    return str(package_root / "src") + ":" + str(package_root / "tests")


_HASH_SEED_PROBE = """
import json
import sys

from swe_runner.common.models import DatasetConfig, SWEInstance
from swe_runner.run.dataset import filter_instances

rows = json.loads(open(sys.argv[1]).read())
instances = [SWEInstance.from_dataset_row(row) for row in rows]
config = DatasetConfig(num_shards=3, shard_index=1)
print(sorted(i.instance_id for i in filter_instances(instances, config)))
"""
