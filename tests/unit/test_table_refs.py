"""Tests for the shared table-reference grammar (core/table_refs).

This is THE grammar — analyzer counting, scanner dependency extraction, and
query attribution all parse through it. Edge cases fixed here are fixed for
all three consumers.
"""
from __future__ import annotations

from bq_assess.core.table_refs import (
    TableRef,
    extract_dataset_tables,
    extract_table_refs,
)


class TestBacktickRefs:
    def test_three_part(self):
        refs = extract_table_refs("SELECT * FROM `my-proj.ds.t`")
        assert TableRef(project="my-proj", dataset="ds", table="t") in refs

    def test_two_part(self):
        refs = extract_table_refs("SELECT * FROM `ds.t`")
        assert TableRef(project=None, dataset="ds", table="t") in refs

    def test_cross_project_dropped_with_filter(self):
        assert extract_dataset_tables(
            "SELECT * FROM `other-proj.ds.t`", "my-proj"
        ) == set()

    def test_own_project_kept_with_filter(self):
        assert extract_dataset_tables(
            "SELECT * FROM `my-proj.ds.t`", "my-proj"
        ) == {"ds.t"}

    def test_no_filter_keeps_all_projects(self):
        assert extract_dataset_tables(
            "SELECT * FROM `other-proj.ds.t`", None
        ) == {"ds.t"}


class TestUnbacktickedRefs:
    def test_three_part(self):
        assert extract_dataset_tables(
            "SELECT * FROM myproj.analytics.events", "myproj"
        ) == {"analytics.events"}

    def test_three_part_cross_project_dropped(self):
        assert extract_dataset_tables(
            "SELECT * FROM otherproj.analytics.events", "myproj"
        ) == set()

    def test_two_part(self):
        assert extract_dataset_tables("SELECT * FROM ds.t", "p") == {"ds.t"}

    def test_dashed_two_part_is_project_not_dataset(self):
        # datasets cannot contain dashes — 'my-proj.ds' is a truncated project ref
        assert extract_dataset_tables("SELECT * FROM my-proj.ds", "other") == set()

    def test_bare_name_has_no_dataset(self):
        refs = extract_table_refs("SELECT * FROM orders")
        assert refs == [TableRef(project=None, dataset=None, table="orders")]
        assert extract_dataset_tables("SELECT * FROM orders") == set()


class TestNonTablePositions:
    def test_extract_from_expression(self):
        assert extract_dataset_tables(
            "SELECT EXTRACT(DAY FROM orders.created_at) FROM ds.t", "p"
        ) == {"ds.t"}

    def test_trim_from_expression(self):
        assert extract_dataset_tables(
            "SELECT TRIM(LEADING 'x' FROM a.b) FROM ds.t", "p"
        ) == {"ds.t"}

    def test_from_unnest_not_a_table(self):
        refs = extract_table_refs("SELECT * FROM UNNEST(arr) AS x")
        assert all(r.table.lower() != "unnest" for r in refs)

    def test_from_subselect_not_a_table(self):
        refs = extract_table_refs("SELECT * FROM (SELECT 1) x")
        assert all(r.table.lower() != "select" for r in refs)


class TestStatementKinds:
    def test_insert_into(self):
        assert extract_dataset_tables(
            "INSERT INTO `p.out.results` SELECT * FROM `p.in.raw`", "p"
        ) == {"out.results", "in.raw"}

    def test_merge_into(self):
        got = extract_dataset_tables(
            "MERGE INTO `p.ds.target` USING `p.ds.source` ON t.id = s.id", "p"
        )
        assert "ds.target" in got

    def test_update(self):
        assert "ds.t" in extract_dataset_tables("UPDATE ds.t SET x = 1", "p")

    def test_join_chain(self):
        assert extract_dataset_tables(
            "SELECT * FROM ds.a JOIN ds.b ON a.i = b.i LEFT JOIN ds.c ON b.j = c.j",
            "p",
        ) == {"ds.a", "ds.b", "ds.c"}

    def test_duplicates_preserved_in_ref_list(self):
        refs = extract_table_refs("SELECT * FROM ds.t JOIN ds.t x ON 1=1")
        assert len([r for r in refs if r.dataset_table == "ds.t"]) == 2

    def test_empty_and_none(self):
        assert extract_table_refs("") == []
        assert extract_table_refs(None) == []
