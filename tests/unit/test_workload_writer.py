"""Tests for the query-workload sidecar writer."""
from __future__ import annotations

import csv
import os

from bq_assess.bundle.models import QueryRecord
from bq_assess.core.query_attribution import (
    TOP_QUERIES_PER_ENTITY,
    EntityWorkload,
    QuerySample,
    attribute_queries,
)
from bq_assess.report.workload_writer import write_workload_sidecar


def _wl(shapes: list[tuple[str, int]]) -> EntityWorkload:
    samples = [
        QuerySample(query=q, statement_type="SELECT", total_slot_ms=ms)
        for q, ms in shapes
    ]
    return EntityWorkload(
        query_count=len(shapes),
        total_slot_ms=sum(ms for _, ms in shapes),
        statement_types={"SELECT": len(shapes)},
        samples=samples[:TOP_QUERIES_PER_ENTITY],
        num_shapes=len(shapes),
        all_shapes=samples,
    )


class TestAttributionKeepsAllShapes:
    def test_all_shapes_beyond_top_n(self):
        queries = [
            QueryRecord(
                query=f"SELECT {i} FROM `p.ds.t`",
                total_slot_ms=(100 - i) * 1000,
                statement_type="SELECT",
            )
            for i in range(20)
        ]
        result = attribute_queries(queries, {"ds.t"}, "p")
        wl = result["ds.t"]
        assert len(wl.samples) == TOP_QUERIES_PER_ENTITY
        assert len(wl.all_shapes) == 20
        assert wl.num_shapes == 20
        # samples is the top-N prefix of all_shapes (both slot-cost ordered)
        assert wl.all_shapes[: TOP_QUERIES_PER_ENTITY] == wl.samples
        # heaviest first
        costs = [s.total_slot_ms for s in wl.all_shapes]
        assert costs == sorted(costs, reverse=True)


class TestWorkloadSidecar:
    def test_writes_per_entity_files_and_index(self, tmp_path):
        workloads = {
            "ds.orders": _wl([("SELECT a FROM `p.ds.orders`", 7_200_000),
                              ("SELECT b FROM `p.ds.orders`", 3_600_000)]),
            "ds.users": _wl([("SELECT * FROM `p.ds.users`", 1_800_000)]),
        }
        out = write_workload_sidecar(workloads, "redshift", str(tmp_path))
        assert out == str(tmp_path / "query-workload")
        assert sorted(os.listdir(out)) == ["INDEX.csv", "ds.orders.sql", "ds.users.sql"]

        content = (tmp_path / "query-workload" / "ds.orders.sql").read_text()
        assert "2 distinct statements" in content
        assert content.count("BigQuery (original)") == 2
        assert content.count("Redshift (translated)") == 2
        # original is commented out; translated is live SQL
        assert "-- SELECT a FROM `p.ds.orders`" in content
        assert "SELECT a FROM ds.orders" in content   # backticks translated
        # heaviest first
        assert content.index("SELECT a") < content.index("SELECT b")

        with open(tmp_path / "query-workload" / "INDEX.csv") as f:
            rows = {r["entity"]: r for r in csv.DictReader(f)}
        assert rows["ds.orders"]["distinct_statements"] == "2"
        assert rows["ds.orders"]["file"] == "ds.orders.sql"
        assert rows["ds.users"]["queries"] == "1"

    def test_athena_translation_target(self, tmp_path):
        workloads = {
            "ds.t": _wl([("SELECT SAFE_CAST(x AS INT64) FROM `p.ds.t`", 1000)]),
        }
        write_workload_sidecar(workloads, "athena", str(tmp_path))
        content = (tmp_path / "query-workload" / "ds.t.sql").read_text()
        assert "Athena (translated)" in content
        assert "TRY_CAST(" in content

    def test_all_shapes_written_not_just_top5(self, tmp_path):
        shapes = [(f"SELECT {i} FROM `p.ds.big`", (50 - i) * 1000) for i in range(50)]
        workloads = {"ds.big": _wl(shapes)}
        write_workload_sidecar(workloads, "redshift", str(tmp_path))
        content = (tmp_path / "query-workload" / "ds.big.sql").read_text()
        assert "Statement 50/50" in content

    def test_empty_map_returns_none(self, tmp_path):
        assert write_workload_sidecar({}, "redshift", str(tmp_path)) is None
        assert not (tmp_path / "query-workload").exists()
        # entities with stats but no shapes also produce nothing
        no_shapes = EntityWorkload(query_count=3, total_slot_ms=1000, num_shapes=0)
        assert write_workload_sidecar({"ds.t": no_shapes}, "redshift", str(tmp_path)) is None

    def test_unsafe_entity_names_sanitized(self, tmp_path):
        workloads = {"ds.we/ird name": _wl([("SELECT 1", 1000)])}
        write_workload_sidecar(workloads, "redshift", str(tmp_path))
        files = os.listdir(tmp_path / "query-workload")
        assert "ds.we_ird_name.sql" in files

    def test_translated_statement_terminated(self, tmp_path):
        """Each live (translated) statement ends with a semicolon so the file
        is runnable as a script."""
        workloads = {"ds.t": _wl([("SELECT 1 FROM `p.ds.t`", 1000)])}
        write_workload_sidecar(workloads, "redshift", str(tmp_path))
        content = (tmp_path / "query-workload" / "ds.t.sql").read_text()
        live = [ln for ln in content.splitlines() if ln and not ln.startswith("--")]
        assert live and live[-1].rstrip().endswith(";")


class TestQuerySampleChunkClipping:
    """Per-sample SQL clipping bounds the HTML embed now that there is no
    per-estate entity cap (2026-08-04)."""

    def test_long_sql_clipped_with_pointer(self):
        from bq_assess.report._serialize import (
            MAX_SAMPLE_SQL_CHARS,
            build_query_sample_chunks,
        )

        long_sql = "SELECT " + ", ".join(f"col_{i}" for i in range(2000))
        assert len(long_sql) > MAX_SAMPLE_SQL_CHARS
        chunks, index = build_query_sample_chunks({
            "ds.t": {"samples": [{
                "query": long_sql, "translated": long_sql,
                "statement_type": "SELECT", "total_slot_ms": 1000,
            }]},
        })
        assert index == {"ds.t": 0}
        sample = chunks[0]["ds.t"][0]
        assert len(sample["o"]) <= MAX_SAMPLE_SQL_CHARS + 100
        assert "query-workload/" in sample["o"]
        assert "query-workload/" in sample["t"]

    def test_short_sql_untouched(self):
        from bq_assess.report._serialize import build_query_sample_chunks

        chunks, _ = build_query_sample_chunks({
            "ds.t": {"samples": [{
                "query": "SELECT 1", "translated": "SELECT 1",
                "statement_type": "SELECT", "total_slot_ms": 1000,
            }]},
        })
        assert chunks[0]["ds.t"][0]["o"] == "SELECT 1"
