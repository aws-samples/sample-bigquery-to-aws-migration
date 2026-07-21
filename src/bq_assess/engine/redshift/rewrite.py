"""Query-Rewrite Guidance + Best-Effort SQL Translation (R13).

Per detected construct, emit a human-readable required change + effort indication.
For entities with SQL surface, also produce a best-effort Redshift translation using
sqlglot (illustrative only — requires validation before production use).

Translation pipeline (2026-07-16 rework — see
the 2026-07-16 translation deep-audit notes):
sqlglot's Redshift generator emits BigQuery's DATETIME()/TIMESTAMP() and STRUCT()
silently (no ``unsupported()`` call), so ``transpile()`` alone shipped SQL that cannot
run on Redshift at HIGH confidence. We now parse per statement, rewrite temporal
constructors on the AST (to a fixpoint — ``transform()`` prunes descent into replaced
nodes, so one pass misses nested calls), then AST-scan the result for residual
constructs. Regex-on-output is deliberately avoided: it false-positives on construct
names inside string literals.

Warning taxonomy (drives ``TranslationResult.confidence``):
- ``[auto-converted]``   → MEDIUM — runs on Redshift, but semantics shifted in a way
  the customer must verify (e.g. BQ TIMESTAMP's absolute instant becomes a naive
  TIMESTAMP holding UTC wall-clock).
- ``[needs manual edit]`` → LOW — a construct with no Redshift equivalent survived.
- anything else (JS UDF, parse failure, scripting) → LOW, as before.
"""
from __future__ import annotations

import logging
import re

import sqlglot
from sqlglot import exp

from bq_assess.models import DetectedConstruct, EntityMetadata, TranslationResult

_GUIDANCE: dict[str, str] = {
    "JS_UDF": "JavaScript UDF has no Redshift equivalent — rewrite as Lambda UDF, Node.js recommended (high effort).",
    "UNNEST": "UNNEST over nested arrays — replace with Redshift FROM-clause unnest pattern: FROM t, t.arr AS x (medium effort).",
    "ARRAY_FN": "ARRAY_* function — replace with Redshift equivalent or SUPER array functions (medium effort).",
    "STRUCT_NAV": "Struct-path navigation (dot notation) — works as-is for Iceberg tables via Spectrum (low effort).",
    "FUNCTION_DRIFT": "Function name/semantic drift — rename or adjust argument order for Redshift dialect (low effort).",
}


_JS_UDF_RE = re.compile(r"\bLANGUAGE\s+js\b", re.IGNORECASE)

# Trailing numeric UTC offset (+11, -05:30) or trailing zone token (UTC, GMT,
# Australia/Melbourne) inside a timestamp string literal — BigQuery honors these;
# a Redshift cast to plain TIMESTAMP discards them ("Time zone values in input
# strings are ignored", r_Datetime_types.html, verified 2026-07-16).
_TZ_IN_STRING_RE = re.compile(
    r"([+-]\d{2}(:?\d{2})?|\s[A-Za-z]+/[A-Za-z_]+|\s(?:UTC|GMT)|Z)\s*$"
)

# Warning-class prefixes — confidence keys off these (see _resolve_confidence).
_AUTO = "[auto-converted] "
_MANUAL = "[needs manual edit] "

_W_DATETIME_TZ = (
    _AUTO + "DATETIME(ts, tz) rewritten to CONVERT_TIMEZONE(tz, ts) — result is a "
    "timezone-naive TIMESTAMP holding local wall-clock time. Verify comparisons "
    "against UTC-based columns before production use."
)
_W_TIMESTAMP_TZ = (
    _AUTO + "TIMESTAMP(dt, tz) rewritten to CONVERT_TIMEZONE(tz, 'UTC', dt) — "
    "BigQuery returned an absolute instant; Redshift returns a naive TIMESTAMP "
    "holding UTC wall-clock. Comparisons against local-time columns will be offset "
    "by the zone difference — verify timezone semantics."
)
_W_TIMESTAMP_OFFSET_LITERAL = (
    _AUTO + "TIMESTAMP('…±hh:mm') cast to TIMESTAMPTZ to preserve the embedded "
    "offset (a plain TIMESTAMP cast would silently discard it). Verify downstream "
    "type expectations."
)
_W_DATETIME_TWO_PART = (
    _MANUAL + "DATETIME(date, time) construction has no direct Redshift equivalent — "
    "combine with (date + time) arithmetic or a CAST, manually."
)
_W_STRUCT = (
    _MANUAL + "STRUCT constructor has no Redshift equivalent. For nested data, land "
    "it in a native table as a SUPER column (INSERT INTO … SELECT from the Iceberg "
    "table) and query with PartiQL — GA in all regions including ap-southeast-2. "
    "Note: array-by-position access and two other nested-query patterns are "
    "preview-cluster-only (not available in ap-southeast-2)."
)

# Function names that reach Redshift output verbatim (via sqlglot's Anonymous or
# silent fallbacks) but do not exist in Redshift. AST-checked, so occurrences inside
# string literals never false-positive.
_KNOWN_INVALID_FUNCS = frozenset({
    "DATETIME",              # leftover constructor form the transform couldn't map
    "MAKE_TIME",
    "MAKE_TIMESTAMP",        # sqlglot's output for the 6-int DATETIME form
    "GENERATE_DATE_ARRAY",
    "GENERATE_TIMESTAMP_ARRAY",
    "GENERATE_UUID",
    "FARM_FINGERPRINT",      # the raw BQ name (FARMFINGERPRINT64 is valid Redshift)
    "PARSE_DATETIME",
    "FORMAT_DATETIME",
    "DATETIME_TRUNC",        # Redshift uses DATE_TRUNC
    "DATETIME_ADD",          # Redshift uses DATEADD
    "DATETIME_SUB",          # Redshift uses DATEADD with negative interval
    "DATETIME_DIFF",         # Redshift uses DATEDIFF
    "REGEXP_EXTRACT_ALL",    # Redshift has REGEXP_SUBSTR only (no array return)
    "FORMAT",                # Redshift has no sprintf-style FORMAT
    "UNIX_SECONDS",          # Redshift: EXTRACT(EPOCH FROM ...)
    "UNIX_MILLIS",
    "UNIX_MICROS",
    "TIMESTAMPDIFF",         # sqlglot emits this with wrong arg order; Redshift uses DATEDIFF
})

_FIXPOINT_MAX_PASSES = 10

# Leading "NAME(" of a rendered function call — used to judge residual functions by
# the name the Redshift generator actually emits (see _scan_residuals).
_LEADING_FUNC_NAME_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(")


class RewriteGuide:
    """Generate human-readable rewrite guidance and best-effort SQL translation."""

    def guide(self, entity: EntityMetadata, constructs: list[DetectedConstruct]) -> list[str]:
        if not constructs:
            return []
        result: list[str] = []
        for c in constructs:
            text = _GUIDANCE.get(c.construct_class)
            if text is None:
                text = f"{c.construct_class}: {c.description} — review and adapt for Redshift."
            result.append(text)
        return result

    def translate(self, sql: str) -> TranslationResult:
        """Best-effort BQ→Redshift translation: parse → AST rewrite → residual scan."""
        if not sql or not sql.strip():
            return TranslationResult(
                redshift_sql="",
                confidence="LOW",
                warnings=["Empty SQL — nothing to translate."],
            )

        warnings: list[str] = []

        if _JS_UDF_RE.search(sql):
            warnings.append(
                "JavaScript UDF cannot be auto-translated — "
                "rewrite as Lambda UDF (Node.js recommended)."
            )

        # Silence sqlglot's logger for the whole parse→transform→generate span —
        # the generator also logs (e.g. "INITCAP does not support custom
        # delimiters") and those must not leak to the CLI's console.
        _sqlglot_logger = logging.getLogger("sqlglot")
        prev_level = _sqlglot_logger.level
        _sqlglot_logger.setLevel(logging.ERROR)
        try:
            try:
                statements = sqlglot.parse(sql, read="bigquery")
            except Exception as e:
                return TranslationResult(
                    redshift_sql=f"-- [TRANSLATION FAILED: {type(e).__name__}]\n{sql}",
                    confidence="LOW",
                    warnings=[f"sqlglot could not parse this SQL: {e}"],
                )

            parts: list[str] = []
            for stmt in statements:
                if stmt is None:
                    continue
                try:
                    stmt = _rewrite_to_fixpoint(stmt, warnings)
                    _scan_residuals(stmt, warnings)
                    parts.append(stmt.sql(dialect="redshift"))
                except Exception as e:
                    # A transform crash is a converter bug, not a parse failure —
                    # label it distinctly so it never hides in the FAILED-to-parse
                    # population.
                    warnings.append(
                        _MANUAL + f"internal transform error ({type(e).__name__}: {e}) — "
                        "statement emitted via plain transpile; review manually."
                    )
                    parts.append(stmt.sql(dialect="redshift"))
            translated = "; ".join(parts)
        finally:
            _sqlglot_logger.setLevel(prev_level)

        translated = self._post_fix(translated, warnings)

        if "BEGIN" in sql and ("DECLARE" in sql or "EXCEPTION" in sql or "FOR " in sql):
            warnings.append("Stored procedure with scripting constructs — partial translation only.")

        warnings = list(dict.fromkeys(warnings))  # dedupe, keep order

        return TranslationResult(
            redshift_sql=translated,
            confidence=_resolve_confidence(warnings),
            warnings=warnings,
        )

    def _post_fix(self, sql: str, warnings: list[str]) -> str:
        """Post-processing hook (regex rewrites removed — all detection is AST-based now)."""
        return sql


def _resolve_confidence(warnings: list[str]) -> str:
    """LOW if anything needs a manual edit; MEDIUM if only auto-conversions; else HIGH."""
    if not warnings:
        return "HIGH"
    if all(w.startswith(_AUTO) for w in warnings):
        return "MEDIUM"
    return "LOW"


def _rewrite_to_fixpoint(tree: exp.Expression, warnings: list[str]) -> exp.Expression:
    """Repeat the temporal rewrite until the tree stops changing.

    ``transform()`` prunes descent into nodes the function replaces (verified against
    the v30.11.0 source: ``prune=lambda n: n is not new_node``), so a replacement that
    re-embeds original children leaves nested constructs unconverted after one pass.
    Fixpoint iteration also makes translation idempotent — the rewritten output
    contains no exp.Datetime/exp.Timestamp nodes to match on a second run.
    """
    prev = tree.sql()
    for _ in range(_FIXPOINT_MAX_PASSES):
        tree = tree.transform(_rewrite_temporal, warnings)
        cur = tree.sql()
        if cur == prev:
            return tree
        prev = cur
    warnings.append(_MANUAL + "temporal rewrite did not converge — review manually.")
    return tree


def _rewrite_temporal(node: exp.Expression, warnings: list[str]) -> exp.Expression:
    """Rewrite BQ temporal constructors sqlglot's Redshift generator passes through.

    Verified semantics (2026-07-16, CONVERT_TIMEZONE.html + BQ datetime/timestamp
    function refs):
    - BQ DATETIME(ts, tz) → naive wall-clock in tz. Redshift CONVERT_TIMEZONE(tz, ts)
      (2-arg: source defaults UTC) → naive TIMESTAMP, same wall-clock. Type-faithful.
    - BQ TIMESTAMP(dt, tz) → absolute instant (dt read as local-in-tz). Redshift
      CONVERT_TIMEZONE(tz, 'UTC', dt) → naive TIMESTAMP holding the UTC wall-clock.
      Value-correct, type-downgraded → always warn.
    """
    if isinstance(node, exp.Datetime):
        tz = node.args.get("expression")
        if isinstance(tz, exp.Literal) and tz.is_string:
            warnings.append(_W_DATETIME_TZ)
            return exp.Anonymous(this="CONVERT_TIMEZONE", expressions=[tz, node.this])
        if tz is None:
            return exp.cast(node.this, "TIMESTAMP")
        # DATETIME(date, time) two-part form — no clean equivalent
        warnings.append(_W_DATETIME_TWO_PART)
        return node

    if isinstance(node, exp.Timestamp):
        tz = node.args.get("zone")
        if isinstance(tz, exp.Literal) and tz.is_string:
            warnings.append(_W_TIMESTAMP_TZ)
            return exp.Anonymous(
                this="CONVERT_TIMEZONE",
                expressions=[tz, exp.Literal.string("UTC"), node.this],
            )
        arg = node.this
        if (
            isinstance(arg, exp.Literal)
            and arg.is_string
            and _TZ_IN_STRING_RE.search(arg.name)
        ):
            # BQ honors the embedded offset; a plain-TIMESTAMP cast silently discards
            # it. TIMESTAMPTZ preserves the instant.
            warnings.append(_W_TIMESTAMP_OFFSET_LITERAL)
            return exp.cast(arg, "TIMESTAMPTZ")
        if arg is not None:
            return exp.cast(arg, "TIMESTAMP")

    return node


def _scan_residuals(tree: exp.Expression, warnings: list[str]) -> None:
    """AST scan for constructs that reached the output but cannot run on Redshift.

    This is the safety net for sqlglot's silent fall-throughs: the Redshift generator
    emits Struct and unknown functions without calling ``unsupported()``, and its
    grammar is a permissive superset — so neither ErrorLevel.RAISE nor a parse-back
    gate can catch these (verified against v30.11.0 source). AST matching means a
    construct name inside a string literal never false-positives.
    """
    if next(tree.find_all(exp.Struct), None) is not None:
        warnings.append(_W_STRUCT)

    # Functions reach the output under two node shapes: exp.Anonymous (names sqlglot
    # doesn't know) and typed exp.Func subclasses. For typed nodes the parse-side
    # name is NOT what the customer gets — sqlglot may convert it correctly
    # (FARM_FINGERPRINT → FARMFINGERPRINT64, FORMAT_DATETIME → TO_CHAR) — so judge
    # each node by the function name the Redshift generator actually EMITS for it.
    # Still per-AST-node, so names inside string literals cannot false-positive.
    for fn in tree.find_all(exp.Func):
        rendered = fn.sql(dialect="redshift")
        m = _LEADING_FUNC_NAME_RE.match(rendered)
        if not m:
            continue
        name = m.group(1).upper()
        if name == "CONVERT_TIMEZONE":
            continue
        if name in _KNOWN_INVALID_FUNCS:
            warnings.append(_MANUAL + f"{name}() is not valid Redshift SQL — manual rewrite required.")
        elif name.startswith("SAFE_") and name != "SAFE_DIVIDE":
            # SAFE_DIVIDE is regex-post-fixed later; SAFE_CAST never renders (sqlglot
            # emits TRY_CAST). The rest of the SAFE. family has no Redshift
            # counterpart.
            warnings.append(_MANUAL + f"{name}() is not valid Redshift SQL — manual rewrite required.")

    # BQ DATETIME_TRUNC/DATETIME_ADD/DATETIME_SUB parse as typed nodes that sqlglot's
    # Redshift generator passes through verbatim (no TRANSFORMS entry, verified v30.11.0).
    for node in tree.find_all(exp.DatetimeTrunc):
        warnings.append(_MANUAL + "DATETIME_TRUNC() is not valid Redshift SQL — use DATE_TRUNC(datepart, timestamp) instead.")
        break
    for node in tree.find_all(exp.DatetimeAdd, exp.DatetimeSub):
        warnings.append(_MANUAL + "DATETIME_ADD/DATETIME_SUB() is not valid Redshift SQL — use DATEADD(datepart, interval, timestamp) instead.")
        break

    # The 6-integer DATETIME(y,m,d,h,mi,s) form parses as TimestampFromParts and
    # generates MAKE_TIMESTAMP(...) — a Postgres function Redshift doesn't have.
    if next(tree.find_all(exp.TimestampFromParts), None) is not None:
        warnings.append(
            _MANUAL + "DATETIME(y, m, d, h, mi, s) renders as MAKE_TIMESTAMP(), "
            "which Redshift does not support — build the timestamp from a string "
            "literal or TO_TIMESTAMP instead."
        )

    # Belt-and-braces: temporal nodes that survived the fixpoint (unmappable forms
    # keep their node type and would render as DATETIME(...) / TIMESTAMP(...)).
    for node in tree.find_all(exp.Datetime, exp.Timestamp):
        if isinstance(node, exp.Datetime) and node.args.get("expression") is not None:
            continue  # two-part form — already warned by the transform
        warnings.append(
            _MANUAL + f"{type(node).__name__.upper()}(…) constructor reached the "
            "output untranslated — manual rewrite required."
        )
