"""Detail/sample embed budgets — the petabyte-scale rule.

Embedding DDL/DML/SQL for every entity makes the single-file report unloadable at
100k-entity scale (measured 471 MB). These tests pin the budget mechanics: spend
in attention order, mark what was dropped, never drop silently.
"""
from __future__ import annotations

from bq_assess.report._serialize import (
    build_query_sample_chunks,
    build_report_rows,
)


def _table(name: str, size_gb: float = 1.0, flags: list | None = None) -> dict:
    return {
        "full_name": name,
        "entity_type": "TABLE",
        "population": "TABLE",
        "logical_size_gb": size_gb,
        "effort": {"category": "AUTO", "score": 1, "flags": flags or []},
        "complexity": {"category": "PORTABLE", "score": 0, "flags": []},
        "conversion": {"ddl": "CREATE TABLE x (id BIGINT)" + "-- pad" * 50},
        "migration_plan": {"phases": ["p1"], "notes": "n" * 400},
    }


def _view(name: str) -> dict:
    return {
        "full_name": name,
        "entity_type": "VIEW",
        "population": "REBUILT",
        "logical_size_gb": 0.0,
        "effort": {"category": "AUTO", "score": 1, "flags": []},
        "complexity": {"category": "ADAPT", "score": 3, "flags": ["qualify"]},
        "translated_sql": {"redshift_sql": "SELECT 1 -- " + "pad " * 100},
    }


class TestDetailByteBudget:
    def test_generous_budget_embeds_everything(self):
        entities = [_table("p.d.t1"), _view("p.d.v1")]
        rows, chunks, omitted = build_report_rows(entities, entities)
        assert omitted == 0
        embedded = {name for c in chunks for name in c}
        assert embedded == {"p.d.t1", "p.d.v1"}
        assert not any(r.get("detail_omitted") for r in rows["query"])

    def test_tight_budget_keeps_rebuilt_first_and_marks_omitted(self):
        """REBUILT SQL is the review surface — it must win the budget over plain
        tables, and dropped rows must carry detail_omitted (never silent)."""
        view = _view("p.d.v1")
        tables = [_table(f"p.d.t{i}") for i in range(5)]
        entities = tables + [view]  # view LAST in row order — priority must not care
        budget = len(__import__("json").dumps(
            {k: v for k, v in view.items() if k == "translated_sql"}
        )) + 200
        rows, chunks, omitted = build_report_rows(entities, entities,
                                                  detail_byte_budget=budget)
        embedded = {name for c in chunks for name in c}
        assert "p.d.v1" in embedded
        assert omitted == 5
        omitted_rows = [r for r in rows["query"] if r.get("detail_omitted")]
        assert len(omitted_rows) == 5
        assert all(r.get("detail_chunk") is None for r in omitted_rows)

    def test_flagged_tables_beat_plain_tables(self):
        flagged = _table("p.d.flagged", size_gb=0.001, flags=["json_column"])
        big_plain = _table("p.d.plain", size_gb=99999.0)
        one_entry = len(__import__("json").dumps(
            {"conversion": flagged["conversion"],
             "migration_plan": flagged["migration_plan"]}
        ))
        _rows, chunks, omitted = build_report_rows(
            [big_plain, flagged], [big_plain, flagged],
            detail_byte_budget=one_entry + 50,
        )
        embedded = {name for c in chunks for name in c}
        assert embedded == {"p.d.flagged"}
        assert omitted == 1

    def test_zero_score_rows_unaffected(self):
        """Row projection semantics unchanged: entities without scores drop."""
        e = _table("p.d.t1")
        e_noscore = {**_table("p.d.t2"), "effort": None, "complexity": None}
        rows, _, _ = build_report_rows([e, e_noscore], [e, e_noscore])
        assert [r["full_name"] for r in rows["effort"]] == ["p.d.t1"]


class TestQuerySampleByteBudget:
    @staticmethod
    def _wl(slot_ms: int, sql: str = "SELECT 1") -> dict:
        return {"samples": [{
            "query": sql, "translated": sql,
            "statement_type": "SELECT", "total_slot_ms": slot_ms,
        }]}

    def test_heaviest_workloads_keep_samples(self):
        big_sql = "SELECT " + "x, " * 200
        workloads = {
            "p.d.light": self._wl(10, big_sql),
            "p.d.heavy": self._wl(9_999_999, big_sql),
        }
        one_entry = len(__import__("json").dumps(
            [{"o": big_sql, "t": big_sql, "s": "SELECT", "m": 9_999_999}],
            separators=(",", ":"),
        ))
        _chunks, index, omitted = build_query_sample_chunks(
            workloads, sample_byte_budget=one_entry + 20,
        )
        assert "p.d.heavy" in index
        assert "p.d.light" not in index
        assert omitted == 1

    def test_no_budget_pressure_keeps_all(self):
        workloads = {"p.d.a": self._wl(5), "p.d.b": self._wl(9)}
        _chunks, index, omitted = build_query_sample_chunks(workloads)
        assert omitted == 0
        assert set(index) == {"p.d.a", "p.d.b"}
