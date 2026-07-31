"""Unit tests for MetadataCache (SQLite EntityMetadata storage) — R5.1, R5.2, R5.3.

The P8 round-trip Hypothesis property is owned by issue #10 (1.5); these unit tests pin
store/load on known entities (tables, nested structs, both partitionings, views, routines)
and the has_cache / overwrite behavior for issue #9 (1.4).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from bq_assess.core.cache import MetadataCache
from bq_assess.models import (
    ColumnSchema,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    RangePartitionConfig,
    RoutineMetadata,
    TimePartitionConfig,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _simple_table(entity_id: str = "users", dataset_id: str = "analytics") -> EntityMetadata:
    """A plain table — no partitioning, no clustering."""
    return EntityMetadata(
        entity_id=entity_id,
        dataset_id=dataset_id,
        full_name=f"{dataset_id}.{entity_id}",
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=1000,
        num_bytes=50000,
        columns=[
            ColumnSchema(name="id", field_type="INT64", mode="REQUIRED", fields=[]),
            ColumnSchema(name="name", field_type="STRING", mode="NULLABLE", fields=[]),
        ],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc),
    )


def _nested_struct_table() -> EntityMetadata:
    """Table with nested STRUCT columns + clean time partitioning (recursive serialization)."""
    return EntityMetadata(
        entity_id="events",
        dataset_id="tracking",
        full_name="tracking.events",
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=5_000_000,
        num_bytes=2_000_000_000,
        columns=[
            ColumnSchema(name="event_id", field_type="STRING", mode="REQUIRED", fields=[]),
            ColumnSchema(
                name="payload",
                field_type="STRUCT",
                mode="NULLABLE",
                fields=[
                    ColumnSchema(name="action", field_type="STRING", mode="NULLABLE", fields=[]),
                    ColumnSchema(
                        name="context",
                        field_type="STRUCT",
                        mode="NULLABLE",
                        fields=[
                            ColumnSchema(name="page", field_type="STRING", mode="NULLABLE", fields=[]),
                            ColumnSchema(name="referrer", field_type="STRING", mode="NULLABLE", fields=[]),
                        ],
                    ),
                ],
            ),
            ColumnSchema(name="tags", field_type="ARRAY", mode="REPEATED", fields=[]),
        ],
        time_partitioning=TimePartitionConfig(type="DAY", field="created_at"),
        range_partitioning=None,
        clustering_fields=["event_id", "payload"],
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2025, 1, 10, 8, 30, 0, tzinfo=timezone.utc),
    )


def _range_partitioned_table() -> EntityMetadata:
    """Table with range partitioning (R3.8) — distinct from time partitioning."""
    return EntityMetadata(
        entity_id="orders",
        dataset_id="sales",
        full_name="sales.orders",
        entity_type=EntityType.TABLE,
        population=EntityPopulation.TABLE,
        num_rows=100_000_000,
        num_bytes=80_000_000_000,
        columns=[
            ColumnSchema(name="order_id", field_type="INT64", mode="REQUIRED", fields=[]),
            ColumnSchema(name="customer_id", field_type="INT64", mode="REQUIRED", fields=[]),
        ],
        time_partitioning=None,
        range_partitioning=RangePartitionConfig(field="customer_id", start=0, end=1000, interval=10),
        clustering_fields=["customer_id"],
        view_query=None,
        mview_query=None,
        routine=None,
        depends_on=[],
        last_modified=datetime(2025, 3, 20, 16, 45, 0, tzinfo=timezone.utc),
    )


def _view_entity() -> EntityMetadata:
    """A VIEW (REBUILT population) carrying view_query + dependency links."""
    return EntityMetadata(
        entity_id="active_users",
        dataset_id="analytics",
        full_name="analytics.active_users",
        entity_type=EntityType.VIEW,
        population=EntityPopulation.REBUILT,
        num_rows=0,
        num_bytes=0,
        columns=[ColumnSchema(name="id", field_type="INT64", mode="NULLABLE", fields=[])],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query="SELECT id FROM analytics.users WHERE active",
        mview_query=None,
        routine=None,
        depends_on=["analytics.users"],
        last_modified=datetime(2025, 2, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _routine_entity() -> EntityMetadata:
    """A ROUTINE (REBUILT population) carrying RoutineMetadata."""
    return EntityMetadata(
        entity_id="to_upper",
        dataset_id="udfs",
        full_name="udfs.to_upper",
        entity_type=EntityType.ROUTINE,
        population=EntityPopulation.REBUILT,
        num_rows=0,
        num_bytes=0,
        columns=[],
        time_partitioning=None,
        range_partitioning=None,
        clustering_fields=None,
        view_query=None,
        mview_query=None,
        routine=RoutineMetadata(
            name="to_upper",
            language="JAVASCRIPT",
            arguments=["s"],
            body="return s.toUpperCase();",
            routine_type="SCALAR_FUNCTION",
        ),
        depends_on=[],
        last_modified=datetime(2025, 4, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _assert_entity_equal(original: EntityMetadata, loaded: EntityMetadata) -> None:
    assert loaded.entity_id == original.entity_id
    assert loaded.dataset_id == original.dataset_id
    assert loaded.full_name == original.full_name
    assert loaded.entity_type == original.entity_type
    assert loaded.population == original.population
    assert loaded.num_rows == original.num_rows
    assert loaded.num_bytes == original.num_bytes
    assert loaded.last_modified == original.last_modified
    assert loaded.clustering_fields == original.clustering_fields
    assert loaded.view_query == original.view_query
    assert loaded.mview_query == original.mview_query
    assert loaded.depends_on == original.depends_on

    if original.time_partitioning is None:
        assert loaded.time_partitioning is None
    else:
        assert loaded.time_partitioning is not None
        assert loaded.time_partitioning.type == original.time_partitioning.type
        assert loaded.time_partitioning.field == original.time_partitioning.field

    if original.range_partitioning is None:
        assert loaded.range_partitioning is None
    else:
        assert loaded.range_partitioning is not None
        assert loaded.range_partitioning.field == original.range_partitioning.field
        assert loaded.range_partitioning.start == original.range_partitioning.start
        assert loaded.range_partitioning.end == original.range_partitioning.end
        assert loaded.range_partitioning.interval == original.range_partitioning.interval

    if original.routine is None:
        assert loaded.routine is None
    else:
        assert loaded.routine is not None
        assert loaded.routine.name == original.routine.name
        assert loaded.routine.language == original.routine.language
        assert loaded.routine.arguments == original.routine.arguments
        assert loaded.routine.body == original.routine.body
        assert loaded.routine.routine_type == original.routine.routine_type

    _assert_columns_equal(original.columns, loaded.columns)


def _assert_columns_equal(original: list[ColumnSchema], loaded: list[ColumnSchema]) -> None:
    assert len(loaded) == len(original)
    for orig, back in zip(original, loaded):
        assert back.name == orig.name
        assert back.field_type == orig.field_type
        assert back.mode == orig.mode
        _assert_columns_equal(orig.fields, back.fields)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStoreAndLoad:
    """store/load with known entities — R5.1."""

    def test_simple_table(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        e = _simple_table()
        cache.store("my-project", [e])
        loaded = cache.load("my-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(e, loaded[0])

    def test_nested_struct(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        e = _nested_struct_table()
        cache.store("struct-project", [e])
        loaded = cache.load("struct-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(e, loaded[0])
        # nesting preserved
        payload = loaded[0].columns[1]
        assert payload.field_type == "STRUCT"
        assert payload.fields[1].name == "context"
        assert payload.fields[1].fields[0].name == "page"

    def test_range_partitioned(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        e = _range_partitioned_table()
        cache.store("range-project", [e])
        loaded = cache.load("range-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(e, loaded[0])
        assert loaded[0].range_partitioning is not None
        assert loaded[0].time_partitioning is None

    def test_view_entity(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        e = _view_entity()
        cache.store("view-project", [e])
        loaded = cache.load("view-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(e, loaded[0])
        assert loaded[0].population is EntityPopulation.REBUILT
        assert loaded[0].view_query is not None
        assert loaded[0].depends_on == ["analytics.users"]

    def test_routine_entity(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        e = _routine_entity()
        cache.store("routine-project", [e])
        loaded = cache.load("routine-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(e, loaded[0])
        assert loaded[0].routine is not None
        assert loaded[0].routine.language == "JAVASCRIPT"

    def test_multiple_entities(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        entities = [
            _simple_table(),
            _nested_struct_table(),
            _range_partitioned_table(),
            _view_entity(),
            _routine_entity(),
        ]
        cache.store("multi-project", entities)
        loaded = cache.load("multi-project")
        assert loaded is not None and len(loaded) == 5

        key = lambda e: (e.dataset_id, e.entity_id)
        for orig, back in zip(sorted(entities, key=key), sorted(loaded, key=key)):
            _assert_entity_equal(orig, back)


class TestHasCache:
    """has_cache / load-miss behavior — R5.2."""

    def test_has_cache_false_for_unknown(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        assert cache.has_cache("unknown-project") is False

    def test_load_none_for_unknown(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        assert cache.load("unknown-project") is None

    def test_has_cache_true_after_store(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        cache.store("my-project", [_simple_table()])
        assert cache.has_cache("my-project") is True


class TestOverwriteBehavior:
    """Re-storing replaces previous data — R5.1."""

    def test_second_store_replaces_first(self, tmp_path) -> None:
        cache = MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))
        first = [
            _simple_table(entity_id="old_a", dataset_id="ds1"),
            _simple_table(entity_id="old_b", dataset_id="ds1"),
        ]
        cache.store("overwrite-project", first)
        assert len(cache.load("overwrite-project")) == 2

        second = [_range_partitioned_table()]
        cache.store("overwrite-project", second)
        loaded = cache.load("overwrite-project")
        assert loaded is not None and len(loaded) == 1
        _assert_entity_equal(second[0], loaded[0])
        assert {e.entity_id for e in loaded} == {"orders"}


class TestCheckpointResume:
    """Mid-scan checkpointing (R5, 2026-07-28 scale review finding #5)."""

    def _cache(self, tmp_path) -> MetadataCache:
        return MetadataCache(db_path=os.path.join(str(tmp_path), "cache.db"))

    def test_fresh_session_returns_no_done_datasets(self, tmp_path) -> None:
        cache = self._cache(tmp_path)
        assert cache.begin_scan_session("proj", None) == set()

    def test_resume_returns_checkpointed_datasets_and_entities(self, tmp_path) -> None:
        """Interrupted scan → new session resumes: done datasets skipped, their
        entities loadable."""
        cache = self._cache(tmp_path)
        cache.begin_scan_session("proj", None)
        e1 = _simple_table("t1", "ds_a")
        e2 = _simple_table("t2", "ds_a")
        e3 = _simple_table("t3", "ds_b")
        cache.checkpoint_datasets("proj", ["ds_a"], [e1, e2])
        cache.checkpoint_datasets("proj", ["ds_b"], [e3])
        # simulate crash: no store() — new collector run begins a session
        done = cache.begin_scan_session("proj", None)
        assert done == {"ds_a", "ds_b"}
        loaded = cache.load_checkpointed("proj", done)
        assert {e.full_name for e in loaded} == {"ds_a.t1", "ds_a.t2", "ds_b.t3"}

    def test_different_filter_discards_checkpoint(self, tmp_path) -> None:
        """A resume with a different --datasets selection must start clean —
        merging two scopes would silently mix snapshots."""
        cache = self._cache(tmp_path)
        cache.begin_scan_session("proj", ["ds_a"])
        cache.checkpoint_datasets("proj", ["ds_a"], [_simple_table("t1", "ds_a")])
        done = cache.begin_scan_session("proj", ["ds_a", "ds_b"])
        assert done == set()
        assert cache.load_checkpointed("proj", {"ds_a"}) == []

    def test_stale_checkpoint_discarded(self, tmp_path) -> None:
        """A checkpoint older than CHECKPOINT_MAX_AGE_HOURS is not resumed —
        finishing yesterday's scan would stitch two inconsistent snapshots."""
        import sqlite3
        from datetime import timedelta

        cache = self._cache(tmp_path)
        cache.begin_scan_session("proj", None)
        cache.checkpoint_datasets("proj", ["ds_a"], [_simple_table("t1", "ds_a")])
        # age the session past the TTL
        old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        conn = sqlite3.connect(os.path.join(str(tmp_path), "cache.db"))
        conn.execute("UPDATE scan_session SET started_at = ?", (old,))
        conn.commit()
        conn.close()
        assert cache.begin_scan_session("proj", None) == set()

    def test_completed_store_clears_session(self, tmp_path) -> None:
        """store() (a finished scan) removes the session + progress markers —
        the next run must not 'resume' a scan that already completed."""
        cache = self._cache(tmp_path)
        cache.begin_scan_session("proj", None)
        e = _simple_table("t1", "ds_a")
        cache.checkpoint_datasets("proj", ["ds_a"], [e])
        cache.store("proj", [e])
        assert cache.has_cache("proj")
        assert cache.begin_scan_session("proj", None) == set()

    def test_fresh_session_invalidates_completed_cache(self, tmp_path) -> None:
        """Starting a new checkpointed scan drops the completed cache — partial
        checkpoint rows must never mix with a prior completed snapshot."""
        cache = self._cache(tmp_path)
        cache.store("proj", [_simple_table("old", "ds_a")])
        assert cache.has_cache("proj")
        cache.begin_scan_session("proj", None)
        assert not cache.has_cache("proj")
        assert cache.load("proj") is None

    def test_checkpoint_survives_new_cache_instance(self, tmp_path) -> None:
        """Resume works across process restarts (fresh MetadataCache object)."""
        path = os.path.join(str(tmp_path), "cache.db")
        c1 = MetadataCache(db_path=path)
        c1.begin_scan_session("proj", None)
        c1.checkpoint_datasets("proj", ["ds_a"], [_simple_table("t1", "ds_a")])
        c2 = MetadataCache(db_path=path)
        done = c2.begin_scan_session("proj", None)
        assert done == {"ds_a"}
        assert len(c2.load_checkpointed("proj", done)) == 1
