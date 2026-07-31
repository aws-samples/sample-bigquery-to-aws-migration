# Storage placement is a per-entity axis: Iceberg default, RMS for hot/fidelity entities

ADR-0001 made S3 Tables (Iceberg) the universal Storage Target. Doc-verified research
(2026-07-22 gap analysis) showed the trade-off is real and entity-shaped, not
estate-shaped: on Iceberg, Redshift loses result caching, AutoMV, automatic query
rewrite, and zone maps/sort keys; and GEOGRAPHY / JSON / INTERVAL columns lose type
fidelity (WKT/ISO-8601/JSON-as-string) that Redshift Managed Storage retains natively
(spatial type, SUPER, INTERVAL). A dashboard-hot table with GEOGRAPHY columns and a
cold 50 TB event log genuinely want different storage.

We therefore **promote storage to a per-entity placement axis**, mirroring ADR-0004's
per-entity engine-home recommendation:

- **Iceberg (S3 Tables) stays the default** for every entity. It is the only storage
  both engines can query, the open/exit-safe format, and the cheaper tier at scale.
- **RMS is recommended per entity** only when the primary Query Engine is Redshift
  (Athena cannot query RMS-native tables) AND the entity shows RMS-favoring signals:
  type-fidelity loss on Iceberg (GEOGRAPHY / JSON / INTERVAL columns), sub-second SLA
  configured, or NOT NULL enforcement needs — weighed against size (storage premium
  and vendor-exit cost grow with bytes). The 500 GB ceiling is **practitioner
  opinion**, not an AWS-published threshold — no prescriptive guidance or
  Well-Architected content publishes a native-vs-open placement threshold
  (absence verified 2026-07-22); label it as such in customer-facing output.
- **Load path for RMS entities is two-phase**: Athena remains the sole
  BigQuery-extraction engine (federated INSERT into an Iceberg staging table —
  unchanged), then Redshift runs `CREATE TABLE` (native DDL) +
  `INSERT INTO … SELECT FROM <iceberg external schema>` and optionally drops the
  staging table. COPY from BigQuery directly is not possible; COPY from S3 would
  require a separate export pipeline we don't generate.

We rejected re-litigating ADR-0001 (all-RMS or estate-level choice) because
multi-engine access over one open storage layer remains the architecture's spine;
RMS is a per-entity hot-tier exception, not a parallel default. We also rejected
advisory-only (report text without scripts) because an advisory the migration
scripts contradict is worse than none.

## Consequences

- ADR-0001's "the old Redshift-native-storage path is dropped, not kept" is
  **partially superseded**: a *bounded* native-DDL generator returns (types + NOT
  NULL only — DISTKEY/SORTKEY/node-sizing stay retired; Serverless-first scope holds).
- `EntityReport` gains `storage_placement` (target, signals, confidence). JSON
  schema and report grow a storage column; `plan.json` entries carry
  `storage_target` and RMS entries carry a `redshift_phase` statement list.
- Migration Effort reflects the two-phase load (+1 factor for RMS entities).
- Cost: the storage line is split by placement — RMS-placed bytes bill as an RMS
  line (Redshift Serverless bills RMS separately by GB/month, verified
  2026-07-22), remaining bytes stay on the S3 Tables tier. RMS at $0.024/GB is
  cheaper than S3 Tables tier 1 at $0.0265/GB, so RMS placements slightly lower
  the estimate. (The original "all-Iceberg is conservative" framing was revised
  by the 2026-07-22 deep audit: the RMS line is real and must appear.)
- Entities placed on RMS lose multi-engine access — the advisor must never place
  an entity consumed by Athena (per engine placement) onto RMS.
