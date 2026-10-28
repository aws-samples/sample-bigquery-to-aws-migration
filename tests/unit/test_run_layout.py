"""Run-folder naming and the latest-run pointer (report/run_layout.py).

Before 0.10.0 runs shared <project>_<date>/ for a whole UTC day. On the testbed
that left two runs' JSON beside one overwritten HTML, and "last file by name"
picked the older run because assessment IDs end in a random hash.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from bq_assess.report.run_layout import (
    allocate_run_folder,
    latest_pointer_name,
    legacy_run_folder_name,
    run_folder_name,
    write_latest_pointer,
)

_T = datetime(2026, 10, 27, 13, 43, 5, tzinfo=timezone.utc)
_PROV = {
    "schema_version": 2,
    "tool_version": "9.9.9",
    "assessment_id": "assess-20261027-abcd1234",
    "project_id": "my-proj",
    "generated_at": _T.isoformat(),
    "advisory": "x",
}


class TestRunFolderName:
    def test_format(self):
        assert run_folder_name("my-proj", _T) == "my-proj_2026-10-27_134305"

    def test_legacy_format_is_date_only(self):
        assert legacy_run_folder_name("my-proj", _T) == "my-proj_2026-10-27"

    def test_non_utc_input_is_normalised_to_utc(self):
        sydney = _T.astimezone(timezone(timedelta(hours=11)))
        assert run_folder_name("my-proj", sydney) == "my-proj_2026-10-27_134305"

    def test_naive_input_is_treated_as_utc(self):
        assert run_folder_name("my-proj", _T.replace(tzinfo=None)) == "my-proj_2026-10-27_134305"

    def test_names_sort_in_run_order_including_collision_suffixes(self):
        """Name order must equal run order — the defect was name order picking an
        older run. A same-second -2 suffix must sort between its stem and the next
        second."""
        stem = run_folder_name("p", _T)
        later = run_folder_name("p", _T + timedelta(seconds=1))
        next_day = run_folder_name("p", _T + timedelta(days=1))
        in_run_order = [stem, f"{stem}-2", f"{stem}-3", later, next_day]
        assert sorted(in_run_order) == in_run_order


class TestAllocateRunFolder:
    def test_creates_the_folder(self, tmp_path):
        name = allocate_run_folder(tmp_path, "my-proj", _T)
        assert name == "my-proj_2026-10-27_134305"
        assert (tmp_path / name).is_dir()

    def test_same_second_runs_never_share_a_folder(self, tmp_path):
        names = [allocate_run_folder(tmp_path, "my-proj", _T) for _ in range(3)]
        assert names == [
            "my-proj_2026-10-27_134305",
            "my-proj_2026-10-27_134305-2",
            "my-proj_2026-10-27_134305-3",
        ]
        assert all((tmp_path / n).is_dir() for n in names)

    def test_existing_content_is_left_alone(self, tmp_path):
        """A re-run must never touch an earlier folder — it may hold terraform
        state tracking live AWS resources."""
        first = allocate_run_folder(tmp_path, "my-proj", _T)
        state = tmp_path / first / "terraform" / "terraform.tfstate"
        state.parent.mkdir(parents=True)
        state.write_text('{"resources": [1, 2, 3]}')

        allocate_run_folder(tmp_path, "my-proj", _T)

        assert state.read_text() == '{"resources": [1, 2, 3]}'

    def test_creates_missing_base_dir(self, tmp_path):
        base = tmp_path / "does" / "not" / "exist"
        name = allocate_run_folder(base, "my-proj", _T)
        assert (base / name).is_dir()


class TestLatestPointer:
    def _artifacts(self, base, run):
        report = base / run / "report"
        report.mkdir(parents=True)
        landing = report / "assessment-landing-x.json"
        landing.write_text("{}")
        html = report / "my-proj-assessment.html"
        html.write_text("<html/>")
        return landing, html

    def test_name_is_per_project(self):
        assert latest_pointer_name("my-proj") == "latest-my-proj.json"

    def test_paths_are_relative_posix_and_carry_provenance(self, tmp_path):
        run = allocate_run_folder(tmp_path, "my-proj", _T)
        landing, html = self._artifacts(tmp_path, run)

        path = write_latest_pointer(
            tmp_path, _PROV, run, {"landing_json": landing, "report_html": html}
        )

        doc = json.loads((tmp_path / "latest-my-proj.json").read_text())
        assert path == str(tmp_path / "latest-my-proj.json")
        assert doc["files"] == {
            "landing_json": f"{run}/report/assessment-landing-x.json",
            "report_html": f"{run}/report/my-proj-assessment.html",
        }
        assert doc["run_folder"] == run
        for key, value in _PROV.items():
            assert doc[key] == value

    def test_missing_or_unset_files_are_dropped_not_dangling(self, tmp_path):
        run = allocate_run_folder(tmp_path, "my-proj", _T)
        landing, _ = self._artifacts(tmp_path, run)

        write_latest_pointer(tmp_path, _PROV, run, {
            "landing_json": landing,
            "plan_json": tmp_path / run / "migration" / "plan.json",  # never written
            "readme_html": None,
        })

        doc = json.loads((tmp_path / "latest-my-proj.json").read_text())
        assert set(doc["files"]) == {"landing_json"}

    def test_rewrite_replaces_atomically_and_leaves_no_temp_files(self, tmp_path):
        first = allocate_run_folder(tmp_path, "my-proj", _T)
        landing1, _ = self._artifacts(tmp_path, first)
        write_latest_pointer(tmp_path, _PROV, first, {"landing_json": landing1})

        second = allocate_run_folder(tmp_path, "my-proj", _T + timedelta(minutes=5))
        landing2, _ = self._artifacts(tmp_path, second)
        write_latest_pointer(
            tmp_path, {**_PROV, "assessment_id": "assess-2"}, second, {"landing_json": landing2}
        )

        doc = json.loads((tmp_path / "latest-my-proj.json").read_text())
        assert doc["run_folder"] == second
        assert doc["assessment_id"] == "assess-2"
        assert not list(tmp_path.glob(".latest-*")), "temp file left behind"

    def test_failed_write_keeps_the_previous_pointer(self, tmp_path, monkeypatch):
        """A crash while writing must not leave a truncated pointer behind."""
        run = allocate_run_folder(tmp_path, "my-proj", _T)
        landing, _ = self._artifacts(tmp_path, run)
        write_latest_pointer(tmp_path, _PROV, run, {"landing_json": landing})
        before = (tmp_path / "latest-my-proj.json").read_text()

        import bq_assess.report.run_layout as layout

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(layout.json, "dump", boom)
        with pytest.raises(OSError):
            write_latest_pointer(tmp_path, {**_PROV, "assessment_id": "new"}, run, {})

        assert (tmp_path / "latest-my-proj.json").read_text() == before
        assert not list(tmp_path.glob(".latest-*"))
