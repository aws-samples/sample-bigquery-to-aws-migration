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
        result = translate_to_athena(
            "SELECT DATE_SUB(DATE_TRUNC(d, MONTH), INTERVAL 1 MONTH)"
        )
        assert "DATE_ADD('month', -1, DATE_TRUNC(d, MONTH))" in result

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
