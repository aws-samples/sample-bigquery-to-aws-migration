"""Tests for bq-collect CLI: --gcp-project all fan-out and --zip (2026-08-03)."""
from __future__ import annotations

import os
import zipfile
from unittest.mock import patch

from click.testing import CliRunner

from bq_assess.collect_cli import _zip_bundle_dir, main


def _fake_bundle(project_id: str):
    """Minimal stand-in for collector.collect()'s Bundle."""
    from types import SimpleNamespace
    return SimpleNamespace(
        project_id=project_id, entities=[], bq_location="EU", aws_region="eu-west-1",
        workload=None, pricing=None, rates=None, queries=[], failures=[],
        storage_basis="assumed",
    )


def _fake_writer_write(self, bundle, out_dir):
    bundle_dir = os.path.join(out_dir, "bundle")
    os.makedirs(bundle_dir, exist_ok=True)
    with open(os.path.join(bundle_dir, "manifest.json"), "w", encoding="utf-8") as f:
        f.write(f'{{"gcp_project": "{bundle.project_id}"}}')
    return bundle_dir


class TestZipBundleDir:
    def test_zip_written_next_to_parent_with_parent_root(self, tmp_path):
        """Archive lands at <parent>.zip with entries rooted at the parent
        folder name — unzipping never spills into cwd, and the loader's
        recursive manifest search resolves it."""
        parent = tmp_path / "bundle-proj-a"
        bdir = parent / "bundle"
        bdir.mkdir(parents=True)
        (bdir / "manifest.json").write_text("{}")
        (bdir / "tables.json").write_text("[]")

        zip_path = _zip_bundle_dir(str(bdir))

        assert zip_path == str(tmp_path / "bundle-proj-a.zip")
        with zipfile.ZipFile(zip_path) as zf:
            names = sorted(zf.namelist())
        assert names == [
            "bundle-proj-a/bundle/manifest.json",
            "bundle-proj-a/bundle/tables.json",
        ]


class TestCollectAllAndZip:
    def _invoke(self, tmp_path, args, discovered, collect_side_effect=None):
        collected: list[str] = []

        def fake_collect(params):
            pid = params["gcp_project"]
            if collect_side_effect and pid in collect_side_effect:
                raise collect_side_effect[pid]
            collected.append(pid)
            return _fake_bundle(pid)

        with patch("bq_assess.collect_cli.collect", side_effect=fake_collect), \
             patch("bq_assess.bundle.BundleWriter.write", _fake_writer_write), \
             patch("bq_assess.core.project_discovery.discover_projects",
                   return_value=discovered):
            result = CliRunner().invoke(main, args, catch_exceptions=False)
        return result, collected

    def test_all_collects_each_project_into_own_folder(self, tmp_path):
        out = str(tmp_path / "out")
        result, collected = self._invoke(
            tmp_path,
            ["--gcp-project", "all", "--use-adc", "--output", out],
            discovered=[("proj-a", True), ("proj-b", True), ("proj-empty", False)],
        )
        assert result.exit_code == 0, result.output
        assert collected == ["proj-a", "proj-b"]
        assert os.path.isdir(os.path.join(out, "bundle-proj-a", "bundle"))
        assert os.path.isdir(os.path.join(out, "bundle-proj-b", "bundle"))
        assert "SKIPPED" in result.output          # empty project surfaced
        assert "proj-empty" in result.output

    def test_all_isolates_per_project_failures(self, tmp_path):
        out = str(tmp_path / "out")
        result, collected = self._invoke(
            tmp_path,
            ["--gcp-project", "all", "--use-adc", "--output", out],
            discovered=[("proj-a", True), ("proj-broken", True), ("proj-c", True)],
            collect_side_effect={"proj-broken": RuntimeError("403 boom")},
        )
        assert result.exit_code == 0, result.output
        assert collected == ["proj-a", "proj-c"]   # continued past the failure
        assert "proj-broken: FAILED" in result.output
        assert "proj-a: OK" in result.output and "proj-c: OK" in result.output

    def test_all_with_zip_leaves_only_zips(self, tmp_path):
        """The customer hand-off shape: one top folder holding one zip per
        project — the unzipped bundle trees are removed (2026-08-03)."""
        out = str(tmp_path / "out")
        result, _ = self._invoke(
            tmp_path,
            ["--gcp-project", "all", "--use-adc", "--output", out, "--zip"],
            discovered=[("proj-a", True), ("proj-b", True)],
        )
        assert result.exit_code == 0, result.output
        assert sorted(os.listdir(out)) == ["bundle-proj-a.zip", "bundle-proj-b.zip"]

    def test_single_project_zip_removes_directory(self, tmp_path):
        out = str(tmp_path / "single")
        result, collected = self._invoke(
            tmp_path,
            ["--gcp-project", "proj-x", "--use-adc", "--output", out, "--zip"],
            discovered=[],  # discovery must not be called for a named project
        )
        assert result.exit_code == 0, result.output
        assert collected == ["proj-x"]
        assert os.path.isfile(str(tmp_path / "single.zip"))
        assert not os.path.exists(out)             # tree replaced by the zip
        assert "Bundle zipped" in result.output

    def test_single_project_without_zip_hints_flag(self, tmp_path):
        out = str(tmp_path / "nozip")
        result, _ = self._invoke(
            tmp_path,
            ["--gcp-project", "proj-x", "--use-adc", "--output", out],
            discovered=[],
        )
        assert result.exit_code == 0, result.output
        assert not os.path.exists(str(tmp_path / "nozip.zip"))
        assert "--zip" in result.output            # next-steps hint mentions the flag

    def test_zip_never_removes_shared_output_dir(self, tmp_path):
        """--output may be a folder the customer already uses: only the bundle
        tree is replaced; sibling files survive."""
        out = str(tmp_path / "shared")
        os.makedirs(out)
        keep = os.path.join(out, "notes.txt")
        with open(keep, "w", encoding="utf-8") as f:
            f.write("keep me")
        result, _ = self._invoke(
            tmp_path,
            ["--gcp-project", "proj-x", "--use-adc", "--output", out, "--zip"],
            discovered=[],
        )
        assert result.exit_code == 0, result.output
        assert os.path.isfile(keep)                # sibling untouched
        assert os.path.isfile(str(tmp_path / "shared.zip"))
        assert not os.path.exists(os.path.join(out, "bundle"))
