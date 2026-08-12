"""Tests for storage-placement integration in migration_scripts.py (ADR-0005)."""
from __future__ import annotations

import json

from bq_assess.engine.athena.migration_scripts import generate_migration_scripts
from bq_assess.models import (
    ConfidenceLevel,
    MigrationDML,
    StoragePlacement,
    StorageTarget,
)


def _dml(table="ds.t1"):
    return MigrationDML(
        table=table,
        statements=[f"INSERT INTO {table} SELECT * FROM src"],
        shortcomings=[],
        post_optimization=[],
        estimated_scan_bytes=1024,
    )


def _rms_placement():
    return StoragePlacement(
        target=StorageTarget.RMS,
        signals=["Type fidelity: GEOGRAPHY column(s) (boundary) map to native Redshift types"],
        confidence=ConfidenceLevel.HIGH,
        redshift_ddl='CREATE TABLE ds.t1 (id BIGINT, boundary GEOMETRY);',
        redshift_load=["INSERT INTO ds.t1 SELECT id, ST_GeomFromText(boundary) FROM iceberg.t1;"],
    )


def _iceberg_placement():
    return StoragePlacement(
        target=StorageTarget.ICEBERG,
        signals=["No RMS-favoring signals"],
        confidence=ConfidenceLevel.HIGH,
    )


def test_plan_json_carries_storage_target(tmp_path):
    plans = {"ds.t1": _dml("ds.t1"), "ds.t2": _dml("ds.t2")}
    placements = {"ds.t1": _rms_placement(), "ds.t2": _iceberg_placement()}
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans=plans, connector_name="bq-conn",
        target_region="ap-southeast-2", storage_placements=placements,
    )
    plan = json.loads((tmp_path / "migration" / "plan.json").read_text())
    by_name = {t["table"]: t for t in plan["tables"]}
    assert by_name["ds.t1"]["storage_target"] == "rms"
    assert by_name["ds.t2"]["storage_target"] == "iceberg"
    assert "redshift_phase" in by_name["ds.t1"]
    assert "redshift_phase" not in by_name["ds.t2"]
    assert by_name["ds.t1"]["redshift_phase"]["ddl"].startswith("CREATE TABLE")


def test_plan_json_defaults_to_iceberg_without_placements(tmp_path):
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2",
    )
    plan = json.loads((tmp_path / "migration" / "plan.json").read_text())
    assert plan["tables"][0]["storage_target"] == "iceberg"


def test_redshift_phase_sql_written_only_when_rms_tables_exist(tmp_path):
    plans = {"ds.t1": _dml("ds.t1")}
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans=plans, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _iceberg_placement()},
    )
    assert not (tmp_path / "migration" / "redshift_phase.sql").exists()

    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans=plans, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    sql = (tmp_path / "migration" / "redshift_phase.sql").read_text()
    assert "CREATE EXTERNAL SCHEMA" in sql      # prerequisite documented
    assert "CREATE TABLE ds.t1" in sql
    assert "ST_GeomFromText" in sql
    assert "run AFTER run_migration.py" in sql


def test_migration_guide_mentions_rms_phase(tmp_path):
    plans = {"ds.t1": _dml("ds.t1")}
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans=plans, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    guide = (tmp_path / "migration" / "MIGRATION_GUIDE.html").read_text()
    assert "Redshift Phase (RMS-Placed Tables)" in guide
    assert "redshift_phase.sql" in guide
    assert "staging" in guide.lower()


def test_run_migration_orchestrator_flags_rms_next_step(tmp_path):
    plans = {"ds.t1": _dml("ds.t1")}
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans=plans, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert 'storage_target' in script
    assert "redshift_phase.sql" in script


def test_run_migration_honors_workgroup_flag(tmp_path):
    """--workgroup must reach start_query_execution, not just the banner (finding F5)."""
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2",
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert "workgroup or WORKGROUP" in script
    assert "workgroup=args.workgroup" in script


def test_run_migration_polls_with_timeout_and_backoff(tmp_path):
    """wait_for_query must cancel a wedged query instead of spinning forever (finding F6)."""
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2",
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert "ATHENA_MAX_WAIT_SECONDS" in script
    assert "stop_query_execution" in script
    assert "delay = min(delay * 2" in script


def test_run_migration_has_phase_3_for_rms(tmp_path):
    """RMS tables are driven by the orchestrator via redshift-data (finding F10)."""
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert "def run_phase_3" in script
    assert "redshift-data" in script
    assert "--redshift-workgroup" in script
    # Phase 3 is gated on Phase 2 success
    assert "not proceeding to Phase 3" in script


def test_migration_names_single_source():
    """Connector/workgroup names derive from one helper everywhere (finding F7)."""
    from bq_assess.engine.athena.naming import connector_name, workgroup_name

    assert connector_name("my_dataset") == "bq-connector-my-dataset"
    assert workgroup_name("my_dataset") == "bq-migration-my-dataset"

    import inspect

    from bq_assess.engine.athena import migration, terraform
    tf_src = inspect.getsource(terraform)
    mig_src = inspect.getsource(migration)
    assert 'f"bq-connector-' not in tf_src
    assert 'f"bq-migration-' not in tf_src
    assert 'f"bq-connector-' not in mig_src


def test_phase3_creates_external_schema_before_loads(tmp_path):
    """Deep audit HRI-1: Phase 3 must ensure the iceberg external schema exists
    (idempotently) before its INSERT..SELECTs, instead of assuming it."""
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert "CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg" in script
    assert "aborting Phase 3" in script


def test_phase_specific_wait_ceilings(tmp_path):
    """Deep audit HRI-2/HRI-3: Athena waits ~the 30-min DML quota (server kills
    first); Redshift waits up to the 24h Data API max and never cancels on timeout."""
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml()}, connector_name=None,
        target_region="ap-southeast-2", storage_placements={"ds.t1": _rms_placement()},
    )
    script = (tmp_path / "migration" / "run_migration.py").read_text()
    assert "ATHENA_MAX_WAIT_SECONDS = 35 * 60" in script
    assert "REDSHIFT_MAX_WAIT_SECONDS = 24 * 3600" in script
    assert "MAX_QUERY_WAIT_SECONDS" not in script  # old single ceiling removed
    assert "NOT cancelling" in script              # Redshift timeout: report, don't cancel
    assert "DML query timeout" in script           # Athena timeout attribution + quota path
    assert "240 min" in script


def test_guide_and_setup_use_doc_verified_credential_var(tmp_path):
    """Deep audit HRI-5: the connector credential var is secret_manager_gcp_creds_name
    (doc-verified); GOOGLE_CREDENTIAL must not appear anywhere."""
    from bq_assess.engine.athena.migration import generate_source_db_setup
    class _T:
        full_name = "ds.t1"
        entity_id = "t1"
    stmts, _ = generate_source_db_setup(
        dataset_id="ds", gcp_project="proj", tables=[_T()], target_region="ap-southeast-2",
    )
    text = "\n".join(stmts)
    assert "GOOGLE_CREDENTIAL" not in text
    assert "secret_manager_gcp_creds_name" in text


# ── Rebuilt entities deliverable (views/MVs/UDFs/procedures) ─────────────────

from datetime import datetime, timezone

from bq_assess.models import (
    EntityMetadata,
    EntityPopulation,
    EntityType,
    RoutineMetadata,
    TranslationResult,
)


def _rebuilt(full_name, etype, **kw):
    return EntityMetadata(
        entity_id=full_name.split(".")[1],
        dataset_id=full_name.split(".")[0],
        full_name=full_name,
        entity_type=etype,
        population=EntityPopulation.REBUILT,
        num_rows=0, num_bytes=0, columns=[],
        time_partitioning=None, range_partitioning=None, clustering_fields=None,
        view_query=kw.get("view_query"), mview_query=kw.get("mview_query"),
        routine=kw.get("routine"), depends_on=[],
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


def test_rebuilt_entities_sql_written(tmp_path):
    """Views/MVs/UDFs must appear in the migration deliverable — previously they
    were only visible in the assessment report (2026-07-30 review gap)."""
    entities = [
        _rebuilt("ds.v_ok", EntityType.VIEW, view_query="SELECT 1"),
        _rebuilt("ds.mv_x", EntityType.MATERIALIZED_VIEW, mview_query="SELECT 2"),
        _rebuilt("ds.fn_js", EntityType.ROUTINE, routine=RoutineMetadata(
            name="fn_js", language="JAVASCRIPT", arguments=[], body="return 1;",
            routine_type="SCALAR_FUNCTION")),
    ]
    translations = {
        "ds.v_ok": TranslationResult(redshift_sql="SELECT 1", confidence="HIGH",
                                     warnings=[], target_engine="athena"),
        "ds.mv_x": TranslationResult(redshift_sql="SELECT 2", confidence="HIGH",
                                     warnings=[], target_engine="athena"),
        "ds.fn_js": TranslationResult(
            redshift_sql="-- Not translatable", confidence="LOW",
            warnings=["BLOCKER: JavaScript UDF — manual rewrite required"],
            target_engine="athena"),
    }
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="ap-southeast-2",
        rebuilt_entities=entities, translation_results=translations,
    )
    sql = (tmp_path / "migration" / "rebuilt_entities.sql").read_text()
    assert "CREATE OR REPLACE VIEW ds.v_ok AS\nSELECT 1;" in sql
    assert "Athena cannot CREATE MATERIALIZED VIEW" in sql
    assert "MANUAL REWRITE REQUIRED" in sql
    assert "JavaScript UDF" in sql

    guide = (tmp_path / "migration" / "MIGRATION_GUIDE.html").read_text()
    assert "Rebuilt Entities" in guide
    assert "rebuilt_entities.sql" in guide


def test_no_rebuilt_entities_no_file(tmp_path):
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="ap-southeast-2",
    )
    assert not (tmp_path / "migration" / "rebuilt_entities.sql").exists()
    guide = (tmp_path / "migration" / "MIGRATION_GUIDE.html").read_text()
    assert "Rebuilt Entities" not in guide


def _generated_module(tmp_path):
    """Import the generated run_migration.py as a module for direct testing.

    The script sys.exit(1)s when boto3 is absent (CI runners don't install
    it), so stub the import — these tests exercise pure statement-rewriting
    logic, never a real client.
    """
    import importlib.util
    import sys
    import types
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="ap-southeast-2",
    )
    spec = importlib.util.spec_from_file_location(
        "run_migration_gen", tmp_path / "migration" / "run_migration.py"
    )
    mod = importlib.util.module_from_spec(spec)
    had_boto3 = "boto3" in sys.modules
    if not had_boto3:
        sys.modules["boto3"] = types.ModuleType("boto3")
    try:
        spec.loader.exec_module(mod)
    finally:
        if not had_boto3:
            del sys.modules["boto3"]
    return mod


def test_generated_script_compiles_and_has_rechunk(tmp_path):
    """The emitted orchestrator must be valid Python (an unterminated f-string
    shipped once — 2026-07-30) and carry the STEP-0 rechunk machinery."""
    mod = _generated_module(tmp_path)
    assert hasattr(mod, "rechunk_statements")
    assert hasattr(mod, "ensure_intelligent_tiering")


def test_rechunk_rewrites_windows_from_source_range(tmp_path, monkeypatch):
    """Baked chunk windows derive from last_modified→now and miss historical
    rows (live: employees loaded 0 of 45). rechunk_statements must rewrite the
    DELETE/INSERT windows to the STEP-0 MIN/MAX of the actual source data."""
    mod = _generated_module(tmp_path)
    statements = [
        "-- STEP 0\nSELECT MIN(d) AS min_val, MAX(d) AS max_val FROM src.t;\n",
        (
            "DELETE FROM ds.t1 WHERE d >= DATE '2026-04-13' AND d < DATE '2026-07-12';\n"
            "INSERT INTO ds.t1\nSELECT * FROM src.t\n"
            "WHERE d >= DATE '2026-04-13' AND d < DATE '2026-07-12';\n"
        ),
    ]
    monkeypatch.setattr(mod, "_fetch_scalar_row", lambda *a, **k: ("2026-01-31", "2026-04-12"))
    out = mod.rechunk_statements(None, statements, dry_run=False)
    assert out, "rechunk must emit windows for a non-empty source"
    assert "DATE '2026-01-31'" in out[0]
    joined = "".join(out)
    assert "2026-04-12" in joined or "2026-04-13" in joined  # range covers MAX
    assert "2026-07-12" not in joined  # stale baked window gone


def test_rechunk_empty_source_returns_no_statements(tmp_path, monkeypatch):
    """A source with no rows in the partition column has nothing to load —
    the table must be treated as done, not failed."""
    mod = _generated_module(tmp_path)
    statements = [
        "SELECT MIN(d) AS min_val, MAX(d) AS max_val FROM src.t;\n",
        (
            "DELETE FROM ds.t1 WHERE d >= DATE '2026-04-13' AND d < DATE '2026-07-12';\n"
            "INSERT INTO ds.t1 SELECT * FROM src.t WHERE d >= DATE '2026-04-13' AND d < DATE '2026-07-12';\n"
        ),
    ]
    monkeypatch.setattr(mod, "_fetch_scalar_row", lambda *a, **k: (None, None))
    assert mod.rechunk_statements(None, statements, dry_run=False) == []


def test_rechunk_passthrough_for_unchunked(tmp_path):
    """Single-INSERT tables carry no discovery query — statements unchanged."""
    mod = _generated_module(tmp_path)
    statements = ["INSERT INTO ds.t1 SELECT * FROM src.t;"]
    assert mod.rechunk_statements(None, statements, dry_run=False) == statements


def test_phase3_prepares_schema_link_and_external_schema_per_dataset(tmp_path):
    """Phase 3 live-verification (2026-07-30): Redshift cannot address the
    s3tablescatalog federated sub-catalog directly — each RMS dataset needs a
    target schema, a Glue resource link in the DEFAULT catalog, and an external
    schema over that link. The generated script must carry all three."""
    mod = _generated_module(tmp_path)
    import inspect
    src = inspect.getsource(mod.run_phase_3)
    assert "CREATE SCHEMA IF NOT EXISTS" in src
    assert "create_database" in src          # Glue resource link
    assert "TargetDatabase" in src
    assert "CREATE EXTERNAL SCHEMA IF NOT EXISTS iceberg_" in src  # per-dataset
    assert "_rl" in src                       # link naming


def test_phase3_is_idempotent(tmp_path):
    """Re-running Phase 3 must not fail on the existing relation nor double
    rows: IF NOT EXISTS on the DDL, TRUNCATE before the INSERT."""
    mod = _generated_module(tmp_path)
    import inspect
    src = inspect.getsource(mod.run_phase_3)
    assert 'replace("CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1)' in src
    assert "TRUNCATE TABLE" in src


def test_rms_staging_schema_is_per_dataset():
    """The Iceberg staging tables live in per-dataset namespaces; a single flat
    'iceberg' external schema can only map one Glue database, so the load SQL
    must reference iceberg_<dataset>.<table> (live-verified 2026-07-30)."""
    from datetime import datetime, timezone

    import bq_assess.models as m
    from bq_assess.engine.redshift.storage_placement import StoragePlacementAdvisor

    entity = m.EntityMetadata(
        entity_id="events", dataset_id="obs", full_name="obs.events",
        entity_type=m.EntityType.TABLE, population=m.EntityPopulation.TABLE,
        num_rows=10, num_bytes=100,
        columns=[
            m.ColumnSchema(name="id", field_type="INT64", mode="REQUIRED"),
            m.ColumnSchema(name="payload", field_type="JSON", mode="NULLABLE"),
        ],
        time_partitioning=None, range_partitioning=None, clustering_fields=None,
        view_query=None, mview_query=None, routine=None, depends_on=[],
        last_modified=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    placement = StoragePlacementAdvisor().recommend(entity)
    assert placement.target == m.StorageTarget.RMS  # JSON column forces RMS
    load_sql = "\n".join(placement.redshift_load)
    assert "iceberg_obs." in load_sql
    assert "FROM iceberg." not in load_sql


def test_rebuilt_view_names_quoted(tmp_path):
    """View names with spaces/hyphens (legal in BQ) must be quoted in the
    emitted CREATE OR REPLACE VIEW (2026-07-31 sandbox estate:
    'sandbox.Inf Count Switches' emitted unquoted)."""
    from types import SimpleNamespace

    from bq_assess.engine.athena.migration_scripts import _write_rebuilt_entities_sql
    from bq_assess.models import EntityType

    entity = SimpleNamespace(
        full_name="sandbox.Inf Count Switches",
        entity_type=EntityType.VIEW,
    )
    tr = SimpleNamespace(redshift_sql="SELECT 1", warnings=[])
    summary = _write_rebuilt_entities_sql(
        tmp_path, [entity], {"sandbox.Inf Count Switches": tr}
    )
    assert summary["views"] == 1
    sql = (tmp_path / "rebuilt_entities.sql").read_text()
    assert 'CREATE OR REPLACE VIEW sandbox."Inf Count Switches" AS' in sql


def test_rebuilt_entities_redshift_dialect_header(tmp_path):
    """2026-08-04 audit: the header said 'run in the Athena workgroup' around
    Redshift-dialect SQL. Instructions must match TranslationResult.target_engine."""
    entities = [_rebuilt("ds.v1", EntityType.VIEW, view_query="SELECT 1")]
    translations = {
        "ds.v1": TranslationResult(redshift_sql="SELECT 1", confidence="HIGH",
                                   warnings=[], target_engine="redshift"),
    }
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="eu-west-1",
        rebuilt_entities=entities, translation_results=translations,
    )
    sql = (tmp_path / "migration" / "rebuilt_entities.sql").read_text()
    assert "Redshift Serverless" in sql
    assert "do NOT" in sql and "Athena workgroup" in sql
    assert "Athena accepts one per call" not in sql


def test_rebuilt_entities_athena_dialect_header(tmp_path):
    entities = [_rebuilt("ds.v1", EntityType.VIEW, view_query="SELECT 1")]
    translations = {
        "ds.v1": TranslationResult(redshift_sql="SELECT 1", confidence="HIGH",
                                   warnings=[], target_engine="athena"),
    }
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="eu-west-1",
        rebuilt_entities=entities, translation_results=translations,
    )
    sql = (tmp_path / "migration" / "rebuilt_entities.sql").read_text()
    assert "Dialect: Athena (Trino)" in sql


def test_rebuilt_view_missing_dependency_flagged(tmp_path):
    """A view depending on a table absent from the plan can never validate —
    it must carry a WARNING naming the missing table."""
    v = _rebuilt("ds.v1", EntityType.VIEW, view_query="SELECT 1")
    v.depends_on = ["curated.not_migrated", "ds.t1"]
    translations = {
        "ds.v1": TranslationResult(redshift_sql="SELECT 1", confidence="HIGH",
                                   warnings=[], target_engine="redshift"),
    }
    generate_migration_scripts(
        project_dir=str(tmp_path), migration_plans={"ds.t1": _dml("ds.t1")},
        connector_name="bq-conn", target_region="eu-west-1",
        rebuilt_entities=[v], translation_results=translations,
    )
    sql = (tmp_path / "migration" / "rebuilt_entities.sql").read_text()
    assert "depends on curated.not_migrated" in sql
    assert "NOT in this migration plan" in sql
    # the satisfied dependency is not flagged
    assert "depends on ds.t1" not in sql
