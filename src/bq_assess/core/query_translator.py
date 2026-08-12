"""Lightweight BQ→target engine query translator for production query samples.

This is a regex-based best-effort translator for the query workload display in the
report. It does NOT use sqlglot (too slow for thousands of queries and often fails on
anonymized/parameterized production SQL). The goal is illustrative — showing customers
what their production queries would look like on the recommended engine.

For entity-level SQL translation (views, routines), the full AST-based translator in
engine/redshift/rewrite.py is used instead.
"""

from __future__ import annotations

import re


def translate_query(sql: str, engine: str = "redshift") -> str:
    """Best-effort regex translation of a BigQuery query to the target engine.

    Args:
        sql: The BigQuery SQL to translate.
        engine: Target engine — "redshift" or "athena".
    """
    if not sql or not sql.strip():
        return sql

    # Anonymizer-corrupted capture (unbalanced parens/quotes in the ORIGINAL):
    # applying rewrites to garbage produces garbage that LOOKS like a completed
    # translation. Return it untranslated with a visible banner instead.
    if not _is_balanced(sql):
        return (
            "-- [NOT TRANSLATED: source query was corrupted during log "
            "anonymization (unbalanced quotes/parentheses) — refer to the "
            "original above]\n" + sql
        )

    result = sql
    # BEFORE backtick conversion: in valid BQ SQL every double quote is a
    # string literal (identifiers are backticked), so this pass can never
    # touch an identifier — including the double-quoted ones the backtick
    # conversion emits for hyphenated names right after.
    result = _double_quoted_literals_to_single(result)
    result = _translate_backtick_refs(result)

    if engine == "redshift":
        result = _translate_functions_redshift(result)
        result = _translate_types_redshift(result)
    else:
        result = _translate_functions_athena(result)
        result = _translate_types_athena(result)

    result = _translate_syntax(result, engine)

    # BQ MERGE uses alias-qualified SET (SET t.col = ...) that neither target
    # accepts; MERGE also needs an Iceberg-aware review. Flag, don't rewrite.
    if re.match(r"\s*MERGE\b", result, flags=re.IGNORECASE):
        result = (
            "-- NOTE: MERGE translated best-effort — review manually (BQ's "
            "alias-qualified SET t.col=... is not accepted on the target; "
            "unqualify the SET column names)\n" + result
        )
    return result


def translate_to_redshift(sql: str) -> str:
    """Convenience wrapper for Redshift translation."""
    return translate_query(sql, engine="redshift")


def translate_to_athena(sql: str) -> str:
    """Convenience wrapper for Athena/Trino translation."""
    return translate_query(sql, engine="athena")


def _split_args(sql: str, open_idx: int) -> tuple:
    """Split the arguments of a function call at top-level commas.

    ``open_idx`` points at the opening paren. Returns ``(args, end_idx)`` where
    ``end_idx`` is the index just past the closing paren, or ``(None, open_idx)``
    if the parens are unbalanced. Commas inside nested parens or string literals
    do not split.
    """
    depth = 1
    i = open_idx + 1
    n = len(sql)
    args = []
    arg_start = i
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < n and sql[i] != quote:
                i += 2 if sql[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                args.append(sql[arg_start:i].strip())
                return args, i + 1
        elif c == "," and depth == 1:
            args.append(sql[arg_start:i].strip())
            arg_start = i + 1
        i += 1
    return None, open_idx


def _rewrite_call(sql: str, func_name: str, nargs: int, render) -> str:
    """Rewrite every ``func_name(...)`` call with exactly ``nargs`` top-level args.

    ``render(args)`` returns the replacement text, or ``None`` to leave the call
    untouched (used to skip already-translated calls). Calls with a different
    arg count or unbalanced parens are also left untouched. Runs to a fixed
    point so nested calls (e.g. IF inside IF) are all rewritten.
    """
    pattern = re.compile(r"\b" + func_name + r"\s*\(", re.IGNORECASE)
    for _ in range(10):  # fixed-point iteration cap for pathological nesting
        out = []
        pos = 0
        changed = False
        while True:
            m = pattern.search(sql, pos)
            if not m:
                break
            args, end = _split_args(sql, m.end() - 1)
            replacement = render(args) if args is not None and len(args) == nargs else None
            if replacement is None:
                out.append(sql[pos:m.end()])
                pos = m.end()
                continue
            out.append(sql[pos:m.start()])
            out.append(replacement)
            pos = end
            changed = True
        out.append(sql[pos:])
        sql = "".join(out)
        if not changed:
            break
    return sql


def _is_balanced(sql: str) -> bool:
    """Parens balanced and single quotes paired, quote-aware.

    Detects anonymizer-corrupted captures (17 real files, 2026-08-04 audit).
    Backslash escapes are rare in BQ SQL ('' doubling is the norm) but handled.
    """
    depth = 0
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    # '' inside a literal is an escaped quote, keep scanning
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            else:
                return False  # unterminated string
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0


# "..." literal: BQ treats double quotes as strings; on the targets they
# delimit IDENTIFIERS — DATE("2021-01-01") parses as a column ref and can
# silently misbehave (the one leftover class that fails quietly; 927 files,
# 2026-08-04 audit). Skip anything containing a single quote or double-doubled
# quotes (likely a real quoted identifier), convert the rest to '...'.
_DQ_LITERAL_RE = re.compile(r'"([^"\'\\]*)"')

# A double-quoted token in identifier POSITION (after . or before .) must stay.
_DQ_IDENTIFIER_GUARD = re.compile(r'(?:\.\s*"|"\s*\.)')


def _double_quoted_literals_to_single(sql: str) -> str:
    def _convert(m: re.Match) -> str:
        # touching-a-dot ⇒ identifier usage (schema."table" / "col".field)
        start, end = m.start(), m.end()
        before = sql[max(0, start - 2):start]
        after = sql[end:end + 2]
        if before.rstrip().endswith(".") or after.lstrip().startswith("."):
            return m.group(0)
        return "'" + m.group(1) + "'"

    return _DQ_LITERAL_RE.sub(_convert, sql)


def _translate_backtick_refs(sql: str) -> str:
    """Convert `project.dataset.table` → schema.table (both engines use this).

    Dataset/table parts may carry hyphens too (`...table_20251101-20260504` —
    845 real refs stayed backticked when only the project part allowed '-',
    2026-08-04 audit); hyphenated parts are emitted double-quoted since a bare
    hyphen ends an unquoted identifier on both target engines.
    """
    def _part(name: str) -> str:
        return f'"{name}"' if "-" in name else name

    sql = re.sub(
        r"`([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)`",
        lambda m: f"{_part(m.group(2))}.{_part(m.group(3))}",
        sql,
    )
    sql = re.sub(
        r"`([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_-]+)`",
        lambda m: f"{_part(m.group(1))}.{_part(m.group(2))}",
        sql,
    )
    sql = re.sub(r"`([a-zA-Z0-9_-]+)`", lambda m: _part(m.group(1)), sql)
    return sql


# ---------------------------------------------------------------------------
# Redshift translations
# ---------------------------------------------------------------------------

def _translate_functions_redshift(sql: str) -> str:
    """Translate BQ function names to Redshift equivalents."""
    sql = re.sub(r"\bIFNULL\s*\(", "NVL(", sql, flags=re.IGNORECASE)
    sql = _rewrite_call(
        sql, "IF", 3,
        lambda a: f"CASE WHEN {a[0]} THEN {a[1]} ELSE {a[2]} END",
    )
    # SAFE_CAST returns NULL on failure; Redshift has no TRY_CAST, so plain
    # CAST (which aborts) is the closest form — annotate the semantic change
    # once per query instead of silently altering failure behavior.
    if re.search(r"\bSAFE_CAST\s*\(", sql, flags=re.IGNORECASE):
        sql = re.sub(r"\bSAFE_CAST\s*\(", "CAST(", sql, flags=re.IGNORECASE)
        sql = "-- NOTE: SAFE_CAST → CAST (Redshift has no TRY_CAST; CAST errors on bad input instead of returning NULL)\n" + sql
    # BQ: TIMESTAMP_DIFF(end, start, unit) / DATE_DIFF(end, start, unit)
    # Redshift: DATEDIFF(unit, start, end)
    for bq_fn in ("TIMESTAMP_DIFF", "DATETIME_DIFF", "DATE_DIFF"):
        sql = _rewrite_call(
            sql, bq_fn, 3,
            lambda a: f"DATEDIFF({a[2]}, {a[1]}, {a[0]})",
        )
    # BQ: DATE_ADD(expr, INTERVAL n unit) → Redshift: DATEADD(unit, n, expr).
    # Structure-aware so a comma inside the first arg (DATE_TRUNC(d, MONTH))
    # doesn't silently defeat the rewrite.
    for bq_fn, sign in (("DATE_SUB", "-"), ("DATE_ADD", ""),
                        ("TIMESTAMP_ADD", ""), ("TIMESTAMP_SUB", "-"),
                        ("DATETIME_ADD", ""), ("DATETIME_SUB", "-")):
        sql = _rewrite_call(
            sql, bq_fn, 2,
            lambda a, s=sign: _interval_to_dateadd(a, s, "DATEADD({u}, {n}, {e})"),
        )
    # BQ FORMAT_*/PARSE_* take (format, expr); Redshift TO_CHAR/TO_DATE/
    # TO_TIMESTAMP take (expr, format) — a blind name substitution shipped
    # TO_CHAR('fmt', ts) with inverted args (2026-08-04 audit, 20 occurrences).
    # Swap args structurally; the strftime format string is left as-is (its
    # tokens differ too), which keeps the mismatch VISIBLE rather than subtly
    # inverted. Calls with a different arity are left untranslated.
    for bq_fn, rs_fn in (("FORMAT_DATE", "TO_CHAR"), ("FORMAT_TIMESTAMP", "TO_CHAR"),
                         ("PARSE_DATE", "TO_DATE"), ("PARSE_TIMESTAMP", "TO_TIMESTAMP")):
        sql = _rewrite_call(
            sql, bq_fn, 2,
            lambda a, f=rs_fn: f"{f}({a[1]}, {a[0]} /* convert format tokens */)",
        )
    sql = re.sub(r"\bCURRENT_DATETIME\s*\(\s*\)", "GETDATE()", sql, flags=re.IGNORECASE)
    # Redshift accepts CURRENT_TIMESTAMP/CURRENT_DATE only WITHOUT parens —
    # the BQ parenthesized form is one character from valid and shipped as-is
    # in 16 real files (2026-08-04 audit).
    sql = re.sub(r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCURRENT_DATE\s*\(\s*\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bGENERATE_UUID\s*\(\s*\)", "MD5(RANDOM()::TEXT || GETDATE()::TEXT)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bARRAY_LENGTH\s*\(", "GET_ARRAY_LENGTH(", sql, flags=re.IGNORECASE)
    # STRUCT is deliberately NOT rewritten for Redshift: there is no equivalent
    # constructor (ROW( is Trino-only). The AST translator flags STRUCT as a
    # manual rewrite (SUPER + PartiQL) — emitting ROW( here contradicted that
    # guidance in the same report with SQL that fails on Redshift.
    return sql


# BQ-only type spellings that are invalid (or semantically different) on the
# targets and therefore must be rewritten in query text. Deliberately narrow:
# names valid on both engines (DATE, TIMESTAMP, INT, DECIMAL, ...) are left
# alone — substituting them risks touching literals and identifiers.
# NUMERIC is included because BQ NUMERIC is fixed (38,9) while Redshift
# NUMERIC defaults to (18,0) — same word, different meaning.
_BQ_ONLY_TYPE_NAMES = (
    "INT64", "FLOAT64", "BOOL", "STRING", "BYTES", "NUMERIC", "BIGNUMERIC",
)

# STRING/NUMERIC are also BQ function-name prefixes (STRING(ts), NUMERIC(x));
# the guard skips those call forms.
_TYPE_GUARD = {"STRING": r"(?!\s*\()", "NUMERIC": r"(?!\s*\()"}


def _apply_type_map(sql: str, type_map: dict) -> str:
    for bq_type, target_type in type_map.items():
        guard = _TYPE_GUARD.get(bq_type, "")
        sql = re.sub(
            rf"\b{bq_type}\b{guard}", target_type, sql, flags=re.IGNORECASE
        )
    return sql


def _redshift_query_type_map() -> dict:
    """BQ→Redshift type substitutions, derived from the doc-verified DDL map.

    Single source (2026-08-04 consolidation): storage_placement._BQ_TO_REDSHIFT
    is the ADR-0005 doc-verified map, pinned by test_storage_placement. Deriving
    from it keeps the translated-query pane and the generated DDL consistent —
    they had drifted (bare VARCHAR here = VARCHAR(256) default on Redshift and
    silent truncation vs VARCHAR(65535) in the DDL).
    """
    from bq_assess.engine.redshift.storage_placement import _BQ_TO_REDSHIFT
    return {name: _BQ_TO_REDSHIFT[name] for name in _BQ_ONLY_TYPE_NAMES}


def _translate_types_redshift(sql: str) -> str:
    """Translate BQ-only type names to their Redshift equivalents."""
    return _apply_type_map(sql, _redshift_query_type_map())


# ---------------------------------------------------------------------------
# Athena/Trino translations
# ---------------------------------------------------------------------------

def _translate_functions_athena(sql: str) -> str:
    """Translate BQ function names to Athena/Trino equivalents."""
    sql = re.sub(r"\bIFNULL\s*\(", "COALESCE(", sql, flags=re.IGNORECASE)
    # Trino supports IF(cond, then, else) natively — no rewrite needed.
    sql = re.sub(r"\bSAFE_CAST\s*\(", "TRY_CAST(", sql, flags=re.IGNORECASE)
    # BQ: TIMESTAMP_DIFF(end, start, unit) / DATE_DIFF(end, start, unit)
    # Trino: DATE_DIFF('unit', start, end)
    # Output is also named DATE_DIFF, so skip calls whose first arg is already a
    # quoted unit string — otherwise the fixed-point pass would re-scramble them.
    def _diff_to_trino(a):
        if a[0].startswith("'"):
            return None
        return f"DATE_DIFF('{a[2].lower()}', {a[1]}, {a[0]})"

    for bq_fn in ("TIMESTAMP_DIFF", "DATETIME_DIFF", "DATE_DIFF"):
        sql = _rewrite_call(sql, bq_fn, 3, _diff_to_trino)
    # BQ: DATE_ADD(expr, INTERVAL n unit) → Trino: DATE_ADD('unit', n, expr).
    # Trino's own DATE_ADD takes a quoted unit first — the interval-form guard
    # inside _interval_to_dateadd skips already-translated calls.
    for bq_fn, sign in (("DATE_SUB", "-"), ("DATE_ADD", ""),
                        ("TIMESTAMP_ADD", ""), ("TIMESTAMP_SUB", "-"),
                        ("DATETIME_ADD", ""), ("DATETIME_SUB", "-")):
        sql = _rewrite_call(
            sql, bq_fn, 2,
            lambda a, s=sign: _interval_to_dateadd(a, s, "DATE_ADD('{ul}', {n}, {e})"),
        )
    # BQ FORMAT_*/PARSE_* take (format, expr); Trino DATE_FORMAT/DATE_PARSE
    # take (expr, format) — swap structurally (same inversion bug as Redshift).
    for bq_fn, tr_fn in (("FORMAT_DATE", "DATE_FORMAT"), ("FORMAT_TIMESTAMP", "DATE_FORMAT"),
                         ("PARSE_DATE", "DATE_PARSE"), ("PARSE_TIMESTAMP", "DATE_PARSE")):
        sql = _rewrite_call(
            sql, bq_fn, 2,
            lambda a, f=tr_fn: f"{f}({a[1]}, {a[0]} /* convert format tokens */)",
        )
    sql = re.sub(r"\bCURRENT_DATETIME\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    # Trino also uses the paren-less forms.
    sql = re.sub(r"\bCURRENT_TIMESTAMP\s*\(\s*\)", "CURRENT_TIMESTAMP", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bCURRENT_DATE\s*\(\s*\)", "CURRENT_DATE", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bGENERATE_UUID\s*\(\s*\)", "UUID()", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bARRAY_LENGTH\s*\(", "CARDINALITY(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bSTRUCT\s*\(", "ROW(", sql, flags=re.IGNORECASE)  # ROW is valid Trino
    return sql


# Iceberg DDL type → Trino query-context spelling. Athena DDL uses lowercase
# Iceberg names ('string'); CAST expressions use Trino types (VARCHAR).
_ICEBERG_TO_TRINO_QUERY = {
    "string": "VARCHAR",
    "bigint": "BIGINT",
    "double": "DOUBLE",
    "boolean": "BOOLEAN",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "decimal(38,9)": "DECIMAL(38,9)",
}


def _athena_query_type_map() -> dict:
    """BQ→Trino type substitutions, derived from the converter's Iceberg maps.

    Single source (2026-08-04 consolidation): on the Athena path the data
    physically lands as the converter's Iceberg types (targets/iceberg), so
    query casts must target what the column actually IS. This matters for the
    lossy mappings — e.g. BYTES lands as a base64 STRING per LOSSY_TYPE_MAP, so
    CAST(x AS BYTES) must become VARCHAR: the old hand-rolled VARBINARY cast
    targeted a type the migrated column doesn't have.
    """
    from bq_assess.targets.iceberg.converter import CLEAN_TYPE_MAP, LOSSY_TYPE_MAP
    iceberg = {**CLEAN_TYPE_MAP, **{k: v[0] for k, v in LOSSY_TYPE_MAP.items()}}
    out = {}
    for name in _BQ_ONLY_TYPE_NAMES:
        # BIGNUMERIC has no converter entry (flagged separately as precision
        # loss) — it lands as decimal(38,9), same as NUMERIC.
        ice = iceberg.get(name, iceberg.get("NUMERIC", "string"))
        out[name] = _ICEBERG_TO_TRINO_QUERY.get(ice, ice.upper())
    return out


def _translate_types_athena(sql: str) -> str:
    """Translate BQ-only type names to their Trino query equivalents."""
    return _apply_type_map(sql, _athena_query_type_map())


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------

# The interval count is a digit run OR the anonymizer's '?' placeholder —
# requiring digits alone made the DATEADD rewrite match 0 of 7,730 real calls
# (anonymized logs carry 'INTERVAL ? DAY'; 2026-08-04 audit).
_INTERVAL_ARG_RE = re.compile(r"^INTERVAL\s+(\d+|\?)\s+(\w+)$", re.IGNORECASE)


def _interval_to_dateadd(args: list, sign: str, template: str) -> str | None:
    """Render a BQ (expr, INTERVAL n unit) pair via ``template``, or None to skip.

    None (leave untouched) when the second arg isn't a literal INTERVAL — that
    covers Trino's own DATE_ADD('unit', n, expr) output on the fixed-point pass
    and interval expressions this best-effort translator doesn't model.
    Template placeholders: {u}=unit, {ul}=unit lowercased, {n}=signed count,
    {e}=expr. A '?' count renders as -? for SUB (the placeholder keeps its
    anonymized meaning; sign still documents direction).
    """
    m = _INTERVAL_ARG_RE.match(args[1])
    if m is None:
        return None
    n, unit = m.group(1), m.group(2)
    return template.format(u=unit, ul=unit.lower(), n=f"{sign}{n}", e=args[0])


# BQ SELECT-modifier `* EXCEPT (cols)`. Only fires right after `*` or `.*` —
# a set-operator EXCEPT (SELECT ... EXCEPT (SELECT ...)) never follows `*`,
# so valid set ops are left alone on both engines.
_SELECT_STAR_EXCEPT_RE = re.compile(r"(\*\s*)EXCEPT\s*\(", re.IGNORECASE)


def _translate_syntax(sql: str, engine: str) -> str:
    """Translate BQ-specific syntax patterns."""
    if engine == "redshift":
        # SELECT * EXCEPT(cols) has no Redshift equivalent — comment the
        # modifier out (paren-aware: the old [^)]+ regex broke on nested
        # parens and also destroyed EXCEPT set-operator subqueries).
        out = []
        pos = 0
        while True:
            m = _SELECT_STAR_EXCEPT_RE.search(sql, pos)
            if not m:
                break
            args, end = _split_args(sql, m.end() - 1)
            if args is None:
                out.append(sql[pos:m.end()])
                pos = m.end()
                continue
            out.append(sql[pos:m.start()])
            out.append(m.group(1))
            out.append(f"/* EXCEPT({', '.join(args)}) — remove manually */")
            pos = end
        out.append(sql[pos:])
        sql = "".join(out)
    # Athena/Trino: SELECT * EXCEPT is invalid Trino too, but Athena engine v3
    # has no rewrite either — leave it visible rather than hide it in a comment.
    return sql
