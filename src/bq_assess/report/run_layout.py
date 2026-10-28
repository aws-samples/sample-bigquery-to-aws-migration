"""Where a run's outputs land, and how a reader finds the newest run.

Layout (0.10.0+)::

    <output>/
      latest-<project>.json                 names the newest SUCCESSFUL run's files
      <project>_<YYYY-MM-DD>_<HHMMSS>/      one folder per run, never reused
        README.html
        report/  terraform/  migration/  bundle/  query-workload/

Before 0.10.0 the run folder was ``<project>_<YYYY-MM-DD>``, so every run on the
same day wrote into the same folder. Two problems followed, both confirmed on the
my-project outputs:

- Report JSON piled up (each file name carries a unique assessment ID) while the
  single HTML was overwritten, so one folder held several runs' JSON next to one
  run's HTML. The ID ends in a random hash, so "last file by name" picked a random
  run: the 2026-09-20 folder held two runs and name order picked the OLDER one.
  The bq-assess skill's "grant the permission and re-run" path is same-day by
  definition, so it could re-read the old, degraded report after the user fixed it.
- A re-run regenerated ``terraform/*.tf`` in place, next to any
  ``terraform.tfstate`` left by an earlier ``terraform apply``. The 2026-08-06
  testbed folder holds one tracking 23 live AWS resources.

Clearing the folder on re-run was rejected: it would delete that state file and
orphan the resources it tracks. Instead every run gets a fresh folder, nothing is
ever deleted, and the pointer file tells readers (the bq-assess skill, integrating
tools) exactly which files belong to the newest run — so nothing has to guess from
folder or file names.

Times are UTC. Names sort chronologically: ``_134305`` < ``_134305-2`` < ``_134306``.
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

POINTER_PREFIX = "latest-"
# Keys the CLI may write into the pointer's "files" object. Consumers (the
# bq-assess skill, integrating tools) depend on these names, so tests tie both
# the skill's references and a real run's pointer to this list.
POINTER_FILE_KEYS = (
    "landing_json", "effort_json", "query_json", "report_html", "readme_html",
    "plan_json", "migration_dir", "terraform_dir", "bundle", "query_workload_dir",
)
# Collision suffixes are only ever needed for runs started in the same second;
# the cap turns a pathological filesystem into an error instead of a hang.
_MAX_SAME_SECOND_RUNS = 1000


def _utc(when: datetime) -> datetime:
    """Normalise to UTC; a naive datetime is taken to already be UTC."""
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def run_folder_name(project_id: str, when: datetime) -> str:
    """``<project>_<YYYY-MM-DD>_<HHMMSS>`` for a run generated at ``when`` (UTC)."""
    return f"{project_id}_{_utc(when):%Y-%m-%d_%H%M%S}"


def legacy_run_folder_name(project_id: str, when: datetime) -> str:
    """The pre-0.10.0 ``<project>_<YYYY-MM-DD>`` name.

    Only for Assessments that carry no recorded ``run_folder`` (objects built
    outside ``analyze_and_report``, e.g. test fixtures). Recomputing a name from a
    timestamp is otherwise unsafe: a same-second collision suffix cannot be
    reconstructed, so callers must use the recorded folder name.
    """
    return f"{project_id}_{_utc(when):%Y-%m-%d}"


def allocate_run_folder(
    base_output_dir: str | os.PathLike, project_id: str, when: datetime
) -> str:
    """Create a fresh, never-reused run folder under ``base_output_dir``; return its name.

    ``mkdir`` without ``exist_ok`` makes the reservation atomic: two runs started in
    the same second (back-to-back in tests, or parallel invocations) get ``-2``,
    ``-3`` instead of sharing a folder — sharing is the defect this module removes.
    """
    base = Path(base_output_dir)
    base.mkdir(parents=True, exist_ok=True)
    stem = run_folder_name(project_id, when)
    name = stem
    for n in range(2, _MAX_SAME_SECOND_RUNS + 2):
        try:
            (base / name).mkdir()
            return name
        except FileExistsError:
            name = f"{stem}-{n}"
    raise RuntimeError(
        f"could not allocate a run folder for {project_id!r} under {base}: "
        f"{_MAX_SAME_SECOND_RUNS} folders already exist for {stem}"
    )


def latest_pointer_name(project_id: str) -> str:
    """``latest-<project>.json``.

    One pointer per project, so assessing project A and then project B into the
    same output folder never makes A's pointer name B's run. GCP project IDs are
    ``[a-z0-9-]``, so the name is filesystem-safe everywhere.
    """
    return f"{POINTER_PREFIX}{project_id}.json"


def write_latest_pointer(
    base_output_dir: str | os.PathLike,
    provenance: dict,
    run_folder: str,
    files: dict[str, str | os.PathLike | None],
) -> str:
    """Write ``<output>/latest-<project>.json`` atomically; return its path.

    Call it LAST, after every artifact is on disk: the pointer then only ever names
    a complete run, and a run that fails midway leaves the previous pointer intact.

    ``files`` maps a stable key (``landing_json``, ``report_html``, ...) to a path.
    Missing or unset entries are dropped rather than written as dangling paths.
    Paths are stored POSIX-style and relative to the output folder — the pointer's
    own directory — so the whole output folder can be moved or zipped intact.

    The write goes to a temp file in the same directory and is renamed into place,
    so a reader never sees a half-written pointer.
    """
    base = Path(base_output_dir)
    relative: dict[str, str] = {}
    for key, path in files.items():
        if not path or not Path(path).exists():
            continue
        relative[key] = Path(os.path.relpath(Path(path), base)).as_posix()

    document = {
        **provenance,
        "run_folder": run_folder,
        "paths_relative_to": "the directory containing this file",
        "files": relative,
    }
    target = base / latest_pointer_name(provenance["project_id"])
    fd, tmp_path = tempfile.mkstemp(prefix=".latest-", suffix=".json.tmp", dir=str(base))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp_path, target)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return str(target)
