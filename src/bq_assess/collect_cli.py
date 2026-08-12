"""bq-collect — the customer-environment collector CLI.

Runs ONLY the collection half of the pipeline (collector.collect) and writes the
bundle. Ships as its own slim distribution (packaging/collector/) with no report,
scoring, conversion, or engine code — see the 2026-07-08 collector/report design.
"""

from __future__ import annotations

import logging
import os
import sys

import click
from rich.console import Console
from rich.panel import Panel

from bq_assess import __version__
from bq_assess.bundle import BundleWriter
from bq_assess.collector import collect
from bq_assess.core.disclaimer import CLI_ONE_LINER, DATA_HANDLING

logger = logging.getLogger(__name__)
console = Console()


@click.command("bq-collect")
@click.version_option(__version__, message=f"bq-collect %(version)s (beta)\n{CLI_ONE_LINER}")
@click.option("--gcp-project", required=True, help="GCP project ID, or 'all' to collect every accessible project.")
@click.option("--credentials", default=None, help="Path to service account JSON.")
@click.option("--use-adc", is_flag=True, default=False, help="Use Application Default Credentials.")
@click.option("--datasets", default=None, help="Comma-separated dataset filter.")
@click.option(
    "--query-log-days",
    type=click.IntRange(1, 90),
    default=30,
    show_default=True,
    help="Lookback window for INFORMATION_SCHEMA.JOBS in days.",
)
@click.option("--reservation-config", default=None, hidden=True, help="[DEPRECATED] Reservation details are now auto-read during collection.")
@click.option("--output", default="bundle-out/", show_default=True, help="Directory the bundle/ is written into.")
@click.option(
    "--exclude-query-text", is_flag=True, default=False,
    help="Omit anonymized query statements from the bundle (privacy opt-out).",
)
@click.option("--concurrency", type=int, default=50, show_default=True, help="Max parallel API requests for metadata scanning.")
@click.option("--skip-workload", is_flag=True, default=False, help="Skip workload analysis.")
@click.option("--offline-pricing", is_flag=True, default=False, help="Skip the live pricing snapshot.")
@click.option(
    "--no-cache/--use-cache", "no_cache", default=True, show_default=True,
    help="Force a fresh metadata scan. Pass --use-cache to reuse cached metadata.",
)
@click.option(
    "--zip", "zip_bundle", is_flag=True, default=False,
    help="Also write the bundle as a ready-to-send .zip next to the directory "
         "(with --gcp-project all: one zip per project).",
)
def main(
    gcp_project: str,
    credentials: str | None,
    use_adc: bool,
    datasets: str | None,
    query_log_days: int,
    reservation_config: str | None,
    output: str,
    exclude_query_text: bool,
    concurrency: int,
    skip_workload: bool,
    offline_pricing: bool,
    no_cache: bool,
    zip_bundle: bool,
) -> None:
    """Collect BigQuery metadata into a bundle for offline assessment.

    Read-only: scans metadata, INFORMATION_SCHEMA statistics, and (unless
    --exclude-query-text) anonymized query statements. Review the bundle
    contents before transmitting them outside your environment.
    """
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("urllib3.connectionpool").setLevel(logging.ERROR)

    if credentials and use_adc:
        console.print("[red]Error: --credentials and --use-adc are mutually exclusive[/red]")
        sys.exit(1)
    if not credentials and not use_adc:
        console.print("[red]Error: provide --credentials or --use-adc[/red]")
        sys.exit(1)

    params: dict = {
        "gcp_project": gcp_project,
        "credentials": credentials,
        "use_adc": use_adc,
        "datasets": datasets,
        "query_log_days": query_log_days,
        "reservation_config": reservation_config,
        "exclude_query_text": exclude_query_text,
        "concurrency": concurrency,
        "skip_workload": skip_workload,
        "offline_pricing": offline_pricing,
        "no_cache": no_cache,
    }

    # Load reservation config if provided (kept file-free inside collect())
    if reservation_config:
        # Deprecation parity with bq-assess (cli.py _validate_collect_params):
        # both surfaces of collect() must present the same contract.
        console.print(
            "[yellow]⚠ --reservation-config is deprecated. Reservation details are now "
            "auto-read during collection.[/yellow]"
        )
        try:
            with open(reservation_config, encoding="utf-8") as f:
                if reservation_config.endswith(".json"):
                    import json
                    params["reservation_config_data"] = json.load(f)
                else:
                    import yaml
                    params["reservation_config_data"] = yaml.safe_load(f)
            console.print(f"[green]✓ Loaded reservation config: {reservation_config}[/green]")
        except Exception as exc:
            console.print(f"[yellow]⚠ Failed to load reservation config: {exc}[/yellow]")

    try:
        if str(gcp_project).lower() == "all":
            _collect_all_projects(params, output, zip_bundle)
        else:
            _collect_one(params, output, zip_bundle)
    except KeyboardInterrupt:
        console.print("\n[yellow]Collection interrupted by user.[/yellow]")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as exc:
        console.print(f"\n[red]Fatal error: {exc}[/red]")
        logger.exception("Fatal error during collection")
        sys.exit(1)


def _collect_one(params: dict, output: str, zip_bundle: bool) -> str:
    """Collect one project and write its bundle. Returns the bundle dir (or zip).

    With --zip the directory tree is replaced by the archive — the customer's
    hand-off artifact is the zip alone (under --gcp-project all: a folder of
    one zip per project, nothing else to pick through).
    """
    bundle = collect(params)

    console.print("\n[bold]Writing bundle...[/bold]")
    writer = BundleWriter()
    bundle_dir = writer.write(bundle, output)
    console.print(f"[green]✓ Bundle written: {bundle_dir}[/green]")

    zip_path = None
    if zip_bundle:
        import shutil

        zip_path = _zip_bundle_dir(bundle_dir)
        shutil.rmtree(bundle_dir)
        # Remove the now-empty parent too (e.g. bundle-<project>/) so only the
        # zip remains — but never a directory that still holds other files
        # (--output may point at a folder the customer already uses).
        parent = os.path.dirname(os.path.abspath(bundle_dir))
        try:
            os.rmdir(parent)
        except OSError:
            pass

    _print_collection_summary(bundle, bundle_dir, zip_path)
    return zip_path or bundle_dir


def _zip_bundle_dir(bundle_dir: str) -> str:
    """Zip the bundle into <parent>.zip (shared bundle.writer.zip_bundle_dir)."""
    from bq_assess.bundle.writer import zip_bundle_dir

    zip_path = zip_bundle_dir(bundle_dir)
    console.print(f"[green]✓ Bundle zipped: {zip_path}[/green]")
    return zip_path


def _collect_all_projects(params: dict, output: str, zip_bundle: bool) -> None:
    """Discover accessible projects and collect each into its own folder.

    Mirrors bq-assess `--gcp-project all`: per-project failure isolation (one
    403 doesn't stop the rest), empty projects skipped up front, and a summary
    table at the end. Each project writes to <output>/bundle-<project>/bundle/
    (zipped per project with --zip).
    """
    from bq_assess.core.project_discovery import discover_projects

    console.print("\n[bold]Discovering accessible GCP projects...[/bold]")
    discovered = discover_projects(params.get("credentials"))

    if not discovered:
        console.print("[red]No accessible GCP projects found for these credentials.[/red]")
        sys.exit(1)

    collectable = [pid for pid, has_data in discovered if has_data]
    skipped = [pid for pid, has_data in discovered if not has_data]

    console.print(
        f"[green]✓ Found {len(discovered)} project(s):[/green] "
        f"{len(collectable)} with BigQuery datasets, {len(skipped)} empty."
    )
    for pid in skipped:
        console.print(f"  [dim]— skipping {pid} (no datasets)[/dim]")

    from bq_assess.core.reservation_reader import ReservationCache
    fleet_reservation_cache = ReservationCache()

    results: list[tuple[str, str]] = [(pid, "SKIPPED (no datasets)") for pid in skipped]
    for i, project_id in enumerate(collectable, 1):
        console.print(f"\n[bold cyan]{'═' * 70}[/bold cyan]")
        console.print(f"[bold cyan]Project {i}/{len(collectable)}: {project_id}[/bold cyan]")
        console.print(f"[bold cyan]{'═' * 70}[/bold cyan]")

        project_params = dict(params)
        project_params["gcp_project"] = project_id
        project_params["reservation_cache"] = fleet_reservation_cache
        project_out = os.path.join(output, f"bundle-{project_id}")
        try:
            _collect_one(project_params, project_out, zip_bundle)
            results.append((project_id, "OK"))
        except SystemExit:
            # collect() exits on fatal per-project errors — record and move on
            # rather than aborting the remaining projects.
            results.append((project_id, "FAILED"))
        except Exception as exc:
            logger.exception("Collection failed for project %s", project_id)
            console.print(f"[red]✗ {project_id}: {exc}[/red]")
            results.append((project_id, "FAILED"))

    # Report any admin projects that were permission-denied across the fleet
    fleet_reservation_cache.print_fleet_denied_summary(console)

    console.print(f"\n[bold]{'═' * 70}[/bold]")
    console.print("[bold]Multi-project collection summary[/bold]")
    for pid, status in results:
        style = "green" if status == "OK" else ("red" if status == "FAILED" else "dim")
        console.print(f"  [{style}]{pid}: {status}[/{style}]")
    failed = sum(1 for _, s in results if s == "FAILED")
    if failed:
        console.print(f"\n[yellow]⚠ {failed} project(s) failed — re-run individually with --gcp-project <id>.[/yellow]")


def _print_collection_summary(bundle, bundle_dir: str, zip_path: str | None = None) -> None:
    """Show what was collected and what is leaving the environment."""
    console.print()
    console.rule("[bold cyan]Collection Summary[/bold cyan]")
    console.print(f"  Project:            {bundle.project_id}")
    console.print(f"  Entities:           {len(bundle.entities)}")
    console.print(f"  Region:             {bundle.bq_location} (AWS: {bundle.aws_region})")
    console.print(f"  Workload data:      {'yes' if bundle.workload else 'no'}")
    console.print(f"  Pricing detection:  {'yes' if bundle.pricing else 'no'}")
    console.print(f"  Rate snapshot:      {'yes' if bundle.rates else 'no'}")
    console.print(
        f"  Query statements:   "
        f"{len(bundle.queries) if bundle.queries else 'none (excluded or unavailable)'}"
        f"{' — anonymized, literals stripped' if bundle.queries else ''}"
    )
    console.print(f"  Scan failures:      {len(bundle.failures)}")
    console.print(f"  Storage basis:      {bundle.storage_basis}")

    if zip_path:
        steps = (
            f"1. Review the archive's JSON files ([dim]unzip -l {os.path.basename(zip_path)}[/dim])\n"
            f"   — everything the bundle contains is plain text you can audit.\n"
            f"2. Send [cyan]{zip_path}[/cyan] to your AWS contact."
        )
    else:
        steps = (
            f"1. Review the JSON files in [cyan]{bundle_dir}[/cyan] — everything the bundle\n"
            f"   contains is plain text you can audit.\n"
            f"2. Zip the directory and send it to your AWS contact:\n"
            f"   [dim]cd {os.path.dirname(bundle_dir) or '.'} && zip -r bundle.zip "
            f"{os.path.basename(bundle_dir)}/[/dim] (or re-run with [cyan]--zip[/cyan])"
        )
    console.print(Panel.fit(
        f"[bold]Next steps[/bold]\n"
        f"{steps}\n\n"
        f"[dim]{DATA_HANDLING}[/dim]",
        border_style="cyan",
    ))
    console.print(f"\n[dim]{CLI_ONE_LINER}[/dim]\n")


if __name__ == "__main__":
    main()
