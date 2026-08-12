"""Identifier quoting for Iceberg DDL (Athena) and Load/Sync DML (Trino) (issue #51).

Two quoting contexts:
- **DDL (Athena engine v3):** Hive-based — reserved/special identifiers use backticks.
- **DML (Trino/Redshift):** ANSI SQL — reserved/special identifiers use double quotes.

Both contexts share the same needs-quoting detection (reserved words + non-standard
names); only the quote character differs.

Delegates reserved-word detection to sqlglot's Redshift dialect (already a project
dependency, and the same dialect engine/redshift/rewrite.py renders with) — one
maintained keyword list instead of a hand-rolled copy.
"""

from __future__ import annotations

import re

from sqlglot import exp

# Redshift standard identifiers: ASCII lowercase start, then ASCII word chars / $.
# Anything outside this (uppercase, non-ASCII, punctuation) must be force-quoted —
# Python's str.isidentifier() is Unicode-aware and would wave through names like
# 'café' that Redshift rejects unquoted.
_PLAIN_IDENTIFIER = re.compile(r"[a-z_][a-z0-9_$]*\Z")

# Reserved words sqlglot's Redshift keyword set misses. 'encrypt' is corrupted
# in sqlglot 30.11.0 ("encrypt     " with trailing spaces), and an unquoted
# encrypt column is a hard syntax error on a live workgroup (verified
# 2026-07-17). Anything appearing here should also be reported upstream.
_KEYWORD_SUPPLEMENT = frozenset({"encrypt"})

# Athena DDL reserved words (Hive grammar) — the full list from the Athena
# "Reserved keywords" doc page (DDL table). Delegating to sqlglot's REDSHIFT
# dialect missed these: Redshift does not reserve DATE/TIME/PRECISION etc., so
# `date date` passed unquoted and failed CREATE TABLE on a live workgroup
# (2026-08-04 audit: 52 tables across three real estates). Over-quoting is safe
# in both contexts — these render backticked in DDL and double-quoted in DML,
# and a quoted lowercase identifier is always valid.
_ATHENA_DDL_RESERVED = frozenset({
    "all", "alter", "and", "array", "as", "authorization", "between", "bigint",
    "binary", "boolean", "both", "by", "cache", "case", "cast", "char",
    "column", "commit", "conf", "constraint", "create", "cross", "cube",
    "current", "current_date", "current_timestamp", "cursor", "database",
    "date", "dayofweek", "decimal", "delete", "describe", "distinct", "double",
    "drop", "else", "end", "exchange", "exists", "extended", "external",
    "extract", "false", "fetch", "float", "floor", "following", "for",
    "foreign", "from", "full", "function", "grant", "group", "grouping",
    "having", "if", "import", "in", "inner", "insert", "int", "integer",
    "intersect", "interval", "into", "is", "join", "lateral", "left", "less",
    "like", "local", "macro", "map", "more", "none", "not", "null", "numeric",
    "of", "on", "only", "or", "order", "out", "outer", "over", "partialscan",
    "partition", "percent", "preceding", "precision", "preserve", "primary",
    "procedure", "range", "reads", "reduce", "references", "regexp", "revoke",
    "right", "rlike", "rollback", "rollup", "row", "rows", "select", "set",
    "smallint", "start", "table", "tablesample", "then", "time", "timestamp",
    "to", "transform", "trigger", "true", "truncate", "unbounded", "union",
    "uniquejoin", "update", "user", "using", "utc_timestamp", "values",
    "varchar", "views", "when", "where", "window", "with",
})


def _needs_quoting(name: str) -> bool:
    """Return True if the identifier requires quoting in either dialect.

    Checks: (1) non-standard characters, (2) local keyword supplement,
    (3) the Athena DDL (Hive) reserved list, (4) sqlglot's Redshift reserved
    word list. The UNION of (3)+(4) is required: each engine reserves words the
    other doesn't (Athena: date/time/precision; Redshift: encrypt/…), and the
    same detection feeds both quoting contexts.
    """
    if _PLAIN_IDENTIFIER.match(name) is None:
        return True
    if name.lower() in _KEYWORD_SUPPLEMENT or name.lower() in _ATHENA_DDL_RESERVED:
        return True
    # sqlglot's Redshift dialect reserved words — if sqlglot would quote it
    # for Redshift, quote it everywhere (over-quoting is always valid).
    ident = exp.to_identifier(name, quoted=None)
    rendered = ident.sql(dialect="redshift")
    return rendered.startswith('"')


def quote_identifier(name: str) -> str:
    """Render one identifier for DML (Trino/Redshift), double-quoting when required.

    Reserved words are quoted per sqlglot's Redshift dialect (plus a local
    supplement for known gaps); non-standard names are force-quoted with
    embedded double quotes doubled.
    """
    force = _needs_quoting(name)
    ident = exp.to_identifier(name, quoted=True if force else None)
    return ident.sql(dialect="redshift")


def quote_identifier_ddl(name: str) -> str:
    """Render one identifier for Athena DDL (Hive-based), backtick-quoting when required.

    Same detection logic as quote_identifier but emits backticks (Athena/Hive syntax)
    instead of double quotes (ANSI/Trino syntax).
    """
    if _needs_quoting(name):
        # Escape any literal backtick in the name (double it, Hive convention)
        escaped = name.replace("`", "``")
        return f"`{escaped}`"
    return name


def quote_full_name(full_name: str) -> str:
    """Quote each dot-separated part of a qualified name for DML (double quotes)."""
    return ".".join(quote_identifier(part) for part in full_name.split("."))


def quote_full_name_ddl(full_name: str) -> str:
    """Quote each dot-separated part of a qualified name for DDL (backticks)."""
    return ".".join(quote_identifier_ddl(part) for part in full_name.split("."))
