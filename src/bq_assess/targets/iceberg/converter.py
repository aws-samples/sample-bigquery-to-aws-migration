"""Iceberg Converter — BigQuery schema → Iceberg DDL in Athena engine v3 dialect (R6, R7, R8, R23.3).

Emits CREATE TABLE in Athena Iceberg DDL syntax with:
- TBLPROPERTIES ('table_type'='ICEBERG') (not USING ICEBERG)
- Clause order: columns → PARTITIONED BY → LOCATION → TBLPROPERTIES
- Athena-valid types: string (not varchar), timestamp (not timestamptz), etc.
- Backtick quoting for reserved/special identifiers (Hive-based DDL)
- Native nesting: STRUCT→struct, ARRAY→list, ARRAY<STRUCT>→list<struct> (R6.2, P10)
- Partition/sort mapping per ADR-0003: clean=annotation, non-clean=flagged (R7, P12)
- Lossy Cast detection with warnings, never silent (R8, P11)
- No DDL for REBUILT entities (R4.4, P6)

Athena is the table-creation engine per the 2026-07-20 design spec; Redshift queries
the same Iceberg tables via an external schema and never creates them.

Type mapping per design.md § Authoritative BigQuery → Iceberg Type Mapping.
"""

from __future__ import annotations

import logging

from bq_assess.models import (
    ColumnSchema,
    ConversionResult,
    EntityMetadata,
    EntityPopulation,
    LossyCast,
    PartitionMapping,
)
from bq_assess.targets.iceberg.constants import (
    BQ_TO_ICEBERG_PARTITION_TRANSFORM,
    V6_JSON_ICEBERG_TYPE,
    V6_JSON_IS_LOSSY,
    V6_JSON_LOSS_DESCRIPTION,
    V6_TIME_ICEBERG_TYPE,
    V6_TIME_IS_LOSSY,
    V6_TIME_LOSS_DESCRIPTION,
)
from bq_assess.targets.iceberg.identifiers import (
    quote_full_name_ddl,
    quote_identifier_ddl,
)

logger = logging.getLogger(__name__)

# ---- Clean type map (round-trippable, P9) ----
# Maps to Athena Iceberg DDL-valid type names (verified on Athena engine v3).
# Athena's supported Iceberg DDL types: binary, boolean, date, decimal, double,
# float, int, bigint, list, map, string, struct, timestamp.
# NOTE: Only Iceberg timestamp (without time zone) is supported for Athena Iceberg
# DDL statements like CREATE TABLE.

# BigQuery INT/SMALLINT/BIGINT/TINYINT/BYTEINT are all aliases of 64-bit INT64 —
# there is no narrower integer type in BigQuery, so every alias maps to Iceberg bigint.
CLEAN_TYPE_MAP: dict[str, str] = {
    "STRING": "string",
    "INT64": "bigint",
    "INTEGER": "bigint",
    "INT": "bigint",
    "SMALLINT": "bigint",
    "BIGINT": "bigint",
    "TINYINT": "bigint",
    "BYTEINT": "bigint",
    "FLOAT64": "double",
    "FLOAT": "double",
    "BOOL": "boolean",
    "BOOLEAN": "boolean",
    "DATE": "date",
    "TIMESTAMP": "timestamp",
    "DATETIME": "timestamp",
    "NUMERIC": "decimal(38,9)",
}

# ---- Lossy type map (contribute to Effort score) ----

LOSSY_TYPE_MAP: dict[str, tuple[str, str]] = {
    "BYTES": (
        "string",
        "Athena Iceberg DDL supports 'binary' but BigQuery exports BYTES as raw "
        "Parquet binary and no layer converts it automatically. Mapped to string; "
        "the load process must encode bytes explicitly (e.g. TO_BASE64 at export).",
    ),
    "GEOGRAPHY": (
        "string",
        "No native Iceberg GEOGRAPHY; stored as WKT string. Spatial semantics lost.",
    ),
    "INTERVAL": (
        "string",
        "No native Iceberg INTERVAL; stored as ISO 8601 duration string.",
    ),
}

if V6_TIME_IS_LOSSY:
    LOSSY_TYPE_MAP["TIME"] = (V6_TIME_ICEBERG_TYPE, V6_TIME_LOSS_DESCRIPTION)
if V6_JSON_IS_LOSSY:
    LOSSY_TYPE_MAP["JSON"] = (V6_JSON_ICEBERG_TYPE, V6_JSON_LOSS_DESCRIPTION)

_NESTED_TYPES = {"STRUCT", "RECORD"}

_BIGNUMERIC_ICEBERG = "decimal(38,18)"
_BIGNUMERIC_LOSS = (
    "BIGNUMERIC (76 digits: 38 integer + 38 fractional) exceeds Iceberg/Redshift max "
    "precision (38). Mapped to decimal(38,18): 20 integer + 18 fractional digits retained."
)


class IcebergConverter:
    """Convert BigQuery EntityMetadata to Iceberg CREATE TABLE DDL.

    Contract (design.md § Component Interfaces):
        def convert(self, entity: EntityMetadata) -> ConversionResult

    ``iceberg_location_root`` is the S3 prefix for table data (V10: the Query
    Engine's Iceberg CREATE TABLE requires a LOCATION clause). When not provided,
    a placeholder is emitted and a warning tells the operator to substitute it.
    """

    _LOCATION_PLACEHOLDER = "s3://<ICEBERG_BUCKET>"

    def __init__(self, iceberg_location_root: str | None = None):
        self._location_root = (iceberg_location_root or "").rstrip("/") or None

    def convert(self, entity: EntityMetadata) -> ConversionResult:
        """Convert entity schema to Iceberg DDL.

        - REBUILT entities → empty DDL, success=True (R4.4, P6)
        - On exception → success=False (maps to Effort MANUAL, R23.3)
        """
        if entity.population == EntityPopulation.REBUILT:
            return ConversionResult(
                ddl="",
                partition_mapping=None,
                lossy_casts=[],
                warnings=[],
                success=True,
            )

        try:
            return self._convert_table(entity)
        except Exception as exc:
            logger.error("Iceberg conversion failed for %s: %s", entity.full_name, exc)
            return ConversionResult(
                ddl="",
                partition_mapping=None,
                lossy_casts=[],
                warnings=[f"Conversion failed: {exc}"],
                success=False,
            )

    # ------------------------------------------------------------------

    def _convert_table(self, entity: EntityMetadata) -> ConversionResult:
        lossy_casts: list[LossyCast] = []
        warnings: list[str] = []

        # Detect nested types — Athena Iceberg DDL supports struct/list natively.
        has_nested = any(
            col.mode == "REPEATED" or col.field_type.upper() in _NESTED_TYPES
            for col in entity.columns
        )

        # Map columns — reserved-word / non-standard names get backtick-quoted
        # (Athena DDL is Hive-based). NOT NULL is never emitted: Athena does not
        # support column constraints on Iceberg tables (nor does Redshift when
        # querying them). REQUIRED-ness is preserved as a trailing comment.
        cols: list[tuple[str, str]] = []  # (definition, trailing comment)
        required_cols: list[str] = []
        for col in entity.columns:
            iceberg_type = self._resolve_type(col, lossy_casts)
            comment = ""
            if iceberg_type == "timestamp" and col.field_type.upper() == "TIMESTAMP":
                comment = (
                    "  -- BigQuery TIMESTAMP is UTC; Athena Iceberg DDL supports "
                    "timestamp without zone only"
                )
            if col.mode == "REQUIRED":
                comment = "  -- REQUIRED in BigQuery (not enforceable here)"
                required_cols.append(col.name)
            cols.append((f"  {quote_identifier_ddl(col.name)} {iceberg_type}", comment))

        # Comma before the comment on every line but the last, so a trailing
        # comment never swallows the separator.
        lines = [
            f"{defn}{',' if i < len(cols) - 1 else ''}{comment}"
            for i, (defn, comment) in enumerate(cols)
        ]
        columns_sql = "\n".join(lines)

        if required_cols:
            warnings.append(
                f"REQUIRED columns ({', '.join(required_cols)}) cannot carry NOT NULL: "
                "Athena does not support column constraints on Iceberg tables "
                "(nor does Redshift when querying them). Nullability must be enforced "
                "by the load process; noted as comments in the DDL."
            )

        # Partition/sort mapping (R7, ADR-0003)
        partition_mapping = self._derive_partition_mapping(entity)

        # Build DDL — Athena Iceberg DDL clause order:
        # columns → PARTITIONED BY → LOCATION → TBLPROPERTIES
        partition_clause = ""
        if partition_mapping and partition_mapping.iceberg_transforms:
            transforms = ", ".join(partition_mapping.iceberg_transforms)
            partition_clause = f"\nPARTITIONED BY ({transforms})"

        # Sort order: Athena Iceberg DDL has no sort mechanism. The Storage
        # Target's CreateTable API does accept writeOrder (design.md V9), so the
        # intent is preserved as a comment for that path.
        sort_comment = ""
        if partition_mapping and partition_mapping.sort_order:
            sorts = ", ".join(partition_mapping.sort_order)
            sort_comment = (
                f"\n-- SORT ORDER ({sorts}): not applicable via Athena DDL;"
                " apply as writeOrder if creating via the S3 Tables API"
            )
            warnings.append(
                f"Sort order ({sorts}) cannot be applied through Athena Iceberg "
                "DDL — no sort mechanism exists. BigQuery clustering intent is "
                "preserved as a DDL comment; it can be applied as writeOrder when "
                "creating the table via the S3 Tables API instead."
            )

        # LOCATION + TBLPROPERTIES form the Athena Iceberg DDL. Location root
        # comes from config or a placeholder.
        location_root = self._location_root or self._LOCATION_PLACEHOLDER
        location = f"{location_root}/{entity.full_name.replace('.', '/')}/"
        if self._location_root is None:
            warnings.append(
                "No Iceberg location root configured — DDL contains the "
                f"placeholder {self._LOCATION_PLACEHOLDER}; substitute the "
                "Storage Target bucket before executing."
            )

        engine_note = ""
        if has_nested:
            engine_note = (
                "-- NESTED TYPES: Athena Iceberg DDL supports struct/list "
                "natively.\n"
                "-- Redshift queries the same table via external schema and reads "
                "nested columns as SUPER.\n"
            )
            warnings.append(
                "Nested columns (struct/list) are supported in Athena Iceberg DDL "
                "natively. Redshift queries the same table via external schema and "
                "reads the nested columns as SUPER."
            )

        ddl = (
            f"{engine_note}"
            f"CREATE TABLE {quote_full_name_ddl(entity.full_name)} "
            f"(\n{columns_sql}\n)"
            f"{partition_clause}\n"
            f"LOCATION '{location}'\n"
            f"TBLPROPERTIES ('table_type'='ICEBERG');{sort_comment}"
        )

        # Lossy cast warnings
        for lc in lossy_casts:
            warnings.append(
                f"Lossy cast: column '{lc.column}' ({lc.source_type}) → "
                f"{lc.iceberg_type}: {lc.loss_description}"
            )

        # Non-clean partition/sort warnings
        if partition_mapping and not partition_mapping.auto_derived:
            for flag in partition_mapping.decision_flags:
                warnings.append(f"Partition/sort decision required: {flag}")

        return ConversionResult(
            ddl=ddl,
            partition_mapping=partition_mapping,
            lossy_casts=lossy_casts,
            warnings=warnings,
            success=True,
        )

    # ------------------------------------------------------------------
    # Type resolution (handles nesting recursively)
    # ------------------------------------------------------------------

    def _resolve_type(self, col: ColumnSchema, lossy: list[LossyCast]) -> str:
        """Resolve a column to its Iceberg type string, preserving nesting (P10)."""
        field_type = col.field_type.upper()

        # REPEATED → array<element> (R6.2)
        # Athena v3 requires `array<T>` syntax; Iceberg's `list<T>` is rejected
        # at DDL parse time ("mismatched input '<'").
        if col.mode == "REPEATED":
            if field_type in _NESTED_TYPES:
                inner = self._struct_fields(col.fields, lossy)
                return f"array<struct<{inner}>>"
            else:
                elem = self._scalar_type(col, lossy)
                return f"array<{elem}>"

        # STRUCT/RECORD → struct<...> (R6.2)
        if field_type in _NESTED_TYPES:
            inner = self._struct_fields(col.fields, lossy)
            return f"struct<{inner}>"

        return self._scalar_type(col, lossy)

    def _struct_fields(self, fields: list[ColumnSchema], lossy: list[LossyCast]) -> str:
        """Render struct fields recursively.

        NOT NULL markers are not emitted: neither Athena nor Redshift supports
        constraints inside a struct type spec — nullability intent lives in the
        ConversionResult warnings instead.
        """
        parts: list[str] = []
        for f in fields:
            f_type = self._resolve_type(f, lossy)
            parts.append(f"{quote_identifier_ddl(f.name)}: {f_type}")
        return ", ".join(parts)

    def _scalar_type(self, col: ColumnSchema, lossy: list[LossyCast]) -> str:
        """Map a scalar BigQuery type to Iceberg (R6.1, R8)."""
        field_type = col.field_type.upper()

        # Clean
        if field_type in CLEAN_TYPE_MAP:
            return CLEAN_TYPE_MAP[field_type]

        # BIGNUMERIC — always treated as lossy (can't know precision from schema)
        if field_type == "BIGNUMERIC":
            lossy.append(LossyCast(
                column=col.name,
                source_type="BIGNUMERIC",
                iceberg_type=_BIGNUMERIC_ICEBERG,
                loss_description=_BIGNUMERIC_LOSS,
            ))
            return _BIGNUMERIC_ICEBERG

        # Known lossy
        if field_type in LOSSY_TYPE_MAP:
            iceberg_type, loss_desc = LOSSY_TYPE_MAP[field_type]
            lossy.append(LossyCast(
                column=col.name,
                source_type=field_type,
                iceberg_type=iceberg_type,
                loss_description=loss_desc,
            ))
            return iceberg_type

        # Unknown type → string fallback + lossy warning (R8.4)
        lossy.append(LossyCast(
            column=col.name,
            source_type=field_type,
            iceberg_type="string",
            loss_description=f"Unknown BigQuery type '{field_type}'; fallback to string.",
        ))
        return "string"

    # ------------------------------------------------------------------
    # Partition / sort mapping (R7, ADR-0003)
    # ------------------------------------------------------------------

    def _derive_partition_mapping(self, entity: EntityMetadata) -> PartitionMapping | None:
        """Derive Iceberg partition transforms + sort order."""
        transforms: list[str] = []
        sort_order: list[str] = []
        auto_derived = True
        decision_flags: list[str] = []

        # Time partitioning (R7.1, R7.3)
        if entity.time_partitioning is not None:
            tp = entity.time_partitioning
            if tp.field is not None:
                # Explicit-field → clean (R7.1, zero effort)
                transform = BQ_TO_ICEBERG_PARTITION_TRANSFORM.get(tp.type.upper(), "day")
                transforms.append(f"{transform}({quote_identifier_ddl(tp.field)})")
            else:
                # Ingestion-time → non-clean (R7.3). Emit a syntactically valid
                # transform suggestion; the review caveat lives in decision_flags
                # (kept out of the transform list so the DDL stays valid).
                auto_derived = False
                decision_flags.append(
                    "ingestion-time partition (no real column) — "
                    "suggested day(_ingestion_time); review before applying"
                )
                transforms.append("day(_ingestion_time)")

        # Range partitioning (R7.4)
        if entity.range_partitioning is not None:
            rp = entity.range_partitioning
            auto_derived = False
            decision_flags.append(
                f"range partition on '{rp.field}' ({rp.start}..{rp.end}, "
                f"interval={rp.interval}) — no clean Iceberg equivalent; "
                f"suggested bucket(16, {rp.field}) is not equivalent to range, review before applying"
            )
            transforms.append(f"bucket(16, {quote_identifier_ddl(rp.field)})")

        # Clustering → sort order (R7.2, R7.5)
        if entity.clustering_fields:
            for field in entity.clustering_fields:
                sort_order.append(field)
            # BQ allows max 4 clustering fields; >4 would be ambiguous (R7.5)
            if len(entity.clustering_fields) > 4:
                auto_derived = False
                decision_flags.append("ambiguous multi-column clustering")

        if not transforms and not sort_order:
            return None

        return PartitionMapping(
            iceberg_transforms=transforms,
            sort_order=sort_order,
            auto_derived=auto_derived,
            decision_flags=decision_flags,
        )
