# Storage target is S3 Tables (Iceberg); Redshift is the query engine

We pivoted the migration target from **Redshift-as-storage** (native tables sized by
node count, with DISTKEY/SORTKEY) to a **decoupled lakehouse**: migrated data lives in
**Amazon S3 Tables (managed Apache Iceberg)**, and **Amazon Redshift Serverless** is the
query/compute engine over it. We chose this because storage and compute now decouple
cleanly — Iceberg stores nested types natively (so STRUCT/ARRAY are no longer a migration
obstacle), Redshift can run `UPDATE/DELETE/MERGE` directly on Iceberg (AWS, Apr 2026), and
Serverless meters compute (RPU-hours) independently of stored volume. **Scope: Serverless
only** — provisioned Redshift and its serverless-vs-provisioned advisor are out.

## Consequences

- Node-sizing-by-storage, DISTKEY/SORTKEY DDL, and the deployment advisor are retired.
- Cost becomes a sum of independent lines (S3 storage + Serverless RPU) rather than node count.
- "Migration complexity" can no longer be one number — see ADR-0002.
- The old Redshift-native-storage path is dropped, not kept as a parallel target. Re-adding
  it later would mean reintroducing a storage-coupled cost/DDL model.

## Amendment (2026-08-14): provisioned Redshift is in scope as a COST scenario

"Scope: Serverless only" above described the migration target, and that part stands —
generated DDL, placement, and the storage layer remain Serverless-over-Iceberg. But the
cost comparison has since grown provisioned scenarios (rg/ra3 node pricing, added with the
capacity-pricing work) and recommends provisioned clusters for sustained workloads where
they are cheaper. The nearest-region policy therefore requires **rg node availability**
alongside S3 Tables + Serverless when selecting an AWS pricing region
(`core/region_mapping.AWS_REGIONS_WITH_STACK`): a region that cannot price every offered
scenario cannot be the pricing anchor. Consequence: locations whose same-metro AWS region
lacks the full stack (Tel Aviv/il-central-1, Doha & Dammam/me-central-1 & me-south-1,
Zurich/eu-central-2) price at the nearest full-stack region, flagged in the report.
