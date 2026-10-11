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

    # Anything BQ-only still present after translation gets named, per statement.
    # Before this (#71), unhandled constructs passed through silently and were
    # written out labelled "translated" — 21 of 49 real statements failed on a
    # live Athena workgroup with no warning anywhere in the report.
    result = _flag_untranslated(result, engine)
    return result


# Constructs with no equivalent this regex translator can produce. Kept
# deliberately small and evidence-based: every entry is either verified to fail
# on the target or is a known semantic divergence. Bare-word entries match as
# whole words; the rest match as function calls.
_UNTRANSLATABLE = {
    "athena": (
        (r"\bST_DWITHIN\s*\(", "ST_DWITHIN has no Trino equivalent (use ST_Distance(...) < d)"),
        (r"\bNET\.\w+\s*\(", "BigQuery NET.* functions have no Trino equivalent"),
        (r"\bML\.\w+\s*\(", "BigQuery ML.* functions have no Trino equivalent"),
        (r"\bGENERATE_DATE_ARRAY\s*\(", "GENERATE_DATE_ARRAY -> Trino SEQUENCE (different signature)"),
        (r"\bGENERATE_ARRAY\s*\(", "GENERATE_ARRAY -> Trino SEQUENCE (different signature)"),
        (r"\*\s*EXCEPT\s*\(", "SELECT * EXCEPT(...) is not valid Trino — list columns explicitly"),
        (r"\bQUALIFY\b", "QUALIFY is not supported in Trino — wrap in a subquery"),
        (r"\bIGNORE\s+NULLS\b", "IGNORE NULLS is not valid Trino — use FILTER (WHERE ... IS NOT NULL)"),
        (r"\bWITH\s+OFFSET\b",
         "UNNEST ... WITH OFFSET -> Trino WITH ORDINALITY AS t(value, ordinal)"),
        # Survives translation only when the quantile count was anonymized to
        # '?', which leaves no literal to expand into approx_percentile's array.
        (r"\bAPPROX_QUANTILES\s*\(",
         ("APPROX_QUANTILES needs a literal bucket count to become "
          "approx_percentile(x, ARRAY[...]) — the count here is a parameter")),
        (r"\bST_GEOGFROMTEXT\s*\(|\bST_GEOMETRYFROMTEXT\s*\(",
         "BQ GEOGRAPHY is geodesic, Trino geometry is planar — distances/areas differ"),
    ),
    "redshift": (
        (r"\bNET\.\w+\s*\(", "BigQuery NET.* functions have no Redshift equivalent"),
        (r"\bML\.\w+\s*\(", "BigQuery ML.* functions have no Redshift equivalent"),
        (r"\bQUALIFY\b", "QUALIFY is not supported in Redshift — wrap in a subquery"),
        (r"\bUNNEST\s*\(", "BQ UNNEST semantics differ on Redshift — review array handling"),
    ),
}


def _flag_untranslated(sql: str, engine: str) -> str:
    """Prepend a per-statement warning naming BQ-only constructs left in ``sql``.

    Regex-only by design: this module deliberately avoids sqlglot (too slow at
    thousands of queries, and it cannot parse the anonymizer's '?' placeholders),
    so validation has to work the same way the translation does.
    """
    hits = []
    for pattern, message in _UNTRANSLATABLE.get(engine, ()):
        if re.search(pattern, sql, flags=re.IGNORECASE):
            hits.append(message)
    if engine == "athena":
        # Context-dependent, so not expressible as a bare pattern: a USING join
        # only breaks when the merged column is also referenced qualified.
        hits.extend(_using_join_conflicts(sql))
    if not hits:
        return sql
    banner = "".join(f"-- WARNING: {h}\n" for h in dict.fromkeys(hits))
    return (
        "-- [NOT FULLY TRANSLATED — this statement will not run as-is]\n"
        + banner + sql
    )


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


# BQ's SAFE. call prefix: SAFE.ST_GEOGFROMTEXT(x) == "evaluate, NULL on error".
_SAFE_PREFIX_RE = re.compile(r"\bSAFE\.(\w+)\s*\(", re.IGNORECASE)


def _safe_prefix_to_try(sql: str) -> str:
    """Rewrite ``SAFE.fn(args)`` to Trino's ``TRY(fn(args))``.

    Paren-aware via ``_split_args`` rather than a regex, because the closing
    paren has to be located to place TRY's own. Left untouched when the call is
    unbalanced (anonymizer damage), matching the rest of this module.
    """
    for _ in range(10):                     # fixed point: SAFE. inside SAFE.
        m = _SAFE_PREFIX_RE.search(sql)
        if not m:
            break
        args, end = _split_args(sql, m.end() - 1)
        if args is None:
            break
        inner = "{}({})".format(m.group(1), ", ".join(args))
        sql = f"{sql[:m.start()]}TRY({inner}){sql[end:]}"
    return sql


# ``UNNEST(`` call sites, plus the tokens that may legally follow the closing
# paren when the UNNEST carries NO alias (so a bare keyword is never eaten as
# one). ``AS`` is absent deliberately: it introduces the aliased form.
_UNNEST_CALL_RE = re.compile(r"\bUNNEST\s*\(", re.IGNORECASE)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_WITH_OFFSET_RE = re.compile(r"\s*WITH\s+OFFSET\b", re.IGNORECASE)
_UNNEST_TRAILING_KEYWORDS = frozenset({
    "with", "on", "using", "where", "group", "order", "having", "limit",
    "offset", "union", "intersect", "except", "cross", "join", "left",
    "right", "full", "inner", "outer", "window", "qualify", "tablesample",
})


def _fix_unnest_aliases_athena(sql: str) -> str:
    """Rewrite BQ's ``UNNEST(expr) alias`` to Trino's ``UNNEST(expr) AS alias_u(alias)``.

    In BigQuery the alias on an UNNEST names the produced *element*, so
    ``FROM t c, UNNEST(c.items) i`` makes ``i.category`` resolve. In Trino the
    very same alias names the produced *relation*; the column inside it stays
    unnamed, so ``i.category`` has nothing to resolve against and the statement
    dies with COLUMN_NOT_FOUND. Trino wants a column alias list instead.

    Verified on Athena engine v3 against the live testbed: UNNEST of an
    ``array(row(...))`` yields exactly ONE ROW-typed column — a 2-entry alias
    list over a 2-field row is rejected with MISMATCHED_COLUMN_ALIASES. That is
    what makes this rewrite possible without knowing the schema, and makes it
    identical for arrays of structs and arrays of scalars.

    Left untouched: the already-correct ``AS rel(col, ...)`` form (idempotent),
    un-aliased UNNEST, and BQ's ``WITH OFFSET`` (that one maps to Trino's
    WITH ORDINALITY and changes the alias arity, so ``_flag_untranslated``
    names it rather than this guessing at it).
    """
    out = []
    pos = 0
    while True:
        m = _UNNEST_CALL_RE.search(sql, pos)
        if not m:
            break
        args, end = _split_args(sql, m.end() - 1)
        if args is None:                    # unbalanced — leave the remainder
            break
        i = _skip_ws(sql, end)
        token = _IDENT_RE.match(sql, i)
        had_as = False
        if token and token.group(0).lower() == "as":
            had_as = True
            token = _IDENT_RE.match(sql, _skip_ws(sql, token.end()))
        # No alias at all, or the next word is a clause keyword rather than one.
        if not token or (not had_as and token.group(0).lower() in _UNNEST_TRAILING_KEYWORDS):
            out.append(sql[pos:end])
            pos = end
            continue
        alias = token.group(0)
        after = _skip_ws(sql, token.end())
        already_trino = after < len(sql) and sql[after] == "("
        if already_trino or _WITH_OFFSET_RE.match(sql, token.end()):
            out.append(sql[pos:end])
            pos = end
            continue
        out.append(sql[pos:end])
        out.append(f" AS {alias}_u({alias})")
        pos = token.end()
    out.append(sql[pos:])
    return "".join(out)


def _skip_ws(sql: str, i: int) -> int:
    """Index of the first non-whitespace character at or after ``i``."""
    while i < len(sql) and sql[i].isspace():
        i += 1
    return i


def _split_top_level(text: str) -> list:
    """Split ``text`` on commas that are not inside parens or quotes."""
    parts = []
    depth = 0
    start = 0
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        elif c == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
        i += 1
    parts.append(text[start:])
    return parts


_CLAUSE_RE = re.compile(
    r"\b(SELECT|FROM|GROUP\s+BY|HAVING|ORDER\s+BY|LIMIT|WINDOW|UNION|INTERSECT"
    r"|EXCEPT|QUALIFY)\b",
    re.IGNORECASE,
)


def _scan_clauses(sql: str) -> list:
    """Locate clause keywords as ``(keyword, start, end, paren_depth)`` tuples.

    Quote- and comment-aware, so a keyword inside a string literal or a ``--``
    comment is never mistaken for clause structure. The depth lets callers pair
    a GROUP BY with the SELECT of its own query block rather than an enclosing
    or nested one.
    """
    spans = []
    depth = 0
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < n and sql[i] != quote:
                i += 2 if sql[i] == "\\" else 1
            i += 1
            continue
        if sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        else:
            m = _CLAUSE_RE.match(sql, i)
            if m:
                spans.append(
                    (re.sub(r"\s+", " ", m.group(1)).upper(), m.start(), m.end(), depth)
                )
                i = m.end()
                continue
        i += 1
    return spans


_AS_ALIAS_RE = re.compile(r"\bAS\s+([A-Za-z_][A-Za-z_0-9]*)\s*$", re.IGNORECASE)
_BARE_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
# An alias over an aggregate can never be a legal GROUP BY key (BigQuery rejects
# it too), so substituting one in would produce worse SQL, not better.
_AGG_HEAD_RE = re.compile(
    r"^(COUNT|SUM|AVG|MIN|MAX|ARRAY_AGG|STRING_AGG|LISTAGG|APPROX\w*"
    r"|STDDEV\w*|VAR\w*|COUNT_IF|ANY_VALUE|BOOL_\w+)\s*\(",
    re.IGNORECASE,
)


def _select_aliases(select_list: str, allow_aggregates: bool = False) -> dict:
    """Map ``AS <alias>`` names to their defining expression, lowercased keys.

    Aggregates are excluded for GROUP BY (grouping by one is invalid on both
    engines) but kept for HAVING, where an aggregate is precisely the point.
    """
    out = {}
    for item in _split_top_level(select_list):
        m = _AS_ALIAS_RE.search(item)
        if not m:
            continue
        expr = item[:m.start()].strip()
        if expr and (allow_aggregates or not _AGG_HEAD_RE.match(expr)):
            out[m.group(1).lower()] = expr
    return out


_ATOMIC_EXPR_RE = re.compile(r"^[\w.]+$")


def _parenthesize(expr: str) -> str:
    """Wrap ``expr`` unless it is already atomic (a path or one function call)."""
    if _ATOMIC_EXPR_RE.match(expr):
        return expr
    m = re.match(r"^\w+\s*\(", expr)
    if m:
        args, end = _split_args(expr, m.end() - 1)
        if args is not None and end == len(expr):
            return expr
    return f"({expr})"


def _substitute_idents(text: str, mapping: dict) -> tuple:
    """Replace bare identifiers found in ``mapping`` with their expressions.

    Skips string literals, qualified names (``t.sessions``) and function names
    (``sessions(``), so only genuine output-alias references are rewritten.
    """
    out = []
    i = 0
    n = len(text)
    changed = False
    while i < n:
        c = text[i]
        if c in ("'", '"', "`"):
            quote = c
            j = i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:min(j + 1, n)])
            i = j + 1
            continue
        m = _BARE_IDENT_RE.match(text, i)
        if not m:
            out.append(c)
            i += 1
            continue
        word = m.group(0)
        prev = text[i - 1] if i else ""
        nxt = _skip_ws(text, m.end())
        qualified = prev == "."
        is_call = nxt < n and text[nxt] == "("
        if not qualified and not is_call and word.lower() in mapping:
            out.append(_parenthesize(mapping[word.lower()]))
            changed = True
        else:
            out.append(word)
        i = m.end()
    return "".join(out), changed


def _fix_output_alias_refs_athena(sql: str) -> str:
    """Inline SELECT output aliases referenced from GROUP BY or HAVING.

    BigQuery resolves output aliases in both clauses; Trino resolves neither,
    because both run before projection, and fails with COLUMN_NOT_FOUND.
    Measured on Athena engine v3: ``SELECT status AS s ... GROUP BY s`` fails
    while ``GROUP BY status`` succeeds; ``HAVING sessions > 1`` over
    ``COUNT(*) AS sessions`` fails the same way. ORDER BY is deliberately left
    alone — it runs after projection and resolves aliases on both engines.
    7 of 49 live statements hit this.

    Substituting the expression (rather than a GROUP BY ordinal) keeps the
    emitted SQL readable, which matters because customers read this output.
    """
    spans = _scan_clauses(sql)
    edits = []
    for k, (kw, _s, end_kw, depth) in enumerate(spans):
        if kw not in ("GROUP BY", "HAVING"):
            continue
        select_end = from_start = None
        for j in range(k - 1, -1, -1):
            kw2, s2, e2, d2 = spans[j]
            if d2 != depth:
                continue
            if kw2 == "FROM" and from_start is None:
                from_start = s2
            elif kw2 == "SELECT":
                select_end = e2
                break
        if select_end is None or from_start is None or from_start < select_end:
            continue
        select_list = sql[select_end:from_start]
        clause_end = _clause_extent(sql, end_kw, depth, spans, k)
        clause = sql[end_kw:clause_end]
        lead = clause[:len(clause) - len(clause.lstrip())] or " "
        trail = clause[len(clause.rstrip()):]

        if kw == "GROUP BY":
            aliases = _select_aliases(select_list)
            if not aliases:
                continue
            rebuilt = []
            changed = False
            for item in _split_top_level(clause):
                bare = item.strip()
                if _BARE_IDENT_RE.fullmatch(bare) and bare.lower() in aliases:
                    rebuilt.append(aliases[bare.lower()])
                    changed = True
                else:
                    rebuilt.append(bare)
            body = ", ".join(rebuilt)
        else:
            aliases = _select_aliases(select_list, allow_aggregates=True)
            if not aliases:
                continue
            body, changed = _substitute_idents(clause.strip(), aliases)

        if changed:
            edits.append((end_kw, clause_end, lead + body + trail))
    for start, stop, text in reversed(edits):
        sql = sql[:start] + text + sql[stop:]
    return sql


def _clause_extent(sql: str, start: int, depth: int, spans: list, k: int) -> int:
    """End index of the clause beginning at ``start`` within its query block.

    Stops at the next clause keyword that is not nested deeper, or at the paren
    that closes the block, whichever comes first.
    """
    limit = len(sql)
    for j in range(k + 1, len(spans)):
        _kw, s2, _e2, d2 = spans[j]
        if d2 <= depth:
            limit = s2
            break
    local = 0
    i = start
    while i < min(limit, len(sql)):
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            i += 1
            while i < len(sql) and sql[i] != quote:
                i += 2 if sql[i] == "\\" else 1
        elif c == "(":
            local += 1
        elif c == ")":
            if local == 0:
                return i
            local -= 1
        i += 1
    return limit


_USING_RE = re.compile(r"\bUSING\s*\(", re.IGNORECASE)
_JOIN_KW_RE = re.compile(r"\bJOIN\b", re.IGNORECASE)
_OUTER_JOIN_RE = re.compile(r"\b(LEFT|RIGHT|FULL)\s+(OUTER\s+)?$", re.IGNORECASE)
_TRAILING_IDENT_RE = re.compile(r"([A-Za-z_][A-Za-z_0-9]*)\s*$")


_SUBQUERY_OPEN_RE = re.compile(r"\(\s*(SELECT|WITH)\b", re.IGNORECASE)
_QUALIFIED_REF_RE = re.compile(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)")


def _scope_depths(sql: str) -> list:
    """Per-character *subquery* nesting depth (not paren depth).

    Only a paren that opens a SELECT/WITH creates a new name-resolution scope.
    A function call's parens must not, or ``COUNT(DISTINCT o.user_id)`` would
    look like it lived in a different scope from the join that merged the
    column. Quote-aware.
    """
    depths = [0] * (len(sql) + 1)
    stack = []
    depth = 0
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            j = i + 1
            while j < n and sql[j] != quote:
                j += 2 if sql[j] == "\\" else 1
            for k in range(i, min(j + 1, n)):
                depths[k] = depth
            i = j + 1
            continue
        if c == "(":
            scoped = bool(_SUBQUERY_OPEN_RE.match(sql, i))
            stack.append(scoped)
            depths[i] = depth
            if scoped:
                depth += 1
            i += 1
            continue
        if c == ")":
            if stack.pop() if stack else False:
                depth -= 1
            depths[i] = depth
            i += 1
            continue
        depths[i] = depth
        i += 1
    depths[n] = depth
    return depths


def _using_clauses(sql: str, depths: list | None = None) -> list:
    """Describe each ``USING (...)`` join: columns, aliases, outer-ness, scope.

    ``aliases`` holds the two relations the join immediately connects, and
    ``depth`` the subquery scope it sits in. Together they bound any rewrite to
    references that genuinely denote the merged column.
    """
    if depths is None:
        depths = _scope_depths(sql)
    out = []
    pos = 0
    while True:
        m = _USING_RE.search(sql, pos)
        if not m:
            break
        args, end = _split_args(sql, m.end() - 1)
        if args is None:
            break
        head = sql[:m.start()]
        join = None
        for join in _JOIN_KW_RE.finditer(head):
            pass                                    # last JOIN before this USING
        if join is None:
            pos = end
            continue
        left = _TRAILING_IDENT_RE.search(head[:join.start()])
        right = _TRAILING_IDENT_RE.search(head[join.end():])
        out.append({
            "cols": [c.strip().strip('"') for c in args if c.strip()],
            "aliases": [m2.group(1) for m2 in (left, right) if m2],
            "outer": bool(_OUTER_JOIN_RE.search(head[:join.start()])),
            "depth": depths[m.start()],
        })
        pos = end
    return out


def _fix_using_qualifiers_athena(sql: str) -> str:
    """Drop the alias qualifier on columns a USING join merges, when safe.

    Trino follows the standard: ``USING`` *merges* the join column, so
    ``o.user_id`` no longer resolves. BigQuery keeps both sides addressable.

    Measured on Athena engine v3 with literal rows, comparing result sets and
    not just syntax:

    - inner join — ``SELECT id ... JOIN b USING (id)`` returns the same rows as
      ``SELECT a.id ... JOIN b ON a.id = b.id``, so dropping the qualifier is
      an exact rewrite (the merged column equals both sides by definition of
      the equi-join).
    - outer join — the merged column behaves like COALESCE(left, right):
      ``RIGHT JOIN ... USING (id)`` yielded ``[2, 3]`` where the ON form
      qualified to the left table yielded ``[2, NULL]``. Dropping the qualifier
      there would silently change the answer, so those are left for
      ``_using_join_conflicts`` to disclose.
    - only USING columns merge — ``a.av`` stays resolvable alongside
      ``USING (id)``, so unrelated qualifiers must be preserved.

    Scoped to the subquery that owns the join, and to that join's own two
    aliases. Without the scope bound, ``o.user_id`` inside a correlated
    ``EXISTS`` was rewritten to a bare ``user_id`` that rebinds to the inner
    relation, turning the correlation into ``x.user_id = x.user_id`` — a
    statement that runs and returns the wrong rows, which is worse than one
    that fails.
    """
    depths = _scope_depths(sql)
    clauses = _using_clauses(sql, depths)
    if not clauses:
        return sql
    unsafe = set()
    targets = {}
    for clause in clauses:
        for col in clause["cols"]:
            if clause["outer"]:
                unsafe.add(col.lower())
            else:
                scope = targets.setdefault(clause["depth"], {})
                scope.setdefault(col.lower(), set()).update(
                    a.lower() for a in clause["aliases"]
                )
    for scope in targets.values():
        for col in list(scope):
            if col in unsafe:                       # merged by an outer join too
                del scope[col]
    if not any(targets.values()):
        return sql

    out = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            quote = c
            j = i + 1
            while j < n and sql[j] != quote:
                j += 2 if sql[j] == "\\" else 1
            out.append(sql[i:min(j + 1, n)])
            i = j + 1
            continue
        m = _QUALIFIED_REF_RE.match(sql, i)
        starts_path = i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] in "_.")
        if m and starts_path:
            alias, col = m.group(1).lower(), m.group(2)
            scope = targets.get(depths[i], {})
            if col.lower() in scope and alias in scope[col.lower()]:
                out.append(col)
                i = m.end()
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _using_join_conflicts(sql: str) -> list:
    """Qualified references to a merged USING column that could not be rewritten.

    Runs after ``_fix_using_qualifiers_athena``, so anything still here is a
    case that rewrite deliberately declined: an outer join (where the merged
    column is COALESCE-like and stripping would change the result) or a
    qualifier belonging to neither joined relation, which happens once joins
    chain and the column's owning table is no longer inferable.
    """
    hits = []
    for clause in _using_clauses(sql):
        for col in clause["cols"]:
            if not re.search(rf"\b\w+\.{re.escape(col)}\b", sql, re.IGNORECASE):
                continue
            if clause["outer"]:
                hits.append(
                    f"{col} is merged by an OUTER JOIN ... USING in Trino, where it "
                    f"means COALESCE(left.{col}, right.{col}) — it cannot be "
                    "alias-qualified, and qualifying it to one side changes the "
                    "result"
                )
            else:
                hits.append(
                    f"JOIN ... USING ({col}) merges the column in Trino, so {col} "
                    "cannot be alias-qualified — and this reference could not be "
                    "safely unqualified, because it sits in a nested scope where "
                    "a bare name would rebind, or its owning table is not "
                    "inferable across chained joins; use an explicit ON clause"
                )
    return hits


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

    # ---- Additions from the 2026-09-24 Tier 2b run (#71) -------------------
    # 21 of 49 real translated statements failed on Athena. Everything below was
    # passing through untranslated and is verified against a live workgroup.

    # BQ: DATE_TRUNC(expr, UNIT)  ->  Trino: DATE_TRUNC('unit', expr)
    # Argument order is REVERSED and the unit is quoted. Untranslated, Trino reads
    # the bare unit as a column: "Column 'month' cannot be resolved".
    def _date_trunc_to_trino(a):
        # Skip Trino's own form (quoted unit first) so the fixed-point pass is
        # idempotent, and skip anonymized units we cannot quote meaningfully.
        if a[0].startswith("'") or a[1] == "?":
            return None
        return f"DATE_TRUNC('{a[1].strip().lower()}', {a[0]})"
    for bq_fn in ("DATE_TRUNC", "TIMESTAMP_TRUNC", "DATETIME_TRUNC"):
        sql = _rewrite_call(sql, bq_fn, 2, _date_trunc_to_trino)

    # BQ: SAFE_DIVIDE(a, b) -> NULL on divide-by-zero.
    # Trino idiom: a / NULLIF(b, 0). Same NULL-on-zero semantics.
    sql = _rewrite_call(
        sql, "SAFE_DIVIDE", 2,
        lambda a: f"({a[0]} / NULLIF({a[1]}, 0))",
    )

    # BQ COUNTIF(pred) -> Trino COUNT_IF(pred). Guard against re-writing an
    # already-translated COUNT_IF into COUNT__IF.
    sql = re.sub(r"\bCOUNTIF\s*\(", "COUNT_IF(", sql, flags=re.IGNORECASE)

    # BQ JSON_VALUE(x, '$.p') / JSON_QUERY. Trino's JSON_VALUE is SQL/JSON and
    # demands a 'lax'/'strict' path mode, so a direct pass-through is a syntax
    # error. json_extract_scalar takes a plain JSONPath and works on varchar,
    # which is how the converter stores BQ JSON columns anyway.
    sql = _rewrite_call(sql, "JSON_VALUE", 2,
                        lambda a: f"JSON_EXTRACT_SCALAR({a[0]}, {a[1]})")
    sql = _rewrite_call(sql, "JSON_QUERY", 2,
                        lambda a: f"JSON_EXTRACT({a[0]}, {a[1]})")

    # BQ APPROX_QUANTILES(expr, n) returns n+1 boundary values as an array.
    # Trino has no direct equivalent; approx_percentile with explicit interior
    # percentiles is the closest. Semantics differ (no min/max boundaries), so
    # the difference is called out inline rather than silently papered over.
    def _approx_quantiles(a):
        try:
            n = int(a[1])
        except (TypeError, ValueError):
            return None                      # anonymized '?' or an expression
        if n < 2 or n > 100:
            return None
        pcts = ", ".join("%g" % (i / n) for i in range(1, n))
        return (f"approx_percentile({a[0]}, ARRAY[{pcts}])"
                f" /* BQ APPROX_QUANTILES returned {n + 1} boundaries incl. min/max */")
    sql = _rewrite_call(sql, "APPROX_QUANTILES", 2, _approx_quantiles)

    # BQ SAFE.<fn>(...) means "NULL instead of an error". Trino's equivalent is
    # TRY(<expr>), so the error-suppressing intent survives instead of becoming
    # an unresolved 'safe.<fn>' function name.
    sql = _safe_prefix_to_try(sql)

    # BQ geography constructors -> Trino geometry equivalents. NOTE: BQ GEOGRAPHY
    # is geodesic and Trino geometry is planar, so results differ even when the
    # SQL runs — flagged separately (see #58), not silently equated.
    sql = re.sub(r"\bST_GEOGFROMTEXT\s*\(", "ST_GeometryFromText(", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bST_GEOGPOINT\s*\(", "ST_Point(", sql, flags=re.IGNORECASE)
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
    else:
        sql = _fix_unnest_aliases_athena(sql)
        sql = _fix_output_alias_refs_athena(sql)
        sql = _fix_using_qualifiers_athena(sql)
    return sql
