"""Tests for query attribution and translation modules."""
from bq_assess.bundle.models import QueryRecord
from bq_assess.core.query_attribution import (
    TOP_QUERIES_PER_ENTITY,
    _extract_tables,
    attribute_queries,
)
from bq_assess.core.query_translator import translate_to_athena, translate_to_redshift


class TestExtractTables:
    def test_backtick_qualified(self):
        sql = "SELECT * FROM `my-project.dataset1.table1` WHERE x > ?"
        assert _extract_tables(sql, "my-project") == {"dataset1.table1"}

    def test_multiple_tables(self):
        sql = (
            "SELECT a.*, b.id FROM `p.ds.orders` a "
            "JOIN `p.ds.users` b ON a.uid = b.id"
        )
        assert _extract_tables(sql, "p") == {"ds.orders", "ds.users"}

    def test_unqualified_from(self):
        sql = "SELECT * FROM dataset.table1"
        assert _extract_tables(sql, "p") == {"dataset.table1"}

    def test_insert_into(self):
        sql = "INSERT INTO `proj.output.results` SELECT * FROM `proj.input.raw`"
        assert _extract_tables(sql, "proj") == {"output.results", "input.raw"}

    def test_merge(self):
        sql = "MERGE INTO `p.ds.target` USING `p.ds.source` ON target.id = source.id"
        assert "ds.target" in _extract_tables(sql, "p")

    def test_unbackticked_three_part_ref(self):
        """FROM proj.dataset.table (no backticks) is legal BQ SQL for dash-free
        project IDs — must parse as dataset.table, not proj.dataset."""
        sql = "SELECT * FROM myproj.analytics.events"
        assert _extract_tables(sql, "myproj") == {"analytics.events"}

    def test_cross_project_backticked_dropped(self):
        """Another project's workload must not be attributed to a same-named
        local entity."""
        sql = "SELECT * FROM `other-project.analytics.events`"
        assert _extract_tables(sql, "myproj") == set()

    def test_cross_project_unbackticked_dropped(self):
        sql = "SELECT * FROM otherproj.analytics.events"
        assert _extract_tables(sql, "myproj") == set()

    def test_extract_day_from_not_a_table(self):
        """EXTRACT(part FROM col.field) — that FROM is an expression position."""
        sql = "SELECT EXTRACT(DAY FROM orders.created_at) FROM ds.t"
        assert _extract_tables(sql, "p") == {"ds.t"}

    def test_trim_from_not_a_table(self):
        sql = "SELECT TRIM(LEADING 'x' FROM a.b) FROM ds.t"
        assert _extract_tables(sql, "p") == {"ds.t"}

    def test_dashed_first_part_without_table_not_a_dataset(self):
        """A dashed first part is a project ref (datasets can't contain dashes) —
        'FROM my-proj.ds' with no 3rd part must not yield phantom 'my-proj.ds'."""
        sql = "SELECT * FROM my-proj.ds"
        assert _extract_tables(sql, "other") == set()


class TestAttributeQueries:
    def test_basic_attribution(self):
        queries = [
            QueryRecord(
                query="SELECT * FROM `proj.analytics.events`",
                total_slot_ms=5_000_000,
                statement_type="SELECT",
            ),
        ]
        known = {"analytics.events", "analytics.users"}
        result = attribute_queries(queries, known, "proj")
        assert "analytics.events" in result
        assert "analytics.users" not in result
        assert result["analytics.events"].query_count == 1
        assert result["analytics.events"].slot_hours == 1.4

    def test_multi_table_query_attributed_to_all(self):
        queries = [
            QueryRecord(
                query="SELECT * FROM `p.ds.a` JOIN `p.ds.b` ON a.id = b.id",
                total_slot_ms=3_600_000,
                statement_type="SELECT",
            ),
        ]
        known = {"ds.a", "ds.b", "ds.c"}
        result = attribute_queries(queries, known, "p")
        assert "ds.a" in result
        assert "ds.b" in result
        assert "ds.c" not in result

    def test_top_n_capped(self):
        queries = [
            QueryRecord(
                query=f"SELECT {i} FROM `p.ds.t`",
                total_slot_ms=(100 - i) * 1000,
                statement_type="SELECT",
            )
            for i in range(20)
        ]
        known = {"ds.t"}
        result = attribute_queries(queries, known, "p")
        assert len(result["ds.t"].samples) == TOP_QUERIES_PER_ENTITY
        # num_shapes reflects distinct query texts, not the capped samples list
        assert result["ds.t"].num_shapes == 20

    def test_no_entity_cap_every_entity_keeps_samples(self):
        """2026-08-04: the 200-entity estate cap is gone — EVERY attributed
        entity embeds its top-N samples (per-sample SQL clipping bounds the
        report instead)."""
        n = 500
        queries = [
            QueryRecord(
                query=f"SELECT * FROM `p.ds.t{i}`",
                total_slot_ms=(n - i) * 1000,
                statement_type="SELECT",
            )
            for i in range(n)
        ]
        known = {f"ds.t{i}" for i in range(n)}
        result = attribute_queries(queries, known, "p")
        assert len(result) == n
        assert all(wl.samples for wl in result.values()), (
            "every attributed entity must keep its embedded samples"
        )

    def test_deduplicates_by_text(self):
        queries = [
            QueryRecord(query="SELECT * FROM `p.ds.t`", total_slot_ms=1000, statement_type="SELECT"),
            QueryRecord(query="SELECT * FROM `p.ds.t`", total_slot_ms=2000, statement_type="SELECT"),
        ]
        known = {"ds.t"}
        result = attribute_queries(queries, known, "p")
        assert result["ds.t"].query_count == 2
        assert len(result["ds.t"].samples) == 1

    def test_null_slot_ms_does_not_crash(self):
        """External bundles can carry explicit null total_slot_ms — attribution
        must aggregate, not TypeError."""
        queries = [
            QueryRecord(query="SELECT * FROM `p.ds.t`", total_slot_ms=None,
                        statement_type="SELECT"),
            QueryRecord(query="SELECT 1 FROM `p.ds.t`", total_slot_ms=1000,
                        statement_type="SELECT"),
        ]
        result = attribute_queries(queries, {"ds.t"}, "p")
        assert result["ds.t"].total_slot_ms == 1000
        assert result["ds.t"].query_count == 2

    def test_unknown_tables_ignored(self):
        queries = [
            QueryRecord(query="SELECT * FROM `p.unknown.table`", total_slot_ms=1000),
        ]
        known = {"analytics.events"}
        result = attribute_queries(queries, known, "p")
        assert len(result) == 0


class TestTranslateToRedshift:
    def test_ifnull(self):
        assert "NVL(" in translate_to_redshift("SELECT IFNULL(a, b)")

    def test_backtick_removal(self):
        result = translate_to_redshift("SELECT * FROM `proj.ds.tbl`")
        assert "`" not in result
        assert "ds.tbl" in result

    def test_timestamp_diff(self):
        # BQ order is (end, start, unit); Redshift DATEDIFF is (unit, start, end)
        result = translate_to_redshift("SELECT TIMESTAMP_DIFF(end_ts, start_ts, SECOND)")
        assert "DATEDIFF(SECOND, start_ts, end_ts)" in result

    def test_date_diff(self):
        result = translate_to_redshift("SELECT DATE_DIFF(end_d, start_d, DAY)")
        assert "DATEDIFF(DAY, start_d, end_d)" in result

    def test_timestamp_diff_nested_args(self):
        result = translate_to_redshift(
            "SELECT TIMESTAMP_DIFF(COALESCE(a, b), CURRENT_TIMESTAMP(), HOUR)"
        )
        # CURRENT_TIMESTAMP loses its parens too (Redshift paren-less form)
        assert "DATEDIFF(HOUR, CURRENT_TIMESTAMP, COALESCE(a, b))" in result

    def test_safe_cast(self):
        result = translate_to_redshift("SELECT SAFE_CAST(x AS INT64)")
        assert "CAST(x AS BIGINT)" in result

    def test_date_sub(self):
        result = translate_to_redshift("SELECT DATE_SUB(d, INTERVAL 7 DAY)")
        assert "DATEADD(DAY, -7, d)" in result

    def test_date_sub_nested_first_arg(self):
        """A comma inside the first arg must not defeat the rewrite."""
        result = translate_to_redshift(
            "SELECT DATE_SUB(DATE_TRUNC(d, MONTH), INTERVAL 1 MONTH)"
        )
        assert "DATEADD(MONTH, -1, DATE_TRUNC(d, MONTH))" in result

    def test_struct_not_rewritten_to_row(self):
        """Redshift has no ROW( constructor — STRUCT must stay visible (the AST
        translator flags it for manual SUPER/PartiQL rewrite)."""
        result = translate_to_redshift("SELECT STRUCT(a, b)")
        assert "STRUCT(a, b)" in result
        assert "ROW(" not in result

    def test_except_set_operator_untouched(self):
        """EXCEPT as a set operator is valid Redshift — only the BQ SELECT *
        EXCEPT(cols) modifier gets commented out."""
        sql = "SELECT id FROM a EXCEPT (SELECT id FROM b)"
        assert translate_to_redshift(sql) == sql

    def test_select_star_except_nested_parens(self):
        result = translate_to_redshift("SELECT * EXCEPT (CAST(x AS BIGINT)) FROM t")
        assert "/* EXCEPT(CAST(x AS BIGINT)) — remove manually */" in result
        # balanced: the trailing FROM survives outside the comment
        assert result.endswith("FROM t")

    def test_if_to_case(self):
        result = translate_to_redshift("SELECT IF(x > 1, a, b)")
        assert "CASE WHEN x > 1 THEN a ELSE b END" in result

    def test_if_with_nested_commas(self):
        result = translate_to_redshift("SELECT IF(x > 0, CONCAT(a, b), other)")
        assert "CASE WHEN x > 0 THEN CONCAT(a, b) ELSE other END" in result

    def test_nested_if(self):
        result = translate_to_redshift("SELECT IF(a, IF(b, 1, 2), 3)")
        assert "CASE WHEN a THEN CASE WHEN b THEN 1 ELSE 2 END ELSE 3 END" in result

    def test_if_wrong_arity_untouched(self):
        # Not the 3-arg conditional — leave it alone rather than corrupt it
        result = translate_to_redshift("SELECT MAGIC_IF(a, b)")
        assert "MAGIC_IF(a, b)" in result

    def test_string_to_varchar(self):
        result = translate_to_redshift("CAST(x AS STRING)")
        assert "VARCHAR" in result

    def test_empty_input(self):
        assert translate_to_redshift("") == ""
        assert translate_to_redshift(None) is None


class TestTranslateToAthena:
    def test_ifnull(self):
        assert "COALESCE(" in translate_to_athena("SELECT IFNULL(a, b)")

    def test_safe_cast(self):
        assert "TRY_CAST(" in translate_to_athena("SELECT SAFE_CAST(x AS INT64)")

    def test_timestamp_diff(self):
        # BQ order is (end, start, unit); Trino DATE_DIFF is ('unit', start, end)
        result = translate_to_athena("SELECT TIMESTAMP_DIFF(end_ts, start_ts, SECOND)")
        assert "DATE_DIFF('second', start_ts, end_ts)" in result

    def test_date_diff(self):
        result = translate_to_athena("SELECT DATE_DIFF(end_d, start_d, DAY)")
        assert "DATE_DIFF('day', start_d, end_d)" in result

    def test_if_preserved(self):
        # Trino supports IF(cond, then, else) natively
        result = translate_to_athena("SELECT IF(x > 0, CONCAT(a, b), other)")
        assert "IF(x > 0, CONCAT(a, b), other)" in result

    def test_backtick_removal(self):
        result = translate_to_athena("SELECT * FROM `proj.ds.tbl`")
        assert "`" not in result
        assert "ds.tbl" in result

    def test_current_datetime(self):
        result = translate_to_athena("SELECT CURRENT_DATETIME()")
        assert "CURRENT_TIMESTAMP" in result

    def test_generate_uuid(self):
        result = translate_to_athena("SELECT GENERATE_UUID()")
        assert "UUID()" in result

    def test_date_sub_nested_first_arg(self):
        # The nested DATE_TRUNC is also translated now (#71): BQ takes
        # (expr, UNIT), Trino takes ('unit', expr). This assertion previously
        # expected the inner call to stay in BigQuery form, which pinned the bug
        # that made Trino read the bare unit as a column name.
        result = translate_to_athena(
            "SELECT DATE_SUB(DATE_TRUNC(d, MONTH), INTERVAL 1 MONTH)"
        )
        assert "DATE_ADD('month', -1, DATE_TRUNC('month', d))" in result

    def test_date_trunc_arg_order_swapped(self):
        """BQ DATE_TRUNC(expr, UNIT) -> Trino DATE_TRUNC('unit', expr) (#71).

        Untranslated, Trino parses the bare unit as a column and fails with
        "Column 'month' cannot be resolved" — 4 of 49 live statements.
        """
        assert "DATE_TRUNC('month', order_date)" in translate_to_athena(
            "SELECT DATE_TRUNC(order_date, MONTH) AS month FROM t"
        )

    def test_date_trunc_already_trino_is_untouched(self):
        """Idempotent: the fixed-point pass must not re-scramble its own output."""
        sql = "SELECT DATE_TRUNC('month', order_date) FROM t"
        assert translate_to_athena(sql) == sql

    def test_date_trunc_anonymized_unit_left_alone(self):
        """An anonymized unit ('?') cannot be quoted meaningfully — skip it."""
        assert "DATE_TRUNC(d, ?)" in translate_to_athena("SELECT DATE_TRUNC(d, ?) FROM t")

    def test_safe_divide_to_nullif(self):
        """BQ SAFE_DIVIDE -> Trino a / NULLIF(b, 0), same NULL-on-zero result."""
        assert "(a / NULLIF(b, 0))" in translate_to_athena("SELECT SAFE_DIVIDE(a, b) FROM t")

    def test_countif_to_count_if(self):
        assert "COUNT_IF(x IS NOT NULL)" in translate_to_athena(
            "SELECT COUNTIF(x IS NOT NULL) FROM t"
        )

    def test_count_if_not_double_rewritten(self):
        """Guard against COUNT_IF -> COUNT__IF on the already-translated form."""
        assert "COUNT__IF" not in translate_to_athena("SELECT COUNT_IF(x) FROM t")

    def test_json_value_to_extract_scalar(self):
        """Trino JSON_VALUE demands a lax/strict path mode; json_extract_scalar does not."""
        out = translate_to_athena("SELECT JSON_VALUE(metrics, '$.revenue') FROM t")
        assert "JSON_EXTRACT_SCALAR(metrics, '$.revenue')" in out

    def test_approx_quantiles_to_percentile(self):
        out = translate_to_athena("SELECT APPROX_QUANTILES(price, 4) FROM t")
        assert "approx_percentile(price, ARRAY[0.25, 0.5, 0.75])" in out

    def test_safe_prefix_becomes_try(self):
        """BQ SAFE.fn(x) means NULL-on-error; Trino's equivalent is TRY(fn(x))."""
        out = translate_to_athena("SELECT SAFE.ST_GEOGFROMTEXT(loc) FROM t")
        assert "TRY(ST_GeometryFromText(loc))" in out

    def test_untranslatable_construct_is_flagged(self):
        """Unhandled BQ-only constructs must be named, not passed off as translated (#71)."""
        out = translate_to_athena("SELECT * EXCEPT (a) FROM t")
        assert "NOT FULLY TRANSLATED" in out
        assert "EXCEPT" in out

    def test_clean_query_gets_no_warning_banner(self):
        """No false positives: a fully translatable query stays banner-free."""
        out = translate_to_athena("SELECT COUNTIF(x) FROM t")
        assert "NOT FULLY TRANSLATED" not in out


class TestUnnestAliasAthena:
    """BQ's ``UNNEST(expr) alias`` names the element; Trino's names the relation.

    Measured on a live Athena workgroup: the BQ form fails COLUMN_NOT_FOUND,
    ``AS rel(col)`` succeeds, and a 2-entry alias list over a 2-field row is
    rejected — i.e. UNNEST of array(row) yields ONE row-typed column, so no
    schema knowledge is needed to rewrite this. 5 of 49 live statements.
    """

    def test_comma_join_struct_array(self):
        out = translate_to_athena(
            "SELECT p.category FROM `p.d.user_profiles` up, UNNEST(up.preferences) p"
        )
        assert "UNNEST(up.preferences) AS p_u(p)" in out

    def test_explicit_as_is_replaced_not_duplicated(self):
        out = translate_to_athena("SELECT i.c FROM t c, UNNEST(c.items) AS i")
        assert "UNNEST(c.items) AS i_u(i)" in out
        assert "AS i AS" not in out

    def test_nested_field_path_argument(self):
        """The UNNEST argument is itself a struct path — paren scan must hold."""
        out = translate_to_athena(
            "SELECT i.category FROM t c, UNNEST(c.ecommerce.items) i"
        )
        assert "UNNEST(c.ecommerce.items) AS i_u(i)" in out

    def test_scalar_array_alias_also_rewritten(self):
        """Same defect for scalar arrays: bare ``tag`` cannot resolve either."""
        out = translate_to_athena("SELECT tag FROM t, UNNEST(t.tags) tag")
        assert "UNNEST(t.tags) AS tag_u(tag)" in out

    def test_cross_join_form_rewritten(self):
        out = translate_to_athena("SELECT i.c FROM t CROSS JOIN UNNEST(t.items) i")
        assert "CROSS JOIN UNNEST(t.items) AS i_u(i)" in out

    def test_already_trino_form_is_untouched(self):
        """Idempotent — must not wrap its own output on a second pass."""
        sql = "SELECT p.category FROM t up, UNNEST(up.preferences) AS p_u(p)"
        assert translate_to_athena(sql) == sql

    def test_unaliased_unnest_left_alone(self):
        out = translate_to_athena("SELECT * FROM t, UNNEST(t.items)")
        assert "UNNEST(t.items)" in out
        assert "_u(" not in out

    def test_trailing_keyword_not_eaten_as_alias(self):
        """``WHERE``/``GROUP`` after UNNEST is a clause, not an alias."""
        out = translate_to_athena("SELECT * FROM t, UNNEST(t.items) WHERE x > 1")
        assert "UNNEST(t.items) WHERE" in out
        assert "WHERE_u" not in out

    def test_multiple_unnests_all_rewritten(self):
        out = translate_to_athena(
            "SELECT a.x, b.y FROM t, UNNEST(t.first) a, UNNEST(t.second) b"
        )
        assert "UNNEST(t.first) AS a_u(a)" in out
        assert "UNNEST(t.second) AS b_u(b)" in out

    def test_with_offset_flagged_not_guessed(self):
        """WITH OFFSET changes the alias arity — disclose instead of rewriting."""
        out = translate_to_athena("SELECT x, i FROM t, UNNEST(t.arr) x WITH OFFSET i")
        assert "NOT FULLY TRANSLATED" in out
        assert "WITH ORDINALITY" in out
        assert "x_u" not in out

    def test_redshift_path_unaffected(self):
        """This is a Trino-shaped fix; Redshift still gets the review warning."""
        out = translate_to_redshift("SELECT p.c FROM t up, UNNEST(up.preferences) p")
        assert "_u(" not in out
        assert "NOT FULLY TRANSLATED" in out


class TestGroupByAliasAthena:
    """BigQuery resolves SELECT output aliases in GROUP BY; Trino does not.

    Measured on a live Athena workgroup: ``SELECT status AS s ... GROUP BY s``
    fails COLUMN_NOT_FOUND while ``GROUP BY status`` succeeds. ORDER BY on an
    alias is fine on both, since ordering happens after projection. 6 of 49
    live statements hit this.
    """

    def test_alias_replaced_by_expression(self):
        out = translate_to_athena("SELECT status AS s, COUNT(*) AS n FROM t GROUP BY s")
        assert "GROUP BY status" in out

    def test_function_expression_substituted(self):
        out = translate_to_athena(
            "SELECT DATE(event_timestamp) AS d, COUNT(*) AS n FROM t GROUP BY d"
        )
        assert "GROUP BY DATE(event_timestamp)" in out

    def test_struct_path_substituted(self):
        out = translate_to_athena(
            "SELECT f.user.tier AS tier, COUNT(*) AS n FROM t f GROUP BY tier"
        )
        assert "GROUP BY f.user.tier" in out

    def test_mixed_alias_and_plain_column(self):
        out = translate_to_athena(
            "SELECT a.b, c.d AS dd, COUNT(*) AS n FROM t GROUP BY a.b, dd"
        )
        assert "GROUP BY a.b, c.d" in out

    def test_order_by_alias_left_alone(self):
        """ORDER BY resolves aliases on Trino — rewriting it would add noise."""
        out = translate_to_athena(
            "SELECT status AS s, COUNT(*) AS n FROM t GROUP BY s ORDER BY n DESC"
        )
        assert "ORDER BY n DESC" in out

    def test_cte_scoped_to_its_own_select(self):
        """The GROUP BY inside a CTE must resolve against the CTE's select list."""
        out = translate_to_athena(
            "WITH m AS (SELECT DATE_TRUNC(order_date, MONTH) AS month, SUM(x) AS r "
            "FROM t GROUP BY month) SELECT month, r FROM m ORDER BY month"
        )
        assert "GROUP BY DATE_TRUNC('month', order_date)" in out

    def test_plain_column_group_by_untouched(self):
        sql = "SELECT status, COUNT(*) AS n FROM t GROUP BY status"
        assert translate_to_athena(sql) == sql

    def test_aggregate_alias_not_substituted(self):
        """Grouping by an aggregate is invalid on both engines — don't 'fix' it."""
        out = translate_to_athena("SELECT COUNT(*) AS n, status FROM t GROUP BY n")
        assert "GROUP BY n" in out

    def test_idempotent(self):
        sql = "SELECT status AS s, COUNT(*) AS n FROM t GROUP BY status"
        assert translate_to_athena(sql) == sql

    def test_having_alias_gets_the_aggregate(self):
        """HAVING runs pre-projection on Trino too — and there an aggregate is
        exactly what belongs, unlike in GROUP BY."""
        out = translate_to_athena(
            "SELECT s, COUNT(*) AS sessions FROM t GROUP BY s HAVING sessions > ?"
        )
        assert "HAVING COUNT(*) > ?" in out

    def test_having_compound_expression_parenthesized(self):
        out = translate_to_athena(
            "SELECT s, SUM(a) / SUM(b) AS ratio FROM t GROUP BY s HAVING ratio > ?"
        )
        assert "HAVING (SUM(a) / SUM(b)) > ?" in out

    def test_having_stops_before_order_by(self):
        out = translate_to_athena(
            "SELECT s, COUNT(*) AS n FROM t GROUP BY s HAVING n > ? ORDER BY n DESC"
        )
        assert "HAVING COUNT(*) > ?" in out
        assert "ORDER BY n DESC" in out

    def test_having_qualified_name_untouched(self):
        out = translate_to_athena("SELECT COUNT(*) AS n FROM t GROUP BY x HAVING t.n > 1")
        assert "HAVING t.n > 1" in out

    def test_having_function_name_untouched(self):
        """An alias that collides with a function name must not eat the call."""
        out = translate_to_athena(
            "SELECT COUNT(*) AS count FROM t GROUP BY x HAVING count(*) > 1"
        )
        assert "HAVING count(*) > 1" in out

    def test_having_string_literal_untouched(self):
        out = translate_to_athena(
            "SELECT s, COUNT(*) AS n FROM t GROUP BY s HAVING n > 1 AND s = 'n'"
        )
        assert "s = 'n'" in out
        assert "HAVING COUNT(*) > 1" in out


class TestUsingJoinDisclosureAthena:
    """Trino merges a USING column; BigQuery keeps both sides qualifiable.

    Verified live: the same query fails with USING + ``o.user_id`` and succeeds
    with an explicit ON. Rewriting needs the left-hand alias, which is ambiguous
    once joins chain, so this is disclosed rather than guessed.
    """

    def test_inner_join_qualifier_is_stripped(self):
        """Measured with literal rows: for an inner join the merged column equals
        both sides, so dropping the qualifier is an exact rewrite."""
        out = translate_to_athena(
            "SELECT COUNT(DISTINCT o.user_id) FROM orders o JOIN users u USING (user_id)"
        )
        assert "COUNT(DISTINCT user_id)" in out
        assert "NOT FULLY TRANSLATED" not in out

    def test_outer_join_qualifier_is_disclosed_not_stripped(self):
        """RIGHT JOIN ... USING(id) returned [2, 3] where the ON form qualified to
        the left table returned [2, NULL] — the merged column is COALESCE-like, so
        stripping would silently change the answer."""
        out = translate_to_athena(
            "SELECT COUNT(DISTINCT o.user_id) FROM orders o "
            "LEFT JOIN users u USING (user_id)"
        )
        assert "o.user_id" in out
        assert "NOT FULLY TRANSLATED" in out
        assert "OUTER JOIN" in out

    def test_non_using_qualifiers_preserved(self):
        """Only USING columns merge; ``a.av`` stays resolvable alongside USING(id)."""
        out = translate_to_athena(
            "SELECT o.order_date, u.country FROM orders o JOIN users u USING (user_id)"
        )
        assert "o.order_date" in out
        assert "u.country" in out

    def test_correlated_subquery_reference_not_rebound(self):
        """The regression that matters: an unscoped strip turned
        ``x.user_id = o.user_id`` into ``x.user_id = user_id``, which rebinds to
        the inner relation and quietly returns the wrong rows. Must disclose
        instead of rewriting."""
        out = translate_to_athena(
            "SELECT o.order_date FROM orders o JOIN users u USING (user_id) "
            "WHERE EXISTS (SELECT ? FROM carts x WHERE x.user_id = o.user_id)"
        )
        assert "x.user_id = o.user_id" in out
        assert "NOT FULLY TRANSLATED" in out

    def test_aggregate_parens_do_not_count_as_a_scope(self):
        """COUNT(...) parens must not look like a subquery, or the real-world
        ``COUNT(DISTINCT o.user_id)`` case would be skipped."""
        out = translate_to_athena(
            "SELECT o.d, COUNT(DISTINCT o.user_id) AS n FROM orders o "
            "JOIN users u USING (user_id) GROUP BY o.d"
        )
        assert "COUNT(DISTINCT user_id)" in out

    def test_multi_column_using(self):
        out = translate_to_athena("SELECT a.k1, a.k2 FROM a JOIN b USING (k1, k2)")
        assert "SELECT k1, k2" in out

    def test_using_without_qualified_reference_not_flagged(self):
        """No false positive: this shape runs fine on Trino."""
        out = translate_to_athena(
            "SELECT o.order_date, p.category FROM orders o "
            "JOIN order_items oi USING (order_id) JOIN products p USING (product_id) "
            "GROUP BY o.order_date, p.category"
        )
        assert "NOT FULLY TRANSLATED" not in out
        assert "o.order_date" in out

    def test_anonymized_approx_quantiles_is_flagged(self):
        """A '?' bucket count leaves no literal to expand — disclose it."""
        out = translate_to_athena("SELECT APPROX_QUANTILES(price, ?) AS q FROM t")
        assert "NOT FULLY TRANSLATED" in out
        assert "APPROX_QUANTILES" in out

    def test_struct_to_row_valid_trino(self):
        result = translate_to_athena("SELECT STRUCT(a, b)")
        assert "ROW(a, b)" in result

    def test_float64(self):
        result = translate_to_athena("CAST(x AS FLOAT64)")
        assert "DOUBLE" in result


class TestTypeMapParity:
    """The translator's type substitutions must stay derived from the pinned
    sources (2026-08-04 consolidation) — drift here means the translated-query
    pane disagrees with the generated DDL in the same report."""

    def test_redshift_types_match_storage_placement_map(self):
        from bq_assess.core.query_translator import _redshift_query_type_map
        from bq_assess.engine.redshift.storage_placement import _BQ_TO_REDSHIFT

        for bq_type, target in _redshift_query_type_map().items():
            assert target == _BQ_TO_REDSHIFT[bq_type], (
                f"{bq_type}: query translation '{target}' != DDL map "
                f"'{_BQ_TO_REDSHIFT[bq_type]}'"
            )

    def test_redshift_string_is_sized_varchar(self):
        """Bare VARCHAR defaults to VARCHAR(256) on Redshift — silent truncation.
        The DDL map's VARCHAR(65535) must be what queries cast to as well."""
        result = translate_to_redshift("SELECT CAST(x AS STRING)")
        assert "VARCHAR(65535)" in result

    def test_athena_bytes_casts_to_varchar_not_varbinary(self):
        """BYTES lands as a base64 string per LOSSY_TYPE_MAP — casting to
        VARBINARY would target a type the migrated column doesn't have."""
        result = translate_to_athena("SELECT CAST(x AS BYTES)")
        assert "VARCHAR" in result
        assert "VARBINARY" not in result

    def test_athena_types_track_converter_iceberg_types(self):
        from bq_assess.core.query_translator import (
            _ICEBERG_TO_TRINO_QUERY,
            _athena_query_type_map,
        )
        from bq_assess.targets.iceberg.converter import CLEAN_TYPE_MAP, LOSSY_TYPE_MAP

        iceberg = {**CLEAN_TYPE_MAP, **{k: v[0] for k, v in LOSSY_TYPE_MAP.items()}}
        for bq_type, target in _athena_query_type_map().items():
            if bq_type in iceberg:
                expected = _ICEBERG_TO_TRINO_QUERY.get(
                    iceberg[bq_type], iceberg[bq_type].upper()
                )
                assert target == expected, (
                    f"{bq_type}: query cast '{target}' but column lands as "
                    f"Iceberg '{iceberg[bq_type]}'"
                )

    def test_type_names_valid_on_both_engines_untouched(self):
        """DATE/TIMESTAMP/INT are valid on both engines — substituting them
        risks touching literals; they must pass through unchanged."""
        sql = "SELECT CAST(a AS DATE), CAST(b AS TIMESTAMP) FROM t"
        assert translate_to_redshift(sql) == sql
        assert translate_to_athena(sql) == sql


class TestTranslatorAuditFixes:
    """2026-08-04 deliverable audit regression tests."""

    def test_anonymized_interval_placeholder_translates(self):
        """'INTERVAL ? DAY' (anonymizer output) matched 0 of 7,730 real calls —
        the flagship date rewrite must work on anonymized SQL."""
        r = translate_to_redshift("SELECT DATE_SUB(d, INTERVAL ? DAY)")
        assert "DATEADD(DAY, -?, d)" in r
        a = translate_to_athena("SELECT TIMESTAMP_ADD(ts, INTERVAL ? HOUR)")
        assert "DATE_ADD('hour', ?, ts)" in a

    def test_format_timestamp_args_swapped(self):
        """BQ (format, expr) → Redshift TO_CHAR(expr, format): the blind name
        sub shipped inverted TO_CHAR('fmt', ts) 20 times."""
        r = translate_to_redshift("SELECT FORMAT_TIMESTAMP('?', start_date)")
        assert "TO_CHAR(start_date, '?'" in r
        a = translate_to_athena("SELECT FORMAT_DATE('%Y', d)")
        assert "DATE_FORMAT(d, '%Y'" in a

    def test_parse_date_args_swapped(self):
        r = translate_to_redshift("SELECT PARSE_DATE('%Y%m%d', s)")
        assert "TO_DATE(s, '%Y%m%d'" in r

    def test_current_timestamp_parens_stripped(self):
        """Redshift only accepts the paren-less form."""
        r = translate_to_redshift("SELECT CURRENT_TIMESTAMP(), CURRENT_DATE()")
        assert "CURRENT_TIMESTAMP()" not in r
        assert "CURRENT_DATE()" not in r
        assert "CURRENT_TIMESTAMP" in r

    def test_hyphenated_table_part_unbackticked_and_quoted(self):
        """845 real refs stayed backticked because only the project part
        allowed hyphens; hyphenated parts must emit double-quoted."""
        r = translate_to_redshift(
            "SELECT * FROM `p.ds.t_20251101-20260504`"
        )
        assert "`" not in r
        assert 'ds."t_20251101-20260504"' in r

    def test_safe_cast_annotated(self):
        r = translate_to_redshift("SELECT SAFE_CAST(x AS INT64)")
        assert "CAST(x AS BIGINT)" in r
        assert "SAFE_CAST → CAST" in r


class TestAuditRemainderFixes:
    """2026-08-04 audit remainder: display-layer translation correctness."""

    def test_double_quoted_literal_becomes_single(self):
        """BQ "..." is a string; on the targets it's an identifier —
        DATE("2021-01-01") parsed as a column ref (silent misbehavior)."""
        r = translate_to_redshift('SELECT DATE("2021-01-01"), SPLIT(x, "___") FROM ds.t')
        assert "DATE('2021-01-01')" in r
        assert "'___'" in r

    def test_quoted_identifier_from_hyphenated_ref_survives(self):
        r = translate_to_redshift('SELECT * FROM `p.ds.t-2026` WHERE x = "abc"')
        assert 'ds."t-2026"' in r
        assert "= 'abc'" in r

    def test_corrupted_shape_not_translated(self):
        """Anonymizer-corrupted SQL (unbalanced) must be returned untranslated
        with a banner, never as a plausible-looking translation."""
        garbage = "SELECT STRUCT<STRING>('?''?'___'?'model_1("
        r = translate_to_redshift(garbage)
        assert r.startswith("-- [NOT TRANSLATED")
        assert garbage in r

    def test_balanced_sql_with_escaped_quotes_translates(self):
        r = translate_to_redshift("SELECT 'it''s fine', IFNULL(a, b) FROM ds.t")
        assert "NVL(" in r
        assert "NOT TRANSLATED" not in r

    def test_merge_gets_review_note(self):
        r = translate_to_redshift(
            "MERGE INTO `p.ds.t` t USING s ON t.id = s.id "
            "WHEN MATCHED THEN UPDATE SET t.x = s.x"
        )
        assert r.startswith("-- NOTE: MERGE")
        assert "alias-qualified SET" in r

    def test_non_merge_has_no_merge_note(self):
        r = translate_to_redshift("SELECT * FROM ds.t")
        assert "MERGE" not in r
