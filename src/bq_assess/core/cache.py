"""SQLite-based local metadata cache for scanned EntityMetadata (R5).

Stores all scanned metadata — entity type, population, nested columns, BOTH partitionings,
view/mview SQL, routines, and depends_on — so the Assessment can be regenerated offline
without re-scanning the Source (R5.3). Schema follows design.md § SQLite Cache Schema.

store/load round-trip is structurally lossless (R5.4 / property P8, owned by issue #10).
Issue #9 / 1.4.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from bq_assess.models import (
    ColumnSchema,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    RangePartitionConfig,
    RoutineMetadata,
    TimePartitionConfig,
)

_CREATE_SCAN_METADATA = """\
CREATE TABLE IF NOT EXISTS scan_metadata (
    project_id   TEXT PRIMARY KEY,
    scanned_at   TIMESTAMP NOT NULL,
    entity_count INTEGER  NOT NULL
);
"""

_CREATE_ENTITIES = """\
CREATE TABLE IF NOT EXISTS entities (
    project_id        TEXT NOT NULL,
    dataset_id        TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    full_name         TEXT NOT NULL,
    entity_type       TEXT NOT NULL,
    population        TEXT NOT NULL,
    num_rows          INTEGER,
    num_bytes         INTEGER,
    columns_json      TEXT NOT NULL,
    time_part_json    TEXT,
    range_part_json   TEXT,
    clustering_json   TEXT,
    view_query        TEXT,
    mview_query       TEXT,
    routine_json      TEXT,
    depends_on_json   TEXT,
    last_modified     TIMESTAMP,
    PRIMARY KEY (project_id, dataset_id, entity_id)
);
"""

# Mid-scan checkpointing (2026-07-28 scale review, finding #5): an interrupted
# scan of a large project must not restart from zero. A scan_session row exists
# while a scan is in flight (deleted on completion); scan_progress marks each
# dataset whose chunk finished. A leftover session + progress rows = a resumable
# partial scan. filter_key pins the resume to the same --datasets selection —
# resuming a differently-filtered scan would silently merge two scopes.
_CREATE_SCAN_SESSION = """\
CREATE TABLE IF NOT EXISTS scan_session (
    project_id  TEXT PRIMARY KEY,
    filter_key  TEXT NOT NULL,
    started_at  TIMESTAMP NOT NULL
);
"""

_CREATE_SCAN_PROGRESS = """\
CREATE TABLE IF NOT EXISTS scan_progress (
    project_id   TEXT NOT NULL,
    dataset_id   TEXT NOT NULL,
    completed_at TIMESTAMP NOT NULL,
    PRIMARY KEY (project_id, dataset_id)
);
"""

# A partial scan older than this is discarded rather than resumed: metadata
# drifts (tables created/dropped/modified), and finishing yesterday's scan
# would stitch two inconsistent snapshots into one bundle.
CHECKPOINT_MAX_AGE_HOURS = 24


# ---------------------------------------------------------------------------
# (De)serialization helpers
# ---------------------------------------------------------------------------


def _column_schema_to_dict(col: ColumnSchema) -> dict:
    """Recursively serialize a ColumnSchema (nesting preserved)."""
    return {
        "name": col.name,
        "field_type": col.field_type,
        "mode": col.mode,
        "fields": [_column_schema_to_dict(f) for f in col.fields],
    }


def _dict_to_column_schema(d: dict) -> ColumnSchema:
    """Recursively deserialize a dict back to a ColumnSchema."""
    return ColumnSchema(
        name=d["name"],
        field_type=d["field_type"],
        mode=d["mode"],
        fields=[_dict_to_column_schema(f) for f in d.get("fields", [])],
    )


def _routine_to_dict(r: RoutineMetadata) -> dict:
    return {
        "name": r.name,
        "language": r.language,
        "arguments": list(r.arguments),
        "body": r.body,
        "routine_type": r.routine_type,
    }


def _dict_to_routine(d: dict) -> RoutineMetadata:
    return RoutineMetadata(
        name=d["name"],
        language=d["language"],
        arguments=list(d.get("arguments", [])),
        body=d["body"],
        routine_type=d["routine_type"],
    )


class MetadataCache:
    """SQLite-based local storage for scanned EntityMetadata."""

    def __init__(self, db_path: str = ".bq-assess-cache.db") -> None:
        self._conn = sqlite3.connect(db_path)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_CREATE_SCAN_METADATA)
        self._conn.execute(_CREATE_ENTITIES)
        self._conn.execute(_CREATE_SCAN_SESSION)
        self._conn.execute(_CREATE_SCAN_PROGRESS)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    _INSERT_ENTITY = (
        "INSERT OR REPLACE INTO entities "
        "(project_id, dataset_id, entity_id, full_name, entity_type, population, "
        "num_rows, num_bytes, columns_json, time_part_json, range_part_json, "
        "clustering_json, view_query, mview_query, routine_json, depends_on_json, "
        "last_modified) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )

    @staticmethod
    def _entity_row(project_id: str, e: EntityMetadata) -> tuple:
        return (
            project_id,
            e.dataset_id,
            e.entity_id,
            e.full_name,
            e.entity_type.value,
            e.population.value,
            e.num_rows,
            e.num_bytes,
            json.dumps([_column_schema_to_dict(c) for c in e.columns]),
            MetadataCache._dump_time_part(e.time_partitioning),
            MetadataCache._dump_range_part(e.range_partitioning),
            json.dumps(e.clustering_fields) if e.clustering_fields is not None else None,
            e.view_query,
            e.mview_query,
            json.dumps(_routine_to_dict(e.routine)) if e.routine is not None else None,
            json.dumps(e.depends_on),
            e.last_modified.isoformat() if e.last_modified is not None else None,
        )

    def store(self, project_id: str, entities: list[EntityMetadata]) -> None:
        """Store scanned metadata, replacing any existing data for the project (R5.1).

        Also clears any in-flight checkpoint session — a completed store IS the
        finished scan, so partial progress markers must not survive it.
        """
        cur = self._conn.cursor()
        cur.execute("DELETE FROM entities WHERE project_id = ?", (project_id,))
        cur.execute("DELETE FROM scan_metadata WHERE project_id = ?", (project_id,))
        cur.executemany(
            self._INSERT_ENTITY,
            (self._entity_row(project_id, e) for e in entities),
        )
        cur.execute(
            "INSERT INTO scan_metadata (project_id, scanned_at, entity_count) VALUES (?, ?, ?)",
            (project_id, datetime.now(timezone.utc).isoformat(), len(entities)),
        )
        self._clear_session(cur, project_id)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Mid-scan checkpointing (resume support)
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_key(dataset_filter: list[str] | None) -> str:
        return ",".join(sorted(dataset_filter)) if dataset_filter else "*"

    def begin_scan_session(
        self, project_id: str, dataset_filter: list[str] | None
    ) -> set[str]:
        """Start (or resume) a checkpointed scan; return dataset_ids already done.

        Returns the completed-dataset set of a resumable prior session — same
        project, same dataset filter, younger than CHECKPOINT_MAX_AGE_HOURS.
        Anything else (different filter, stale, none) starts clean: progress and
        partial entities for the project are discarded first.
        """
        cur = self._conn.cursor()
        key = self._filter_key(dataset_filter)
        cur.execute(
            "SELECT filter_key, started_at FROM scan_session WHERE project_id = ?",
            (project_id,),
        )
        row = cur.fetchone()
        if row is not None:
            prior_key, started_at = row
            age_hours = (
                datetime.now(timezone.utc) - datetime.fromisoformat(started_at)
            ).total_seconds() / 3600
            if prior_key == key and age_hours <= CHECKPOINT_MAX_AGE_HOURS:
                cur.execute(
                    "SELECT dataset_id FROM scan_progress WHERE project_id = ?",
                    (project_id,),
                )
                return {r[0] for r in cur.fetchall()}

        # No resumable session — start clean. Checkpoints write into the same
        # entities table, so a fresh session invalidates any completed cache
        # too: mixing a new partial snapshot over old completed rows would let
        # an interrupted scan corrupt the "completed" cache. The operator chose
        # to rescan; a successful store() rebuilds the completed cache anyway.
        self._clear_session(cur, project_id)
        cur.execute("DELETE FROM entities WHERE project_id = ?", (project_id,))
        cur.execute("DELETE FROM scan_metadata WHERE project_id = ?", (project_id,))
        cur.execute(
            "INSERT INTO scan_session (project_id, filter_key, started_at) VALUES (?, ?, ?)",
            (project_id, key, datetime.now(timezone.utc).isoformat()),
        )
        self._conn.commit()
        return set()

    def checkpoint_datasets(
        self, project_id: str, dataset_ids: list[str], entities: list[EntityMetadata]
    ) -> None:
        """Persist one finished chunk: its entities + per-dataset progress markers.

        Committed atomically — a crash mid-checkpoint loses at most this chunk.
        """
        cur = self._conn.cursor()
        cur.executemany(
            self._INSERT_ENTITY,
            (self._entity_row(project_id, e) for e in entities),
        )
        now = datetime.now(timezone.utc).isoformat()
        cur.executemany(
            "INSERT OR REPLACE INTO scan_progress (project_id, dataset_id, completed_at) "
            "VALUES (?, ?, ?)",
            ((project_id, ds, now) for ds in dataset_ids),
        )
        self._conn.commit()

    def load_checkpointed(
        self, project_id: str, dataset_ids: set[str]
    ) -> list[EntityMetadata]:
        """Load the entities of already-completed datasets for a resumed scan."""
        if not dataset_ids:
            return []
        all_rows = self.load(project_id, include_partial=True) or []
        return [e for e in all_rows if e.dataset_id in dataset_ids]

    def _clear_session(self, cur, project_id: str) -> None:
        cur.execute("DELETE FROM scan_session WHERE project_id = ?", (project_id,))
        cur.execute("DELETE FROM scan_progress WHERE project_id = ?", (project_id,))

    def load(
        self, project_id: str, include_partial: bool = False
    ) -> list[EntityMetadata] | None:
        """Load cached metadata for a project; None if no cache exists (R5.3).

        ``include_partial=True`` skips the completed-scan gate so a resumed scan
        can read its checkpointed rows (no scan_metadata row exists mid-scan).
        """
        if not include_partial and not self.has_cache(project_id):
            return None

        cur = self._conn.cursor()
        cur.execute(
            "SELECT dataset_id, entity_id, full_name, entity_type, population, num_rows, "
            "num_bytes, columns_json, time_part_json, range_part_json, clustering_json, "
            "view_query, mview_query, routine_json, depends_on_json, last_modified "
            "FROM entities WHERE project_id = ?",
            (project_id,),
        )

        result: list[EntityMetadata] = []
        for row in cur.fetchall():
            (
                dataset_id, entity_id, full_name, entity_type, population, num_rows,
                num_bytes, columns_json, time_part_json, range_part_json, clustering_json,
                view_query, mview_query, routine_json, depends_on_json, last_modified_str,
            ) = row

            result.append(EntityMetadata(
                entity_id=entity_id,
                dataset_id=dataset_id,
                full_name=full_name,
                entity_type=EntityType(entity_type),
                population=EntityPopulation(population),
                num_rows=num_rows,
                num_bytes=num_bytes,
                columns=[_dict_to_column_schema(d) for d in json.loads(columns_json)],
                time_partitioning=self._load_time_part(time_part_json),
                range_partitioning=self._load_range_part(range_part_json),
                clustering_fields=json.loads(clustering_json) if clustering_json is not None else None,
                view_query=view_query,
                mview_query=mview_query,
                routine=_dict_to_routine(json.loads(routine_json)) if routine_json is not None else None,
                depends_on=json.loads(depends_on_json) if depends_on_json is not None else [],
                last_modified=datetime.fromisoformat(last_modified_str) if last_modified_str else None,
            ))

        return result

    def has_cache(self, project_id: str) -> bool:
        """Check if cached data exists for a project (R5.2)."""
        cur = self._conn.cursor()
        cur.execute("SELECT 1 FROM scan_metadata WHERE project_id = ? LIMIT 1", (project_id,))
        return cur.fetchone() is not None

    # ------------------------------------------------------------------
    # Partition (de)serialization
    # ------------------------------------------------------------------

    @staticmethod
    def _dump_time_part(tp: TimePartitionConfig | None) -> str | None:
        if tp is None:
            return None
        return json.dumps({"type": tp.type, "field": tp.field})

    @staticmethod
    def _load_time_part(raw: str | None) -> TimePartitionConfig | None:
        if raw is None:
            return None
        d = json.loads(raw)
        return TimePartitionConfig(type=d["type"], field=d["field"])

    @staticmethod
    def _dump_range_part(rp: RangePartitionConfig | None) -> str | None:
        if rp is None:
            return None
        return json.dumps(
            {"field": rp.field, "start": rp.start, "end": rp.end, "interval": rp.interval}
        )

    @staticmethod
    def _load_range_part(raw: str | None) -> RangePartitionConfig | None:
        if raw is None:
            return None
        d = json.loads(raw)
        return RangePartitionConfig(
            field=d["field"], start=d["start"], end=d["end"], interval=d["interval"]
        )
