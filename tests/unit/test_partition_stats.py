"""Tests for core/partition_stats.py — real partition counts from BigQuery (#53).

The partition_id formats asserted here were verified against a live BigQuery
dataset holding one table per granularity, not taken from documentation:
  YEAR "2025" | MONTH "202503" | DAY "20250307" | HOUR "2025030713"
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bq_assess.core.partition_stats import (
    PartitionInfo,
    granularity_for_partition_id,
    partition_id_to_date,
    resolve_partition_counts,
)
from bq_assess.models import (
    EntityMetadata,
    EntityPopulation,
    EntityType,
    RangePartitionConfig,
    TimePartitionConfig,
)


def _entity(name, *, num_bytes=1000, dataset="ds", tp=None, rp=None):
    return EntityMetadata(
        entity_id=name,
        dataset_id=dataset,
        full_name=f"{dataset}.{name}",
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=10,
        num_bytes=num_bytes,
        columns=[],
        time_partitioning=tp,
        range_partitioning=rp,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime.now(timezone.utc),
    )


class _Row:
    def __init__(self, table_name, partitions, min_id, max_id):
        self.table_name = table_name
        self.partitions = partitions
        self.min_id = min_id
        self.max_id = max_id


class _FakeClient:
    """Records every query issued so per-dataset behaviour can be asserted."""

    def __init__(self, rows_by_dataset=None, raise_for=()):
        self.rows_by_dataset = rows_by_dataset or {}
        self.raise_for = raise_for
        self.queries = []

    def query(self, sql, location=None):
        self.queries.append((sql, location))
        for needle in self.raise_for:
            if f"`{needle}`" in sql:
                raise PermissionError(f"denied on {needle}")
        dataset = next(
            (d for d in self.rows_by_dataset if f"`{d}`" in sql), None
        )
        rows = self.rows_by_dataset.get(dataset, [])
        return _Result(rows)


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class TestPartitionIdParsing:
    @pytest.mark.parametrize("pid,expected", [
        ("2025", "2025-01-01"),            # YEAR
        ("202503", "2025-03-01"),          # MONTH
        ("20250307", "2025-03-07"),        # DAY
        ("2025030713", "2025-03-07"),      # HOUR — windows are whole days
    ])
    def test_widths_map_to_dates(self, pid, expected):
        assert partition_id_to_date(pid) == expected

    @pytest.mark.parametrize("pid", [
        None, "", "__NULL__", "__UNPARTITIONED__", "abc", "2025-03-07", "202513",
        "20250332", "123", "123456789",
    ])
    def test_unparseable_returns_none(self, pid):
        """Must not produce a malformed DATE literal for the emitted SQL."""
        assert partition_id_to_date(pid) is None

    @pytest.mark.parametrize("pid,gran", [
        ("2025", "YEAR"), ("202503", "MONTH"),
        ("20250307", "DAY"), ("2025030713", "HOUR"),
    ])
    def test_granularity_inferred_from_width(self, pid, gran):
        assert granularity_for_partition_id(pid) == gran

    def test_granularity_unknown_width(self):
        assert granularity_for_partition_id("12345") is None
        assert granularity_for_partition_id(None) is None


class TestResolvePartitionCounts:
    def test_reads_counts_and_bounds(self):
        client = _FakeClient({"ds": [_Row("t1", 730, "20240824", "20260823")]})
        entities = [_entity("t1", tp=TimePartitionConfig(type="DAY", field="d"))]
        stats = resolve_partition_counts(client, "proj", "US", entities)
        assert stats.partitions == {
            "ds.t1": PartitionInfo(count=730, min_id="20240824", max_id="20260823")
        }
        assert stats.basis == "measured"

    def test_excludes_sentinel_partition_ids(self):
        """__NULL__ is real data but not a date — it must not skew the bounds.
        (The generator emits a dedicated IS NULL chunk for those rows.)"""
        client = _FakeClient({"ds": []})
        resolve_partition_counts(
            client, "proj", "US",
            [_entity("t1", tp=TimePartitionConfig(type="DAY", field="d"))],
        )
        sql = client.queries[0][0]
        assert "__NULL__" in sql and "__UNPARTITIONED__" in sql
        assert "NOT IN" in sql
        assert "partition_id IS NOT NULL" in sql

    def test_query_is_dataset_scoped_not_region_qualified(self):
        """The region-<loc> form that TABLE_STORAGE uses returns 403 for
        PARTITIONS (probe-verified), so the view must be addressed per dataset."""
        client = _FakeClient({"ds": []})
        resolve_partition_counts(
            client, "proj", "US",
            [_entity("t1", tp=TimePartitionConfig(type="DAY", field="d"))],
        )
        sql, location = client.queries[0]
        assert "`proj`.`ds`.INFORMATION_SCHEMA.PARTITIONS" in sql
        assert "region-" not in sql
        assert location == "US", "job must still be pinned to the data's region"

    def test_skips_datasets_with_no_partitioned_tables(self):
        """Reading PARTITIONS for a dataset of unpartitioned tables is a wasted job."""
        client = _FakeClient({"ds": []})
        stats = resolve_partition_counts(
            client, "proj", "US", [_entity("plain", tp=None, rp=None)]
        )
        assert client.queries == []
        assert stats.basis == "unavailable"

    def test_skips_zero_byte_entities(self):
        client = _FakeClient({"ds": []})
        resolve_partition_counts(
            client, "proj", "US",
            [_entity("v", num_bytes=0, tp=TimePartitionConfig(type="DAY", field="d"))],
        )
        assert client.queries == []

    def test_range_partitioned_dataset_is_read(self):
        client = _FakeClient({"ds": []})
        resolve_partition_counts(
            client, "proj", "US",
            [_entity("r", rp=RangePartitionConfig(field="n", start=0, end=100, interval=1))],
        )
        assert len(client.queries) == 1

    def test_multi_region_map_queries_each_dataset_in_its_own_region(self):
        client = _FakeClient({
            "us_ds": [_Row("a", 200, "20240101", "20240501")],
            "eu_ds": [_Row("b", 300, "20240101", "20240601")],
        })
        tp = TimePartitionConfig(type="DAY", field="d")
        entities = [
            _entity("a", dataset="us_ds", tp=tp),
            _entity("b", dataset="eu_ds", tp=tp),
        ]
        stats = resolve_partition_counts(
            client, "proj", {"US": ["us_ds"], "EU": ["eu_ds"]}, entities
        )
        assert stats.partitions["us_ds.a"].count == 200
        assert stats.partitions["eu_ds.b"].count == 300
        locs = {loc for _sql, loc in client.queries}
        assert locs == {"US", "EU"}

    def test_failure_degrades_and_records_reason(self):
        """A permission failure must leave counts unresolved, not abort the run —
        the caller falls back to estimating."""
        client = _FakeClient({"ds": []}, raise_for=("ds",))
        stats = resolve_partition_counts(
            client, "proj", "US",
            [_entity("t1", tp=TimePartitionConfig(type="DAY", field="d"))],
        )
        assert stats.partitions == {}
        assert stats.basis == "unavailable"
        assert "PermissionError" in stats.failure_reason
        assert "PARTITIONS unavailable" in stats.source_note

    def test_partial_failure_is_reported_as_mixed(self):
        client = _FakeClient(
            {"good": [_Row("a", 400, "20240101", "20240701")], "bad": []},
            raise_for=("bad",),
        )
        tp = TimePartitionConfig(type="DAY", field="d")
        stats = resolve_partition_counts(
            client, "proj", {"US": ["good", "bad"]},
            [_entity("a", dataset="good", tp=tp), _entity("b", dataset="bad", tp=tp)],
        )
        assert stats.basis == "mixed"
        assert "good.a" in stats.partitions
        assert "bad" in stats.failure_reason

    def test_rows_without_a_min_id_are_dropped(self):
        """A non-partitioned table returns one row with a NULL partition_id."""
        client = _FakeClient({"ds": [_Row("t1", 1, None, None)]})
        stats = resolve_partition_counts(
            client, "proj", "US",
            [_entity("t1", tp=TimePartitionConfig(type="DAY", field="d"))],
        )
        assert stats.partitions == {}
