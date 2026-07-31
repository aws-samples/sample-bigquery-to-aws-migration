"""Per-entity Storage Target advisor: Iceberg (default) vs RMS (ADR-0005).

Iceberg (S3 Tables) is the universal default — the only storage both engines can
query, the open/exit-safe format. RMS is recommended per entity only when the
primary Query Engine is Redshift AND the entity shows RMS-favoring signals, each
traceable to a doc-verified trade-off (2026-07-22 gap analysis):

- Type fidelity: GEOGRAPHY (native spatial vs WKT string), JSON (SUPER + PartiQL
  vs string), INTERVAL (native vs ISO-8601 string) survive on RMS, degrade on Iceberg.
- Latency: result caching, AutoMV, automatic query rewrite, zone maps/sort keys are
  RMS-only — sub-second SLA workloads need them.
- Constraints: NOT NULL is enforced on RMS, not enforceable on Iceberg.

Size pushes the other way: the RMS storage premium and vendor-exit cost (UNLOAD)
grow with bytes, so large tables stay on Iceberg unless fidelity loss is severe.

Load path for RMS entities is two-phase (Athena remains the sole BQ-extraction
engine): phase 1 lands data in the Iceberg staging table exactly as today; phase 2
runs in Redshift — native CREATE TABLE, INSERT INTO … SELECT from the Iceberg
external schema, then optionally drop the staging table.
"""
from __future__ import annotations

from bq_assess.models import (
    ColumnSchema,
    ConfidenceLevel,
    ConversionResult,
    EntityMetadata,
    EntityType,
    StoragePlacement,
    StorageTarget,
)
from bq_assess.targets.iceberg.identifiers import quote_identifier

_ONE_GB = 1024**3

# Above this size the storage premium + exit cost outweigh fidelity/latency wins
# unless the entity carries high-fidelity types AND is small enough to UNLOAD fast.
_RMS_SIZE_CEILING_BYTES = 500 * _ONE_GB

# BQ types whose Iceberg mapping is lossy but whose RMS mapping is native.
# GEOGRAPHY → GEOMETRY/GEOGRAPHY (spatial functions); JSON → SUPER (PartiQL);
# INTERVAL → INTERVAL Y2M/D2S. (Doc-verified 2026-07-22.)
_RMS_FIDELITY_TYPES = {"GEOGRAPHY", "JSON", "INTERVAL"}

# Redshift external schema over the migrated Iceberg Glue database. Single source:
# the generated phase-2 load SQL and every doc line that says CREATE EXTERNAL SCHEMA
# must import this name, never re-hardcode the literal.
ICEBERG_EXTERNAL_SCHEMA = "iceberg"

# BQ scalar → Redshift native type (bounded map, ADR-0005: types + NOT NULL only;
# DISTKEY/SORTKEY/node-sizing stay retired per ADR-0001). Key coverage must stay a
# superset of the converter's BQ alias set (CLEAN_TYPE_MAP / LOSSY_TYPE_MAP in
# targets/iceberg/converter.py) — pinned by test_storage_placement type-parity test.
_BQ_TO_REDSHIFT = {
    "STRING": "VARCHAR(65535)",
    "BYTES": "VARBYTE(1024000)",
    "INT64": "BIGINT",
    "INTEGER": "BIGINT",
    "INT": "BIGINT",
    "SMALLINT": "BIGINT",
    "BIGINT": "BIGINT",
    "TINYINT": "BIGINT",
    "BYTEINT": "BIGINT",
    "FLOAT64": "DOUBLE PRECISION",
    "FLOAT": "DOUBLE PRECISION",
    "NUMERIC": "DECIMAL(38,9)",
    "BIGNUMERIC": "DECIMAL(38,9)",   # precision >38 truncates — flagged by converter
    "BOOL": "BOOLEAN",
    "BOOLEAN": "BOOLEAN",
    "DATE": "DATE",
    "DATETIME": "TIMESTAMP",
    "TIME": "TIME",
    "TIMESTAMP": "TIMESTAMPTZ",
    "GEOGRAPHY": "GEOMETRY",         # native spatial — the fidelity win
    "JSON": "SUPER",                 # PartiQL-queryable — the fidelity win
    "INTERVAL": "INTERVAL DAY TO SECOND",
    "STRUCT": "SUPER",
    "RECORD": "SUPER",
}


class StoragePlacementAdvisor:
    """Recommend a Storage Target per TABLE entity (ADR-0005).

    Only meaningful when the primary Query Engine is Redshift — the CLI must not
    invoke this on the Athena path (Athena cannot query RMS-native tables).
    """

    def __init__(self, query_sla_ms: int = 30_000, iceberg_schema: str = ICEBERG_EXTERNAL_SCHEMA):
        self._sla_ms = query_sla_ms
        # External-schema name Redshift uses to read the Iceberg staging table
        self._iceberg_schema = iceberg_schema

    def recommend(
        self,
        entity: EntityMetadata,
        conversion: ConversionResult | None = None,
    ) -> StoragePlacement:
        if entity.entity_type != EntityType.TABLE:
            return self._iceberg([("Only TABLE entities carry a storage placement — "
                                  "views/MVs/UDFs are engine-layer objects (ADR-0004)")])

        size_bytes = entity.num_bytes or 0
        fidelity_cols = self._fidelity_columns(entity.columns)
        required_cols = [c.name for c in entity.columns if c.mode == "REQUIRED"]
        sub_second_sla = self._sla_ms < 1000

        signals: list[str] = []

        if fidelity_cols:
            type_list = ", ".join(sorted({c.field_type.upper() for c in fidelity_cols}))
            col_list = ", ".join(c.name for c in fidelity_cols[:5])
            signals.append(
                f"Type fidelity: {type_list} column(s) ({col_list}) map to native Redshift "
                f"types on RMS (GEOMETRY/SUPER/INTERVAL) but degrade to strings on Iceberg"
            )
        if sub_second_sla:
            signals.append(
                f"Sub-second SLA ({self._sla_ms}ms): result caching, AutoMV, auto-rewrite, "
                f"and zone maps are RMS-only — Iceberg cannot meet repeated-query latency"
            )
        if required_cols:
            signals.append(
                f"{len(required_cols)} REQUIRED column(s): NOT NULL is enforced on RMS, "
                f"not enforceable on Iceberg (informational — does not force RMS alone)"
            )

        # RMS placement requires a per-entity fidelity signal. The SLA is a
        # workload-global flag — letting it flip storage alone would move the
        # entire estate of small tables to RMS from one config value, inverting
        # ADR-0005's "Iceberg default, RMS exception". SLA and NOT NULL only
        # reinforce a fidelity-driven placement (they appear in the signals).
        if not fidelity_cols:
            if sub_second_sla:
                signals.append(
                    "Staying on Iceberg: the sub-second SLA is workload-global, not "
                    "entity-specific — without a type-fidelity signal it does not "
                    "justify leaving the multi-engine storage layer. Consider RMS "
                    "materialization only for the specific hot entities behind the SLA"
                )
                return StoragePlacement(
                    target=StorageTarget.ICEBERG,
                    signals=signals,
                    confidence=ConfidenceLevel.MEDIUM,
                )
            return self._iceberg(
                [("No RMS-favoring signals — Iceberg keeps multi-engine access and the "
                 "open format at lower storage cost")]
            )

        if size_bytes > _RMS_SIZE_CEILING_BYTES:
            signals.append(
                f"Size gate: {size_bytes / _ONE_GB:.0f} GB exceeds the "
                f"{_RMS_SIZE_CEILING_BYTES // _ONE_GB} GB RMS ceiling — storage premium and "
                f"vendor-exit (UNLOAD) cost outweigh the wins; staying on Iceberg. "
                f"The ceiling is practitioner judgment, not an AWS-published threshold "
                f"(no such guidance exists; verified 2026-07-22)"
            )
            return StoragePlacement(
                target=StorageTarget.ICEBERG,
                signals=signals,
                confidence=ConfidenceLevel.HIGH,
            )

        ddl = self._redshift_ddl(entity)
        load = self._redshift_load(entity, fidelity_cols)
        return StoragePlacement(
            target=StorageTarget.RMS,
            signals=signals,
            confidence=ConfidenceLevel.HIGH,
            redshift_ddl=ddl,
            redshift_load=load,
        )

    # ---- helpers -----------------------------------------------------------

    def _iceberg(self, signals: list[str]) -> StoragePlacement:
        return StoragePlacement(
            target=StorageTarget.ICEBERG,
            signals=signals,
            confidence=ConfidenceLevel.HIGH,
        )

    def _fidelity_columns(self, columns: list[ColumnSchema]) -> list[ColumnSchema]:
        return [c for c in columns if c.field_type.upper() in _RMS_FIDELITY_TYPES]

    def _redshift_type(self, col: ColumnSchema) -> str:
        if col.mode == "REPEATED":
            return "SUPER"          # arrays land in SUPER on RMS
        return _BQ_TO_REDSHIFT.get(col.field_type.upper(), "VARCHAR(65535)")

    @staticmethod
    def _table_part(entity: EntityMetadata) -> str:
        """Table part of the shared 'dataset.entity' key, tolerant of a dot-less name
        (mirrors engine/optimization.iceberg_table_name's fallback)."""
        parts = entity.full_name.split(".", 1)
        return parts[1] if len(parts) == 2 else entity.full_name

    def _redshift_ddl(self, entity: EntityMetadata) -> str:
        """Bounded native DDL: types + NOT NULL only (ADR-0005). No DISTKEY/SORTKEY."""
        dataset = quote_identifier(entity.dataset_id)
        table = quote_identifier(self._table_part(entity))
        col_lines = []
        for col in entity.columns:
            line = f"    {quote_identifier(col.name)} {self._redshift_type(col)}"
            if col.mode == "REQUIRED":
                line += " NOT NULL"
            col_lines.append(line)
        cols = ",\n".join(col_lines)
        return f"CREATE TABLE {dataset}.{table} (\n{cols}\n);"

    def _redshift_load(
        self, entity: EntityMetadata, fidelity_cols: list[ColumnSchema]
    ) -> list[str]:
        """Phase-2 statements (run in Redshift after the Athena→Iceberg load).

        SELECT * is safe here: the staging Iceberg table is generated by our own
        converter with identical column order. GEOMETRY/SUPER columns need explicit
        casts from their Iceberg string forms — emitted as a per-column SELECT when
        fidelity columns exist.
        """
        dataset = quote_identifier(entity.dataset_id)
        table = quote_identifier(self._table_part(entity))
        target = f"{dataset}.{table}"
        # Staging schemas are per-dataset: the Iceberg staging tables live in
        # per-dataset S3 Tables namespaces, and a Redshift external schema maps
        # exactly ONE Glue database — a single flat "iceberg" schema cannot
        # reach tables across namespaces (live-verified 2026-07-30).
        staging_schema = f"{self._iceberg_schema}_{entity.dataset_id}"
        staging = f"{quote_identifier(staging_schema)}.{table}"

        fidelity = {c.name for c in fidelity_cols}
        if fidelity:
            select_cols = []
            for col in entity.columns:
                ident = quote_identifier(col.name)
                ftype = col.field_type.upper()
                if col.name in fidelity and ftype == "GEOGRAPHY":
                    select_cols.append(f"ST_GeomFromText({ident})")
                elif col.name in fidelity and ftype == "JSON":
                    select_cols.append(f"JSON_PARSE({ident})")
                else:
                    # INTERVAL strings cast implicitly; everything else passes through
                    select_cols.append(ident)
            select = ",\n    ".join(select_cols)
            insert = (
                f"INSERT INTO {target}\nSELECT\n    {select}\nFROM {staging};"
            )
        else:
            insert = f"INSERT INTO {target}\nSELECT * FROM {staging};"

        return [
            insert,
            (f"-- After validating row counts, the Iceberg staging table can be dropped"
            f" (via Athena):\n-- DROP TABLE {staging};"),
        ]
