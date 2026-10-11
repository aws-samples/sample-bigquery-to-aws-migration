"""Petabyte-scale E2E harness. Opt-in: ``pytest -m scale``.

Generates a synthetic 20k-entity, multi-PB bundle, runs the real ``bq-assess
report`` pipeline on it, and asserts the report stays browser-loadable and the
lazy-loading structure intact. 20k keeps the harness ~1 minute; bump --entities
in generate_bundle.py for full 100k runs when touching scale-sensitive code.
"""
from __future__ import annotations

import json
import re
import subprocess  # nosec B404 - test runs the CLI as a subprocess with fixed args, no shell
import sys
from pathlib import Path

import pytest

from tests.scale.generate_bundle import build_bundle

# Ceiling for the single-file report. Measured: flip (17.9k entities) shipped
# 76 MB and loads; 471 MB (100k, pre-budget) does not. The detail/sample byte
# budgets exist to hold this line no matter the entity count.
MAX_REPORT_HTML_BYTES = 200 * 1024 * 1024

N_ENTITIES = 20_000


@pytest.mark.scale
def test_report_e2e_at_scale(tmp_path: Path) -> None:
    from bq_assess.bundle.writer import BundleWriter

    bundle = build_bundle(N_ENTITIES, "pb-scale-harness", seed=7)
    assert sum(e.num_bytes for e in bundle.entities) > 10**15  # genuinely PB-scale
    bundle_dir = BundleWriter().write(bundle, str(tmp_path / "in"))

    out_dir = tmp_path / "out"
    result = subprocess.run(  # noqa: PLW1510 — returncode asserted with captured output below  # nosec B603 B607 - fixed arg list, no shell, test-only
        [sys.executable, "-m", "bq_assess.cli", "report",
         "--bundle", str(bundle_dir), "--output", str(out_dir)],
        capture_output=True, text=True, timeout=1200,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]

    html_path = next(out_dir.glob("*/report/*.html"))
    html = html_path.read_text()

    # 1. Browser-loadable: the byte budgets hold the ceiling.
    assert len(html) < MAX_REPORT_HTML_BYTES, f"report is {len(html)/1e6:.0f} MB"

    # 2. Lazy structure intact: no server-rendered entity rows.
    for tbody in re.findall(r'<tbody data-page-size="100">(.*?)</tbody>', html, re.DOTALL):
        assert not re.findall(r"<tr", tbody), "server-rendered rows defeat the pager"

    # 3. Every entity is in the client-side row data (pagination shows them all).
    data = json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.DOTALL).group(1))
    assert len(data["query"]) == N_ENTITIES

    # 4. Truncation is visible, never silent: if any row lost its detail embed,
    #    the row is marked and the banner renders.
    omitted_rows = [r for r in data["query"] if r.get("detail_omitted")]
    if omitted_rows:
        assert "exceed this report" in html
        assert all(r.get("detail_chunk") is None for r in omitted_rows)

    # 5. PB-scale units render (no scientific notation / unit overflow).
    assert re.search(r">[\d,.]+ PB<", html)
    assert "e+" not in re.search(r'id="report-data">(.*?)</script>', html, re.DOTALL).group(1)[:10000]
