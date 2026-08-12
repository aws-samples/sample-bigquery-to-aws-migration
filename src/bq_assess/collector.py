"""Collection pipeline — every stage that touches the customer's GCP environment.

``collect(params)`` runs credential validation, metadata scan, pricing detection,
workload analysis, anonymized query collection, live-rate snapshotting, and physical-
bytes resolution, and returns a ``Bundle``. It deliberately imports NOTHING from
``report/``, ``scoring/``, ``targets/``, or ``engine/`` — the collector distribution
ships this module (with ``core/`` and ``bundle/``) and must run without them.

``bq-assess assess`` composes ``collect()`` with ``analyze_and_report()`` in-process;
``bq-collect`` runs ``collect()`` alone and writes the bundle. One code path, two
distributions (docs/superpowers/specs/2026-07-08-collector-report-split-design.md).
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import datetime, timezone

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
)
from rich.prompt import Confirm

from bq_assess import __version__
from bq_assess.bundle.models import Bundle, QueryRecord
from bq_assess.core import pricing_constants as pk
from bq_assess.core.analyzer import QueryAnalyzer
from bq_assess.core.cache import MetadataCache
from bq_assess.core.jobs_query import (
    QUERIES_EXPORT_LIMIT,
    read_jobs_queries,
    read_reservation_groups,
)
from bq_assess.core.price_lookup import (
    PriceLookup,
    PricingTimeout,
    fetch_live_rates_with_timeout,
    rates_to_dict,
)
from bq_assess.core.pricing import PricingDetector
from bq_assess.core.region_mapping import bq_location_to_aws_region
from bq_assess.core.reservation_reader import (
    ReservationCache,
    parse_admin_project,
    read_reservation_details,
)
from bq_assess.core.scanner import BigQueryScanner, ScanAbortedError
from bq_assess.core.storage_stats import (
    ASSUMED_PHYSICAL_RATIO,
    effective_physical_bytes,
    resolve_physical_bytes,
)
from bq_assess.core.workload import WorkloadAnalyzer
from bq_assess.models import (
    BQPricingModel,
    ConfidenceLevel,
    EntityMetadata,
    FailureRecord,
)

logger = logging.getLogger(__name__)
console = Console()


def _apply_reservation_result(pricing, read_result, source_note: str) -> None:
    """Apply reservation read results to a PricingDetection in place."""
    pricing.baseline_slots = read_result.baseline_slots
    pricing.max_slots = read_result.max_slots
    pricing.commitment_slots = read_result.commitment_slots
    pricing.commitment_plan = read_result.commitment_plan
    if read_result.edition:
        pricing.edition = read_result.edition
    pricing.reservation_readable = True
    pricing.autoscale_slot_seconds = read_result.autoscale_slot_seconds
    pricing.timeline_window_seconds = read_result.timeline_window_seconds
    pricing.assigned_projects = read_result.assigned_projects
    pricing.assigned_count = read_result.assigned_count
    pricing.commitments = [
        {"slot_count": c.slot_count, "plan": c.plan, "edition": c.edition}
        for c in read_result.commitments
    ]
    pricing.confidence = ConfidenceLevel.HIGH
    pricing.source_note = source_note


def _edition_requires_commitment(pricing) -> bool:
    """True if this edition cannot be priced without readable reservation details.

    ENTERPRISE/ENTERPRISE_PLUS require commitment data to determine the plan rate;
    STANDARD uses a single PAYG rate and can fall back to a measured-slot-usage range.
    Unknown edition is treated as ENTERPRISE (the downstream default).
    """
    if pricing.edition:
        return pricing.edition in pk.V4_EDITIONS_WITH_CAPACITY_COMMITMENTS
    return True  # Unknown edition → treat as ENTERPRISE


def collect(params: dict) -> Bundle:
    """Run all GCP-touching stages and return the Bundle. Exits(1) on fatal errors."""
    gcp_project: str = params["gcp_project"]
    credentials: str | None = params.get("credentials")
    use_adc: bool = params.get("use_adc", False)
    datasets_str: str | None = params.get("datasets")
    # Query-log analysis is ALWAYS ON (2026-08-03 parity decision): the opt-outs
    # are --skip-workload (no JOBS reads at all) and --exclude-query-text
    # (no statements in the bundle). include_query_logs=False survives only as
    # an internal override for tests/config.
    include_query_logs: bool = params.get("include_query_logs", True)
    query_logs_path: str | None = params.get("query_logs")
    query_log_days: int = int(params.get("query_log_days") or 30)
    reservation_config: dict | None = params.get("reservation_config_data")
    exclude_query_text: bool = params.get("exclude_query_text", False)

    dataset_filter: list[str] | None = None
    if datasets_str:
        dataset_filter = [d.strip() for d in datasets_str.split(",") if d.strip()]

    failures: list[FailureRecord] = []

    # ── Stage 1: Validate credentials ──────────────────────────────
    console.print("\n[bold]Stage 1:[/bold] Validating BigQuery credentials...")
    scanner = BigQueryScanner(
        project_id=gcp_project,
        credentials_path=credentials,
        use_adc=use_adc,
        max_concurrent_requests=params.get("concurrency", 16),
    )
    if not scanner.validate_credentials():
        console.print("[red]✗ Credential validation failed.[/red]")
        sys.exit(1)
    console.print("[green]✓ Credentials validated successfully.[/green]")

    # ── Stage 2: Scan or load from cache ───────────────────────────
    cache = MetadataCache()
    entities: list[EntityMetadata] | None = None

    if not params.get("no_cache") and cache.has_cache(gcp_project):
        use_cache = True
        if params.get("interactive"):
            use_cache = Confirm.ask(
                f"Cached metadata found for project '{gcp_project}'. Use cached data?",
                default=True,
            )
        else:
            console.print(f"[cyan]Using cached metadata for project '{gcp_project}'.[/cyan]")

        if use_cache:
            entities = cache.load(gcp_project)
            if entities:
                console.print(f"[green]✓ Loaded {len(entities)} entities from cache.[/green]")
            else:
                console.print("[yellow]⚠ Cache is empty — rescanning.[/yellow]")
                entities = None

    if entities is None:
        console.print("\n[bold]Stage 2:[/bold] Scanning BigQuery metadata...")

        # Resume support (R5, 2026-07-28): a recent interrupted scan of the same
        # project + dataset filter picks up where it left off — resuming is
        # finishing the job already started, distinct from --use-cache's reuse
        # of a COMPLETED scan. Anything unmatched/stale starts clean.
        done_datasets = cache.begin_scan_session(gcp_project, dataset_filter)
        scanned_entities = cache.load_checkpointed(gcp_project, done_datasets)
        if scanned_entities:
            console.print(
                f"[cyan]Resuming interrupted scan — {len(done_datasets)} datasets "
                f"({len(scanned_entities)} entities) already completed.[/cyan]"
            )

        try:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                TaskProgressColumn(),
                console=console,
            ) as progress:
                task = progress.add_task("Scanning entities...", total=None)
                for entity_meta in scanner.scan(
                    dataset_filter=dataset_filter,
                    skip_datasets=done_datasets,
                    on_chunk_complete=lambda ds_ids, chunk: cache.checkpoint_datasets(
                        gcp_project, ds_ids, chunk
                    ),
                ):
                    scanned_entities.append(entity_meta)
                    progress.update(task, description=f"Scanned {len(scanned_entities)} entities...")
        except ScanAbortedError as exc:
            # The scanner raises on every recorded failure once thresholds are
            # crossed — including failure-only tails and listing failures the old
            # in-loop check could never see (2026-07-28 review). A bundle missing
            # that much of the estate must not ship quietly.
            console.print(
                f"\n[red]✗ Aborting scan: {exc} "
                "Common causes: sustained API throttling (retry with "
                "--concurrency 20) or permissions revoked mid-scan.[/red]"
            )
            # os._exit: in-flight worker threads may be mid-retry-sleep (up to
            # 60s each); sys.exit would join them at interpreter exit and turn
            # the loud fast failure into a minutes-long hang.
            console.file.flush()
            os._exit(1)

        failures.extend(scanner.failures)

        entities = scanned_entities
        console.print(f"[green]✓ Scanned {len(entities)} entities.[/green]")
        if scanner.failures:
            console.print(f"[yellow]⚠ {len(scanner.failures)} entities failed to scan.[/yellow]")

        if entities:
            console.print("[bold]Caching metadata...[/bold]")
            cache.store(gcp_project, entities)
            console.print("[green]✓ Metadata cached.[/green]")

    if not entities:
        console.print("[red]No entities found. Nothing to assess.[/red]")
        sys.exit(1)

    # ── Stage 3: Pricing Detection ─────────────────────────────────
    console.print("\n[bold]Stage 3:[/bold] Detecting BigQuery pricing model...")
    pricing_detector = PricingDetector()
    client = scanner._get_client()

    # Detect ALL dataset regions so every INFORMATION_SCHEMA read hits the right
    # location(s). Primary source is the scanner's dataset→location map (free —
    # captured from Tables it already fetched); the primary region (most
    # datasets) anchors the AWS-region mapping and rate snapshot, and pricing
    # detection + workload/query-text/TABLE_STORAGE all loop per region.
    region_datasets = _detect_dataset_locations(
        client, entities, known=getattr(scanner, "dataset_locations", None)
    )
    unresolved = region_datasets.pop("", [])
    if unresolved:
        # An unplaced dataset means its region's data may be missing — say so
        # and record it; silence here is the finding-#4 bug in miniature.
        console.print(
            f"[yellow]⚠ Could not resolve the region of {len(unresolved)} dataset(s) "
            f"({', '.join(unresolved[:5])}{'…' if len(unresolved) > 5 else ''}) — their "
            f"region's workload/storage stats may be missing from the bundle.[/yellow]"
        )
        failures.append(FailureRecord(
            entity_name=f"{gcp_project} (region detection)",
            stage="scan",
            error=f"could not resolve dataset region(s): {', '.join(unresolved)}",
        ))
    regions = sorted(region_datasets, key=lambda r: (-len(region_datasets[r]), r)) or ["US"]
    detected_location = regions[0]
    if len(regions) > 1:
        console.print(
            f"[cyan]Multi-region project: {', '.join(regions)} — "
            f"'{detected_location}' is primary (most datasets); pricing detection, "
            f"workload, query text, and storage stats are collected per region "
            f"and merged.[/cyan]"
        )

    try:
        pricing = pricing_detector.detect(client, gcp_project, reservation_config, location=regions)
        console.print(f"[green]✓ Detected pricing model: {pricing.model.value}[/green]")
    except Exception as exc:
        console.print(f"[yellow]⚠ Pricing detection failed: {exc}[/yellow]")
        pricing = None

    # ── Stage 3b: Auto-read reservation details (all capacity editions) ──
    reservation_cache: ReservationCache | None = params.get("reservation_cache")
    if (
        pricing is not None
        and pricing.model is BQPricingModel.CAPACITY
        and pricing.baseline_slots is None
    ):
        # Detection already read the grouped JOBS rows and carries the
        # reservation_id — re-querying INFORMATION_SCHEMA here doubled the
        # JOBS scan per capacity collection (2026-07-28 review). The query
        # remains only as a fallback for detections predating the field.
        res_id = pricing.reservation_id
        if res_id is None:
            cap_rows = []
            for _region in regions:
                cap_rows.extend(
                    read_reservation_groups(client, gcp_project, location=_region)
                )
            res_id = next(
                (r.get(pk.V5_JOBS_RESERVATION_ID_COLUMN) for r in cap_rows
                 if r.get(pk.V5_JOBS_RESERVATION_ID_COLUMN)),
                None,
            )
        parsed = parse_admin_project(res_id)
        if parsed:
            admin_proj, res_location, res_name = parsed
            console.print(
                f"[dim]  Reservation: {admin_proj}:{res_location}.{res_name}[/dim]"
            )

            # Check cache first (fleet mode: avoid re-querying the same reservation)
            read_result = None
            if reservation_cache:
                read_result = reservation_cache.get(admin_proj, res_location, res_name)
                if read_result:
                    console.print("[dim]  (cached from prior project read)[/dim]")

            if read_result is None:
                console.print(
                    f"[dim]  Attempting to read reservation details from project "
                    f"'{admin_proj}'...[/dim]"
                )
                read_result = read_reservation_details(
                    client, admin_proj, res_location, res_name,
                    lookback_days=query_log_days,
                )
                if reservation_cache:
                    reservation_cache.put(admin_proj, res_location, res_name, read_result)

            if read_result.success:
                _apply_reservation_result(pricing, read_result,
                    f"Capacity model auto-read from {admin_proj} RESERVATIONS "
                    f"(baseline={read_result.baseline_slots}, "
                    f"commitment={read_result.commitment_slots}/{read_result.commitment_plan})."
                )
                console.print(
                    f"[green]✓ Reservation details: "
                    f"baseline={read_result.baseline_slots} slots, "
                    f"commitment={read_result.commitment_slots} "
                    f"({read_result.commitment_plan})[/green]"
                )
            elif read_result.permission_denied:
                # Branch messaging: ENTERPRISE/ENTERPRISE_PLUS require commitments (hard stop),
                # STANDARD has a fallback (modelled range from measured slot usage).
                is_commitment_required = _edition_requires_commitment(pricing)

                console.print()
                console.print(
                    f"[yellow]⚠️  Permission denied on project '{admin_proj}'.[/yellow]"
                )
                console.print()

                if is_commitment_required:
                    console.print(
                        "[bold]BigQuery capacity cost CANNOT be estimated without "
                        "reservation details.[/bold]"
                    )
                    console.print(
                        "The report will show this project's capacity cost as "
                        "UNAVAILABLE (not estimated)."
                    )
                else:
                    console.print(
                        "[bold]Reservation details unreadable — the estimate will fall back to "
                        "a modelled range[/bold]\n"
                        "[bold]from measured slot usage (MEDIUM confidence; actual bill can "
                        "exceed it for bursty workloads).[/bold]"
                    )

                console.print()
                console.print("[bold]To fix (takes ~30 seconds):[/bold]")
                console.print(
                    f"  In another terminal, run:\n"
                    f"  [cyan]gcloud projects add-iam-policy-binding {admin_proj} \\\n"
                    f"    --member=\"user:<your-email>\" \\\n"
                    f"    --role=\"roles/bigquery.resourceViewer\"[/cyan]"
                )
                if is_commitment_required:
                    console.print(
                        "  Or supply [cyan]--bigquery-monthly-cost[/cyan] to override."
                    )
                else:
                    console.print(
                        "  (Grant access for an exact billed-capacity figure.)"
                    )
                console.print(
                    "  Tip: if a Cloud Billing BigQuery export already exists, read "
                    "access to that\n  dataset typically suffices to get the exact "
                    "billed amount for [cyan]--bigquery-monthly-cost[/cyan]."
                )
                console.print()

                retry = (
                    params.get("interactive")
                    and Confirm.ask("Grant access and retry?", default=False)
                )
                if retry:
                    read_result = read_reservation_details(
                        client, admin_proj, res_location, res_name,
                        lookback_days=query_log_days,
                    )
                    if read_result.success:
                        _apply_reservation_result(pricing, read_result,
                            f"Capacity model auto-read from {admin_proj} RESERVATIONS "
                            f"(retry succeeded)."
                        )
                        if reservation_cache:
                            reservation_cache.put(admin_proj, res_location, res_name, read_result)
                        console.print("[green]✓ Reservation details read on retry.[/green]")
                    else:
                        pricing.reservation_readable = False
                        if is_commitment_required:
                            console.print(
                                "[yellow]Still denied. Capacity cost will be "
                                "reported as UNAVAILABLE.[/yellow]"
                            )
                        else:
                            console.print(
                                "[yellow]Still denied. Capacity cost will be estimated as a range "
                                "from measured slot usage.[/yellow]"
                            )
                else:
                    pricing.reservation_readable = False
                    if is_commitment_required:
                        console.print(
                            "[dim]  Capacity cost will be reported as UNAVAILABLE.[/dim]"
                        )
                    else:
                        console.print(
                            "[dim]  Capacity cost will be estimated as a range from measured slot usage.[/dim]"
                        )
            else:
                # Non-permission read error (API failure, etc.)
                is_commitment_required = _edition_requires_commitment(pricing)
                pricing.reservation_readable = False
                console.print(
                    f"[yellow]⚠ Could not read reservation details: "
                    f"{read_result.error_message}[/yellow]"
                )
                if is_commitment_required:
                    console.print(
                        "[dim]  Capacity cost will be reported as UNAVAILABLE.[/dim]"
                    )
                else:
                    console.print(
                        "[dim]  Capacity cost will be estimated as a range from measured slot usage.[/dim]"
                    )

    # ── Stage 4: Workload Analysis ─────────────────────────────────
    # The live path reads HOURLY aggregates (≤ 24×days rows server-side) — bounded memory
    # and wall-clock regardless of the Source's query volume.
    console.print("\n[bold]Stage 4:[/bold] Analyzing workload...")
    workload_analyzer = WorkloadAnalyzer()
    slots = None
    skip_workload = params.get("skip_workload", False)

    try:
        if skip_workload:
            console.print("[yellow]⚠ Workload analysis skipped (--skip-workload).[/yellow]")
        elif query_logs_path:
            slots, _ = workload_analyzer.analyze_from_file(query_logs_path)
            if slots:
                console.print("[green]✓ Workload analyzed from file.[/green]")
        elif include_query_logs or pricing:
            if pricing and not include_query_logs:
                console.print("[dim]  Pricing detected — auto-running workload analysis for accurate cost (skip with --skip-workload)...[/dim]")
            slots, _, empty_regions = workload_analyzer.analyze_from_api_multi(
                client, gcp_project, days=query_log_days, locations=regions
            )
            if slots:
                covered = [r for r in regions if r not in empty_regions]
                region_note = f" across {len(covered)} region(s)" if len(regions) > 1 else ""
                console.print(f"[green]✓ Workload analyzed from API (last {query_log_days} days{region_note}).[/green]")
            # A region with datasets but no workload rows may be a failed read
            # (JOBS degrades to [] on error) — surface it; a silently missing
            # region understates the merged curve in the forbidden direction.
            if slots and len(regions) > 1 and empty_regions:
                console.print(
                    f"[yellow]⚠ No workload rows from region(s): {', '.join(empty_regions)} — "
                    f"idle region or unreadable JOBS view; the merged workload may "
                    f"exclude them.[/yellow]"
                )
                failures.append(FailureRecord(
                    entity_name=f"{gcp_project} (INFORMATION_SCHEMA.JOBS: {', '.join(empty_regions)})",
                    stage="scan",
                    error="no workload rows from region(s) — idle or unreadable; "
                          "merged workload may be incomplete",
                ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Workload analysis failed: {exc}[/yellow]")

    if not slots:
        console.print("[yellow]⚠ No workload data available - cost will be estimated as range.[/yellow]")

    # ── Stage 5: Anonymized Query Collection ───────────────────────
    # Default ON (design decision 2, amending R22.4): anonymized statements + per-job
    # stats ride on queries.jsonl. Literals are stripped BEFORE anything touches disk.
    # --skip-workload also skips this stage: both read INFORMATION_SCHEMA.JOBS, and
    # the flag's promise is "no project-wide JOBS scans".
    queries: list[QueryRecord] | None = None
    if exclude_query_text:
        console.print("\n[bold]Stage 5:[/bold] Query collection [yellow]skipped[/yellow] (--exclude-query-text).")
    elif skip_workload and not query_logs_path:
        console.print("\n[bold]Stage 5:[/bold] Query collection [yellow]skipped[/yellow] (--skip-workload covers all JOBS reads).")
    else:
        console.print("\n[bold]Stage 5:[/bold] Collecting anonymized query statements...")
        truncated = False
        try:
            if query_logs_path:
                queries = _queries_from_file(query_logs_path)
            elif include_query_logs or pricing:
                queries, truncated, failed_q_regions = _queries_from_api_multi(
                    client, gcp_project, query_log_days, regions
                )
                if failed_q_regions:
                    console.print(
                        f"[yellow]⚠ Query collection failed for region(s): "
                        f"{', '.join(failed_q_regions)} — other regions' statements "
                        f"were kept.[/yellow]"
                    )
        except Exception as exc:
            console.print(f"[yellow]⚠ Query collection failed: {exc}[/yellow]")
            queries = None

        if queries:
            console.print(f"[green]✓ Collected {len(queries)} anonymized statements (literals stripped).[/green]")
            if truncated:
                console.print(
                    f"[yellow]⚠ Statement export capped at {QUERIES_EXPORT_LIMIT:,} "
                    f"(heaviest by slot-ms kept).[/yellow]"
                )
        else:
            queries = None
            console.print("[dim]  No query statements available (missing permission or no workload).[/dim]")

    # ── Stage 6: Region Detection + Rate Snapshot ──────────────────
    console.print("\n[bold]Stage 6:[/bold] Snapshotting pricing rates...")
    aws_region, is_fallback = bq_location_to_aws_region(detected_location)
    if is_fallback:
        console.print(
            f"[yellow]⚠ BigQuery location '{detected_location}' has no AWS mapping — "
            f"defaulting to us-east-1 pricing; override with --target-region[/yellow]"
        )
    else:
        console.print(f"[dim]  Source region: {detected_location} → AWS region: {aws_region}[/dim]")

    rates_snapshot: dict | None = None
    if not params.get("offline_pricing", False):
        try:
            price_lookup = PriceLookup(
                aws_region=aws_region, bq_location=detected_location,
                use_cache=not params.get("no_cache", True),
            )
            with console.status("[dim]Fetching live pricing from AWS/GCP APIs…[/dim]", spinner="dots"):
                live_rates = fetch_live_rates_with_timeout(price_lookup, gcp_client=client)
            rates_snapshot = rates_to_dict(live_rates)
            if live_rates.is_live:
                console.print(f"[green]✓ Live rates snapshotted (AWS: {live_rates.aws.fetched_at}, GCP: {live_rates.gcp.fetched_at}).[/green]")
            elif live_rates.staleness_warning:
                console.print(f"[yellow]⚠ {live_rates.staleness_warning}[/yellow]")
            else:
                console.print("[dim]  Using cached/hardcoded pricing rates.[/dim]")
        except PricingTimeout as exc:
            console.print(f"[yellow]⚠ {exc} — bundle will carry no live snapshot; report prices with region-adjusted hardcoded rates.[/yellow]")
        except Exception as exc:
            console.print(f"[dim]  Pricing snapshot skipped: {exc}[/dim]")
    else:
        console.print("[dim]  Live pricing lookup skipped (--offline-pricing).[/dim]")

    # ── Stage 7: Physical Storage Resolution ───────────────────────
    console.print("\n[bold]Stage 7:[/bold] Resolving physical storage bytes...")
    try:
        storage_stats = resolve_physical_bytes(
            client, gcp_project, region_datasets, entities
        )
        for entity in entities:
            entity.physical_bytes = storage_stats.physical_map.get(entity.full_name)

        if storage_stats.basis == "measured":
            console.print("[green]✓ Physical bytes measured from TABLE_STORAGE.[/green]")
        elif storage_stats.basis == "mixed":
            console.print(f"[yellow]⚠ Partial TABLE_STORAGE coverage — {storage_stats.source_note}[/yellow]")
        else:  # assumed
            console.print(f"[yellow]⚠ TABLE_STORAGE unavailable — using {ASSUMED_PHYSICAL_RATIO}× logical fallback.[/yellow]")
        # The reason must survive into failures.json — the 2026-07-23 audit found
        # three customer bundles at 100% fallback with the error text unrecoverable.
        if storage_stats.failure_reason:
            # Wording must match scope: a per-region failure alongside other
            # regions' successful measurement is PARTIAL, not "unreadable" —
            # audit tooling treats "unreadable" as 100% ratio fallback
            # (2026-07-28 review).
            scope = (
                "TABLE_STORAGE unreadable"
                if storage_stats.measured_count == 0
                else "TABLE_STORAGE partially read — some region(s) failed"
            )
            failures.append(FailureRecord(
                entity_name=f"{gcp_project} (INFORMATION_SCHEMA.TABLE_STORAGE)",
                stage="scan",
                error=f"{scope} — {storage_stats.failure_reason}",
            ))
    except Exception as exc:
        console.print(f"[yellow]⚠ Physical storage resolution failed: {exc}[/yellow]")
        failures.append(FailureRecord(
            entity_name=f"{gcp_project} (INFORMATION_SCHEMA.TABLE_STORAGE)",
            stage="scan",
            error=f"physical storage resolution failed — {type(exc).__name__}: {exc}",
        ))
        storage_stats = None
    finally:
        # Guarantee population even on failure — backfill any unpopulated physical_bytes
        for entity in entities:
            if entity.physical_bytes is None:
                entity.physical_bytes = effective_physical_bytes(entity.num_bytes, None)

    storage_basis = storage_stats.basis if storage_stats else "assumed"

    # ── Storage Read API egress estimation (Cloud Monitoring) ─────────
    egress_sessions: int | None = None
    egress_gib: float | None = None
    try:
        from bq_assess.core.egress_estimator import estimate_storage_api_egress

        total_logical_bytes = sum(e.num_bytes for e in entities if e.population.value == "TABLE")
        table_count = sum(1 for e in entities if e.population.value == "TABLE")
        if total_logical_bytes > 0 and table_count > 0:
            egress = estimate_storage_api_egress(
                gcp_project, total_logical_bytes, table_count,
            )
            if egress is not None:
                egress_sessions = egress.read_sessions
                egress_gib = egress.estimated_egress_gib
                console.print(
                    f"[green]✓ Storage Read API egress: {egress.read_sessions} sessions, "
                    f"~{egress.estimated_egress_gib:,.1f} GiB/month.[/green]"
                )
            else:
                console.print(
                    "[yellow]⚠ Storage Read API egress: no sessions detected or "
                    "roles/monitoring.viewer unavailable — egress cost excluded from estimate.[/yellow]"
                )
    except Exception as exc:
        console.print(
            f"[yellow]⚠ Storage Read API egress estimation skipped: {exc}[/yellow]"
        )

    return Bundle(
        project_id=gcp_project,
        bq_location=detected_location,
        aws_region=aws_region,
        regions=regions,
        entities=entities,
        failures=failures,
        workload=slots,
        pricing=pricing,
        rates=rates_snapshot,
        queries=queries,
        storage_basis=storage_basis,
        egress_sessions=egress_sessions,
        egress_gib=egress_gib,
        collector_version=__version__,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _queries_from_api(
    client, project_id: str, days: int, location: str
) -> tuple[list[QueryRecord], bool]:
    """Read distinct statements from JOBS (bounded), anonymize BEFORE returning.

    Returns ``(records, truncated)``. Reads limit+1 rows so truncation is PROVEN
    from the RAW row count — empty-text rows are filtered out of the records, so
    the caller's list length alone can't distinguish "hit the limit" from
    "exactly at the limit" (review find: one empty-text group silently defeated
    the boundary check).
    """
    analyzer = QueryAnalyzer()
    rows = read_jobs_queries(
        client, project_id, days=days, location=location,
        limit=QUERIES_EXPORT_LIMIT + 1,
    )
    truncated = len(rows) > QUERIES_EXPORT_LIMIT
    records: list[QueryRecord] = []
    for row in rows:
        text = _col(row, "query")
        if not text:
            continue
        missing = _col(row, "missing_billed_jobs") or 0
        billed = _col(row, "total_bytes_billed")
        creation = _col(row, "creation_time")
        records.append(QueryRecord(
            query=analyzer.anonymize_query(text),
            total_slot_ms=_col(row, "total_slot_ms") or 0,
            total_bytes_processed=_col(row, "total_bytes_processed") or 0,
            # Billed carried only when EVERY job in the statement group had the column —
            # same all-or-nothing rule as the hourly workload read.
            total_bytes_billed=billed if (billed is not None and not missing) else None,
            statement_type=_col(row, "statement_type"),
            creation_time=creation.isoformat() if isinstance(creation, datetime) else (str(creation) if creation else None),
        ))
    return records[:QUERIES_EXPORT_LIMIT], truncated


def _queries_from_api_multi(
    client, project_id: str, days: int, regions: list[str]
) -> tuple[list[QueryRecord] | None, bool, list[str]]:
    """Read + merge distinct statements across regions under the global cap.

    Owns the cross-region selection rule so it has ONE home next to the
    single-region reader (2026-07-28 review of the multi-region MR):
    - per-region isolation: a region that raises is recorded and skipped —
      one bad region must not discard every other region's collected records;
    - cross-region dedup: the same anonymized statement run in N regions
      merges into one record with summed stats (the single-region path's
      server-side GROUP BY guaranteed distinctness; without this, a shared
      statement's slot-ms splits across rows and both can be evicted at the
      cap while genuinely lighter statements survive). Dedup keys on the
      Python-ANONYMIZED text, which is safe against jobs_query's per-region
      ANY_VALUE picking different representatives for one shape: any two
      texts in a shape group anonymize identically (Python replaces a
      superset of QUERY_SHAPE_SQL's spans — invariant documented there and
      pinned by TestQueryShapeKey);
    - global cap: heaviest by (merged) total_slot_ms, mirroring the SQL's
      ORDER BY total_slot_ms DESC LIMIT.

    Returns ``(records | None, truncated, failed_regions)``.
    """
    merged: dict[str, QueryRecord] = {}
    truncated = False
    failed_regions: list[str] = []
    for region in regions:
        try:
            region_records, region_truncated = _queries_from_api(
                client, project_id, days, region
            )
        except Exception as exc:
            logger.warning("Query collection failed for region %s: %s", region, exc)
            failed_regions.append(region)
            continue
        truncated = truncated or region_truncated
        for rec in region_records:
            existing = merged.get(rec.query)
            if existing is None:
                merged[rec.query] = rec
            else:
                existing.total_slot_ms += rec.total_slot_ms
                existing.total_bytes_processed += rec.total_bytes_processed
                # Billed stays all-or-nothing across the merged group — one
                # region without the column degrades the statement's billed
                # basis, same rule as the SQL-side missing-billed COUNTIF.
                if existing.total_bytes_billed is not None and rec.total_bytes_billed is not None:
                    existing.total_bytes_billed += rec.total_bytes_billed
                else:
                    existing.total_bytes_billed = None
    records = list(merged.values())
    if len(records) > QUERIES_EXPORT_LIMIT:
        records.sort(key=lambda q: q.total_slot_ms, reverse=True)
        records = records[:QUERIES_EXPORT_LIMIT]
        truncated = True
    return (records or None), truncated, failed_regions


def _queries_from_file(path: str) -> list[QueryRecord]:
    """Extract per-job entries carrying query text from an exported query-log file.

    Accepts the same formats as WorkloadAnalyzer.analyze_from_file (JSON array OR
    JSONL) via the shared parser — the two stages read the same --query-logs file
    and must never disagree on what parses.
    """
    from pathlib import Path as _Path

    from bq_assess.core.workload import parse_json_or_jsonl

    analyzer = QueryAnalyzer()
    try:
        text = _Path(path).read_text(encoding="utf-8")
    except OSError:
        return []
    data = parse_json_or_jsonl(text)
    if not data:
        return []

    records: list[QueryRecord] = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("query"):
            continue
        billed = entry.get("total_bytes_billed")
        records.append(QueryRecord(
            query=analyzer.anonymize_query(entry["query"]),
            total_slot_ms=int(entry.get("total_slot_ms") or 0),
            total_bytes_processed=int(entry.get("total_bytes_processed") or 0),
            total_bytes_billed=int(billed) if billed is not None else None,
            statement_type=entry.get("statement_type"),
            creation_time=str(entry["creation_time"]) if entry.get("creation_time") else None,
        ))
    return records


def _col(row, key):
    """Read a column from a dict (tests/files) or a BigQuery Row (live)."""
    return row.get(key) if isinstance(row, dict) else getattr(row, key, None)


def _detect_dataset_locations(
    client, entities: list[EntityMetadata], known: dict[str, str] | None = None
) -> dict[str, list[str]]:
    """Map each distinct BigQuery region to the dataset_ids living in it.

    ``known`` is the scanner's dataset→location map, captured for free from the
    Table objects it already fetched — the primary source, costing zero extra
    API calls. Only datasets the scan couldn't place (e.g. routine-only
    datasets) fall back to one get_dataset call each; a fallback failure is
    surfaced to the caller via the "" region bucket rather than swallowed
    (a silently dropped dataset would silently drop its region's workload —
    the finding-#4 bug this map exists to fix). Returns {} if nothing resolves.
    """
    known = known or {}
    region_datasets: dict[str, list[str]] = {}
    unresolved: list[str] = []
    seen: set[str] = set()
    for e in entities:
        if e.dataset_id in seen:
            continue
        seen.add(e.dataset_id)
        loc = known.get(e.dataset_id)
        if loc is None:
            try:
                ds = client.get_dataset(f"{client.project}.{e.dataset_id}")
                loc = ds.location or None
            except Exception:
                loc = None
        if loc:
            region_datasets.setdefault(loc, []).append(e.dataset_id)
        else:
            unresolved.append(e.dataset_id)
    if unresolved:
        region_datasets[""] = unresolved
    return region_datasets
