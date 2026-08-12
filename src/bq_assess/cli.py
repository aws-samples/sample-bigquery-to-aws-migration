"""CLI interface and pipeline orchestration for bq-assess.

Entry point for the BigQuery migration assessment tool. The pipeline is split at the
collection seam (2026-07-08 collector/report design):

- ``collect(params) -> Bundle`` (collector.py) — every stage that touches GCP.
- ``analyze_and_report(bundle, params)`` (here) — pure computation + report writing.

``bq-assess assess`` composes both in-process (behavior unchanged); ``bq-assess report
--bundle`` runs the analysis half offline on a customer bundle produced by bq-collect.
"""

from __future__ import annotations

import hashlib
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
import yaml
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.prompt import Confirm, Prompt
from rich.table import Table

from bq_assess import __version__
from bq_assess.bundle import Bundle, BundleLoader, BundleWriter
from bq_assess.bundle.loader import BundleError
from bq_assess.collector import collect
from bq_assess.core.disclaimer import CLI_ONE_LINER
from bq_assess.core.price_lookup import (
    PriceLookup,
    PricingTimeout,
    apply_live_rates,
    fetch_live_rates_with_timeout,
    rates_from_dict,
)
from bq_assess.core.relationships import RelationshipInferrer
from bq_assess.core.sql_surface import SQLSurfaceAnalyzer
from bq_assess.core.storage_stats import effective_physical_bytes
from bq_assess.engine.comparison import assemble_cost_comparison
from bq_assess.engine.redshift.cost import CostEstimator, _window_days
from bq_assess.engine.redshift.placement import PlacementAdvisor
from bq_assess.engine.redshift.rewrite import RewriteGuide
from bq_assess.models import (
    Assessment,
    AssessmentSummary,
    BQPricingModel,
    ComplexityResult,
    ConfidenceLevel,
    CostComparison,
    EffortResult,
    EngineConfig,
    EntityMetadata,
    EntityPopulation,
    EntityReport,
    FailureRecord,
    MigrationDML,
    PlacementRecommendation,
    PricingDetection,
    SlotUtilization,
    StoragePlacement,
    StorageTarget,
    TargetEngine,
    TranslationResult,
    WorkloadProfile,
)
from bq_assess.report.html_writer import HTMLWriter
from bq_assess.report.json_writer import JSONWriter
from bq_assess.scoring.complexity import ComplexityScorer
from bq_assess.scoring.effort import EffortScorer
from bq_assess.targets.iceberg.converter import IcebergConverter

logger = logging.getLogger(__name__)
console = Console()


def _build_workload_profile(
    slots: SlotUtilization | None,
    entities: list[EntityMetadata],
    config: EngineConfig,
) -> WorkloadProfile:
    """Build WorkloadProfile from slots, applying overrides (Findings 1 & 5).

    Wires active_hour_fraction, peak_concurrent_queries, and avg_concurrent_queries
    from SlotUtilization, and applies config overrides if present.
    """
    if not slots or slots.total_queries == 0:
        return WorkloadProfile()

    # Base profile from slots — use cost.py's canonical window calculation
    lookback_days = _window_days(slots)

    profile = WorkloadProfile(
        has_data=True,
        total_stored_gb=sum(entity.num_bytes for entity in entities) / (1024**3),
        total_queries=slots.total_queries,
        days_sampled=slots.days_sampled,
        lookback_days=lookback_days,
        queries_per_day=slots.total_queries / lookback_days,
        monthly_scanned_tb=(slots.total_bytes_processed / (1024**4)) * (30 / max(lookback_days, 1)),
        total_slot_ms=slots.total_slot_ms,
        avg_slots=slots.avg_slots,
        peak_slots=slots.peak_slots,
        active_hour_fraction=slots.active_hour_fraction,
        # Derive concurrency from avg_slots (a proxy — slots ≈ concurrent queries for BQ on-demand)
        avg_concurrent_queries=slots.avg_slots,
        peak_concurrent_queries=slots.peak_slots,
    )

    # User-provided workload overrides win over inferred values
    if config.peak_concurrency_override is not None:
        profile.peak_concurrent_queries = float(config.peak_concurrency_override)

    if config.idle_hours_override is not None:
        # idle_hours_override is hours idle per day → active_hour_fraction = 1 - (idle/24)
        active_fraction = 1.0 - (config.idle_hours_override / 24.0)
        profile.active_hour_fraction = max(0.0, min(1.0, active_fraction))

    return profile




def _load_config(config_path: str) -> dict:
    """Load a YAML config file and return a flat dict of values."""
    path = Path(config_path)
    if not path.exists():
        console.print(f"[red]Config file not found: {config_path}[/red]")
        sys.exit(1)
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        console.print(f"[red]Invalid YAML in config file: {exc}[/red]")
        sys.exit(1)

    result: dict = {}
    gcp = raw.get("gcp", {})
    if gcp.get("project_id"):
        result["gcp_project"] = gcp["project_id"]
    if gcp.get("credentials"):
        result["credentials"] = gcp["credentials"]
    if gcp.get("use_adc") is not None:
        result["use_adc"] = bool(gcp["use_adc"])
    if gcp.get("datasets"):
        datasets = gcp["datasets"]
        result["datasets"] = ",".join(datasets) if isinstance(datasets, list) else str(datasets)

    ql = raw.get("query_logs", {})
    if ql.get("enabled") is not None:
        result["include_query_logs"] = bool(ql["enabled"])
    if ql.get("file"):
        result["query_logs"] = ql["file"]
    if ql.get("days") is not None:
        result["query_log_days"] = int(ql["days"])

    cost = raw.get("cost", {})
    if cost.get("bigquery_monthly") is not None:
        result["bigquery_monthly_cost"] = float(cost["bigquery_monthly"])
    if cost.get("reservation_config"):
        result["reservation_config"] = cost["reservation_config"]

    opts = raw.get("options", {})
    if opts.get("output"):
        result["output"] = opts["output"]
    if opts.get("format"):
        fmt = opts["format"]
        result["format"] = ",".join(fmt) if isinstance(fmt, list) else str(fmt)

    # Engine config keys (top-level and nested migration/workload blocks)
    if raw.get("target_region"):
        result["target_region"] = raw["target_region"]
    if raw.get("query_sla_ms") is not None:
        result["query_sla_ms"] = int(raw["query_sla_ms"])
    if raw.get("preferred_engine") is not None:
        result["preferred_engine"] = raw["preferred_engine"]

    migration = raw.get("migration", {})
    if migration.get("chunk_days") is not None:
        result["chunk_days"] = int(migration["chunk_days"])
    if migration.get("post_optimization") is not None:
        result["post_optimization"] = bool(migration["post_optimization"])
    if migration.get("compaction_threshold_gb") is not None:
        result["compaction_threshold_gb"] = float(migration["compaction_threshold_gb"])

    workload = raw.get("workload", {})
    if workload.get("peak_concurrency_override") is not None:
        result["peak_concurrency_override"] = workload["peak_concurrency_override"]
    if workload.get("idle_hours_override") is not None:
        result["idle_hours_override"] = float(workload["idle_hours_override"])

    return result


def _merge_config(cli_params: dict, config_values: dict) -> dict:
    """Merge CLI params with config file values. CLI args take precedence."""
    merged = dict(config_values)
    for key, value in cli_params.items():
        if value is not None:
            merged[key] = value
    return merged


def _interactive_prompts(params: dict) -> dict:
    """Prompt the user for missing values using Rich prompts."""
    console.print("\n[bold cyan]Interactive configuration mode[/bold cyan]\n")

    if not params.get("gcp_project"):
        params["gcp_project"] = Prompt.ask("GCP Project ID")

    if not params.get("credentials") and not params.get("use_adc"):
        cred_choice = Prompt.ask(
            "Authentication method",
            choices=["credentials", "adc"],
            default="adc",
        )
        if cred_choice == "credentials":
            params["credentials"] = Prompt.ask("Path to service account JSON")
        else:
            params["use_adc"] = True

    if not params.get("datasets"):
        ds = Prompt.ask("Datasets to scan (comma-separated, or empty for all)", default="")
        if ds.strip():
            params["datasets"] = ds.strip()

    if params.get("include_query_logs") and not params.get("query_logs"):
        ql = Prompt.ask("Path to exported query logs JSON (or empty for API)", default="")
        if ql.strip():
            params["query_logs"] = ql.strip()

    if params.get("include_query_logs") and not params.get("query_logs") and params.get("query_log_days") is None:
        qld = Prompt.ask("Query log lookback window in days (1-90)", default="30")
        try:
            qld_int = int(qld.strip())
            if 1 <= qld_int <= 90:
                params["query_log_days"] = qld_int
            else:
                console.print("[yellow]Out of range, using default 30 days.[/yellow]")
        except ValueError:
            console.print("[yellow]Invalid value, using default 30 days.[/yellow]")

    if params.get("bigquery_monthly_cost") is None:
        bq_cost = Prompt.ask("Monthly BigQuery cost override (or empty to calculate)", default="")
        if bq_cost.strip():
            try:
                params["bigquery_monthly_cost"] = float(bq_cost.strip())
            except ValueError:
                console.print("[yellow]Invalid cost value, will calculate automatically.[/yellow]")

    if not params.get("output"):
        params["output"] = Prompt.ask("Output directory", default="bq-migration/")

    if not params.get("format"):
        params["format"] = Prompt.ask("Output formats (html,json)", default="html")

    return params


def _engine_prompts(params: dict, has_clustering: bool = False) -> dict:
    """Interactive Rich prompts for engine configuration (fires after scan).

    Args:
        params: CLI parameters dict to check for pre-provided values
        has_clustering: Whether clustering fields were detected in any entity

    Returns:
        Dict of prompt responses for target_region, query_sla_ms,
        preferred_engine, and post_optimization (conditional)
    """
    responses: dict = {}

    # Skip prompts if values already provided via CLI
    if params.get("target_region") is None:
        region = Prompt.ask(
            "Target AWS region",
            choices=["us-east-1", "us-west-2", "eu-west-1", "ap-southeast-2", "other"],
            default="us-east-1",
        )
        if region == "other":
            region = Prompt.ask("Enter AWS region")
        responses["target_region"] = region

    if params.get("query_sla_ms") is None:
        sla_choice = Prompt.ask(
            "Query latency SLA",
            choices=["1s", "5s", "30s", "no constraint"],
            default="5s",
        )
        sla_map = {"1s": 1000, "5s": 5000, "30s": 30000, "no constraint": 60000}
        responses["query_sla_ms"] = sla_map.get(sla_choice, 5000)

    # Skip preferred_engine prompt if already set via --engine (unless it's "both")
    if not params.get("preferred_engine") and params.get("engine") in (None, "both"):
        engine = Prompt.ask(
            "Preferred query engine",
            choices=["athena", "redshift", "let tool decide"],
            default="let tool decide",
        )
        responses["preferred_engine"] = None if engine == "let tool decide" else engine

    # Only prompt for post-optimization if clustering detected AND not already set
    if has_clustering and params.get("post_optimization") is None:
        responses["post_optimization"] = Confirm.ask(
            "Include post-migration optimization steps (Spark sort/compact)?",
            default=True,
        )

    return responses




def _validate_report_params(params: dict) -> list[str]:
    """Validate output format params; return the parsed formats list. Exits on error."""
    output_format: str = params.get("format", "html")
    formats = [f.strip().lower() for f in output_format.split(",") if f.strip()]
    for fmt in formats:
        if fmt not in ("json", "html"):
            console.print(f"[red]Error: format '{fmt}' not supported. Use 'json' or 'html'.[/red]")
            sys.exit(1)
    return formats


def _validate_collect_params(params: dict) -> None:
    """Validate credential params and load reservation config. Exits on error."""
    credentials: str | None = params.get("credentials")
    use_adc: bool = params.get("use_adc", False)

    if credentials and use_adc:
        console.print("[red]Error: --credentials and --use-adc are mutually exclusive[/red]")
        sys.exit(1)
    if not credentials and not use_adc:
        console.print("[red]Error: provide --credentials or --use-adc[/red]")
        sys.exit(1)

    # Load reservation config if provided (deprecated — auto-read replaces this)
    reservation_config_path: str | None = params.get("reservation_config")
    if reservation_config_path:
        console.print(
            "[yellow]⚠ --reservation-config is deprecated. Reservation details are now "
            "auto-read during collection. Use --bigquery-monthly-cost to override the "
            "total BQ cost instead.[/yellow]"
        )
        try:
            with open(reservation_config_path, encoding="utf-8") as f:
                if reservation_config_path.endswith(".json"):
                    import json
                    params["reservation_config_data"] = json.load(f)
                else:
                    params["reservation_config_data"] = yaml.safe_load(f)
            console.print(f"[green]✓ Loaded reservation config: {reservation_config_path}[/green]")
        except Exception as exc:
            console.print(f"[yellow]⚠ Failed to load reservation config: {exc}[/yellow]")


def analyze_and_report(bundle: Bundle, params: dict) -> Assessment:
    """Run the pure-computation stages (3-16) on a Bundle and write reports.

    Works identically whether the Bundle came from an in-process collect() (assess)
    or was loaded from disk (report --bundle) — the anti-drift guarantee.
    """
    entities = bundle.entities
    failures = list(bundle.failures)
    gcp_project = bundle.project_id
    detected_location = bundle.bq_location
    slots = bundle.workload
    pricing = bundle.pricing
    storage_basis = bundle.storage_basis

    # Extract the raw YAML and CLI engine configs for precedence tracking
    yaml_engine_config = params.get("_yaml_engine_config", {})
    cli_engine_params = params.get("_cli_engine_params", {})

    bigquery_monthly_cost: float | None = params.get("bigquery_monthly_cost")
    base_output_dir: str = params.get("output", "bq-migration/")
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    project_folder = f"{gcp_project}_{run_date}"
    output_dir = str(Path(base_output_dir) / project_folder / "report")
    formats = _validate_report_params(params)

    if not entities:
        console.print("[red]Bundle contains no entities. Nothing to assess.[/red]")
        sys.exit(1)

    # ── Stage 3: SQL Surface Detection ─────────────────────────────
    console.print("\n[bold]Stage 3:[/bold] Detecting SQL surface constructs...")
    sql_analyzer = SQLSurfaceAnalyzer()
    query_log_text: list[str] | None = None

    # Anonymized statements from the bundle (queries.jsonl or in-process collection)
    if bundle.queries:
        query_log_text = [q.query for q in bundle.queries if q.query]
        console.print(f"[green]✓ {len(query_log_text)} anonymized statements available for analysis.[/green]")

    # ── Stage 3b: Query Attribution ───────────────────────────────
    # Attribute query log entries to the specific tables they reference.
    # This powers the workload display (side-by-side translations) but does NOT
    # affect the entity's complexity score — scoring uses only the entity's own
    # definition (views, routines) + the global ad-hoc bucket.
    from bq_assess.core.query_attribution import EntityWorkload, attribute_queries
    query_workload_map: dict[str, EntityWorkload] = {}
    if bundle.queries:
        known_entities = {e.full_name for e in entities}
        query_workload_map = attribute_queries(bundle.queries, known_entities, gcp_project)
        if query_workload_map:
            console.print(
                f"[green]✓ Attributed queries to {len(query_workload_map)} entities "
                f"(of {len(known_entities)} total).[/green]"
            )

    constructs_by_entity = sql_analyzer.detect_for_entities(entities, query_log_text)

    # Constructs found in the collected workload (application/BI queries) live in
    # the __ad_hoc__ bucket — not owned by any entity, surfaced on the report's
    # Query Complexity tab as a workload-level finding (R10.5).
    workload_constructs = sorted(
        c.construct_class for c in constructs_by_entity.pop("__ad_hoc__", [])
    )
    console.print(f"[green]✓ Detected SQL constructs for {len(constructs_by_entity)} entities.[/green]")
    if workload_constructs:
        console.print(
            f"[green]✓ Workload query logs contain: {', '.join(workload_constructs)}.[/green]"
        )

    # ── Stage 4: Iceberg Conversion ────────────────────────────────
    console.print("\n[bold]Stage 4:[/bold] Converting TABLE entities to Iceberg schemas...")
    converter = IcebergConverter()
    conversion_results: dict[str, object] = {}

    table_entities = [e for e in entities if e.population == EntityPopulation.TABLE]
    for entity in table_entities:
        try:
            result = converter.convert(entity)
            conversion_results[entity.full_name] = result
            if not result.success:
                failures.append(FailureRecord(
                    entity_name=entity.full_name,
                    stage="convert",
                    error="; ".join(result.warnings),
                ))
        except Exception as exc:
            console.print(f"[yellow]⚠ Conversion failed for {entity.full_name}: {exc}[/yellow]")
            failures.append(FailureRecord(
                entity_name=entity.full_name,
                stage="convert",
                error=str(exc),
            ))

    console.print(f"[green]✓ Converted {len(conversion_results)} schemas.[/green]")

    # ── Stage 4b: Engine Configuration Prompts ─────────────────────
    # Fire interactive prompts if enabled and TTY is available
    prompt_responses: dict = {}
    if params.get("interactive") and sys.stdout.isatty():
        # Detect clustering fields across all entities
        has_clustering = any(
            entity.clustering_fields for entity in entities
        )
        console.print("\n[bold cyan]Engine configuration[/bold cyan]")
        prompt_responses = _engine_prompts(params, has_clustering=has_clustering)

    # ── Stage 5: Score Effort ──────────────────────────────────────
    console.print("\n[bold]Stage 5:[/bold] Scoring migration effort for TABLE entities...")
    effort_scorer = EffortScorer()
    effort_results: dict[str, EffortResult] = {}

    for entity in table_entities:
        try:
            conversion = conversion_results.get(entity.full_name)
            if conversion:
                result = effort_scorer.score(entity, conversion)
                effort_results[entity.full_name] = result
        except Exception as exc:
            console.print(f"[yellow]⚠ Effort scoring failed for {entity.full_name}: {exc}[/yellow]")
            failures.append(FailureRecord(
                entity_name=entity.full_name,
                stage="score_effort",
                error=str(exc),
            ))

    console.print(f"[green]✓ Scored effort for {len(effort_results)} tables.[/green]")

    # ── Stage 6: Relationships ─────────────────────────────────────
    console.print("\n[bold]Stage 6:[/bold] Inferring table relationships...")
    inferrer = RelationshipInferrer()

    # View SQL is already scanned — feed it to the JOIN-clause inference path
    # (was passed as None until the 2026-07-08 collector/report split; SCRUM_NOTES).
    view_definitions = {
        e.full_name: (e.view_query or e.mview_query)
        for e in entities
        if e.view_query or e.mview_query
    }

    try:
        rel_result = inferrer.infer(entities, query_analysis=None, view_definitions=view_definitions or None)
        console.print(
            f"[green]✓ Found {len(rel_result.relationships)} relationships.[/green]"
        )
    except Exception as exc:
        console.print(f"[yellow]⚠ Relationship inference failed: {exc}[/yellow]")
        rel_result = None

    # ── Stage 7: Score Complexity ──────────────────────────────────
    console.print("\n[bold]Stage 7:[/bold] Scoring query complexity...")
    complexity_scorer = ComplexityScorer()
    complexity_results: dict[str, ComplexityResult] = {}
    has_query_logs = bool(bundle.queries)
    dep_counts = ComplexityScorer.build_dep_counts(rel_result)

    with Progress(
        SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
        BarColumn(), TaskProgressColumn(), console=console,
    ) as progress:
        task = progress.add_task("Scoring complexity...", total=len(entities))
        for entity in entities:
            try:
                constructs = constructs_by_entity.get(entity.full_name, [])
                result = complexity_scorer.score(
                    entity, constructs, has_logs=has_query_logs,
                    dep_counts=dep_counts,
                    # Attributed production queries ARE this entity's observed
                    # SQL surface — without this, every actively-queried table
                    # showed LOW/schema-only beside its own analyzed queries.
                    has_attributed_queries=entity.full_name in query_workload_map,
                )
                complexity_results[entity.full_name] = result
            except Exception as exc:
                console.print(f"[yellow]⚠ Complexity scoring failed for {entity.full_name}: {exc}[/yellow]")
                failures.append(FailureRecord(
                    entity_name=entity.full_name,
                    stage="score_complexity",
                    error=str(exc),
                ))
            progress.advance(task)

    console.print(f"[green]✓ Scored complexity for {len(complexity_results)} entities.[/green]")

    # ── Stage 8: Region + Rates Replay ─────────────────────────────
    # Re-point the rate tables at the Source's geography recorded in the bundle, then
    # apply the collection-time rate snapshot. Fully offline — the report side never
    # hits GCP or the pricing APIs unless --refresh-pricing explicitly asks for it.
    console.print("\n[bold]Stage 8:[/bold] Applying region and pricing snapshot...")
    from bq_assess.core import pricing_constants as _v4
    from bq_assess.engine.redshift import cost_constants as _k
    if _v4.apply_bq_region(detected_location):
        console.print(f"[green]✓ BigQuery priced for region: {detected_location}[/green]")
    else:
        console.print(
            f"[yellow]⚠ No verified rate table for BigQuery location "
            f"'{detected_location}' — using US multi-region rates (may understate cost).[/yellow]"
        )
    if bundle.aws_region:
        aws_region = bundle.aws_region
        is_fallback = False
    else:
        aws_region, is_fallback = _k.bq_location_to_aws_region(detected_location)
    _k.apply_aws_region(aws_region)
    if is_fallback:
        console.print(
            f"[yellow]⚠ BigQuery location '{detected_location}' has no AWS mapping — "
            f"defaulting to us-east-1 pricing; override with --target-region[/yellow]"
        )
    else:
        console.print(f"[green]✓ AWS priced for region: {aws_region}[/green]")

    if params.get("refresh_pricing", False):
        try:
            price_lookup = PriceLookup(
                aws_region=aws_region, bq_location=detected_location,
                use_cache=not params.get("no_cache", True),
            )
            # No GCP client in report mode — only the AWS half (public Price List
            # API) can actually refresh; say so rather than claiming both.
            with console.status("[dim]Fetching live AWS pricing…[/dim]", spinner="dots"):
                live_rates = fetch_live_rates_with_timeout(price_lookup, gcp_client=None)
            if live_rates.is_live:
                # Bundle snapshot first (GCP half), then the fresh AWS fetch on top —
                # reversed order would let the older snapshot clobber the refresh.
                _apply_bundle_rates(bundle, aws_region, detected_location)
                apply_live_rates(live_rates)
                console.print(
                    f"[green]✓ AWS pricing refreshed ({live_rates.aws.fetched_at}).[/green] "
                    f"[dim]GCP rates cannot be refreshed offline — using bundle snapshot/regional rates.[/dim]"
                )
            else:
                console.print("[dim]  Refresh returned no live rates — falling back to bundle snapshot.[/dim]")
                _apply_bundle_rates(bundle, aws_region, detected_location)
        except PricingTimeout as exc:
            console.print(f"[yellow]⚠ {exc} — falling back to bundle snapshot.[/yellow]")
            _apply_bundle_rates(bundle, aws_region, detected_location)
        except Exception as exc:
            console.print(f"[dim]  Pricing refresh skipped: {exc}[/dim]")
            _apply_bundle_rates(bundle, aws_region, detected_location)
    else:
        _apply_bundle_rates(bundle, aws_region, detected_location)

    # ── Stage 10: Cost Estimation ──────────────────────────────────
    console.print("\n[bold]Stage 10:[/bold] Estimating costs...")
    if pricing:
        cost_estimator = CostEstimator(skip_live_pricing=True)
        effort_total = sum(er.score for er in effort_results.values())

        try:
            # Long-term storage anchors to collection time, not report time —
            # a bundle re-processed months later must not drift tables long-term.
            as_of = None
            if bundle.created_at:
                try:
                    as_of = datetime.fromisoformat(bundle.created_at.replace("Z", "+00:00"))
                except ValueError:
                    pass
            cost_comparison = cost_estimator.estimate(
                entities, pricing, slots, bigquery_monthly_cost, effort_total,
                location=detected_location,
                storage_basis=storage_basis,
                as_of=as_of,
                egress_gib=bundle.egress_gib,
            )
            # Multi-region caveat (2026-07-28 review): the v2 collector merges
            # all regions' bytes and workload into the totals, but both clouds
            # are priced at the PRIMARY region's rates. Without this note the
            # report asserts a single-region mapping over multi-region data.
            other_regions = [r for r in bundle.regions if r != bundle.bq_location]
            if other_regions:
                cost_comparison.pricing_notes.append(
                    f"Multi-region Source: datasets also live in "
                    f"{', '.join(other_regions)}, and their storage and workload are "
                    f"INCLUDED in the totals — but both clouds are priced at the "
                    f"primary region's rates ({bundle.bq_location} / "
                    f"{bundle.aws_region}). Regional rate differences are not "
                    f"reflected; treat the comparison as primary-region pricing "
                    f"applied to the whole estate."
                )
            console.print("[green]✓ Cost estimation complete.[/green]")
        except Exception as exc:
            console.print(f"[yellow]⚠ Cost estimation failed: {exc}[/yellow]")
            # Fix 2: Create sentinel CostComparison on failure
            cost_comparison = CostComparison(
                bq_pricing_model=BQPricingModel.ON_DEMAND,
                bigquery_monthly=0.0,
                bigquery_breakdown=[],
                aws_lines=[],
                aws_monthly_low=0.0,
                aws_monthly_high=0.0,
                monthly_delta_low=0.0,
                monthly_delta_high=0.0,
                annual_savings_low=0.0,
                annual_savings_high=0.0,
                migration_onetime=0.0,
                breakeven_months_low=9999.0,
                breakeven_months_high=9999.0,
                compute_confidence=ConfidenceLevel.LOW,
            )
    else:
        console.print("[yellow]⚠ Skipping cost estimation (no pricing data).[/yellow]")
        # Fix 2: Create sentinel CostComparison when no pricing data
        cost_comparison = CostComparison(
            bq_pricing_model=BQPricingModel.UNKNOWN,
            bigquery_monthly=0.0,
            bigquery_breakdown=[],
            aws_lines=[],
            aws_monthly_low=0.0,
            aws_monthly_high=0.0,
            monthly_delta_low=0.0,
            monthly_delta_high=0.0,
            annual_savings_low=0.0,
            annual_savings_high=0.0,
            migration_onetime=0.0,
            breakeven_months_low=9999.0,
            breakeven_months_high=9999.0,
            compute_confidence=ConfidenceLevel.LOW,
        )

    # ── Stage 11: (removed — load_sync_dml superseded by engine/athena/migration) ──

    # ── Stage 11b: Engine Recommendation (moved before translation so Stages 12/13 are engine-aware) ──
    console.print("\n[bold]Stage 11b:[/bold] Running engine recommendation...")
    from bq_assess.core.engine_config import resolve_engine_config
    from bq_assess.engine.recommendation import RecommendationScorer

    # Map CLI "both" sentinel to None so RecommendationScorer runs its 8-signal analysis
    # instead of treating "both" as a user-specified override.
    engine_param = params.get("engine")

    # Distinguish an explicit --engine flag from the Click default so CLI keeps
    # precedence over YAML even when the value maps to no-preference
    engine_explicitly_provided = False
    try:
        ctx = click.get_current_context()
        engine_explicitly_provided = ctx.get_parameter_source("engine") == click.core.ParameterSource.COMMANDLINE
    except RuntimeError:
        # No active Click context (e.g., when called from tests) — treat as not explicitly provided
        pass

    # Infer target_region from pricing detection's region (Stage 8) when user provided no explicit region
    inferred_params: dict = {}
    if not params.get("target_region") and aws_region:
        inferred_params["target_region"] = aws_region
        # Thread the fallback signal so source tracking reflects "fallback" not "inferred"
        inferred_params["_region_is_fallback"] = is_fallback

    # Build CLI params for engine config: use tracked CLI-only values to respect precedence
    cli_config_params = dict(cli_engine_params)
    # Engine param needs special handling for "both" → None mapping
    if engine_param is not None:
        cli_config_params["preferred_engine"] = None if engine_param == "both" else engine_param
    cli_config_params["_engine_cli_provided"] = engine_explicitly_provided

    engine_config = resolve_engine_config(
        cli_params=cli_config_params,
        yaml_config=yaml_engine_config,
        prompt_responses=prompt_responses,
        inferred=inferred_params,
    )

    # Build workload profile from slots data (Findings 1 & 5)
    workload_profile = _build_workload_profile(slots, entities, engine_config)

    recommendation_scorer = RecommendationScorer()
    engine_recommendation = recommendation_scorer.recommend(workload_profile, engine_config)
    console.print(f"[green]✓ Recommended engine: {engine_recommendation.primary_engine} (confidence: {engine_recommendation.confidence:.0%})[/green]")

    # ── Engine toolkit selection (Fix 7: consolidated dispatch) ────────────────────
    # Select rewrite guide, placement advisor, and label ONCE based on recommended engine.
    if engine_recommendation.primary_engine == TargetEngine.ATHENA:
        from bq_assess.engine.athena.placement import AthenaPlacementAdvisor
        from bq_assess.engine.athena.rewrite import AthenaRewriteGuide
        rewrite_guide = AthenaRewriteGuide()
        _placement_advisor: AthenaPlacementAdvisor | PlacementAdvisor = AthenaPlacementAdvisor()
        _engine_label = "Athena"
        _is_athena = True
    else:
        rewrite_guide = RewriteGuide()
        _placement_advisor = PlacementAdvisor()
        _engine_label = "Redshift"
        _is_athena = False

    # ── Stage 12: Rewrite Guidance (engine-aware) ─────────────────────────────────
    console.print("\n[bold]Stage 12:[/bold] Generating rewrite guidance...")

    guidance_results: dict[str, list[str]] = {}

    for entity in entities:
        try:
            constructs = constructs_by_entity.get(entity.full_name, [])
            if constructs:
                guidance = rewrite_guide.guide(entity, constructs)
                guidance_results[entity.full_name] = guidance
        except Exception as exc:
            console.print(f"[yellow]⚠ Guidance generation failed for {entity.full_name}: {exc}[/yellow]")

    console.print(f"[green]✓ Generated guidance for {len(guidance_results)} entities ({_engine_label}).[/green]")

    # ── Stage 12b: Best-Effort SQL Translation (engine-aware) ─────────────────────
    translation_results: dict[str, TranslationResult] = {}

    if params.get("skip_translation"):
        console.print("\n[bold]Stage 12b:[/bold] SQL translation [yellow]skipped[/yellow] (--skip-translation).")
    else:
        console.print(f"\n[bold]Stage 12b:[/bold] Translating SQL to {_engine_label}...")
        translation_cache: dict[str, TranslationResult] = {}

        with Progress(
            SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
            BarColumn(), TaskProgressColumn(), console=console,
        ) as progress:
            task = progress.add_task("Translating...", total=len(entities))
            for entity in entities:
                try:
                    # A JAVASCRIPT routine's body is JS, not SQL — translating it
                    # through a SQL transpiler just echoes the JS (or garbage) back.
                    # Emit an explicit not-translatable result instead.
                    if entity.routine is not None and entity.routine.language == "JAVASCRIPT":
                        translation_results[entity.full_name] = TranslationResult(
                            redshift_sql=(
                                "-- Not translatable: JavaScript UDF (body is JavaScript, not SQL).\n"
                                "-- Rewrite by hand: "
                                + ("as a SQL expression or an Athena/Spark job."
                                   if _is_athena else
                                   "as a SQL UDF or a Redshift Lambda UDF (Node.js).")
                            ),
                            confidence="LOW",
                            warnings=["BLOCKER: JavaScript UDF — manual rewrite required"],
                            target_engine="athena" if _is_athena else "redshift",
                        )
                        progress.advance(task)
                        continue
                    sql = entity.view_query or entity.mview_query or (entity.routine.body if entity.routine else None)
                    if sql:
                        if sql in translation_cache:
                            translation_results[entity.full_name] = translation_cache[sql]
                        else:
                            if _is_athena:
                                engine_rewrite = rewrite_guide.translate(sql)
                                merged_warnings = engine_rewrite.warnings + [
                                    f"BLOCKER: {c}" for c in engine_rewrite.unsupported_constructs
                                ]
                                result = TranslationResult(
                                    redshift_sql=engine_rewrite.translated_sql,
                                    confidence=engine_rewrite.confidence,
                                    warnings=merged_warnings,
                                    target_engine="athena",
                                )
                            else:
                                result = rewrite_guide.translate(sql)
                            translation_cache[sql] = result
                            translation_results[entity.full_name] = result
                except Exception as exc:
                    console.print(f"[yellow]⚠ Translation failed for {entity.full_name}: {exc}[/yellow]")
                progress.advance(task)

        console.print(f"[green]✓ Translated SQL for {len(translation_results)} entities ({_engine_label}).[/green]")

    # ── Stage 13: Placement (engine-aware) ────────────────────────────────────────
    console.print("\n[bold]Stage 13:[/bold] Recommending placement for REBUILT entities...")
    placement_results: dict[str, PlacementRecommendation] = {}

    rebuilt_entities = [e for e in entities if e.population == EntityPopulation.REBUILT]
    for entity in rebuilt_entities:
        try:
            if _is_athena:
                athena_placement = _placement_advisor.recommend(entity, has_logs=has_query_logs)
                if athena_placement:
                    # Map EnginePlacement → PlacementRecommendation for downstream compat
                    placement_results[entity.full_name] = PlacementRecommendation(
                        home=athena_placement.home,
                        signals=athena_placement.signals,
                        confidence=ConfidenceLevel[athena_placement.confidence] if isinstance(athena_placement.confidence, str) else athena_placement.confidence,
                        refresh_unverified=False,
                    )
            else:
                placement = _placement_advisor.recommend(entity, rel_result, has_query_logs)
                if placement:
                    placement_results[entity.full_name] = placement
        except Exception as exc:
            console.print(f"[yellow]⚠ Placement recommendation failed for {entity.full_name}: {exc}[/yellow]")

    console.print(f"[green]✓ Recommended placement for {len(placement_results)} entities ({_engine_label}).[/green]")

    # ── Stage 13a: Storage Placement (ADR-0005, Redshift path only) ──────────
    # Iceberg stays the default for every entity; RMS is a per-entity hot-tier
    # exception, only reachable when Redshift is the primary Query Engine
    # (Athena cannot query RMS-native tables). RMS entities get a two-phase
    # load, so their effort is re-scored (+1 rms_two_phase_load).
    storage_placement_results: dict[str, StoragePlacement] = {}
    if not _is_athena:
        from bq_assess.engine.redshift.storage_placement import StoragePlacementAdvisor
        from bq_assess.scoring.effort import amend_for_rms_placement
        console.print("\n[bold]Stage 13a:[/bold] Recommending storage placement (Iceberg vs RMS)...")
        storage_advisor = StoragePlacementAdvisor(query_sla_ms=engine_config.query_sla_ms)
        rms_count = 0
        for entity in table_entities:
            try:
                sp = storage_advisor.recommend(entity, conversion_results.get(entity.full_name))
                storage_placement_results[entity.full_name] = sp
                if sp.target == StorageTarget.RMS:
                    rms_count += 1
                    if entity.full_name in effort_results:
                        effort_results[entity.full_name] = amend_for_rms_placement(
                            effort_results[entity.full_name]
                        )
            except Exception as exc:
                console.print(f"[yellow]⚠ Storage placement failed for {entity.full_name}: {exc}[/yellow]")
        console.print(
            f"[green]✓ Storage placement: {len(storage_placement_results) - rms_count} Iceberg, "
            f"{rms_count} RMS (hot-tier exception).[/green]"
        )
        # Stage 10 priced migration effort BEFORE the RMS amendments above — reprice
        # so the cost summary agrees with the amended per-entity effort cards, and
        # split the storage line: RMS-resident bytes bill as RMS (billed separately
        # by GB/month on both Serverless and Provisioned), not S3 Tables. Two pools:
        # RMS-placed tables (Stage 13a) and Redshift-homed MVs (Stage 13 — a native
        # Redshift MV stores its materialized result set in RMS).
        if pricing:
            from bq_assess.engine.redshift.cost import (
                apply_rms_storage_split,
                collect_rms_bytes,
                reprice_migration_effort,
            )
            if rms_count:
                reprice_migration_effort(
                    cost_comparison, sum(er.score for er in effort_results.values())
                )
            table_rms_bytes, mv_rms_bytes = collect_rms_bytes(
                entities, storage_placement_results, placement_results
            )
            if table_rms_bytes or mv_rms_bytes:
                total_physical_bytes = sum(
                    effective_physical_bytes(e.num_bytes, e.physical_bytes)
                    for e in entities
                )
                apply_rms_storage_split(
                    cost_comparison, table_rms_bytes, total_physical_bytes,
                    mv_physical_bytes=mv_rms_bytes,
                )
                console.print(
                    f"[green]✓ RMS storage line: "
                    f"{table_rms_bytes / 1024**3:.1f} GB RMS-placed tables + "
                    f"{mv_rms_bytes / 1024**3:.1f} GB Redshift-native MVs.[/green]"
                )

    # ── Stage 13b: Engine-Aware Cost Comparison ──────────────────────────────
    # Assemble engine-aware cost comparison with all scenarios in ONE pass (Fix 1/3/4/5)
    # The Athena scenario is built whenever workload data exists, regardless of pricing.
    # When pricing is None (offline bundle without pricing), cost_comparison is a sentinel
    # and we skip the assembly — the engine recommendation still renders.
    if workload_profile.has_data and cost_comparison.bq_pricing_model != BQPricingModel.UNKNOWN:
        from bq_assess.engine.athena.cost import AthenaCostEstimator
        athena_estimator = AthenaCostEstimator()
        # AthenaCostEstimator.estimate_cost needs pricing only for the type annotation —
        # it uses workload_profile for the actual computation. Safe to call always.
        athena_estimate = athena_estimator.estimate_cost(workload_profile, pricing or PricingDetection(
            model=BQPricingModel.UNKNOWN, confidence=ConfidenceLevel.LOW, source_note="offline",
        ))

        cost_comparison = assemble_cost_comparison(
            base_comparison=cost_comparison,
            athena_estimate=athena_estimate,
            workload_profile=workload_profile,
            engine_recommendation=engine_recommendation,
        )
        console.print(f"[green]✓ Athena one-time OPTIMIZE cost: ${float(athena_estimate.one_time_migration):,.2f} (upper bound).[/green]")
        console.print(f"[green]✓ Unified recommendation: {cost_comparison.recommendation.recommended_scenario}[/green]")
    elif not workload_profile.has_data:
        console.print("[yellow]⚠ No workload data — Query Engine cost comparison skipped; engine recommendation above is signal-based only.[/yellow]")

    # ── Stage 13c: Athena Assessment ──────────────────────────────────
    console.print("\n[bold]Stage 13c:[/bold] Running Athena assessment...")
    from bq_assess.engine.athena.migration import (
        AthenaMigrationGenerator,
        generate_source_db_setup,
    )

    # Generate source database setup DDL (one-time prerequisite for all INSERTs)
    datasets_used = sorted({e.dataset_id for e in table_entities})
    source_db_setup: list[str] = []
    connector_name: str | None = None
    if datasets_used:
        primary_dataset = datasets_used[0]
        setup_stmts, connector_name = generate_source_db_setup(
            dataset_id=primary_dataset,
            gcp_project=gcp_project,
            tables=table_entities,
            target_region=engine_config.target_region,
        )
        source_db_setup = setup_stmts

    athena_migration = AthenaMigrationGenerator(connector_name=connector_name)

    # Generate migration DML for all tables
    migration_plans: dict[str, MigrationDML] = {}
    for entity in table_entities:
        conversion = conversion_results.get(entity.full_name)
        if conversion:
            try:
                migration_dml = athena_migration.generate(entity, conversion, engine_config)
                migration_plans[entity.full_name] = migration_dml
            except Exception as exc:
                console.print(f"[yellow]⚠ Athena migration generation failed for {entity.full_name}: {exc}[/yellow]")

    console.print(f"[green]✓ Generated Athena migration plans for {len(migration_plans)} tables.[/green]")

    # ── Stage 14: Assemble Assessment ──────────────────────────────
    console.print("\n[bold]Stage 14:[/bold] Assembling assessment report...")

    # Build summary
    effort_counts = {"AUTO": 0, "ASSISTED": 0, "MANUAL": 0}
    complexity_counts = {"PORTABLE": 0, "ADAPT": 0, "REWRITE": 0}
    total_size_gb = 0.0
    total_logical_size_gb = 0.0

    for entity in entities:
        total_size_gb += effective_physical_bytes(entity.num_bytes, entity.physical_bytes) / (1024 ** 3)
        total_logical_size_gb += entity.num_bytes / (1024 ** 3)

        if entity.full_name in effort_results:
            effort = effort_results[entity.full_name]
            effort_counts[effort.category.value] += 1

        if entity.full_name in complexity_results:
            comp = complexity_results[entity.full_name]
            complexity_counts[comp.category.value] += 1

    # Determine overall SQL surface confidence
    complexity_confidences = [cr.confidence for cr in complexity_results.values()]
    if not complexity_confidences:
        sql_confidence = ConfidenceLevel.LOW
    elif any(c == ConfidenceLevel.HIGH for c in complexity_confidences):
        sql_confidence = ConfidenceLevel.HIGH
    elif any(c == ConfidenceLevel.MEDIUM for c in complexity_confidences):
        sql_confidence = ConfidenceLevel.MEDIUM
    else:
        sql_confidence = ConfidenceLevel.LOW

    summary = AssessmentSummary(
        total_entities=len(entities),
        total_tables=len(table_entities),
        total_size_gb=round(total_size_gb, 4),
        effort_counts=effort_counts,
        complexity_counts=complexity_counts,
        sql_surface_confidence=sql_confidence,
        total_logical_size_gb=round(total_logical_size_gb, 4),
        workload_constructs=workload_constructs,
    )

    # Build entity reports
    entity_reports = []
    for entity in entities:
        effort = effort_results.get(entity.full_name)
        conversion = conversion_results.get(entity.full_name)
        complexity = complexity_results.get(entity.full_name)
        guidance = guidance_results.get(entity.full_name, [])
        placement = placement_results.get(entity.full_name)
        wl = query_workload_map.get(entity.full_name)

        entity_reports.append(EntityReport(
            full_name=entity.full_name,
            entity_type=entity.entity_type,
            population=entity.population,
            rows=entity.num_rows,
            size_gb=round(entity.num_bytes / (1024 ** 3), 4),
            depends_on=entity.depends_on,
            effort=effort,
            conversion=conversion,
            load_sync_dml=None,  # deprecated: superseded by engine/athena/migration
            complexity=complexity,
            rewrite_guidance=guidance,
            translated_sql=translation_results.get(entity.full_name),
            placement=placement,
            physical_bytes=entity.physical_bytes,
            storage_placement=storage_placement_results.get(entity.full_name),
            query_workload={
                "query_count": wl.query_count,
                "total_slot_ms": wl.total_slot_ms,
                "slot_hours": wl.slot_hours,
                "num_shapes": wl.num_shapes,
                "statement_types": wl.statement_types,
            } if wl else None,
        ))

    # Generate assessment ID
    now = datetime.now(timezone.utc)
    hash_input = f"{gcp_project}-{now.isoformat()}"
    short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:8]
    assessment_id = f"assess-{now.strftime('%Y%m%d')}-{short_hash}"

    assessment = Assessment(
        assessment_id=assessment_id,
        generated_at=now,
        project_id=gcp_project,
        summary=summary,
        cost=cost_comparison,
        entities=entity_reports,
        failures=failures,
        engine_recommendation=engine_recommendation,
        migration_plans=migration_plans,
        source_db_setup=source_db_setup or None,
    )

    console.print("[green]✓ Assessment assembled.[/green]")

    # ── Stage 14b: Translate Query Workload Samples ───────────────────
    # Translate the top-N query samples per entity for the report's side-by-side view.
    # Uses the recommended engine (redshift or athena) for the translation target.
    translated_workloads: dict[str, dict] | None = None
    pe = engine_recommendation.primary_engine if engine_recommendation else "redshift"
    workload_target_engine = pe.value if hasattr(pe, "value") else str(pe)
    if query_workload_map:
        from bq_assess.core.query_translator import translate_query
        target_engine = workload_target_engine
        console.print(f"\n[bold]Stage 14b:[/bold] Translating query workload samples (target: {target_engine})...")
        translated_workloads = {}
        # Entities beyond the sample cap have empty samples but keep their
        # workload stats — they still show the workload column in the report.
        for entity_name, wl in query_workload_map.items():
            translated_workloads[entity_name] = {
                "query_count": wl.query_count,
                "total_slot_ms": wl.total_slot_ms,
                "slot_hours": wl.slot_hours,
                "num_shapes": wl.num_shapes,
                "statement_types": wl.statement_types,
                "samples": [
                    {
                        "query": s.query,
                        "translated": translate_query(s.query, engine=target_engine),
                        "statement_type": s.statement_type,
                        "total_slot_ms": s.total_slot_ms,
                    }
                    for s in wl.samples
                ],
            }
        console.print(f"[green]✓ Translated samples for {len(translated_workloads)} entities.[/green]")

    # ── Stage 15: Write Deliverables ─────────────────────────────────
    console.print("\n[bold]Stage 15:[/bold] Writing deliverables...")

    project_dir = str(Path(base_output_dir) / project_folder)

    # Ensure output directory exists
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    output_files: list[str] = []

    if "json" in formats:
        json_writer = JSONWriter()
        paths = json_writer.write(assessment, output_dir)
        output_files.extend(paths)
        for path in paths:
            console.print(f"  [green]✓ JSON report: {path}[/green]")

    if "html" in formats:
        html_writer = HTMLWriter()
        paths = html_writer.write(
            assessment, output_dir,
            storage_basis=storage_basis,
            query_workloads=translated_workloads,
        )
        output_files.extend(paths)
        for path in paths:
            console.print(f"  [green]✓ HTML report: {path}[/green]")

    # Query-workload sidecar: EVERY attributed shape translated, one .sql per
    # entity — the HTML embeds only the top 5, the rest live here (+ INDEX.csv).
    workload_sidecar_dir: str | None = None
    if query_workload_map:
        from bq_assess.report.workload_writer import write_workload_sidecar
        workload_sidecar_dir = write_workload_sidecar(
            query_workload_map, workload_target_engine, project_dir,
        )
        if workload_sidecar_dir:
            output_files.append(workload_sidecar_dir)
            console.print(f"  [green]✓ Query workload: {workload_sidecar_dir}[/green]")

    # Terraform infrastructure files
    from bq_assess.engine.athena.terraform import generate_terraform
    datasets_used = sorted({e.dataset_id for e in table_entities})
    if datasets_used:
        tf_dir = generate_terraform(
            project_dir=project_dir,
            dataset_id=datasets_used[0],
            gcp_project=gcp_project,
            tables=table_entities,
            target_region=engine_config.target_region,
        )
        output_files.append(tf_dir)
        console.print(f"  [green]✓ Terraform: {tf_dir}[/green]")

    # Migration scripts (plan.json + run_migration.py)
    if migration_plans:
        from bq_assess.engine.athena.migration_scripts import generate_migration_scripts
        from bq_assess.engine.athena.naming import workgroup_name as _wg_name
        # Same derivation generate_terraform uses (single source: engine/athena/naming)
        tf_workgroup = _wg_name(datasets_used[0]) if datasets_used else None
        mig_dir = generate_migration_scripts(
            project_dir=project_dir,
            migration_plans=migration_plans,
            connector_name=connector_name,
            target_region=engine_config.target_region,
            workgroup_name=tf_workgroup,
            conversion_results=conversion_results,
            storage_placements=storage_placement_results,
            rebuilt_entities=[
                e for e in entities if e.population is EntityPopulation.REBUILT
            ],
            translation_results=translation_results,
            dataset_id=datasets_used[0] if datasets_used else None,
        )
        output_files.append(mig_dir)
        console.print(f"  [green]✓ Migration scripts: {mig_dir}[/green]")

    # Bundle export (replaces the pre-0.3 metadata/ export — the bundle is a strict
    # superset and is re-processable by `bq-assess report`).
    if params.get("export_bundle", True):
        writer = BundleWriter()
        bundle_dir = writer.write(bundle, project_dir)
        if params.get("zip_bundle"):
            # Same hand-off artifact bq-collect --zip produces: the tree is
            # replaced by a zip (here <project_dir>/bundle.zip — the project
            # folder itself holds the report/terraform/migration siblings).
            import shutil

            from bq_assess.bundle.writer import zip_bundle_dir
            zip_path = zip_bundle_dir(
                bundle_dir,
                zip_path=str(Path(project_dir) / "bundle.zip"),
                root_name="",
            )
            shutil.rmtree(bundle_dir)
            output_files.append(zip_path)
            console.print(f"  [green]✓ Bundle exported (zipped): {zip_path}[/green]")
        else:
            output_files.append(bundle_dir)
            console.print(f"  [green]✓ Bundle exported: {bundle_dir}[/green]")

    # Customer-facing README at the project root. Skipped in multi-project mode
    # (2026-08-03): the fleet writes ONE top-level README next to SUMMARY.html
    # instead of near-identical per-project copies.
    if params.get("_multi_project"):
        _print_summary(assessment, output_files)
        return assessment
    from bq_assess.report.readme_writer import write_readme
    readme_path = write_readme(
        project_dir=project_dir,
        gcp_project=gcp_project,
        has_report="html" in formats or "json" in formats,
        has_terraform=bool(datasets_used),
        has_migration=bool(migration_plans),
        has_bundle=params.get("export_bundle", True),
        has_rebuilt_entities=bool(rebuilt_entities),
        has_redshift_phase=bool(
            storage_placement_results and any(
                p.target == StorageTarget.RMS
                for p in storage_placement_results.values()
            )
        ),
        has_query_workload=bool(workload_sidecar_dir),
    )
    output_files.append(readme_path)
    console.print(f"  [green]✓ README: {readme_path}[/green]")

    # ── Stage 16: Terminal Summary ─────────────────────────────────
    _print_summary(assessment, output_files)
    return assessment


def _apply_bundle_rates(bundle: Bundle, aws_region: str, bq_location: str) -> None:
    """Apply the bundle's collection-time rate snapshot (offline pricing replay).

    apply_live_rates gates per half: only genuinely LIVE halves overwrite the
    region-cascaded constants Stage 8 installed. A hardcoded-fallback half was
    captured from the collector's un-cascaded default constants — applying it
    would price the report in the wrong geography (the Sydney-as-US class).
    """
    if not bundle.rates:
        console.print("[dim]  No rate snapshot in bundle — using region-adjusted hardcoded rates.[/dim]")
        return
    try:
        rates = rates_from_dict(
            bundle.rates, default_aws_region=aws_region, default_bq_location=bq_location
        )
        # apply_live_rates reports which halves it actually applied — the message
        # derives from that, never from a re-derived live-ness predicate.
        aws_live, gcp_live = apply_live_rates(rates)
        if aws_live and gcp_live:
            console.print(
                f"[green]✓ Bundle rate snapshot applied "
                f"(AWS: {rates.aws.fetched_at}, GCP: {rates.gcp.fetched_at}).[/green]"
            )
        elif aws_live or gcp_live:
            live_half = "AWS" if aws_live else "GCP"
            console.print(
                f"[yellow]⚠ Bundle snapshot is part-live — applied {live_half} half only; "
                f"the other side uses region-adjusted hardcoded rates.[/yellow]"
            )
        else:
            console.print(
                "[dim]  Bundle snapshot carries hardcoded fallback rates — "
                "keeping region-adjusted rates instead.[/dim]"
            )
    except Exception as exc:
        console.print(f"[yellow]⚠ Could not apply bundle rate snapshot: {exc} — using hardcoded rates.[/yellow]")


def _print_summary(assessment: Assessment, output_files: list[str]) -> None:
    """Print a Rich summary table to the terminal."""
    console.print("\n")
    console.rule("[bold cyan]Assessment Summary[/bold cyan]")

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Metric", style="bold")
    table.add_column("Value")

    table.add_row("Total entities scanned", str(assessment.summary.total_entities))
    table.add_row("Total tables", str(assessment.summary.total_tables))
    table.add_row("Total data size (BigQuery logical)", f"{assessment.summary.total_logical_size_gb:.2f} GB")
    table.add_row("Projected size on S3 Iceberg", f"{assessment.summary.total_size_gb:.2f} GB")

    effort_counts = assessment.summary.effort_counts
    table.add_row(
        "Migration effort",
        f"[green]AUTO: {effort_counts['AUTO']}[/green]  "
        f"[yellow]ASSISTED: {effort_counts['ASSISTED']}[/yellow]  "
        f"[red]MANUAL: {effort_counts['MANUAL']}[/red]",
    )

    complexity_counts = assessment.summary.complexity_counts
    table.add_row(
        "Query complexity",
        f"[green]PORTABLE: {complexity_counts['PORTABLE']}[/green]  "
        f"[yellow]ADAPT: {complexity_counts['ADAPT']}[/yellow]  "
        f"[red]REWRITE: {complexity_counts['REWRITE']}[/red]",
    )

    if assessment.cost:
        cost = assessment.cost
        if cost.monthly_delta_low == cost.monthly_delta_high:
            delta_str = f"${cost.monthly_delta_low:,.2f}"
        else:
            delta_str = f"${cost.monthly_delta_low:,.2f} - ${cost.monthly_delta_high:,.2f}"

        table.add_row("Monthly cost delta", delta_str)

        confidence_color = {"LOW": "red", "MEDIUM": "yellow", "HIGH": "green"}
        color = confidence_color.get(cost.compute_confidence.value, "white")
        table.add_row("Cost confidence", f"[{color}]{cost.compute_confidence.value}[/{color}]")

    sql_confidence_color = {"LOW": "red", "MEDIUM": "yellow", "HIGH": "green"}
    sql_color = sql_confidence_color.get(assessment.summary.sql_surface_confidence.value, "white")
    table.add_row("SQL surface confidence", f"[{sql_color}]{assessment.summary.sql_surface_confidence.value}[/{sql_color}]")

    if assessment.failures:
        table.add_row("Failed entities", f"[yellow]{len(assessment.failures)}[/yellow]")

    console.print(table)

    if output_files:
        console.print("\n[bold]Output files:[/bold]")
        for f in output_files:
            console.print(f"  • {f}")

    console.print(f"\n[dim]{CLI_ONE_LINER}[/dim]\n")


class _DefaultToAssessGroup(click.Group):
    """Click group that routes unrecognized invocations to `assess`.

    Backward compatibility for the pre-0.3 single-command CLI: when the first
    token is not a known subcommand or a group-level option (derived from the
    group's OWN params — never a hardcoded list, so adding a group option can't
    silently reroute it), "assess" is prepended and its parser handles every
    option. Options are registered once, on the subcommands, so
    `bq-assess --gcp-project p assess` errors instead of dropping flags.
    """

    def parse_args(self, ctx, args):
        if not args:
            return super().parse_args(ctx, ["assess"])
        # Group-level options = declared params + the auto-added help option
        # (click injects it at parse time, so it is not in self.params).
        group_opts = {opt for p in self.params for opt in (*p.opts, *p.secondary_opts)}
        group_opts.update(ctx.help_option_names)
        first = args[0].split("=", 1)[0]
        if first.startswith("-") and first not in group_opts:
            args = ["assess", *args]
        return super().parse_args(ctx, args)


@click.group(
    "bq-assess",
    cls=_DefaultToAssessGroup,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, message=f"bq-assess %(version)s (beta)\n{CLI_ONE_LINER}")
def main() -> None:
    """BigQuery migration assessment tool.

    Scans BigQuery metadata and generates a comprehensive lakehouse migration
    assessment report with effort scoring, complexity scoring, cost estimates,
    and Iceberg DDL.

    Bare invocation runs `assess` (backward compatible). Use `report` to generate
    a report from a customer bundle produced by bq-collect.
    """


def _assess_options(f):
    """Click options for the assess subcommand (registered once, here only)."""
    options = [
        click.option("--gcp-project", default=None, help="GCP project ID, or 'all' to assess every accessible project."),
        click.option("--credentials", default=None, help="Path to service account JSON."),
        click.option("--use-adc", is_flag=True, default=False, help="Use Application Default Credentials."),
        click.option("--datasets", default=None, help="Comma-separated dataset filter."),
        click.option("--query-logs", default=None, help="Path to exported query logs JSON."),
        click.option(
            "--query-log-days",
            type=click.IntRange(1, 90),
            default=None,
            help="Lookback window for INFORMATION_SCHEMA.JOBS in days (1-90, default: 30).",
        ),
        click.option("--bigquery-monthly-cost", type=float, default=None, help="Monthly BigQuery spend override."),
        click.option("--reservation-config", default=None, hidden=True, help="[DEPRECATED] Reservation details are now auto-read. Use --bigquery-monthly-cost instead."),
        click.option("--output", default=None, help="Output directory (default: bq-migration/)."),
        click.option("--format", "output_format", default=None, help="Output formats: html,json (default: html)."),
        click.option("--interactive", is_flag=True, default=False, help="Interactive prompt mode."),
        click.option(
            "--export-bundle/--no-export-bundle", "export_bundle", default=True,
            help="Write the re-processable bundle/ next to the report (default: enabled).",
        ),
        click.option(
            "--zip", "zip_bundle", is_flag=True, default=False,
            help="Write the exported bundle as bundle.zip instead of a directory "
                 "(same hand-off artifact bq-collect --zip produces).",
        ),
        click.option(
            "--exclude-query-text", is_flag=True, default=False,
            help="Omit anonymized query statements from the bundle (privacy opt-out).",
        ),
        click.option("--concurrency", type=int, default=50, show_default=True, help="Max parallel API requests for metadata scanning."),
        click.option("--skip-translation", is_flag=True, default=False, help="Skip SQL translation stage for faster runs."),
        click.option("--skip-workload", is_flag=True, default=False, help="Skip workload analysis even when pricing is detected."),
        click.option("--offline-pricing", is_flag=True, default=False, help="Skip live pricing lookup (use hardcoded rates)."),
        click.option(
            "--no-cache/--use-cache", "no_cache", default=True, show_default=True,
            help="Force a fresh metadata scan (default — stale cached metadata produced wrong "
                 "customer-facing numbers). Pass --use-cache to reuse cached metadata offline.",
        ),
        click.option("--config", default=None, help="Path to YAML config file."),
        click.option(
            "--engine", type=click.Choice(["athena", "redshift", "both"], case_sensitive=False),
            default="both", help="Force engine assessment: athena|redshift|both (default: both)."
        ),
        click.option("--query-sla-ms", type=int, default=None, help="Query latency SLA in milliseconds (affects engine recommendation)."),
        click.option("--target-region", default=None, help="Target AWS region for engine pricing."),
        click.option(
            "--post-optimization/--no-post-optimization", "post_optimization", default=None,
            help="Emit post-migration Spark optimization steps (default: on)."
        ),
    ]
    for option in reversed(options):
        f = option(f)
    return f


@main.command("assess")
@_assess_options
def assess_cmd(
    gcp_project: str | None,
    credentials: str | None,
    use_adc: bool,
    datasets: str | None,
    query_logs: str | None,
    query_log_days: int | None,
    bigquery_monthly_cost: float | None,
    reservation_config: str | None,
    output: str | None,
    output_format: str | None,
    interactive: bool,
    export_bundle: bool,
    zip_bundle: bool,
    exclude_query_text: bool,
    concurrency: int,
    skip_translation: bool,
    skip_workload: bool,
    offline_pricing: bool,
    no_cache: bool,
    config: str | None,
    engine: str | None,
    query_sla_ms: int | None,
    target_region: str | None,
    post_optimization: bool | None,
) -> None:
    """Full end-to-end assessment: scan the Source, analyze, write reports + bundle."""
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    params = _build_params(
        gcp_project=gcp_project, credentials=credentials, use_adc=use_adc,
        datasets=datasets, query_logs=query_logs,
        query_log_days=query_log_days, bigquery_monthly_cost=bigquery_monthly_cost,
        reservation_config=reservation_config, output=output, output_format=output_format,
        interactive=interactive, export_bundle=export_bundle,
        zip_bundle=zip_bundle,
        exclude_query_text=exclude_query_text, concurrency=concurrency,
        skip_translation=skip_translation, skip_workload=skip_workload,
        offline_pricing=offline_pricing, no_cache=no_cache, config=config,
        engine=engine, query_sla_ms=query_sla_ms, target_region=target_region,
        post_optimization=post_optimization,
    )

    try:
        _validate_collect_params(params)
        _validate_report_params(params)

        if str(params.get("gcp_project", "")).lower() == "all":
            _assess_all_projects(params)
        else:
            bundle = collect(params)
            analyze_and_report(bundle, params)
    except KeyboardInterrupt:
        console.print("\n[yellow]Assessment interrupted by user.[/yellow]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        logger.exception("Fatal error during assessment")
        sys.exit(1)


def _discover_projects(credentials_path: str | None) -> list[tuple[str, bool]]:
    """Delegates to core.project_discovery (shared with bq-collect since 0.6.1)."""
    from bq_assess.core.project_discovery import discover_projects
    return discover_projects(credentials_path)


def _assess_all_projects(params: dict) -> None:
    """Discover all accessible projects and run the full pipeline for each.

    Each project gets its own <project>_<date>/ output folder. One project
    failing does not stop the others; a summary table prints at the end.
    """
    console.print("\n[bold]Discovering accessible GCP projects...[/bold]")
    discovered = _discover_projects(params.get("credentials"))

    if not discovered:
        console.print("[red]No accessible GCP projects found for these credentials.[/red]")
        sys.exit(1)

    assessable = [pid for pid, has_data in discovered if has_data]
    skipped = [pid for pid, has_data in discovered if not has_data]

    console.print(
        f"[green]✓ Found {len(discovered)} project(s):[/green] "
        f"{len(assessable)} with BigQuery datasets, {len(skipped)} empty."
    )
    for pid in skipped:
        console.print(f"  [dim]— skipping {pid} (no datasets)[/dim]")
    console.print()

    # Share a reservation cache across all projects so each unique reservation
    # is queried only once (avoids redundant API calls when multiple projects
    # share the same reservation).
    from bq_assess.core.reservation_reader import ReservationCache
    fleet_reservation_cache = ReservationCache()

    results: list[tuple[str, str]] = [(pid, "SKIPPED (no datasets)") for pid in skipped]
    completed_assessments: list = []
    for i, project_id in enumerate(assessable, 1):
        console.print(f"\n[bold cyan]{'═' * 70}[/bold cyan]")
        console.print(f"[bold cyan]Project {i}/{len(assessable)}: {project_id}[/bold cyan]")
        console.print(f"[bold cyan]{'═' * 70}[/bold cyan]")

        project_params = dict(params)
        project_params["gcp_project"] = project_id
        project_params["_multi_project"] = True  # fleet README replaces per-project ones
        project_params["reservation_cache"] = fleet_reservation_cache
        try:
            bundle = collect(project_params)
            assessment = analyze_and_report(bundle, project_params)
            completed_assessments.append(assessment)
            results.append((project_id, "OK"))
        except SystemExit:
            # collect() calls sys.exit(1) on fatal per-project errors — record
            # and move on rather than aborting the remaining projects.
            results.append((project_id, "FAILED"))
        except Exception as exc:
            logger.exception("Assessment failed for project %s", project_id)
            console.print(f"[red]✗ {project_id}: {exc}[/red]")
            results.append((project_id, "FAILED"))

    # Report any admin projects that were permission-denied across the fleet
    fleet_reservation_cache.print_fleet_denied_summary(console)

    # ── Cross-project SUMMARY.html + top-level README ────────────────
    if completed_assessments:
        from bq_assess.report.readme_writer import write_fleet_readme
        from bq_assess.report.summary_writer import write_summary
        base_output_dir = params.get("output") or "bq-migration/"
        try:
            summary_path = write_summary(completed_assessments, base_output_dir)
            console.print(f"\n[green]✓ Cross-project summary: {summary_path}[/green]")
        except Exception as exc:
            logger.exception("Failed to write cross-project summary")
            console.print(f"[yellow]⚠ Could not write cross-project summary: {exc}[/yellow]")
        try:
            # Folder names match the per-project output convention
            # (<project>_<date>/) used by analyze_and_report + summary links.
            folders = [
                (a.project_id, f"{a.project_id}_{a.generated_at.strftime('%Y-%m-%d')}")
                for a in completed_assessments
            ]
            readme_path = write_fleet_readme(base_output_dir, folders)
            console.print(f"[green]✓ Top-level README: {readme_path}[/green]")
        except Exception as exc:
            logger.exception("Failed to write top-level README")
            console.print(f"[yellow]⚠ Could not write top-level README: {exc}[/yellow]")

    # ── Roll-up summary ─────────────────────────────────────────────
    console.print(f"\n[bold]{'═' * 70}[/bold]")
    console.print("[bold]Multi-project assessment summary[/bold]")
    summary_table = Table(show_header=True)
    summary_table.add_column("Project")
    summary_table.add_column("Status")
    ok_count = 0
    failed_count = 0
    for project_id, status in sorted(results):
        if status == "OK":
            style = "green"
            ok_count += 1
        elif status.startswith("SKIPPED"):
            style = "dim"
        else:
            style = "red"
            failed_count += 1
        summary_table.add_row(project_id, f"[{style}]{status}[/{style}]")
    console.print(summary_table)
    console.print(f"\n{ok_count} assessed, {failed_count} failed, {len(results) - ok_count - failed_count} skipped.")

    if ok_count == 0:
        sys.exit(1)


@main.command("report")
@click.option(
    "--bundle", "bundle_path", required=True,
    help="Path to a bundle directory or .zip produced by bq-collect (or bq-assess assess).",
)
@click.option("--output", default=None, help="Output directory (default: bq-migration/).")
@click.option("--format", "output_format", default=None, help="Output formats: html,json (default: html).")
@click.option("--bigquery-monthly-cost", type=float, default=None, help="Monthly BigQuery spend override.")
@click.option("--skip-translation", is_flag=True, default=False, help="Skip SQL translation stage for faster runs.")
@click.option(
    "--refresh-pricing", is_flag=True, default=False,
    help="Re-fetch live AWS/GCP rates instead of using the bundle's snapshot (needs network).",
)
@click.option(
    "--export-bundle/--no-export-bundle", "export_bundle", default=False,
    help="Re-write the bundle next to the report (default: off — the input bundle already exists).",
)
@click.option(
    "--engine", type=click.Choice(["athena", "redshift", "both"], case_sensitive=False),
    default="both", help="Force engine assessment: athena|redshift|both (default: both)."
)
@click.option("--query-sla-ms", type=int, default=None, help="Query latency SLA in milliseconds (affects engine recommendation).")
@click.option("--target-region", default=None, help="Target AWS region for engine pricing.")
@click.option("--config", default=None, help="Path to YAML config file.")
def report_cmd(
    bundle_path: str,
    output: str | None,
    output_format: str | None,
    bigquery_monthly_cost: float | None,
    skip_translation: bool,
    refresh_pricing: bool,
    export_bundle: bool,
    engine: str | None,
    query_sla_ms: int | None,
    target_region: str | None,
    config: str | None,
) -> None:
    """Generate the assessment report from a customer bundle — fully offline."""
    logging.basicConfig(level=logging.WARNING)

    # Load config file if provided
    config_values: dict = {}
    if config:
        with open(config, "r", encoding="utf-8") as cf:
            config_values = yaml.safe_load(cf) or {}

    # Build params with CLI precedence (same pattern as assess)
    params: dict = {
        "output": output or "bq-migration/",
        "format": output_format or "html",
        "export_bundle": export_bundle,
        "refresh_pricing": refresh_pricing,
    }
    if bigquery_monthly_cost is not None:
        params["bigquery_monthly_cost"] = bigquery_monthly_cost
    if skip_translation:
        params["skip_translation"] = True

    # Engine params (CLI > YAML)
    yaml_engine_keys = {
        "engine", "query_sla_ms", "target_region"
    }
    yaml_engine_config = {k: v for k, v in config_values.items() if k in yaml_engine_keys}
    cli_engine_params = {}
    if engine is not None:
        cli_engine_params["engine"] = engine
    if query_sla_ms is not None:
        cli_engine_params["query_sla_ms"] = query_sla_ms
    if target_region is not None:
        cli_engine_params["target_region"] = target_region

    # Track engine CLI vs YAML for precedence
    params["_yaml_engine_config"] = yaml_engine_config
    params["_cli_engine_params"] = cli_engine_params

    # Merge: CLI params win over YAML
    for key in yaml_engine_keys:
        if key in cli_engine_params:
            params[key] = cli_engine_params[key]
        elif key in yaml_engine_config:
            params[key] = yaml_engine_config[key]

    console.print(f"\n[bold]Loading bundle:[/bold] {bundle_path}")
    try:
        loader = BundleLoader()
        bundle = loader.load(bundle_path)
    except BundleError as exc:
        console.print(f"[red]✗ Bundle verification failed: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"[green]✓ Bundle verified[/green] — project '{bundle.project_id}', "
        f"{len(bundle.entities)} entities, collected {bundle.created_at or 'unknown'} "
        f"by collector v{bundle.collector_version or '?'} "
        f"(region: {bundle.bq_location} → {bundle.aws_region})"
    )

    try:
        analyze_and_report(bundle, params)
    except KeyboardInterrupt:
        console.print("\n[yellow]Report generation interrupted by user.[/yellow]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        logger.exception("Fatal error during report generation")
        sys.exit(1)


def _build_params(**kwargs) -> dict:
    """Merge CLI args, config file, and defaults into the params dict (CLI wins)."""
    cli_params: dict = {}
    if kwargs.get("gcp_project") is not None:
        cli_params["gcp_project"] = kwargs["gcp_project"]
    if kwargs.get("credentials") is not None:
        cli_params["credentials"] = kwargs["credentials"]
    if kwargs.get("use_adc"):
        cli_params["use_adc"] = True
    if kwargs.get("datasets") is not None:
        cli_params["datasets"] = kwargs["datasets"]
    if kwargs.get("query_logs") is not None:
        cli_params["query_logs"] = kwargs["query_logs"]
    if kwargs.get("query_log_days") is not None:
        cli_params["query_log_days"] = kwargs["query_log_days"]
    if kwargs.get("bigquery_monthly_cost") is not None:
        cli_params["bigquery_monthly_cost"] = kwargs["bigquery_monthly_cost"]
    if kwargs.get("reservation_config") is not None:
        cli_params["reservation_config"] = kwargs["reservation_config"]
    if kwargs.get("output") is not None:
        cli_params["output"] = kwargs["output"]
    if kwargs.get("output_format") is not None:
        cli_params["format"] = kwargs["output_format"]
    if kwargs.get("interactive"):
        cli_params["interactive"] = True
    cli_params["export_bundle"] = kwargs.get("export_bundle", True)
    if kwargs.get("exclude_query_text"):
        cli_params["exclude_query_text"] = True
    if kwargs.get("zip_bundle"):
        cli_params["zip_bundle"] = True
    cli_params["concurrency"] = kwargs.get("concurrency", 50)
    if kwargs.get("skip_translation"):
        cli_params["skip_translation"] = True
    if kwargs.get("skip_workload"):
        cli_params["skip_workload"] = True
    if kwargs.get("offline_pricing"):
        cli_params["offline_pricing"] = True
    # Always set (default True): fresh scan unless --use-cache was passed explicitly.
    cli_params["no_cache"] = kwargs.get("no_cache", True)
    if kwargs.get("engine") is not None:
        cli_params["engine"] = kwargs["engine"]
    if kwargs.get("query_sla_ms") is not None:
        cli_params["query_sla_ms"] = kwargs["query_sla_ms"]
    if kwargs.get("target_region") is not None:
        cli_params["target_region"] = kwargs["target_region"]
    if kwargs.get("post_optimization") is not None:
        cli_params["post_optimization"] = kwargs["post_optimization"]

    # Load config file if provided
    config_values: dict = {}
    if kwargs.get("config"):
        config_values = _load_config(kwargs["config"])

    # Preserve YAML engine config keys for resolve_engine_config (before CLI merge)
    yaml_engine_keys = {
        "target_region", "query_sla_ms", "preferred_engine",
        "chunk_days", "post_optimization", "compaction_threshold_gb",
        "peak_concurrency_override", "idle_hours_override"
    }
    yaml_engine_config = {k: v for k, v in config_values.items() if k in yaml_engine_keys}

    # Track CLI-only engine params (values explicitly provided via CLI, not from YAML)
    cli_engine_params = {k: v for k, v in cli_params.items() if k in yaml_engine_keys}

    # Merge: CLI args > config file > defaults
    params = _merge_config(cli_params, config_values)

    # Store YAML engine config and CLI engine params separately for precedence tracking
    params["_yaml_engine_config"] = yaml_engine_config
    params["_cli_engine_params"] = cli_engine_params

    params.setdefault("use_adc", False)
    # Query-log analysis is always on (2026-08-03 parity with bq-collect);
    # opt-outs are --skip-workload / --exclude-query-text. YAML query_logs.enabled
    # can still force it off for tests/special cases.
    params.setdefault("include_query_logs", True)
    params.setdefault("output", "bq-migration/")
    params.setdefault("format", "html")
    params.setdefault("interactive", False)

    if params.get("interactive"):
        params = _interactive_prompts(params)

    if not params.get("gcp_project"):
        console.print(
            "[red]Error: --gcp-project is required.[/red]\n"
            "Provide it via CLI argument, config file, or use --interactive mode."
        )
        sys.exit(1)

    if not params.get("credentials") and not params.get("use_adc"):
        console.print(
            "[red]Error: Either --credentials or --use-adc is required.[/red]\n"
            "Provide a service account JSON path or enable Application Default Credentials."
        )
        sys.exit(1)

    return params


if __name__ == "__main__":
    main()
