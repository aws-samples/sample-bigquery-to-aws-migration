"""BQ → Athena/Trino SQL translation via sqlglot.

Uses sqlglot's bigquery→trino dialect pair. After transpilation, applies
deterministic fixups for known sqlglot gaps, then validates every function
in the output against the Athena engine v3 allowlist. Any function not on
the list is flagged as unsupported — guaranteeing the output is executable.
"""
from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp

from bq_assess.models import DetectedConstruct, EngineRewrite, EntityMetadata

_JS_UDF_RE = re.compile(r"\bLANGUAGE\s+js\b", re.IGNORECASE)

_MERGE_RE = re.compile(r"^\s*MERGE\s+INTO\s", re.IGNORECASE)

# BigQuery procedural scripts (stored-procedure bodies): BEGIN blocks and
# DECLARE'd variables. Athena has no stored procedures or scripting at all —
# these can never execute, regardless of how the statements inside translate.
_SCRIPT_RE = re.compile(r"^\s*BEGIN\b|^\s*DECLARE\s+\w+", re.IGNORECASE)

# Trino/Athena engine v3 reserved words (Trino language docs). An unquoted
# column/field reference with one of these names is a syntax error in Athena
# even though it was legal in BigQuery (e.g. a STRUCT field named `user`).
_TRINO_RESERVED = frozenset({
    "alter", "and", "as", "between", "by", "case", "cast", "constraint",
    "create", "cross", "cube", "current_catalog", "current_date",
    "current_path", "current_role", "current_schema", "current_time",
    "current_timestamp", "current_user", "deallocate", "delete", "describe",
    "distinct", "drop", "else", "end", "escape", "except", "execute",
    "exists", "extract", "false", "for", "from", "full", "group", "grouping",
    "having", "in", "inner", "insert", "intersect", "into", "is", "join",
    "left", "like", "listagg", "localtime", "localtimestamp", "natural",
    "normalize", "not", "null", "on", "or", "order", "outer", "prepare",
    "recursive", "right", "rollup", "select", "skip", "table", "then",
    "trim", "true", "uescape", "union", "unnest", "user", "using", "values",
    "when", "where", "with",
})

# Post-transpile fixup patterns — sqlglot emits these but they aren't valid Athena/Trino
_UNIT_NAMES = r"YEAR|QUARTER|MONTH|WEEK|DAY|HOUR|MINUTE|SECOND"

# sqlglot emits TIMESTAMPDIFF(expr1, expr2, UNIT) — unit as 3rd arg
_TIMESTAMPDIFF_RE = re.compile(
    r"\bTIMESTAMPDIFF\s*\(\s*(.+?)\s*,\s*(.+?)\s*,\s*("
    + _UNIT_NAMES + r")\s*\)",
    re.IGNORECASE,
)
# sqlglot emits TIMESTAMP_SUB(expr, 'N', UNIT)
_TIMESTAMP_SUB_RE = re.compile(
    r"\bTIMESTAMP_SUB\s*\(\s*(.+?)\s*,\s*'(\d+)'\s*,\s*("
    + _UNIT_NAMES + r")\s*\)",
    re.IGNORECASE,
)
_ARRAY_BRACKET_RE = re.compile(
    r"\bARRAY\[([^]]+)\]",
    re.IGNORECASE,
)
_IGNORE_NULLS_AGG_RE = re.compile(
    r"\b(ARRAY_AGG|SUM|AVG|MIN|MAX|COUNT|BOOL_OR|BOOL_AND|ARBITRARY)"
    r"\s*\((.+?)\)\s+IGNORE\s+NULLS\b",
    re.IGNORECASE,
)

# Athena engine v3 function allowlist — verified against AWS docs.
# Any function NOT in this set that appears in transpiled output is flagged.
_ATHENA_ALLOWED_FUNCTIONS = frozenset({
    # Aggregate
    "ANY_VALUE", "ARBITRARY", "ARRAY_AGG", "AVG", "BOOL_AND", "BOOL_OR",
    "CHECKSUM", "COUNT", "COUNT_IF", "EVERY", "GEOMETRIC_MEAN", "LISTAGG",
    "MAX", "MAX_BY", "MIN", "MIN_BY", "SUM",
    "BITWISE_AND_AGG", "BITWISE_OR_AGG", "BITWISE_XOR_AGG",
    "HISTOGRAM", "MAP_AGG", "MAP_UNION", "MULTIMAP_AGG",
    "APPROX_DISTINCT", "APPROX_MOST_FREQUENT", "APPROX_PERCENTILE",
    "APPROX_SET", "MERGE", "NUMERIC_HISTOGRAM",
    "QDIGEST_AGG", "TDIGEST_AGG",
    "CORR", "COVAR_POP", "COVAR_SAMP", "KURTOSIS",
    "REGR_INTERCEPT", "REGR_SLOPE", "SKEWNESS",
    "STDDEV", "STDDEV_POP", "STDDEV_SAMP",
    "VARIANCE", "VAR_POP", "VAR_SAMP", "REDUCE_AGG",
    # String
    "CHR", "CODEPOINT", "CONCAT", "CONCAT_WS", "ENDS_WITH", "FORMAT",
    "HAMMING_DISTANCE", "LENGTH", "LEVENSHTEIN_DISTANCE", "LOWER", "LPAD",
    "LTRIM", "LUHN_CHECK", "OVERLAY", "POSITION", "REPLACE", "REVERSE",
    "RPAD", "RTRIM", "SOUNDEX", "SPLIT", "SPLIT_PART", "SPLIT_TO_MAP",
    "SPLIT_TO_MULTIMAP", "STRPOS", "STARTS_WITH", "SUBSTR", "SUBSTRING",
    "TITLE_CASE", "TRANSLATE", "TRIM", "UPPER", "WORD_STEM",
    "NORMALIZE", "TO_UTF8", "FROM_UTF8",
    # Math
    "ABS", "CBRT", "CEIL", "CEILING", "DEGREES", "E", "EXP", "FLOOR",
    "LN", "LOG", "LOG2", "LOG10", "MOD", "PI", "POW", "POWER",
    "RADIANS", "ROUND", "SIGN", "SQRT", "TRUNCATE", "WIDTH_BUCKET",
    "RAND", "RANDOM",
    "ACOS", "ASIN", "ATAN", "ATAN2", "COS", "COSH", "SIN", "SINH",
    "TAN", "TANH", "COSINE_DISTANCE", "COSINE_SIMILARITY",
    "INFINITY", "IS_FINITE", "IS_INFINITE", "IS_NAN", "NAN",
    "FROM_BASE", "TO_BASE",
    "BETA_CDF", "INVERSE_BETA_CDF", "INVERSE_NORMAL_CDF", "NORMAL_CDF",
    # Date/Time
    "CURRENT_DATE", "CURRENT_TIME", "CURRENT_TIMESTAMP", "CURRENT_TIMEZONE",
    "DATE", "LAST_DAY_OF_MONTH",
    "FROM_ISO8601_TIMESTAMP", "FROM_ISO8601_TIMESTAMP_NANOS", "FROM_ISO8601_DATE",
    "AT_TIMEZONE", "WITH_TIMEZONE",
    "FROM_UNIXTIME", "FROM_UNIXTIME_NANOS",
    "LOCALTIME", "LOCALTIMESTAMP", "NOW",
    "TO_ISO8601", "TO_MILLISECONDS", "TO_UNIXTIME",
    "DATE_TRUNC", "DATE_ADD", "DATE_DIFF",
    "PARSE_DURATION", "HUMAN_READABLE_SECONDS",
    "DATE_FORMAT", "DATE_PARSE", "FORMAT_DATETIME", "PARSE_DATETIME",
    "EXTRACT", "DAY", "DAY_OF_MONTH", "DAY_OF_WEEK", "DAY_OF_YEAR",
    "DOW", "DOY", "HOUR", "MILLISECOND", "MINUTE", "MONTH", "QUARTER",
    "SECOND", "TIMEZONE_HOUR", "TIMEZONE_MINUTE", "WEEK", "WEEK_OF_YEAR",
    "YEAR", "YEAR_OF_WEEK", "YOW",
    # Window
    "CUME_DIST", "DENSE_RANK", "NTILE", "PERCENT_RANK", "RANK", "ROW_NUMBER",
    "FIRST_VALUE", "LAST_VALUE", "NTH_VALUE", "LEAD", "LAG",
    # Array
    "ALL_MATCH", "ANY_MATCH", "ARRAY_DISTINCT", "ARRAY_INTERSECT",
    "ARRAY_UNION", "ARRAY_EXCEPT", "ARRAY_JOIN", "ARRAY_MAX", "ARRAY_MIN",
    "ARRAY_POSITION", "ARRAY_REMOVE", "ARRAY_SORT", "ARRAYS_OVERLAP",
    "CARDINALITY", "COMBINATIONS", "CONTAINS", "CONTAINS_SEQUENCE",
    "ELEMENT_AT", "FILTER", "FLATTEN", "NGRAMS", "NONE_MATCH", "REDUCE",
    "REPEAT", "SEQUENCE", "SHUFFLE", "SLICE", "TRIM_ARRAY", "TRANSFORM",
    "ZIP", "ZIP_WITH",
    # Map
    "MAP", "MAP_FROM_ENTRIES", "MULTIMAP_FROM_ENTRIES", "MAP_ENTRIES",
    "MAP_CONCAT", "MAP_FILTER", "MAP_KEYS", "MAP_VALUES", "MAP_ZIP_WITH",
    "TRANSFORM_KEYS", "TRANSFORM_VALUES",
    # Conditional
    "IF", "COALESCE", "NULLIF", "TRY",
    # Conversion
    "CAST", "TRY_CAST", "FORMAT_NUMBER", "TYPEOF",
    # Binary / Hash
    "FROM_BASE64", "TO_BASE64", "FROM_BASE64URL", "TO_BASE64URL",
    "FROM_HEX", "TO_HEX", "CRC32", "MD5", "SHA1", "SHA256", "SHA512",
    "XXHASH64", "MURMUR3", "HMAC_MD5", "HMAC_SHA1", "HMAC_SHA256", "HMAC_SHA512",
    # Bitwise
    "BIT_COUNT", "BITWISE_AND", "BITWISE_NOT", "BITWISE_OR",
    "BITWISE_XOR", "BITWISE_LEFT_SHIFT", "BITWISE_RIGHT_SHIFT",
    # Regex
    "REGEXP_COUNT", "REGEXP_EXTRACT_ALL", "REGEXP_EXTRACT",
    "REGEXP_LIKE", "REGEXP_POSITION", "REGEXP_REPLACE", "REGEXP_SPLIT",
    # URL
    "URL_EXTRACT_FRAGMENT", "URL_EXTRACT_HOST", "URL_EXTRACT_PARAMETER",
    "URL_EXTRACT_PATH", "URL_EXTRACT_PORT", "URL_EXTRACT_PROTOCOL",
    "URL_EXTRACT_QUERY", "URL_ENCODE", "URL_DECODE",
    # JSON
    "JSON_EXISTS", "JSON_QUERY", "JSON_VALUE", "JSON_ARRAY", "JSON_OBJECT",
    "IS_JSON_SCALAR", "JSON_ARRAY_CONTAINS", "JSON_ARRAY_GET",
    "JSON_ARRAY_LENGTH", "JSON_EXTRACT", "JSON_EXTRACT_SCALAR",
    "JSON_FORMAT", "JSON_PARSE", "JSON_SIZE",
    # Comparison
    "GREATEST", "LEAST",
    # Geospatial (Athena supports these on non-Iceberg; flagged separately for Iceberg)
    "ST_POINT", "ST_DISTANCE", "ST_WITHIN", "ST_CONTAINS", "ST_INTERSECTS",
    "ST_AREA", "ST_LENGTH", "ST_GEOMETRYFROMTEXT", "ST_ASTEXT",
    # Session
    "CURRENT_USER", "CURRENT_CATALOG", "CURRENT_SCHEMA",
    # Unnest
    "UNNEST",
    # Misc
    "UUID", "TO_CHAR", "TO_TIMESTAMP", "TO_DATE", "ROW",
})

# Geospatial functions that ARE valid Athena functions but NOT on Iceberg tables
_GEO_FUNCTIONS_NOT_ON_ICEBERG = frozenset({
    "ST_GEOGPOINT", "ST_DISTANCE", "ST_WITHIN", "ST_CONTAINS",
    "ST_INTERSECTION", "ST_UNION", "ST_AREA", "ST_LENGTH",
    "ST_MAKELINE", "ST_GEOGFROMTEXT", "ST_ASTEXT", "ST_CENTROID",
    "ST_POINT", "ST_INTERSECTS", "ST_GEOMETRYFROMTEXT",
})

# sqlglot AST node class names that map to SQL keywords/operators, not callable functions
_SQLGLOT_STRUCTURAL_NODES = frozenset({
    "ANONYMOUS", "STRUCT", "ARRAY", "IN", "EXISTS", "BETWEEN", "LIKE",
    "ILIKE", "NOT", "AND", "OR", "IS", "CASE", "WHEN", "INTERVAL",
    "PAREN", "SUBQUERY", "COLUMN", "TABLE", "STAR", "LITERAL",
    "ORDERED", "ALIAS", "WINDOW", "PARTITION", "LAMBDA",
    "SAFEDIVIDE", "GROUPCONCAT",
})

# sqlglot internal node names that differ from the emitted SQL text but map to
# valid Athena functions. The validator sees these names from sql_name(); we
# treat them as allowed since the OUTPUT text is the correct Athena function.
_SQLGLOT_NODE_ALIASES = frozenset({
    "TIME_TO_STR",       # emits DATE_FORMAT
    "STR_TO_TIME",       # emits DATE_PARSE
    "TIMESTAMP_TRUNC",   # emits DATE_TRUNC
    "ARRAY_SIZE",        # emits CARDINALITY
    "GENERATE_SERIES",   # emits SEQUENCE
    "TS_OR_DS_TO_TIMESTAMP",  # emits CAST(...AS TIMESTAMP)
    "DATEDIFF",          # emits DATE_DIFF
    "DATEADD",           # emits DATE_ADD
    "TIMESTAMPADD",      # emits DATE_ADD
    "CURRENTDATE",       # emits CURRENT_DATE
    "CURRENTTIMESTAMP",  # emits CURRENT_TIMESTAMP
    "GROUP_CONCAT",      # emits LISTAGG
})


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
                guidance.append("UNNEST — Athena supports UNNEST directly (same syntax, verify CROSS JOIN vs LEFT JOIN).")
            elif c.construct_class == "ARRAY_FN":
                guidance.append("ARRAY_* function — Athena supports most array functions natively (CARDINALITY, TRANSFORM, FILTER); check name mapping (low effort).")
            elif c.construct_class == "STRUCT_NAV":
                guidance.append("Struct navigation works in Athena via ROW field access (dot notation).")
            elif c.construct_class == "FUNCTION_DRIFT":
                guidance.append(f"{c.description} — check Athena function name mapping.")
            else:
                guidance.append(f"{c.construct_class}: {c.description} — review for Athena compatibility.")
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

        # BigQuery procedural scripts can never run on Athena (no stored
        # procedures, no scripting) — even if each inner statement transpiles,
        # the artifact as a whole is not executable. Flag as unsupported so the
        # result is LOW confidence with a BLOCKER, never HIGH.
        if _SCRIPT_RE.search(sql):
            unsupported.append(
                "BigQuery procedural script (BEGIN/DECLARE) — Athena has no stored "
                "procedures or scripting; re-implement as an orchestrated query "
                "sequence (e.g. Step Functions / Airflow / dbt)"
            )

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
                    # Strip the BQ project (catalog) qualifier: migrated tables
                    # are dataset.table on the target; the workgroup's
                    # QueryExecutionContext catalog resolves them. Shared reason
                    # with engine/redshift/rewrite._strip_project_qualifiers.
                    from bq_assess.engine.redshift.rewrite import (
                        _strip_project_qualifiers,
                    )
                    _strip_project_qualifiers(stmt, warnings)
                    self._quote_reserved_identifiers(stmt, warnings)
                    translated = stmt.sql(dialect="trino")
                    parts.append(translated)
                except Exception as e:
                    warnings.append(f"Transform error: {e}")
                    parts.append(stmt.sql(dialect="trino"))

            result_sql = "; ".join(parts)
        finally:
            sqlglot_logger.setLevel(prev_level)

        result_sql = self._post_transpile_fixups(result_sql, warnings)

        # Validate all functions against Athena allowlist
        self._validate_functions(result_sql, unsupported, warnings)

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

    def _post_transpile_fixups(self, sql: str, warnings: list[str]) -> str:
        """Apply deterministic rewrites for constructs sqlglot doesn't handle correctly."""
        # TIMESTAMPDIFF(start, end, UNIT) → date_diff('unit', start, end)
        def _fix_timestampdiff(m):
            unit = m.group(3).lower()
            warnings.append(f"TIMESTAMPDIFF rewritten to date_diff('{unit}', ...)")
            return f"date_diff('{unit}', {m.group(1)}, {m.group(2)})"

        sql = _TIMESTAMPDIFF_RE.sub(_fix_timestampdiff, sql)

        # TIMESTAMP_SUB(expr, 'N', UNIT) → date_add('unit', -N, expr)
        def _fix_timestamp_sub(m):
            unit = m.group(3).lower()
            n = m.group(2)
            warnings.append(f"TIMESTAMP_SUB rewritten to date_add('{unit}', -{n}, ...)")
            return f"date_add('{unit}', -{n}, {m.group(1)})"

        sql = _TIMESTAMP_SUB_RE.sub(_fix_timestamp_sub, sql)

        # ARRAY[subquery] → ARRAY(subquery)
        def _fix_array_bracket(m):
            inner = m.group(1)
            if inner.strip().upper().startswith("SELECT"):
                warnings.append("ARRAY[subquery] rewritten to ARRAY(subquery) for Athena")
                return f"ARRAY({inner})"
            return m.group(0)

        sql = _ARRAY_BRACKET_RE.sub(_fix_array_bracket, sql)

        # AGG(x) IGNORE NULLS → AGG(x) FILTER (WHERE x IS NOT NULL)
        # (sqlglot emits IGNORE NULLS after the closing paren for aggregates)
        def _fix_ignore_nulls(m):
            func_name = m.group(1)
            arg = m.group(2).strip()
            # The FILTER predicate takes the bare expression — a leading DISTINCT
            # belongs to the aggregate only (`WHERE DISTINCT x` is a syntax error).
            predicate = re.sub(r"^\s*DISTINCT\s+", "", arg, flags=re.IGNORECASE)
            warnings.append(f"{func_name} IGNORE NULLS rewritten to FILTER (WHERE ... IS NOT NULL)")
            return f"{func_name}({arg}) FILTER (WHERE {predicate} IS NOT NULL)"

        sql = _IGNORE_NULLS_AGG_RE.sub(_fix_ignore_nulls, sql)

        return sql

    def _quote_reserved_identifiers(
        self, stmt: exp.Expression, warnings: list[str]
    ) -> None:
        """Force-quote column/field identifiers that are Trino reserved words.

        BigQuery allows bare STRUCT fields like ``e.user.profile``; Trino/Athena
        rejects the unquoted reserved word. Marking the identifier quoted makes
        the trino generator emit ``e."user".profile``. Table/alias names are
        handled by the generator already; this covers column-path identifiers.
        """
        flagged: set[str] = set()
        for ident in stmt.find_all(exp.Identifier):
            if ident.name.lower() in _TRINO_RESERVED and not ident.args.get("quoted"):
                ident.set("quoted", True)
                flagged.add(ident.name)
        for name in sorted(flagged):
            warnings.append(
                f"Identifier '{name}' is reserved in Athena/Trino — quoted as \"{name}\""
            )

    def _validate_functions(
        self, sql: str, unsupported: list[str], warnings: list[str]
    ) -> None:
        """Parse output SQL and verify every function is in the Athena allowlist."""
        sqlglot_logger = logging.getLogger("sqlglot")
        prev_level = sqlglot_logger.level
        sqlglot_logger.setLevel(logging.ERROR)
        try:
            try:
                stmts = sqlglot.parse(sql, read="trino")
            except Exception:
                return
            for stmt in stmts:
                if stmt is None:
                    continue
                for fn in stmt.find_all(exp.Func):
                    name = self._extract_func_name(fn)
                    if not name or name in _SQLGLOT_STRUCTURAL_NODES:
                        continue
                    if name in _ATHENA_ALLOWED_FUNCTIONS:
                        if name in _GEO_FUNCTIONS_NOT_ON_ICEBERG:
                            unsupported.append(f"{name} — not available on Iceberg tables in Athena")
                        continue
                    if name in _SQLGLOT_NODE_ALIASES:
                        continue
                    unsupported.append(
                        f"{name}() is not a recognized Athena function — manual rewrite required"
                    )
        finally:
            sqlglot_logger.setLevel(prev_level)

    @staticmethod
    def _extract_func_name(fn: exp.Expression) -> str | None:
        """Get the canonical uppercase function name from a sqlglot Func node."""
        if isinstance(fn, exp.Anonymous):
            return fn.name.upper() if hasattr(fn, "name") else None
        if hasattr(fn, "sql_name"):
            name = fn.sql_name().upper()
            if name != "ANONYMOUS":
                return name
        return type(fn).__name__.upper()

    def _scan_unsupported(
        self, tree: exp.Expression, unsupported: list[str], warnings: list[str]
    ) -> None:
        """Pre-transpile scan of the BQ AST for known-unsupported patterns."""
        for fn in tree.find_all(exp.Func):
            name = self._extract_func_name(fn)
            if not name:
                continue
            if name in _GEO_FUNCTIONS_NOT_ON_ICEBERG:
                unsupported.append(f"{name} — no geospatial support on Iceberg in Athena")
                warnings.append(f"{name}() is not available in Athena for Iceberg tables")

    def _resolve_confidence(self, warnings: list[str], unsupported: list[str]) -> str:
        if unsupported:
            return "LOW"
        if warnings:
            return "MEDIUM"
        return "HIGH"
