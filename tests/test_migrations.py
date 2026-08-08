from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

import pytest

import app.migrations as migrations
from app.database import Database
from app.migrations import (
    CURRENT_SCHEMA_VERSION,
    FORMAL_SCHEMA_V1_FINGERPRINT,
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_FINGERPRINT,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_FINGERPRINT,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V14_FINGERPRINT,
    LEGACY_SCHEMA_FINGERPRINT,
    LEGACY_SCHEMA_SQL,
    BackupVerificationError,
    DiagnosticWriteError,
    MigrationChecksumError,
    MIGRATION_V1_CHECKSUM,
    MIGRATION_V1_NAME,
    MIGRATION_V2_CHECKSUM,
    MIGRATION_V2_NAME,
    MIGRATION_V3_CHECKSUM,
    MIGRATION_V3_NAME,
    MIGRATION_V4_CHECKSUM,
    MIGRATION_V4_NAME,
    MIGRATION_V5_CHECKSUM,
    MIGRATION_V5_NAME,
    MIGRATION_V6_CHECKSUM,
    MIGRATION_V6_NAME,
    MIGRATION_V7_CHECKSUM,
    MIGRATION_V7_NAME,
    MIGRATION_V8_CHECKSUM,
    MIGRATION_V8_NAME,
    MIGRATION_V9_CHECKSUM,
    MIGRATION_V10_CHECKSUM,
    MIGRATION_V10_NAME,
    MIGRATION_V11_CHECKSUM,
    MIGRATION_V11_NAME,
    MIGRATION_V12_CHECKSUM,
    MIGRATION_V12_NAME,
    MIGRATION_V13_CHECKSUM,
    MIGRATION_V13_NAME,
    MIGRATION_V14_CHECKSUM,
    MIGRATION_V14_NAME,
    MIGRATION_V9_NAME,
    NewerSchemaVersionError,
    UnknownSchemaError,
    schema_fingerprint,
)


LEGACY_RUN_ID = "run_20260711_11111111111111111111111111111111"
LEGACY_EVENT_ID = "legacy-event"
TIMESTAMP = "2026-07-11T12:00:00+00:00"


def _create_legacy_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.executescript(LEGACY_SCHEMA_SQL)
        connection.execute(
            """
            INSERT INTO collector_runs(
                run_id, requested_date, status, started_at, completed_at,
                error_message, artifact_zip
            ) VALUES (?, '2026-07-11', 'completed', ?, ?, NULL, 'run.zip')
            """,
            (LEGACY_RUN_ID, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO games(
                event_id, sport_key, commence_time, home_team, away_team,
                home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
            ) VALUES (?, 'baseball_mlb', ?, 'Home', 'Away', 'home', 'away', '{}', ?, ?)
            """,
            (LEGACY_EVENT_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO odds_snapshots(
                run_id, event_id, bookmaker_key, bookmaker_title, market_key,
                outcome_name, price, point, bookmaker_last_update, retrieved_at,
                raw_json
            ) VALUES (?, ?, 'book', 'Book', 'h2h', 'Home', -110, NULL, ?, ?, '{}')
            """,
            (LEGACY_RUN_ID, LEGACY_EVENT_ID, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO weather_snapshots(
                run_id, event_id, provider, forecast_time, temperature_f,
                humidity_pct, precipitation_probability_pct, wind_speed_mph,
                wind_direction_deg, short_forecast, raw_json, retrieved_at
            ) VALUES (?, ?, 'nws', ?, 70, 50, 5, 10, 180, 'Clear', '{}', ?)
            """,
            (LEGACY_RUN_ID, LEGACY_EVENT_ID, TIMESTAMP, TIMESTAMP),
        )
        connection.execute(
            """
            INSERT INTO collector_errors(
                run_id, stage, event_id, message, details, created_at
            ) VALUES (?, 'weather', ?, 'warning', NULL, ?)
            """,
            (LEGACY_RUN_ID, LEGACY_EVENT_ID, TIMESTAMP),
        )
        connection.commit()
        assert schema_fingerprint(connection) == LEGACY_SCHEMA_FINGERPRINT
    finally:
        connection.close()


def _create_formal_v1_database(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("BEGIN IMMEDIATE")
        for statement in FORMAL_SCHEMA_V1_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (1, ?, ?, ?)
            """,
            (MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM, TIMESTAMP),
        )
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V1_FINGERPRINT
    finally:
        connection.close()


def _create_formal_v2_database(path: Path) -> None:
    _create_formal_v1_database(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for statement in FORMAL_SCHEMA_V2_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (2, ?, ?, ?)
            """,
            (MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM, TIMESTAMP),
        )
        connection.execute("PRAGMA user_version=2")
        connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V2_FINGERPRINT
    finally:
        connection.close()


def _create_formal_v3_database(path: Path) -> None:
    _create_formal_v2_database(path)
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        for statement in FORMAL_SCHEMA_V3_STATEMENTS:
            connection.execute(statement)
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (3, ?, ?, ?)
            """,
            (MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM, TIMESTAMP),
        )
        connection.execute("PRAGMA user_version=3")
        connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V3_FINGERPRINT
    finally:
        connection.close()


def test_empty_database_installs_formal_schema_with_atomic_diagnostic(
    tmp_path: Path,
) -> None:
    path = tmp_path / "empty.db"

    database = Database(path)

    info = database.schema_info()
    assert info["version"] == CURRENT_SCHEMA_VERSION
    assert info["fingerprint"] == FORMAL_SCHEMA_V14_FINGERPRINT
    assert database.migration_result.source_kind == "empty"
    diagnostic_path = database.migration_result.diagnostic_path
    assert diagnostic_path is not None
    diagnostic = json.loads(diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic["status"] == "migration_verified"
    assert diagnostic["backup_filename"] is None
    assert Path(diagnostic["database_path"]) == path.resolve()
    assert diagnostic["backup_path"] is None
    assert list(path.with_name(f"{path.name}.migration-backups").glob("*.tmp")) == []


def test_schema_fingerprint_is_deterministic(tmp_path: Path) -> None:
    first = Database(tmp_path / "first.db")
    second = Database(tmp_path / "second.db")

    assert first.schema_info()["fingerprint"] == second.schema_info()["fingerprint"]
    assert first.schema_info()["fingerprint"] == FORMAL_SCHEMA_V14_FINGERPRINT


def test_formal_v1_database_upgrades_transactionally_to_current(tmp_path: Path) -> None:
    path = tmp_path / "formal-v1.db"
    _create_formal_v1_database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute(
        """
        INSERT INTO collector_runs(
            run_id, requested_date, status, created_at, queued_at, started_at,
            completed_at, updated_at, failure_stage, error_message,
            artifact_relpath, app_version, schema_version
        ) VALUES (?, '2026-07-11', 'completed', ?, ?, ?, ?, ?, NULL, NULL,
                  NULL, 'test', 1)
        """,
        (LEGACY_RUN_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP, TIMESTAMP),
    )
    connection.execute(
        """
        INSERT INTO games(
            event_id, sport_key, commence_time, home_team, away_team,
            home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
        ) VALUES (?, 'baseball_mlb', ?, 'Home', 'Away', 'home', 'away', '{}', ?, ?)
        """,
        (LEGACY_EVENT_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP),
    )
    connection.execute(
        "INSERT INTO run_games(run_id, event_id, associated_at) VALUES (?, ?, ?)",
        (LEGACY_RUN_ID, LEGACY_EVENT_ID, TIMESTAMP),
    )
    for _ in range(2):
        connection.execute(
            """
            INSERT INTO odds_snapshots(
                run_id, event_id, bookmaker_key, bookmaker_title, market_key,
                market_last_update, outcome_name, price, point,
                bookmaker_last_update, retrieved_at, raw_json
            ) VALUES (?, ?, 'book', 'Book', 'h2h', ?, 'Home', -110, NULL, ?, ?, '{}')
            """,
            (LEGACY_RUN_ID, LEGACY_EVENT_ID, TIMESTAMP, TIMESTAMP, TIMESTAMP),
        )
    connection.commit()
    connection.close()

    database = Database(path)

    assert database.migration_result.version == CURRENT_SCHEMA_VERSION
    assert database.migration_result.source_kind == "formal_v1"
    assert database.schema_info()["fingerprint"] == FORMAL_SCHEMA_V14_FINGERPRINT
    with database.connect() as connection:
        history = connection.execute(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [tuple(row) for row in history] == [
            (1, MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM),
            (2, MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM),
            (3, MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM),
            (4, MIGRATION_V4_NAME, MIGRATION_V4_CHECKSUM),
            (5, MIGRATION_V5_NAME, MIGRATION_V5_CHECKSUM),
            (6, MIGRATION_V6_NAME, MIGRATION_V6_CHECKSUM),
            (7, MIGRATION_V7_NAME, MIGRATION_V7_CHECKSUM),
            (8, MIGRATION_V8_NAME, MIGRATION_V8_CHECKSUM),
            (9, MIGRATION_V9_NAME, MIGRATION_V9_CHECKSUM),
            (10, MIGRATION_V10_NAME, MIGRATION_V10_CHECKSUM),
            (11, MIGRATION_V11_NAME, MIGRATION_V11_CHECKSUM),
            (12, MIGRATION_V12_NAME, MIGRATION_V12_CHECKSUM),
            (13, MIGRATION_V13_NAME, MIGRATION_V13_CHECKSUM),
            (14, MIGRATION_V14_NAME, MIGRATION_V14_CHECKSUM),
        ]
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(odds_snapshots)")
        }
        assert {
            "provider_age_seconds",
            "bookmaker_age_seconds",
            "market_age_seconds",
            "freshness_status",
        }.issubset(columns)
        assert connection.execute(
            "SELECT COUNT(*) FROM odds_normalization_warnings"
        ).fetchone()[0] == 0
        snapshots = connection.execute(
            "SELECT freshness_status FROM odds_snapshots ORDER BY id"
        ).fetchall()
        assert [row[0] for row in snapshots] == ["unknown", "unknown"]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_v2_migration_rolls_back_schema_and_version_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "formal-v1-failed.db"
    _create_formal_v1_database(path)
    real_execute = migrations._execute_statements

    def fail_v2(
        connection: sqlite3.Connection, statements: Iterable[str]
    ) -> None:
        if statements is FORMAL_SCHEMA_V2_STATEMENTS:
            connection.execute(FORMAL_SCHEMA_V2_STATEMENTS[0])
            raise RuntimeError("injected v2 failure")
        real_execute(connection, statements)

    monkeypatch.setattr(migrations, "_execute_statements", fail_v2)
    with pytest.raises(RuntimeError, match="injected v2 failure"):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V1_FINGERPRINT
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=2"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_collector_runs_v2'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_failed_v3_migration_rolls_back_schema_and_version_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "formal-v2-failed.db"
    _create_formal_v2_database(path)
    real_execute = migrations._execute_statements

    def fail_v3(
        connection: sqlite3.Connection, statements: Iterable[str]
    ) -> None:
        if statements is FORMAL_SCHEMA_V3_STATEMENTS:
            connection.execute(FORMAL_SCHEMA_V3_STATEMENTS[0])
            raise RuntimeError("injected v3 failure")
        real_execute(connection, statements)

    monkeypatch.setattr(migrations, "_execute_statements", fail_v3)
    with pytest.raises(RuntimeError, match="injected v3 failure"):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V2_FINGERPRINT
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=3"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_collector_runs_v3'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_failed_v4_migration_rolls_back_schema_and_version_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "formal-v3-failed.db"
    _create_formal_v3_database(path)
    real_execute = migrations._execute_statements

    def fail_v4(
        connection: sqlite3.Connection, statements: Iterable[str]
    ) -> None:
        if statements is FORMAL_SCHEMA_V4_STATEMENTS:
            connection.execute(FORMAL_SCHEMA_V4_STATEMENTS[0])
            raise RuntimeError("injected v4 failure")
        real_execute(connection, statements)

    monkeypatch.setattr(migrations, "_execute_statements", fail_v4)
    with pytest.raises(RuntimeError, match="injected v4 failure"):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V3_FINGERPRINT
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=4"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='_collector_runs_v4'"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_exact_legacy_schema_is_backed_up_verified_and_migrated(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    _create_legacy_database(path)

    database = Database(path)

    result = database.migration_result
    assert result.source_kind == "legacy_inline"
    assert result.backup_path is not None
    assert result.backup_path.parent == path.with_name(
        f"{path.name}.migration-backups"
    )
    backup = sqlite3.connect(result.backup_path)
    try:
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert schema_fingerprint(backup) == LEGACY_SCHEMA_FINGERPRINT
        assert backup.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 1
    finally:
        backup.close()

    with database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM weather_snapshots").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM collector_errors").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM run_games").fetchone()[0] == 1
        diagnostic = connection.execute(
            "SELECT * FROM migration_diagnostics"
        ).fetchone()
        assert diagnostic["source_fingerprint"] == LEGACY_SCHEMA_FINGERPRINT
        assert diagnostic["target_fingerprint"] == FORMAL_SCHEMA_V1_FINGERPRINT
        assert Path(diagnostic["backup_path"]) == result.backup_path
        run = connection.execute(
            "SELECT artifact_relpath FROM collector_runs WHERE run_id=?",
            (LEGACY_RUN_ID,),
        ).fetchone()
        assert run["artifact_relpath"] is None

    assert result.diagnostic_path is not None
    sidecar = json.loads(result.diagnostic_path.read_text(encoding="utf-8"))
    assert Path(sidecar["database_path"]) == path.resolve()
    assert Path(sidecar["backup_path"]) == result.backup_path
    assert sidecar["legacy_rows_preserved"][
        "legacy_artifact_paths_retained_in_backup_only"
    ] == 1


def test_unknown_unversioned_schema_is_refused_without_mutation(tmp_path: Path) -> None:
    path = tmp_path / "unknown.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrelated(value TEXT)")
    before = schema_fingerprint(connection)
    connection.close()

    with pytest.raises(UnknownSchemaError):
        Database(path)

    verification = sqlite3.connect(path)
    try:
        assert schema_fingerprint(verification) == before
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 0
    finally:
        verification.close()


def test_newer_schema_version_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "newer.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute(f"PRAGMA user_version={CURRENT_SCHEMA_VERSION + 1}")
    connection.commit()
    connection.close()

    with pytest.raises(NewerSchemaVersionError):
        Database(path)


def test_changed_migration_checksum_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "checksum.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE schema_migrations SET checksum='changed' WHERE version=1"
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationChecksumError):
        Database(path)


def test_changed_v2_migration_checksum_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "checksum-v2.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE schema_migrations SET checksum='changed' WHERE version=2"
    )
    connection.commit()
    connection.close()

    with pytest.raises(MigrationChecksumError):
        Database(path)


def test_backup_failure_aborts_before_legacy_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "backup-failure.db"
    _create_legacy_database(path)

    def fail_backup(*args: object, **kwargs: object) -> None:
        raise BackupVerificationError("injected backup failure")

    monkeypatch.setattr(migrations, "_create_verified_backup", fail_backup)
    with pytest.raises(BackupVerificationError):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert schema_fingerprint(connection) == LEGACY_SCHEMA_FINGERPRINT
    finally:
        connection.close()


def test_diagnostic_failure_aborts_before_legacy_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "diagnostic-failure.db"
    _create_legacy_database(path)

    def fail_diagnostic(*args: object, **kwargs: object) -> None:
        raise DiagnosticWriteError("injected diagnostic failure")

    monkeypatch.setattr(migrations, "_write_atomic_diagnostic", fail_diagnostic)
    with pytest.raises(DiagnosticWriteError):
        Database(path)

    connection = sqlite3.connect(path)
    try:
        assert schema_fingerprint(connection) == LEGACY_SCHEMA_FINGERPRINT
    finally:
        connection.close()


def test_post_commit_sidecar_failure_does_not_roll_back_committed_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "post-commit-diagnostic.db"
    real_writer = migrations._write_atomic_diagnostic
    calls = 0

    def fail_second_write(diagnostic_path: Path, payload: dict[str, object]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DiagnosticWriteError("injected post-commit failure")
        real_writer(diagnostic_path, payload)

    monkeypatch.setattr(migrations, "_write_atomic_diagnostic", fail_second_write)
    with pytest.raises(DiagnosticWriteError, match="post-commit"):
        Database(path)

    monkeypatch.setattr(migrations, "_write_atomic_diagnostic", real_writer)
    database = Database(path)
    assert database.schema_info()["fingerprint"] == FORMAL_SCHEMA_V14_FINGERPRINT
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version=1"
        ).fetchone()[0] == 1


def test_legacy_copy_failure_rolls_back_all_schema_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollback.db"
    _create_legacy_database(path)

    def fail_copy(connection: sqlite3.Connection) -> dict[str, int]:
        raise RuntimeError("injected copy failure")

    monkeypatch.setattr(migrations, "_copy_legacy_data", fail_copy)
    with pytest.raises(RuntimeError, match="injected copy failure"):
        Database(path)

    verification = sqlite3.connect(path)
    try:
        assert schema_fingerprint(verification) == LEGACY_SCHEMA_FINGERPRINT
        assert verification.execute("SELECT COUNT(*) FROM odds_snapshots").fetchone()[0] == 1
    finally:
        verification.close()
