"""BQ → Athena/Trino SQL translation via sqlglot.

Uses sqlglot's bigquery→trino dialect pair. Flags constructs that Athena
cannot handle (MERGE, geospatial on Iceberg, JS UDFs) as unsupported.
"""
from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp

from bq_assess.models import DetectedConstruct, EntityMetadata, EngineRewrite

_JS_UDF_RE = re.compile(r"\bLANGUAGE\s+js\b", re.IGNORECASE)

_UNSUPPORTED_FUNCS = frozenset({
    "ST_GEOGPOINT", "ST_DISTANCE", "ST_WITHIN", "ST_CONTAINS",
    "ST_INTERSECTION", "ST_UNION", "ST_AREA", "ST_LENGTH",
    "ST_MAKELINE", "ST_GEOGFROMTEXT", "ST_ASTEXT", "ST_CENTROID",
})

_MERGE_RE = re.compile(r"^\s*MERGE\s+INTO\s", re.IGNORECASE)


class AthenaRewriteGuide:
    """Generate BQ→Athena/Trino translation and rewrite guidance."""

    engine_id = "athena"

    def guide(self, entity: EntityMetadata, constructs: list[DetectedConstruct]) -> list[str]:
        if not constructs:
            return []
        guidance: list[str] = []
        for c in constructs:
            if c.construct_class == "JS_UDF":
                guidance.append("JavaScript UDF has no Athena equivalent — rewrite as a SQL scalar function or move to Spark.")
            elif c.construct_class == "UNNEST":
                guidance.append("UNNEST — Trino supports UNNEST directly (same syntax, verify CROSS JOIN vs LEFT JOIN).")
            elif c.construct_class == "STRUCT_NAV":
                guidance.append("Struct navigation works in Trino via ROW field access (dot notation).")
            elif c.construct_class == "FUNCTION_DRIFT":
                guidance.append(f"{c.description} — check Trino function name mapping.")
            else:
                guidance.append(f"{c.construct_class}: {c.description} — review for Trino compatibility.")
        return guidance

    def translate(self, sql: str) -> EngineRewrite:
        if not sql or not sql.strip():
            return EngineRewrite(
                engine_id=self.engine_id,
                translated_sql="",
                confidence="LOW",
                warnings=["Empty SQL"],
                unsupported_constructs=[],
            )

        warnings: list[str] = []
        unsupported: list[str] = []

        # Check for MERGE (supported on Athena engine v3 for Iceberg, but merge-on-read only)
        if _MERGE_RE.match(sql):
            warnings.append(
                "MERGE supported on Athena engine v3 for Iceberg (merge-on-read with positional deletes); "
                "consider compaction cadence for MERGE-heavy tables"
            )

        if _JS_UDF_RE.search(sql):
            unsupported.append("JavaScript UDF — no JS runtime in Athena")
            warnings.append("JavaScript UDF cannot run in Athena — rewrite as SQL or use Spark")

        # Suppress sqlglot logger noise
        sqlglot_logger = logging.getLogger("sqlglot")
        prev_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        try:
            try:
                statements = sqlglot.parse(sql, read="bigquery")
            except Exception as e:
                return EngineRewrite(
                    engine_id=self.engine_id,
                    translated_sql=f"-- [TRANSLATION FAILED: {type(e).__name__}]\n{sql}",
                    confidence="LOW",
                    warnings=[f"Parse error: {e}"],
                    unsupported_constructs=unsupported,
                )

            parts: list[str] = []
            for stmt in statements:
                if stmt is None:
                    continue
                try:
                    self._scan_unsupported(stmt, unsupported, warnings)
                    translated = stmt.sql(dialect="trino")
                    parts.append(translated)
                except Exception as e:
                    warnings.append(f"Transform error: {e}")
                    parts.append(stmt.sql(dialect="trino"))

            result_sql = "; ".join(parts)
        finally:
            sqlglot_logger.setLevel(prev_level)

        # Dedupe
        warnings = list(dict.fromkeys(warnings))
        unsupported = list(dict.fromkeys(unsupported))

        confidence = self._resolve_confidence(warnings, unsupported)

        return EngineRewrite(
            engine_id=self.engine_id,
            translated_sql=result_sql,
            confidence=confidence,
            warnings=warnings,
            unsupported_constructs=unsupported,
        )

    def _scan_unsupported(
        self, tree: exp.Expression, unsupported: list[str], warnings: list[str]
    ) -> None:
        # Geography functions
        for fn in tree.find_all(exp.Func):
            name = None
            if isinstance(fn, exp.Anonymous):
                # Anonymous functions have the name as an attribute
                name = fn.name.upper() if hasattr(fn, "name") else str(fn.this).upper()
            elif hasattr(fn, "sql_name"):
                sql_name = fn.sql_name().upper()
                # Avoid generic "ANONYMOUS" — use the class name instead
                if sql_name != "ANONYMOUS":
                    name = sql_name
            if name is None:
                name = type(fn).__name__.upper()

            if name in _UNSUPPORTED_FUNCS:
                unsupported.append(f"{name} — no geospatial support on Iceberg in Athena")
                warnings.append(f"{name}() is not available in Athena for Iceberg tables")

        # STRUCT constructor
        if next(tree.find_all(exp.Struct), None) is not None:
            unsupported.append("STRUCT — use ROW() constructor in Trino")
            warnings.append("STRUCT constructor → ROW() in Trino; verify field access patterns")

    def _resolve_confidence(self, warnings: list[str], unsupported: list[str]) -> str:
        if unsupported:
            return "LOW"
        if warnings:
            return "MEDIUM"
        return "HIGH"
