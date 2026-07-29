from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

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
    FORMAL_SCHEMA_V4_FINGERPRINT,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_FINGERPRINT,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_FINGERPRINT,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_FINGERPRINT,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_FINGERPRINT,
    FORMAL_SCHEMA_V8_STATEMENTS,
    FORMAL_SCHEMA_V9_FINGERPRINT,
    FORMAL_SCHEMA_V9_STATEMENTS,
    MIGRATION_HISTORY,
    MIGRATION_V1_CHECKSUM,
    MIGRATION_V2_CHECKSUM,
    MIGRATION_V3_CHECKSUM,
    MIGRATION_V4_CHECKSUM,
    MIGRATION_V5_CHECKSUM,
    MIGRATION_V6_CHECKSUM,
    MIGRATION_V7_CHECKSUM,
    MIGRATION_V8_CHECKSUM,
    MIGRATION_V9_CHECKSUM,
    MIGRATION_V9_NAME,
    BackupVerificationError,
    DiagnosticWriteError,
    SchemaVerificationError,
    ensure_schema,
    schema_fingerprint,
)


RUN_ID = "run_20260729_11111111111111111111111111111111"
OTHER_RUN_ID = "run_20260729_22222222222222222222222222222222"
REQUESTED_DATE = "2026-07-29"
TS = "2026-07-29T12:00:00+00:00"
SLATE_CHECKSUM = "a" * 64
STATE_CHECKSUM = "b" * 64
RAW_LINK_CHECKSUM = "c" * 64
ROW_CHECKSUM = "d" * 64
CONFIG_CHECKSUM = "e" * 64
SLATE_ID = f"slate:{SLATE_CHECKSUM}"
STATE_ID = f"state:{STATE_CHECKSUM}"
EDGE_EVENT_ID = "edge:mlb:777001"
DAILY_MLB_GAME_ID = "game:mlb:777001"
SOURCE_GAME_ID = "777001"

MIGRATION_STATEMENTS = (
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_STATEMENTS,
)

PINNED_V1_TO_V8 = (
    (
        "5ade37c867d9dd887e567f75be2044d18b175f6ecd80527ad26c9df8e0725263",
        "f19a6cf797f6ef532af55ca986512e6ea713068c23a8112af56bb41bc4f28ae2",
    ),
    (
        "c259e90f1d5e8242cda30a8a3a1510fe03234c0d0ccc01b16ead35103b9543ce",
        "8c28eb16739fb19b78817a88070835fad84947e06349cf1cd879fbce6bfef91f",
    ),
    (
        "6b5a8f618c744e8492177a8dbcb3bc414c8d6040b87bcb9f36ebae56de95af08",
        "699e554d65998c41eb922703b17cd5aa508f9229040130bee8b48fa6a73dd24f",
    ),
    (
        "c25d35f00e92c055afd5b3168273ea763064ae91148b6997d1aef38a6975afbd",
        "e9080c8543c6c98f29af79ba9e7f837d6a10d9a5beb6f41a6535c3ab6fe9587e",
    ),
    (
        "2695c3f401b0bf5711ecdf26493289381de7b59255f271d0e0a4274cae6b9a4d",
        "5bd9483d29fdf0fd514b9f39c192fe8fe4095fbb2aa1138da25c252dd0accc87",
    ),
    (
        "c31c585f13be7ff7df928eafddb49f052aafb4ddaae38ddef1ec26ce28713aba",
        "a777640b19ce8dc1509c7d9b5235330ce9dbac47250ca0d4a018f125afccc116",
    ),
    (
        "90d7d5e27ebe3c348d9b031fdd6c4231580599ba46800f218b288708fdbb773d",
        "c568f91e2f69309d59438e5639fc97f1037d6c9a859157fa37a26df11a01a716",
    ),
    (
        "75d6478383a5b35db93e9b6c43b6979e0093f6a2036d61c1bec1163533bd5f6b",
        "0438698c19d3afd6ed2bbfd42393f2bf5e1c3897e7588e754eee94d8c0173fc0",
    ),
)
PINNED_V1_TO_V8_NAMES = (
    "formal_phase1_schema",
    "phase1_odds_history_and_freshness",
    "release_candidate_evidence_ledger",
    "release_candidate_policy_enforcement",
    "mlb_stats_persistence_foundation",
    "retrosheet_fielding_source_row_grain",
    "manual_pipeline_run_controller",
    "daily_slate_v1_temporal_persistence",
)


def _install_formal_v8(path: Path) -> None:
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, history in zip(
            range(1, 9),
            MIGRATION_STATEMENTS,
            MIGRATION_HISTORY[:8],
            strict=True,
        ):
            assert version == history[0]
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                """
                INSERT INTO schema_migrations(version,name,checksum,applied_at)
                VALUES (?,?,?,?)
                """,
                (*history, TS),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V8_FINGERPRINT
    finally:
        connection.close()


def _insert_pipeline_and_daily_slate(
    connection: sqlite3.Connection,
    *,
    run_id: str = RUN_ID,
    database_schema_version: int,
    seal_slate: bool = True,
    zero_games: bool = False,
) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id,sport,run_type,requested_date,as_of_time,timezone,
            pipeline_version,configuration_version,configuration_fingerprint,
            configuration_metadata_json,code_revision,database_schema_version,
            force_refresh,status,failure_phase,error_message,final_summary_json,
            created_at,started_at,completed_at,updated_at
        ) VALUES (
            ?,'MLB','manual_daily',?,?,'America/Los_Angeles',
            'DSE_MANUAL_RUN_CONTROLLER_V1','DSE_DAILY_MLB_CONFIG_V1',?,
            '{}','test-revision',?,0,'running',NULL,NULL,NULL,?,?,NULL,?
        )
        """,
        (
            run_id,
            REQUESTED_DATE,
            TS,
            CONFIG_CHECKSUM,
            database_schema_version,
            TS,
            TS,
            TS,
        ),
    )
    for ordinal, phase_key in enumerate(migrations.PIPELINE_PHASE_KEYS, start=1):
        if phase_key == "daily_slate":
            status = "running"
            attempt_count = 1
            started_at = TS
        else:
            status = "pending"
            attempt_count = 0
            started_at = None
        connection.execute(
            """
            INSERT INTO pipeline_run_phases(
                run_id,phase_key,ordinal,status,attempt_count,started_at,
                completed_at,updated_at,input_checksum,output_checksum,
                artifact_relpath,warnings_json,error_json,reused_from_run_id
            ) VALUES (?,?,?,?,?,?,NULL,?,NULL,NULL,NULL,NULL,NULL,NULL)
            """,
            (
                run_id,
                phase_key,
                ordinal,
                status,
                attempt_count,
                started_at,
                TS,
            ),
        )

    games_payload: list[dict[str, object]] = []
    if not zero_games:
        games_payload.append(
            {
                "daily_mlb_game_id": DAILY_MLB_GAME_ID,
                "edge_event_id": EDGE_EVENT_ID,
                "source_game_id": SOURCE_GAME_ID,
            }
        )
    slate_id = SLATE_ID if run_id == RUN_ID else f"slate:{'f' * 64}"
    slate_checksum = SLATE_CHECKSUM if run_id == RUN_ID else "f" * 64
    connection.execute(
        """
        INSERT INTO daily_slate_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,
            as_of_time,observed_at,sport,league,source_authority,
            source_version,contract_version,snapshot_checksum,
            artifact_relpath,artifact_checksum,provenance_json,canonical_json,
            game_count,sealed_at,created_at
        ) VALUES (
            ?,?,'daily_slate',1,?,?,?,'MLB','MLB','MLB','v1',
            'DSE_DAILY_SLATE_V1',?,NULL,NULL,'{}',?,?,NULL,?
        )
        """,
        (
            slate_id,
            run_id,
            REQUESTED_DATE,
            TS,
            TS,
            slate_checksum,
            json.dumps({"games": games_payload}, separators=(",", ":")),
            len(games_payload),
            TS,
        ),
    )
    if not zero_games:
        connection.execute(
            """
            INSERT INTO daily_slate_games(
                snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,
                official_date,scheduled_start_time,away_team_id,home_team_id,
                venue_id,venue_mapping_status,game_number,doubleheader_status,
                game_status,source_game_id,source_provider,source_home_team_id,
                source_away_team_id,source_venue_id,source_venue_name,
                observed_at,source_updated_at,
                away_probable_player_identity_id,
                away_probable_canonical_player_id,
                home_probable_player_identity_id,
                home_probable_canonical_player_id,
                provenance_json,canonical_json,row_checksum
            ) VALUES (
                ?,1,?,?,?,'2026-07-29T19:00:00+00:00',
                'team:mlb:147','team:mlb:119',NULL,'unresolved',1,'single',
                'scheduled',?,'MLB','119','147',NULL,'Test Park',?,NULL,
                NULL,NULL,NULL,NULL,'{}','{}',?
            )
            """,
            (
                slate_id,
                EDGE_EVENT_ID,
                DAILY_MLB_GAME_ID,
                REQUESTED_DATE,
                SOURCE_GAME_ID,
                TS,
                ROW_CHECKSUM,
            ),
        )
    if seal_slate:
        connection.execute(
            "UPDATE daily_slate_snapshots SET sealed_at=? WHERE snapshot_id=?",
            (TS, slate_id),
        )
    connection.execute(
        """
        UPDATE pipeline_run_phases
        SET status='succeeded',completed_at=?,updated_at=?,
            output_checksum=?
        WHERE run_id=? AND phase_key='daily_slate'
        """,
        (TS, TS, slate_checksum, run_id),
    )
    connection.execute(
        """
        UPDATE pipeline_run_phases
        SET status='running',attempt_count=1,started_at=?,updated_at=?
        WHERE run_id=? AND phase_key='game_state'
        """,
        (TS, TS, run_id),
    )


def _v9_fixture(
    tmp_path: Path,
    *,
    seal_slate: bool = True,
    zero_games: bool = False,
) -> tuple[Path, sqlite3.Connection]:
    path = tmp_path / "v9-fixture.db"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_pipeline_and_daily_slate(
        connection,
        database_schema_version=9,
        seal_slate=seal_slate,
        zero_games=zero_games,
    )
    connection.commit()
    return path, connection


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    run_id: str = RUN_ID,
    phase_attempt: int = 1,
    requested_date: str = REQUESTED_DATE,
    upstream_checksum: str = SLATE_CHECKSUM,
    outcome: str = "normalized",
    normalized_checksum: str | None = STATE_CHECKSUM,
    warnings_json: str = "[]",
    warning_count: int = 0,
) -> None:
    connection.execute(
        """
        INSERT INTO game_state_attempt_evidence(
            run_id,phase_key,phase_attempt,requested_date,
            upstream_daily_slate_checksum,outcome,
            normalized_snapshot_checksum,raw_link_relpath,raw_link_checksum,
            raw_link_byte_count,warnings_json,warning_count,created_at
        ) VALUES (
            ?,'game_state',?,?,?,?,?,
            'game_state/raw_links/attempt.json',?,128,?,?,?
        )
        """,
        (
            run_id,
            phase_attempt,
            requested_date,
            upstream_checksum,
            outcome,
            normalized_checksum,
            RAW_LINK_CHECKSUM,
            warnings_json,
            warning_count,
            TS,
        ),
    )


def _insert_snapshot(
    connection: sqlite3.Connection,
    *,
    run_id: str = RUN_ID,
    phase_attempt: int = 1,
    snapshot_checksum: str = STATE_CHECKSUM,
    requested_date: str = REQUESTED_DATE,
    upstream_snapshot_id: str = SLATE_ID,
    upstream_checksum: str = SLATE_CHECKSUM,
    artifact_relpath: str | None = None,
    artifact_checksum: str | None = None,
    artifact_byte_count: int | None = None,
    zero_games: bool = False,
) -> None:
    games_payload: list[dict[str, object]] = []
    if not zero_games:
        games_payload.append(
            {
                "daily_mlb_game_id": DAILY_MLB_GAME_ID,
                "edge_event_id": EDGE_EVENT_ID,
                "source_game_id": SOURCE_GAME_ID,
            }
        )
    connection.execute(
        """
        INSERT INTO game_state_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,
            as_of_time,observed_at,sport,league,source_authority,
            source_version,contract_version,upstream_daily_slate_snapshot_id,
            upstream_daily_slate_checksum,snapshot_checksum,artifact_relpath,
            artifact_checksum,artifact_byte_count,provenance_json,
            canonical_json,game_count,sealed_at,created_at
        ) VALUES (
            ?,?,'game_state',?,?,?,?,'MLB','MLB','MLB','v1',
            'DSE_GAME_STATE_V1',?,?,?,?,?,?,'{}',?,?,NULL,?
        )
        """,
        (
            f"state:{snapshot_checksum}",
            run_id,
            phase_attempt,
            requested_date,
            TS,
            TS,
            upstream_snapshot_id,
            upstream_checksum,
            snapshot_checksum,
            artifact_relpath,
            artifact_checksum,
            artifact_byte_count,
            json.dumps({"games": games_payload}, separators=(",", ":")),
            len(games_payload),
            TS,
        ),
    )


def _insert_game(
    connection: sqlite3.Connection,
    *,
    away_team_id: str = "team:mlb:147",
    home_team_id: str = "team:mlb:119",
) -> None:
    connection.execute(
        """
        INSERT INTO game_state_games(
            snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,source_game_id,
            away_team_id,home_team_id,game_status,away_starter_certainty,
            home_starter_certainty,away_lineup_availability,
            home_lineup_availability,observed_at,provenance_json,
            canonical_json,row_checksum
        ) VALUES (
            ?,1,?,?,?,?,?,'scheduled','unavailable','unavailable',
            'unavailable','unavailable',?,'{}','{}',?
        )
        """,
        (
            STATE_ID,
            EDGE_EVENT_ID,
            DAILY_MLB_GAME_ID,
            SOURCE_GAME_ID,
            away_team_id,
            home_team_id,
            TS,
            ROW_CHECKSUM,
        ),
    )


def _assert_v8_unchanged(path: Path) -> None:
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 8
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V8_FINGERPRINT
        assert verification.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() == list(MIGRATION_HISTORY[:8])
        assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        verification.close()


def test_v9_constants_and_historical_v1_to_v8_identities_are_pinned() -> None:
    checksums = (
        MIGRATION_V1_CHECKSUM,
        MIGRATION_V2_CHECKSUM,
        MIGRATION_V3_CHECKSUM,
        MIGRATION_V4_CHECKSUM,
        MIGRATION_V5_CHECKSUM,
        MIGRATION_V6_CHECKSUM,
        MIGRATION_V7_CHECKSUM,
        MIGRATION_V8_CHECKSUM,
    )
    fingerprints = (
        FORMAL_SCHEMA_V1_FINGERPRINT,
        FORMAL_SCHEMA_V2_FINGERPRINT,
        FORMAL_SCHEMA_V3_FINGERPRINT,
        FORMAL_SCHEMA_V4_FINGERPRINT,
        FORMAL_SCHEMA_V5_FINGERPRINT,
        FORMAL_SCHEMA_V6_FINGERPRINT,
        FORMAL_SCHEMA_V7_FINGERPRINT,
        FORMAL_SCHEMA_V8_FINGERPRINT,
    )
    assert tuple(zip(checksums, fingerprints, strict=True)) == PINNED_V1_TO_V8
    assert tuple(row[1] for row in MIGRATION_HISTORY[:8]) == PINNED_V1_TO_V8_NAMES
    assert CURRENT_SCHEMA_VERSION == 9
    assert MIGRATION_V9_NAME == "game_state_v1_temporal_persistence"
    assert isinstance(FORMAL_SCHEMA_V9_STATEMENTS, tuple)
    assert FORMAL_SCHEMA_V9_STATEMENTS
    assert all(statement.strip() for statement in FORMAL_SCHEMA_V9_STATEMENTS)
    assert MIGRATION_V9_CHECKSUM == (
        "11b154c398d10b07dc2a892670dabd928fcbccced6a3c7327b79cc9b6b1f4ede"
    )
    assert FORMAL_SCHEMA_V9_FINGERPRINT == (
        "c54cdd8b10591e7247f7c1a4d4f1b2bbe385c1ac7fbcb09be8bccd70228130bd"
    )
    assert [row[0] for row in MIGRATION_HISTORY] == list(range(1, 10))
    assert MIGRATION_HISTORY[-1] == (
        9,
        MIGRATION_V9_NAME,
        MIGRATION_V9_CHECKSUM,
    )


def test_fresh_install_has_exact_v9_objects_without_pre_v9_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "fresh.db"
    result = ensure_schema(path)

    assert result.version == 9
    assert result.schema_fingerprint == FORMAL_SCHEMA_V9_FINGERPRINT
    assert result.backup_path is None
    migration_dir = path.with_name(f"{path.name}.migration-backups")
    assert list(migration_dir.glob("*.pre-v9-*.sqlite3")) == []
    connection = sqlite3.connect(path)
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert {
            "game_state_attempt_evidence",
            "game_state_snapshots",
            "game_state_games",
        }.issubset(tables)
        assert {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(game_state_attempt_evidence)"
            )
        } == {
            "run_id",
            "phase_key",
            "phase_attempt",
            "requested_date",
            "upstream_daily_slate_checksum",
            "outcome",
            "normalized_snapshot_checksum",
            "raw_link_relpath",
            "raw_link_checksum",
            "raw_link_byte_count",
            "warnings_json",
            "warning_count",
            "created_at",
        }
        assert "artifact_byte_count" in {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(game_state_snapshots)"
            )
        }
        indexes = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='index' AND name LIKE 'idx_game_state_%'
                """
            )
        }
        assert indexes == {
            "idx_game_state_attempt_evidence_date",
            "idx_game_state_attempt_evidence_outcome",
            "idx_game_state_games_identity",
            "idx_game_state_snapshots_date",
            "idx_game_state_snapshots_run",
            "idx_game_state_snapshots_upstream",
        }
        triggers = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type='trigger' AND name LIKE 'game_state_%'
                """
            )
        }
        assert triggers == {
            "game_state_attempt_evidence_reject_delete",
            "game_state_attempt_evidence_reject_update",
            "game_state_attempt_evidence_validate_phase_and_upstream",
            "game_state_games_reject_delete",
            "game_state_games_reject_update",
            "game_state_games_validate_unsealed_snapshot",
            "game_state_snapshots_reject_delete",
            "game_state_snapshots_reject_update",
            "game_state_snapshots_validate_phase_and_upstream",
            "game_state_snapshots_validate_seal",
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert connection.execute(
            "SELECT version,name,checksum FROM schema_migrations ORDER BY version"
        ).fetchall() == list(MIGRATION_HISTORY)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_recognized_v8_upgrade_creates_verified_backup_and_preserves_rows(
    tmp_path: Path,
) -> None:
    path = tmp_path / "preserve-v8.db"
    _install_formal_v8(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    _insert_pipeline_and_daily_slate(connection, database_schema_version=8)
    connection.execute(
        """
        INSERT INTO collector_runs(
            run_id,requested_date,status,created_at,queued_at,started_at,
            completed_at,updated_at,failure_stage,error_message,
            artifact_relpath,app_version,schema_version
        ) VALUES (
            'run_20260729_99999999999999999999999999999999',?,
            'completed',?,?,?,?,?,NULL,NULL,'collector/result.zip','test',8
        )
        """,
        (REQUESTED_DATE, TS, TS, TS, TS, TS),
    )
    connection.execute(
        """
        INSERT INTO pipeline_run_transitions(
            run_id,phase_key,from_status,to_status,transition_type,reason,
            audit_metadata_json,transitioned_at
        ) VALUES (?,NULL,'pending','running','state_transition','test','{}',?)
        """,
        (RUN_ID, TS),
    )
    connection.commit()
    preserved_tables = (
        "collector_runs",
        "pipeline_runs",
        "pipeline_run_phases",
        "pipeline_run_transitions",
        "daily_slate_snapshots",
        "daily_slate_games",
    )
    before = {
        table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        for table in preserved_tables
    }
    connection.close()

    result = ensure_schema(path)

    assert result.version == 9
    assert result.source_kind == "formal_v8"
    assert result.backup_path is not None and result.backup_path.exists()
    assert ".pre-v9-" in result.backup_path.name
    assert result.diagnostic_path is not None and result.diagnostic_path.exists()
    backup = sqlite3.connect(result.backup_path)
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V8_FINGERPRINT
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        backup.close()
    diagnostic = json.loads(result.diagnostic_path.read_text(encoding="utf-8"))
    assert diagnostic == {
        **diagnostic,
        "backup_foreign_key_violations": [],
        "backup_integrity_check": ["ok"],
        "backup_path": str(result.backup_path.resolve()),
        "database_path": str(path.resolve()),
        "migration_checksum": MIGRATION_V9_CHECKSUM,
        "migration_name": MIGRATION_V9_NAME,
        "outcome": "migration_verified",
        "source_fingerprint": FORMAL_SCHEMA_V8_FINGERPRINT,
        "source_foreign_key_violations": [],
        "source_integrity_check": ["ok"],
        "source_version": 8,
        "status": "completed",
        "target_fingerprint": FORMAL_SCHEMA_V9_FINGERPRINT,
        "target_version": 9,
    }
    assert diagnostic["completed_at"]
    verification = sqlite3.connect(path)
    try:
        after = {
            table: verification.execute(
                f"SELECT * FROM {table} ORDER BY 1"
            ).fetchall()
            for table in preserved_tables
        }
        assert after == before
        collector_sql = "".join(
            str(
                verification.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='collector_runs'
                    """
                ).fetchone()[0]
            ).split()
        )
        pipeline_sql = "".join(
            str(
                verification.execute(
                    """
                    SELECT sql FROM sqlite_master
                    WHERE type='table' AND name='pipeline_runs'
                    """
                ).fetchone()[0]
            ).split()
        )
        assert "schema_versionIN(1,2,3,4,5,6,7,8,9)" in collector_sql
        assert "database_schema_versionIN(7,8,9)" in pipeline_sql
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V9_FINGERPRINT
        assert verification.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert verification.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        verification.close()


@pytest.mark.parametrize(
    ("target", "message"),
    (
        ("_create_verified_v9_backup", "injected backup creation failure"),
        ("_verify_v9_backup", "injected backup fingerprint mismatch"),
        ("_verify_v9_backup", "injected backup integrity failure"),
        ("_verify_v9_backup", "injected backup foreign-key failure"),
    ),
    ids=(
        "backup-creation",
        "backup-fingerprint",
        "backup-integrity",
        "backup-foreign-keys",
    ),
)
def test_v9_backup_verification_failure_prevents_migration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    message: str,
) -> None:
    path = tmp_path / f"{target}-{message[-7:]}.db"
    _install_formal_v8(path)

    def fail(*args: object, **kwargs: object) -> dict[str, object]:
        raise BackupVerificationError(message)

    monkeypatch.setattr(migrations, target, fail)
    with pytest.raises(BackupVerificationError, match=message):
        ensure_schema(path)
    _assert_v8_unchanged(path)


def test_v9_diagnostic_failure_prevents_migration_and_preserves_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "diagnostic-failure.db"
    _install_formal_v8(path)

    def fail(*args: object, **kwargs: object) -> None:
        raise DiagnosticWriteError("injected v9 diagnostic failure")

    monkeypatch.setattr(migrations, "_write_atomic_diagnostic", fail)
    with pytest.raises(DiagnosticWriteError, match="injected v9 diagnostic"):
        ensure_schema(path)
    _assert_v8_unchanged(path)
    backups = list(
        path.with_name(f"{path.name}.migration-backups").glob("*.pre-v9-*.sqlite3")
    )
    assert len(backups) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V8_FINGERPRINT
    finally:
        backup.close()


@pytest.mark.parametrize(
    "failure_mode",
    ("statement", "precommit-verification"),
)
def test_v9_transaction_failure_rolls_back_and_retains_verified_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    path = tmp_path / f"rollback-{failure_mode}.db"
    _install_formal_v8(path)
    if failure_mode == "statement":
        original = migrations._execute_statements

        def fail_statements(
            connection: sqlite3.Connection,
            statements: object,
        ) -> None:
            if statements is migrations.FORMAL_SCHEMA_V9_STATEMENTS:
                connection.execute("CREATE TABLE injected_v9_partial(id INTEGER)")
                raise sqlite3.OperationalError("injected v9 statement failure")
            original(connection, statements)  # type: ignore[arg-type]

        monkeypatch.setattr(migrations, "_execute_statements", fail_statements)
        expected_error: type[Exception] = sqlite3.OperationalError
    else:
        original_assert = migrations._assert_target_schema

        def fail_verification(
            connection: sqlite3.Connection,
            version: int,
        ) -> None:
            if version == 9:
                raise SchemaVerificationError(
                    "injected v9 precommit verification failure"
                )
            original_assert(connection, version)

        monkeypatch.setattr(migrations, "_assert_target_schema", fail_verification)
        expected_error = SchemaVerificationError

    with pytest.raises(expected_error):
        ensure_schema(path)

    _assert_v8_unchanged(path)
    migration_dir = path.with_name(f"{path.name}.migration-backups")
    backups = list(migration_dir.glob("*.pre-v9-*.sqlite3"))
    diagnostics = list(migration_dir.glob("migration-v9-*.json"))
    assert len(backups) == len(diagnostics) == 1
    backup = sqlite3.connect(backups[0])
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V8_FINGERPRINT
        assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        backup.close()
    diagnostic = json.loads(diagnostics[0].read_text(encoding="utf-8"))
    assert diagnostic["status"] == "completed"
    assert diagnostic["outcome"] == "migration_failed"
    assert "injected" in diagnostic["error"]


def test_modified_v8_fingerprint_fails_closed_before_backup(tmp_path: Path) -> None:
    path = tmp_path / "modified-v8.db"
    _install_formal_v8(path)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE unrecognized_drift(id INTEGER)")
    connection.commit()
    connection.close()

    with pytest.raises(SchemaVerificationError, match="fingerprint"):
        ensure_schema(path)
    assert list(
        path.with_name(f"{path.name}.migration-backups").glob("*.pre-v9-*.sqlite3")
    ) == []


def test_repeated_v9_assurance_does_not_rerun_or_create_another_backup(
    tmp_path: Path,
) -> None:
    path = tmp_path / "idempotent-v9.db"
    _install_formal_v8(path)
    first = ensure_schema(path)
    migration_dir = path.with_name(f"{path.name}.migration-backups")
    first_backups = list(migration_dir.glob("*.pre-v9-*.sqlite3"))
    first_diagnostics = list(migration_dir.glob("migration-v9-*.json"))

    second = ensure_schema(path)

    assert first.version == second.version == 9
    assert second.migrated is False
    assert list(migration_dir.glob("*.pre-v9-*.sqlite3")) == first_backups
    assert list(migration_dir.glob("migration-v9-*.json")) == first_diagnostics


@pytest.mark.parametrize(
    ("mutation", "expected"),
    (
        ("phase-status", "active phase"),
        ("attempt", "active phase"),
        ("date", "active phase"),
        ("upstream", "active phase"),
        ("unsealed", "active phase"),
    ),
)
def test_attempt_evidence_requires_exact_active_daily_slate_lineage(
    tmp_path: Path,
    mutation: str,
    expected: str,
) -> None:
    _, connection = _v9_fixture(
        tmp_path,
        seal_slate=mutation != "unsealed",
    )
    try:
        kwargs: dict[str, object] = {}
        if mutation == "phase-status":
            connection.execute(
                """
                UPDATE pipeline_run_phases
                SET status='succeeded',completed_at=?
                WHERE run_id=? AND phase_key='game_state'
                """,
                (TS, RUN_ID),
            )
        elif mutation == "attempt":
            kwargs["phase_attempt"] = 2
        elif mutation == "date":
            kwargs["requested_date"] = "2026-07-30"
        elif mutation == "upstream":
            kwargs["upstream_checksum"] = "f" * 64
        with pytest.raises(sqlite3.IntegrityError, match=expected):
            _insert_attempt(connection, **kwargs)  # type: ignore[arg-type]
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("outcome", "normalized_checksum"),
    (
        ("normalized", None),
        ("acquisition_failed", STATE_CHECKSUM),
        ("normalization_failed", STATE_CHECKSUM),
    ),
)
def test_attempt_outcome_checksum_contract(
    tmp_path: Path,
    outcome: str,
    normalized_checksum: str | None,
) -> None:
    _, connection = _v9_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(
                connection,
                outcome=outcome,
                normalized_checksum=normalized_checksum,
            )
    finally:
        connection.close()


def test_attempt_warning_count_immutability_and_duplicate_protection(
    tmp_path: Path,
) -> None:
    _, connection = _v9_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(
                connection,
                warnings_json='["warning"]',
                warning_count=0,
            )
        _insert_attempt(
            connection,
            warnings_json='["warning"]',
            warning_count=1,
        )
        original = connection.execute(
            "SELECT * FROM game_state_attempt_evidence"
        ).fetchone()
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE game_state_attempt_evidence
                SET warning_count=0 WHERE run_id=? AND phase_attempt=1
                """,
                (RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            connection.execute(
                """
                DELETE FROM game_state_attempt_evidence
                WHERE run_id=? AND phase_attempt=1
                """,
                (RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(connection)
        assert connection.execute(
            "SELECT * FROM game_state_attempt_evidence"
        ).fetchone() == original
    finally:
        connection.close()


def test_snapshot_requires_matching_attempt_and_atomic_artifact_group(
    tmp_path: Path,
) -> None:
    _, connection = _v9_fixture(tmp_path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot(connection)
        _insert_attempt(connection)
        for artifact_group in (
            ("game_state/example.json", None, None),
            (None, STATE_CHECKSUM, None),
            (None, None, 10),
            ("game_state/example.json", STATE_CHECKSUM, None),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_snapshot(
                    connection,
                    artifact_relpath=artifact_group[0],
                    artifact_checksum=artifact_group[1],
                    artifact_byte_count=artifact_group[2],
                )
        _insert_snapshot(
            connection,
            artifact_relpath=(
                f"game_state/snapshots/{STATE_CHECKSUM}/game_state_v1.json"
            ),
            artifact_checksum=STATE_CHECKSUM,
            artifact_byte_count=512,
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("run_id", OTHER_RUN_ID),
        ("phase_attempt", 2),
        ("snapshot_checksum", "f" * 64),
        ("requested_date", "2026-07-30"),
        ("upstream_checksum", "f" * 64),
        ("upstream_snapshot_id", f"slate:{'f' * 64}"),
    ),
)
def test_snapshot_rejects_mismatched_attempt_or_upstream(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _, connection = _v9_fixture(tmp_path)
    try:
        _insert_attempt(connection)
        with pytest.raises(sqlite3.IntegrityError):
            arguments: dict[str, Any] = {field: value}
            _insert_snapshot(connection, **arguments)
    finally:
        connection.close()


def test_game_lineage_sealing_and_immutability_triggers(tmp_path: Path) -> None:
    _, connection = _v9_fixture(tmp_path)
    try:
        _insert_attempt(connection)
        _insert_snapshot(connection)
        with pytest.raises(sqlite3.IntegrityError, match="incomplete"):
            connection.execute(
                "UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?",
                (TS, STATE_ID),
            )
        with pytest.raises(sqlite3.IntegrityError, match="exact"):
            _insert_game(connection, home_team_id="team:mlb:999")
        _insert_game(connection)
        connection.execute(
            "UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?",
            (TS, STATE_ID),
        )
        with pytest.raises(sqlite3.IntegrityError, match="exact"):
            _insert_game(connection)
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE game_state_games SET game_status='final'
                WHERE snapshot_id=?
                """,
                (STATE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            connection.execute(
                "DELETE FROM game_state_games WHERE snapshot_id=?",
                (STATE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE game_state_snapshots SET artifact_byte_count=999
                WHERE snapshot_id=?
                """,
                (STATE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="retained"):
            connection.execute(
                "DELETE FROM game_state_snapshots WHERE snapshot_id=?",
                (STATE_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="incomplete"):
            connection.execute(
                "UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?",
                ("2026-07-29T12:01:00+00:00", STATE_ID),
            )
    finally:
        connection.close()


def test_zero_game_snapshot_seals_and_remains_complete(tmp_path: Path) -> None:
    _, connection = _v9_fixture(tmp_path, zero_games=True)
    try:
        _insert_attempt(connection)
        _insert_snapshot(connection, zero_games=True)
        connection.execute(
            "UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?",
            (TS, STATE_ID),
        )
        assert connection.execute(
            "SELECT game_count,sealed_at FROM game_state_snapshots"
        ).fetchone() == (0, TS)
        assert connection.execute(
            "SELECT COUNT(*) FROM game_state_games"
        ).fetchone()[0] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()
