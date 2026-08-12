"""Shared data models for bq-assess — normative dataclasses and enums.

These are the NORMATIVE models for the lakehouse assessment, implemented exactly per
``.kiro/specs/phase1-assessment-tool/design.md`` § Data Models. Canonical glossary names
(CONTEXT.md); nested types preserved (no flattening).

All legacy code has been migrated to these models as of Phase 8 (8.1). Module-local types
(e.g., ``QueryAnalysis``, ``RelationshipResult``) were moved to their respective consumer
modules (``core/analyzer.py``, ``core/relationships.py``) where they remain internal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum

# ---- Enums -----------------------------------------------------------------


class EntityType(Enum):
    TABLE = "TABLE"
    EXTERNAL = "EXTERNAL"            # treated as Table (moves)
    VIEW = "VIEW"
    MATERIALIZED_VIEW = "MATERIALIZED_VIEW"
    ROUTINE = "ROUTINE"             # UDF / stored procedure


class EntityPopulation(Enum):
    TABLE = "TABLE"                 # scored on both axes
    REBUILT = "REBUILT"             # view/mv/udf — Query Complexity only, Effort = 0


class EffortCategory(Enum):         # Migration Effort axis (R9)
    AUTO = "AUTO"
    ASSISTED = "ASSISTED"
    MANUAL = "MANUAL"


class ComplexityCategory(Enum):     # Query Complexity axis (R11)
    PORTABLE = "PORTABLE"
    ADAPT = "ADAPT"
    REWRITE = "REWRITE"


class ConfidenceLevel(Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class ConfidenceSource(Enum):
    QUERY_LOGS = "query_logs"
    VIEW_DEFINITION = "view_definition"
    NAMING_HEURISTIC = "naming_heuristic"
    SCHEMA_ONLY = "schema_only"
    MANUAL_INPUT = "manual_input"
    SAFE_DEFAULT = "safe_default"


class TargetEngine(str, Enum):
    """Recommended Query Engine target — typo-proof string enum (Fix 7).

    The string values stay identical to the existing JSON/JS contract so
    downstream consumers (report JS, JSON schema) are unaffected.
    """
    ATHENA = "athena"
    REDSHIFT = "redshift"


class StorageTarget(str, Enum):
    """Per-entity Storage Target (ADR-0005). Iceberg is the default; RMS is the
    hot-tier exception, only reachable when Redshift is the primary Query Engine."""
    ICEBERG = "iceberg"             # S3 Tables (open, multi-engine) — default
    RMS = "rms"                     # Redshift Managed Storage (native, engine-local)


class BQPricingModel(Enum):
    ON_DEMAND = "ON_DEMAND"         # bytes scanned
    CAPACITY = "CAPACITY"           # slot reservations / Editions
    UNKNOWN = "UNKNOWN"             # → default on-demand, LOW confidence (R16.3)


# ---- Scanned metadata ------------------------------------------------------


@dataclass
class ColumnSchema:
    name: str
    field_type: str                 # BigQuery type name
    mode: str                       # NULLABLE | REQUIRED | REPEATED
    fields: list[ColumnSchema] = field(default_factory=list)  # nested STRUCT/RECORD


@dataclass
class TimePartitionConfig:
    type: str                       # DAY | HOUR | MONTH | YEAR
    field: str | None               # None => ingestion-time (_PARTITIONTIME) => non-clean (R7.3)


@dataclass
class RangePartitionConfig:         # R3.8 — previously uncaptured
    field: str
    start: int
    end: int
    interval: int


@dataclass
class RoutineMetadata:              # R3.3 — UDFs / stored procedures
    name: str
    language: str                   # SQL | JAVASCRIPT | ...
    arguments: list[str]
    body: str
    routine_type: str               # SCALAR_FUNCTION | PROCEDURE | ...


@dataclass
class EntityMetadata:
    entity_id: str
    dataset_id: str
    full_name: str                  # "dataset.entity" (the shared cross-file key, R19.5)
    entity_type: EntityType
    population: EntityPopulation
    num_rows: int                   # 0 for views/mviews/routines
    num_bytes: int
    columns: list[ColumnSchema]
    time_partitioning: TimePartitionConfig | None
    range_partitioning: RangePartitionConfig | None
    clustering_fields: list[str] | None
    view_query: str | None          # views (R3.2)
    mview_query: str | None         # materialized views (R3.2)
    routine: RoutineMetadata | None  # routines (R3.3)
    depends_on: list[str]           # FQNs of Tables this entity references (R4.5)
    last_modified: datetime
    physical_bytes: int | None = None  # populated by storage_stats; None = not yet resolved


# ---- Conversion / scoring results -----------------------------------------


@dataclass
class PartitionMapping:             # R7
    iceberg_transforms: list[str]   # e.g. ["day(event_date)"]
    sort_order: list[str]
    auto_derived: bool              # True = clean annotation; False = flagged decision
    decision_flags: list[str]       # partition_decision_required / sort_decision_required


@dataclass
class LossyCast:                    # R8
    column: str
    source_type: str
    iceberg_type: str
    loss_description: str


@dataclass
class ConversionResult:
    ddl: str                        # "" for non-Tables
    partition_mapping: PartitionMapping | None
    lossy_casts: list[LossyCast]
    warnings: list[str]
    success: bool


@dataclass
class EffortResult:                 # R9 — Tables only
    category: EffortCategory
    score: int
    flags: list[str]
    reasoning: str
    confidence: ConfidenceLevel


@dataclass
class DetectedConstruct:            # R10.3
    construct_class: str            # UNNEST | FUNCTION_DRIFT | ARRAY_FN | STRUCT_NAV | JS_UDF | ...
    snippet: str                    # anonymized (R10.4 / R22.4)
    description: str


@dataclass
class ComplexityResult:             # R11
    category: ComplexityCategory
    score: int
    constructs: list[DetectedConstruct]
    flags: list[str]
    reasoning: str
    confidence: ConfidenceLevel
    confidence_source: ConfidenceSource


@dataclass
class TranslationResult:            # Best-effort BQ→target-engine SQL translation
    redshift_sql: str               # translated SQL — field name kept for JSON schema compat; holds recommended engine's SQL
    confidence: str                 # "HIGH" | "MEDIUM" (auto-converted, verify semantics) | "LOW"
    warnings: list[str]             # e.g. ["JavaScript UDF requires manual rewrite"]
    target_engine: str = "redshift"  # "redshift" | "athena" — which engine dialect this translation targets


@dataclass
class PlacementRecommendation:      # R14
    home: str                       # "REDSHIFT" | "ICEBERG_CATALOG"
    signals: list[str]
    confidence: ConfidenceLevel
    refresh_unverified: bool        # True for Iceberg-MV until V7 confirmed


@dataclass
class StoragePlacement:             # ADR-0005 — Tables only
    target: StorageTarget           # ICEBERG (default) | RMS (hot-tier exception)
    signals: list[str]              # why — each traceable to a doc-verified trade-off
    confidence: ConfidenceLevel
    redshift_ddl: str | None = None  # native CREATE TABLE for RMS entities; None for Iceberg
    redshift_load: list[str] | None = None  # phase-2 statements (Redshift) for RMS entities


# ---- Cost ------------------------------------------------------------------


@dataclass
class PricingDetection:             # R16 — what PricingDetector.detect() returns
    # The bare BQPricingModel enum cannot carry the figures R16.2 and the confidence
    # R16.3/P20 require, so detect() returns this. `model` is the classification; the
    # capacity_* fields are populated only when model is CAPACITY (R16.2).
    model: BQPricingModel
    confidence: ConfidenceLevel
    source_note: str                # how the model was determined + date (R16.3, P20; V4/V5)
    edition: str | None = None              # "STANDARD" | "ENTERPRISE" | "ENTERPRISE_PLUS"
    baseline_slots: int | None = None
    max_slots: int | None = None
    commitment_slots: int | None = None
    commitment_plan: str | None = None      # "FLEX" | "MONTHLY" | "ANNUAL" | "THREE_YEAR"
    reservation_id: str | None = None       # "project:location.name" from JOBS — lets the
                                            # collector's auto-read reuse detection's rows
                                            # instead of re-querying INFORMATION_SCHEMA
    reservation_readable: bool = True       # False = permission denied, cost not estimable
    # False = bundle predates the reservation auto-reader (collector < 0.8): the
    # fields were never collected, as opposed to collected-but-denied. Drives the
    # cause-specific unavailable message (old-bundle vs permission-denied).
    reservation_data_collected: bool = True
    autoscale_slot_seconds: int | None = None
    timeline_window_seconds: int | None = None
    assigned_projects: list[str] = field(default_factory=list)
    assigned_count: int = 0
    commitments: list[dict] = field(default_factory=list)  # [{slot_count, plan, edition}]


@dataclass
class SlotUtilization:              # R17
    avg_slots: float
    p50_slots: float
    p99_slots: float
    peak_slots: float
    active_hour_fraction: float
    total_slot_ms: int
    days_sampled: int               # distinct UTC dates with slot-bearing activity
    total_bytes_processed: int = 0
    # Bytes actually billed (10 MiB per-query minimum, rounded up to the nearest MiB) —
    # what on-demand billing charges. Zero is a legitimate value (all-cached or
    # reservation-served windows); has_billed_bytes below says whether the source
    # carried the column at all.
    total_bytes_billed: int = 0
    # True when the job source exposed total_bytes_billed (JOBS_BY_PROJECT always does;
    # old query-log exports may not). Distinguishes "billed 0" from "column unavailable"
    # so the cost model doesn't fall back to processed bytes on genuinely-zero windows.
    has_billed_bytes: bool = False
    total_queries: int = 0
    lookback_days: int = 30         # calendar days in the observation window


@dataclass
class CostLine:
    label: str
    monthly: float | None           # None when expressed as a range
    monthly_low: float | None
    monthly_high: float | None
    confidence: ConfidenceLevel
    source_note: str                # pricing-constant provenance + date (R18.7; V1–V4)
    # When set, totals (aws_monthly_low/high, scenario totals) use THIS value on
    # both bounds while monthly_low/high remain the displayed range. Used by the
    # Intelligent-Tiering storage line (2026-07-31): the comparison prices the
    # customer's observed access pattern; the breakdown still shows the month-1
    # transition bound. None = totals derive from monthly/low/high as before.
    headline: float | None = None


@dataclass
class WorkloadProfile:
    """Customer-specific workload metrics used for AWS cluster sizing and justification."""
    has_data: bool = False
    total_stored_gb: float = 0.0
    total_queries: int = 0
    days_sampled: int = 0
    lookback_days: int = 30
    queries_per_day: float = 0.0
    queries_per_second_avg: float = 0.0
    avg_concurrent_queries: float = 0.0
    peak_concurrent_queries: float = 0.0
    avg_bytes_per_query: float = 0.0
    monthly_scanned_tb: float = 0.0
    active_hour_fraction: float = 0.0
    total_slot_ms: int = 0
    avg_slots: float = 0.0
    p99_slots: float = 0.0
    peak_slots: float = 0.0


@dataclass
class AWSScenario:
    """One AWS deployment option with its cost lines, total, and justification."""
    label: str                      # e.g. "Redshift Serverless", "Provisioned 3× ra3.4xl (1yr RI)"
    category: str                   # "SERVERLESS" | "SERVERLESS_1YR" | "SERVERLESS_1YR_NO_UPFRONT" | "SERVERLESS_3YR" | "PROVISIONED_ONDEMAND" | "PROVISIONED_1YR" | "PROVISIONED_3YR"
    lines: list[CostLine]
    monthly_total: float
    confidence: ConfidenceLevel
    is_recommended: bool = False
    justification: str = ""         # why this option is/isn't recommended for this workload
    cluster_config: str = ""        # e.g. "3× ra3.4xlarge" (empty for serverless)
    workload_fit_notes: list[str] = field(default_factory=list)
    # Set when the scenario is priced correctly but is a poor fit for THIS workload (e.g. a
    # 24/7 reservation for a 5%-active workload shows 34-95× the recommended cost). Reports
    # render it as a visual demotion so the number reads as "not for you", not "tool broken".
    not_recommended_reason: str = ""


@dataclass
class AWSRecommendation:
    """The tool's best-fit recommendation with reasoning anchored to customer workload."""
    recommended_scenario: str       # label of the recommended AWSScenario
    reasoning: str                  # paragraph explaining why, referencing actual workload numbers
    workload_profile: WorkloadProfile = field(default_factory=WorkloadProfile)
    alternatives_considered: list[str] = field(default_factory=list)


@dataclass
class CostComparison:               # R18
    bq_pricing_model: BQPricingModel
    bigquery_monthly: float
    bigquery_breakdown: list[CostLine]
    aws_lines: list[CostLine]       # storage (point) + compute (point or range) — best scenario
    aws_monthly_low: float
    aws_monthly_high: float
    monthly_delta_low: float        # headline: bigquery_monthly - aws_monthly_high
    monthly_delta_high: float       # bigquery_monthly - aws_monthly_low
    annual_savings_low: float
    annual_savings_high: float
    migration_onetime: float        # derived from aggregate Migration Effort (R18.5)
    breakeven_months_low: float
    breakeven_months_high: float
    compute_confidence: ConfidenceLevel
    athena_one_time_optimize: Decimal | None = None  # Athena OPTIMIZE compaction cost (upper bound)
    aws_scenarios: list[AWSScenario] = field(default_factory=list)
    recommendation: AWSRecommendation | None = None
    # Region provenance: which geography each side was priced in (2026-07-02 region cascade).
    bq_pricing_region: str = "us"           # BigQuery dataset location the BQ rates reflect
    aws_pricing_region: str = "us-east-1"   # AWS region the AWS rates reflect
    # BQ-cost availability (2026-08-10): Enterprise/EP capacity without reservation
    # data has BOTH rate (~2× commitment spread) and quantity (24/7 baseline) unknown —
    # no defensible estimate exists. When available=False the report suppresses the
    # entire cost-comparison section; bigquery_monthly holds 0.0 only so legacy math
    # doesn't crash — every consumer must check the flag first.
    bq_cost_available: bool = True
    # "modelled" | "customer_provided" (--bigquery-monthly-cost) | "unavailable"
    bq_cost_basis: str = "modelled"
    # Range-basis BQ cost (2026-08-11): when the BQ side is itself a modelled range
    # (STANDARD capacity from slots), this holds the measured-minimum total while
    # bigquery_monthly holds the upper estimate. monthly_delta_low is computed
    # against THIS value so the headline savings is the committable floor-based
    # figure, not one anchored to the upper estimate. None = point-estimate basis.
    bigquery_monthly_low: float | None = None
    # Cause shown to the customer when unavailable: old-bundle vs permission-denied.
    bq_cost_unavailable_reason: str = ""
    # Cost narrative (2026-07-16 restructure: the cost-section callout moved into the
    # "Assumptions & Methodology" section; each statement now has exactly one home —
    # see SCRUM_NOTES).
    # What the estimate does NOT cover, on BOTH sides of the comparison (BQ: streaming
    # inserts, Storage R/W API, free tier, … / AWS: migration transfer, Spectrum, ML) —
    # rendered verbatim so the estimate is never mistaken for the whole bill.
    scope_notes: list[str] = field(default_factory=list)
    # Pricing mechanics a reader needs to reconcile the figure against a real bill
    # (30-day-month normalization, unknown-region rate fallback caveat).
    pricing_notes: list[str] = field(default_factory=list)
    # S3 Tables Intelligent-Tiering derivation rows for the report's storage table
    # (2026-07-31). Rows: frequent / infrequent / archive / monitoring / total, each
    # {"tier", "label", "tables", "gb", "rate", "rate_note", "monthly"}. Empty when
    # the estate has no cold bytes (storage line stays a point — nothing to derive).
    storage_tier_breakdown: list[dict] = field(default_factory=list)
    # The assumptions most likely to move the AWS figure — each bullet names the
    # uncertainty AND how to validate it (pilot workload / SYS_SERVERLESS_USAGE).
    key_uncertainties: list[str] = field(default_factory=list)
    # The single always-visible confidence statement for the whole cost section
    # (2026-07-15 consolidation: replaces per-scenario and per-line badge scatter).
    # Level = minimum across BQ breakdown + recommended option's lines; the sentence names
    # what the estimate was priced from (uncertainties live in key_uncertainties).
    estimate_basis_level: ConfidenceLevel = ConfidenceLevel.LOW
    estimate_basis: str = ""
    # Pristine all-Iceberg storage line, stashed by apply_rms_storage_split before
    # it moves RMS-resident bytes out of the Redshift scenarios' S3 lines. The
    # Athena scenario (engine/comparison.py) must price the FULL estate as S3
    # Tables — on an Athena deployment nothing lives in RMS. None when no split ran.
    all_iceberg_storage_line: CostLine | None = None


# ---- Report ----------------------------------------------------------------


@dataclass
class FailureRecord:
    entity_name: str
    stage: str                      # scan | classify | convert | detect | score
    error: str


@dataclass
class EntityReport:
    full_name: str                  # shared cross-file key (R19.5)
    entity_type: EntityType
    population: EntityPopulation
    rows: int
    size_gb: float
    depends_on: list[str]
    # Effort axis (Tables only; None for REBUILT)
    effort: EffortResult | None
    conversion: ConversionResult | None
    # DEPRECATED: superseded by engine/athena/migration (MigrationDML per entity).
    # Kept for schema compatibility; always None for new assessments.
    load_sync_dml: str | None = None
    # Query axis (any entity with SQL surface / direct query target)
    complexity: ComplexityResult | None = None
    rewrite_guidance: list[str] = field(default_factory=list)
    translated_sql: TranslationResult | None = None
    placement: PlacementRecommendation | None = None
    physical_bytes: int | None = None  # measured physical storage; None = not available/measured
    storage_placement: StoragePlacement | None = None  # ADR-0005; None for non-tables / Athena-primary
    # Attributed production workload from INFORMATION_SCHEMA.JOBS (query
    # attribution, 2026-08-03): {query_count, total_slot_ms, slot_hours,
    # num_shapes, statement_types}. Summary only — sample SQL is delivered via
    # the HTML embed (top N) and the query-workload/ sidecar (all shapes), not
    # the model. None when the entity has no attributed queries.
    query_workload: dict | None = None


@dataclass
class AssessmentSummary:
    total_entities: int
    total_tables: int
    total_size_gb: float                # projected post-migration (physical) size
    effort_counts: dict[str, int]       # {"AUTO": n, "ASSISTED": n, "MANUAL": n}
    complexity_counts: dict[str, int]   # {"PORTABLE": n, "ADAPT": n, "REWRITE": n}
    sql_surface_confidence: ConfidenceLevel
    total_logical_size_gb: float = 0.0  # BigQuery logical size (what the customer's console shows)
    # Construct classes (R10.3) detected in the collected query-log workload (the
    # __ad_hoc__ SQL-surface bucket) — SQL not owned by any defined entity, e.g.
    # application/BI queries. Empty when no logs were collected or none matched.
    workload_constructs: list[str] = field(default_factory=list)


@dataclass
class Assessment:
    assessment_id: str                  # "assess-{date}-{hash}"
    generated_at: datetime
    project_id: str
    summary: AssessmentSummary
    cost: CostComparison
    entities: list[EntityReport]
    failures: list[FailureRecord]
    engine_recommendation: EngineRecommendation | None = None
    migration_plans: dict[str, MigrationDML] | None = None
    source_db_setup: list[str] | None = None


# ---- Engine layer (Phase 9 — Athena addition) -----------------------------


@dataclass
class EngineCostEstimate:
    engine_id: str
    monthly_total: Decimal
    monthly_compute: Decimal
    monthly_storage: Decimal
    pricing_mode: str  # "on_demand" | "provisioned"
    confidence: str
    source_note: str
    one_time_migration: Decimal | None = None  # e.g., Athena OPTIMIZE compaction cost


@dataclass
class SignalContribution:
    signal: str
    value: float
    direction: str  # "athena" | "redshift" | "neutral"
    weight: float


@dataclass
class EngineRecommendation:
    primary_engine: str  # "athena" | "redshift"
    confidence: float
    reasoning: list[SignalContribution]
    crossover_point_tb_day: Decimal
    override_reason: str | None


@dataclass
class EngineRewrite:
    engine_id: str
    translated_sql: str
    confidence: str  # "HIGH" | "MEDIUM" | "LOW"
    warnings: list[str]
    unsupported_constructs: list[str]


@dataclass
class EnginePlacement:
    engine_id: str
    home: str
    signals: list[str]
    confidence: str
    gaps: list[str]


@dataclass
class MigrationShortcoming:
    category: str       # "sort_order" | "partition_evolution" | "merge" | "type_cast" | "compaction"
    severity: str       # "advisory" | "action_required"
    bq_source: str
    description: str
    remediation: str
    remediation_engine: str  # "spark" | "redshift" | "manual"


@dataclass
class PostMigrationStep:
    table: str
    step_type: str      # "sort" | "compact" | "partition_transform"
    command: str
    engine: str         # "spark_emr" | "athena"
    reason: str
    priority: str       # "recommended" | "optional"


@dataclass
class MigrationDML:
    table: str
    statements: list[str]
    shortcomings: list[MigrationShortcoming]
    post_optimization: list[PostMigrationStep]
    estimated_scan_bytes: int | None
    # Post-load row-count check: SELECT comparing source (federated connector)
    # vs target (Iceberg) counts in one query. Additive default (2026-08-04
    # audit: validation existed only as prose).
    validation_query: str | None = None


@dataclass
class EngineConfig:
    target_region: str
    query_sla_ms: int
    preferred_engine: str | None
    chunk_days: int
    post_optimization: bool
    compaction_threshold_gb: float
    peak_concurrency_override: int | None
    idle_hours_override: float | None
    source: dict[str, str]  # field → "cli" | "yaml" | "prompt" | "inferred"
