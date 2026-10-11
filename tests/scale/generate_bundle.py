"""Synthetic petabyte-scale bundle generator.

Builds a schema-valid bundle directory the way the collector would, at a scale
real customers hit: order 100k entities, PB of storage, millions of queries in
the workload stats with the statement export at its 50k cap. Deterministic for
a given seed so E2E measurements are comparable across runs.

CLI:  python3 tests/scale/generate_bundle.py --entities 100000 --out /tmp/scale-bundle
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone

from bq_assess.bundle.models import Bundle, QueryRecord
from bq_assess.bundle.writer import BundleWriter
from bq_assess.core.jobs_query import QUERIES_EXPORT_LIMIT
from bq_assess.models import (
    BQPricingModel,
    ColumnSchema,
    ConfidenceLevel,
    EntityMetadata,
    EntityPopulation,
    EntityType,
    PricingDetection,
    RoutineMetadata,
    SlotUtilization,
    TimePartitionConfig,
)

_ANCHOR = datetime(2026, 8, 1, tzinfo=timezone.utc)

_SCALAR_TYPES = ["STRING", "INT64", "FLOAT64", "TIMESTAMP", "DATE", "BOOL", "NUMERIC"]

_VIEW_SQL = [
    # portable
    "SELECT id, name, created_at FROM `{p}.{ds}.{t}` WHERE created_at > '2026-01-01'",
    "SELECT a.id, b.total FROM `{p}.{ds}.{t}` a JOIN `{p}.{ds}.{t2}` b ON a.id = b.id",
    # BQ-specific constructs (ADAPT/REWRITE candidates)
    ("SELECT id, ARRAY_AGG(item ORDER BY ts DESC LIMIT 3) AS latest "
     "FROM `{p}.{ds}.{t}` GROUP BY id"),
    ("SELECT * FROM `{p}.{ds}.{t}` "
     "QUALIFY ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) = 1"),
    ("SELECT id, payload.address.city AS city, SAFE_CAST(amount AS INT64) AS amt "
     "FROM `{p}.{ds}.{t}`"),
    ("SELECT id, item FROM `{p}.{ds}.{t}`, UNNEST(items) AS item WHERE item.qty > 0"),
]

_WORKLOAD_SQL = [
    "SELECT COUNT(*) FROM `{p}.{ds}.{t}` WHERE dt = CURRENT_DATE()",
    ("SELECT user_id, SUM(amount) AS total FROM `{p}.{ds}.{t}` "
     "WHERE ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY) GROUP BY user_id"),
    ("SELECT * FROM `{p}.{ds}.{t}` a LEFT JOIN `{p}.{ds}.{t2}` b USING (id) "
     "QUALIFY ROW_NUMBER() OVER (PARTITION BY a.id ORDER BY a.ts DESC) = 1"),
    ("MERGE `{p}.{ds}.{t}` T USING `{p}.{ds}.{t2}` S ON T.id = S.id "
     "WHEN MATCHED THEN UPDATE SET T.v = S.v WHEN NOT MATCHED THEN INSERT ROW"),
    ("SELECT id, ARRAY_LENGTH(items) AS n, payload.meta.source AS src "
     "FROM `{p}.{ds}.{t}` WHERE _PARTITIONDATE = '2026-08-01'"),
]

_JS_UDF_BODY = "return x.split(',').map(function(s){return s.trim();}).join('|');"


def _columns(rng: random.Random, nested: bool) -> list[ColumnSchema]:
    cols = [
        ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
        ColumnSchema(name="ts", field_type="TIMESTAMP", mode="NULLABLE"),
    ]
    for i in range(rng.randint(3, 23)):
        cols.append(ColumnSchema(
            name=f"col_{i}", field_type=rng.choice(_SCALAR_TYPES), mode="NULLABLE",
        ))
    if nested:
        cols.append(ColumnSchema(
            name="payload", field_type="RECORD", mode="NULLABLE",
            fields=[
                ColumnSchema(name="address", field_type="RECORD", mode="NULLABLE",
                             fields=[ColumnSchema(name="city", field_type="STRING",
                                                  mode="NULLABLE")]),
                ColumnSchema(name="meta", field_type="RECORD", mode="NULLABLE",
                             fields=[ColumnSchema(name="source", field_type="STRING",
                                                  mode="NULLABLE")]),
            ],
        ))
        cols.append(ColumnSchema(
            name="items", field_type="RECORD", mode="REPEATED",
            fields=[ColumnSchema(name="qty", field_type="INT64", mode="NULLABLE")],
        ))
    return cols


def build_bundle(n_entities: int, project_id: str, seed: int = 42) -> Bundle:
    rng = random.Random(seed)  # nosec B311 - deterministic synthetic test data, not cryptographic
    n_datasets = max(1, n_entities // 500)
    datasets = [f"ds_{i:04d}" for i in range(n_datasets)]

    n_views = int(n_entities * 0.08)
    n_mviews = int(n_entities * 0.04)
    n_routines = int(n_entities * 0.03)
    n_tables = n_entities - n_views - n_mviews - n_routines

    entities: list[EntityMetadata] = []
    table_names: list[tuple[str, str]] = []  # (dataset, table)

    # --- tables: lognormal sizes + a monster tail so the estate totals ~PBs ---
    for i in range(n_tables):
        ds = datasets[i % n_datasets]
        name = f"tbl_{i:06d}"
        table_names.append((ds, name))
        if i < 25:                                   # monster tables: 50–300 TB each
            size = rng.randint(50, 300) * 10**12
        else:                                        # lognormal body, ~1 GB median
            size = int(min(rng.lognormvariate(20.7, 2.2), 4e13))
        rows = max(1, size // 200)
        partitioned = rng.random() < 0.30
        entities.append(EntityMetadata(
            entity_id=name,
            dataset_id=ds,
            full_name=f"{project_id}.{ds}.{name}",
            entity_type=EntityType.TABLE,
            population=EntityPopulation.TABLE,
            num_rows=rows,
            num_bytes=size,
            columns=_columns(rng, nested=rng.random() < 0.15),
            time_partitioning=TimePartitionConfig(type="DAY", field="ts") if partitioned else None,
            range_partitioning=None,
            clustering_fields=["id"] if rng.random() < 0.20 else None,
            view_query=None,
            mview_query=None,
            routine=None,
            depends_on=[],
            last_modified=_ANCHOR - timedelta(days=rng.randint(0, 400)),
            physical_bytes=int(size * 0.4),
        ))

    def _sql(template_pool) -> str:
        ds, t = rng.choice(table_names)
        _, t2 = rng.choice(table_names)
        return rng.choice(template_pool).format(p=project_id, ds=ds, t=t, t2=t2)

    for i in range(n_views):
        ds = datasets[rng.randrange(n_datasets)]
        name = f"vw_{i:06d}"
        dep_ds, dep_t = rng.choice(table_names)
        entities.append(EntityMetadata(
            entity_id=name, dataset_id=ds, full_name=f"{project_id}.{ds}.{name}",
            entity_type=EntityType.VIEW, population=EntityPopulation.REBUILT,
            num_rows=0, num_bytes=0, columns=[],
            time_partitioning=None, range_partitioning=None, clustering_fields=None,
            view_query=_sql(_VIEW_SQL), mview_query=None, routine=None,
            depends_on=[f"{project_id}.{dep_ds}.{dep_t}"],
            last_modified=_ANCHOR - timedelta(days=rng.randint(0, 400)),
            physical_bytes=0,
        ))

    for i in range(n_mviews):
        ds = datasets[rng.randrange(n_datasets)]
        name = f"mv_{i:06d}"
        size = int(min(rng.lognormvariate(19.0, 1.8), 1e12))
        entities.append(EntityMetadata(
            entity_id=name, dataset_id=ds, full_name=f"{project_id}.{ds}.{name}",
            entity_type=EntityType.MATERIALIZED_VIEW, population=EntityPopulation.REBUILT,
            num_rows=max(1, size // 200), num_bytes=size, columns=[],
            time_partitioning=None, range_partitioning=None, clustering_fields=None,
            view_query=None, mview_query=_sql(_VIEW_SQL), routine=None,
            depends_on=[], last_modified=_ANCHOR - timedelta(days=rng.randint(0, 90)),
            physical_bytes=int(size * 0.4),
        ))

    for i in range(n_routines):
        ds = datasets[rng.randrange(n_datasets)]
        name = f"fn_{i:06d}"
        is_js = rng.random() < 0.10
        entities.append(EntityMetadata(
            entity_id=name, dataset_id=ds, full_name=f"{project_id}.{ds}.{name}",
            entity_type=EntityType.ROUTINE, population=EntityPopulation.REBUILT,
            num_rows=0, num_bytes=0, columns=[],
            time_partitioning=None, range_partitioning=None, clustering_fields=None,
            view_query=None, mview_query=None,
            routine=RoutineMetadata(
                name=name,
                language="JAVASCRIPT" if is_js else "SQL",
                arguments=["x STRING"],
                body=_JS_UDF_BODY if is_js else "(SELECT UPPER(x))",
                routine_type="SCALAR_FUNCTION",
            ),
            depends_on=[], last_modified=_ANCHOR - timedelta(days=rng.randint(0, 400)),
            physical_bytes=0,
        ))

    # --- workload: PB-scale month, statement export at its cap ---
    total_queries = 6_000_000
    lookback = 31
    five_pb = 5 * 10**15
    workload = SlotUtilization(
        avg_slots=5_200.0, p50_slots=3_100.0, p99_slots=41_000.0, peak_slots=92_000.0,
        active_hour_fraction=0.98,
        total_slot_ms=int(5_200 * 3_600_000 * 24 * lookback),
        days_sampled=lookback,
        total_bytes_processed=five_pb,
        total_bytes_billed=five_pb + 2 * 10**12,
        has_billed_bytes=True,
        total_queries=total_queries,
        lookback_days=lookback,
    )

    queries = [
        QueryRecord(
            query=_sql(_WORKLOAD_SQL),
            total_slot_ms=int(rng.lognormvariate(13.0, 2.0)),
            total_bytes_processed=int(rng.lognormvariate(22.0, 2.5)),
            total_bytes_billed=int(rng.lognormvariate(22.0, 2.5)),
            statement_type="SELECT",
            creation_time=(_ANCHOR - timedelta(minutes=i % 44_640)).isoformat(),
        )
        for i in range(QUERIES_EXPORT_LIMIT)
    ]

    pricing = PricingDetection(
        model=BQPricingModel.ON_DEMAND,
        confidence=ConfidenceLevel.MEDIUM,
        source_note=f"Synthetic scale bundle: ON_DEMAND over {total_queries:,} jobs.",
    )

    return Bundle(
        project_id=project_id,
        bq_location="asia-southeast2",
        aws_region="ap-southeast-3",
        entities=entities,
        workload=workload,
        pricing=pricing,
        rates=None,
        queries=queries,
        storage_basis="measured",
        collector_version="0.8.0-scale-synthetic",
        created_at=_ANCHOR.isoformat(),
        regions=["asia-southeast2"],
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--entities", type=int, default=100_000)
    ap.add_argument("--out", required=True)
    ap.add_argument("--project-id", default="pb-scale-synthetic")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    bundle = build_bundle(args.entities, args.project_id, args.seed)
    total_bytes = sum(e.num_bytes for e in bundle.entities)
    path = BundleWriter().write(bundle, args.out)
    print(f"bundle: {path}")
    print(f"entities: {len(bundle.entities):,}  total logical: {total_bytes / 1e15:.2f} PB")


if __name__ == "__main__":
    main()
