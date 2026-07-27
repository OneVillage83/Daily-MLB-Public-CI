from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sqlite3
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import requests

from app.daily_slate import (
    DailySlateArtifactV1,
    DailySlateContractError,
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlatePersistenceConflict,
    DailySlateProvenanceV1,
    DailySlateRepository,
    DailySlateRepositoryError,
    DailySlateV1,
    VenueMappingStatus,
    daily_slate_artifact_relpath,
    daily_mlb_game_id,
    edge_event_id,
    write_daily_slate_artifact,
)
from app.database import Database
from app.run_controller.contracts import (
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.repository import PipelineRunRepository
from app.stats.repository import StatsRepository


RUN_ID = "run_20260727_11111111111111111111111111111111"
NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64


def _provenance(source_record_id: str | None) -> DailySlateProvenanceV1:
    return DailySlateProvenanceV1(
        source_provider="mlb",
        source_record_id=source_record_id,
        observed_at=NOW,
        source_updated_at=NOW - timedelta(minutes=1),
        source_version="schedule-v1",
        raw_status="Scheduled",
        upstream_checksum=HASH_A,
    )


def _game(
    source_game_id: str,
    *,
    start: datetime = START,
    status: DailySlateGameStatus = DailySlateGameStatus.SCHEDULED,
) -> DailySlateGameV1:
    return DailySlateGameV1(
        edge_event_id=edge_event_id(source_game_id),
        daily_mlb_game_id=daily_mlb_game_id(source_game_id),
        official_date="2026-07-27",
        scheduled_start_time=start,
        away_team_id="SF",
        home_team_id="LAD",
        venue_id="dodger-stadium-los-angeles",
        venue_mapping_status=VenueMappingStatus.RESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=status,
        source_game_id=source_game_id,
        source_provider="mlb",
        observed_at=NOW,
        provenance=_provenance(source_game_id),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
        source_updated_at=NOW - timedelta(minutes=1),
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    return DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        source_authority="mlb",
        source_version="schedule-v1",
        games=tuple(games),
        provenance=_provenance(None),
    )


def _repositories(
    tmp_path: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> tuple[Database, PipelineRunRepository, DailySlateRepository]:
    database = Database(tmp_path / "daily-slate.db")
    pipeline = PipelineRunRepository(
        database,
        repository_root=tmp_path / "not-a-git-repository",
    )
    pipeline.create_pipeline_run(
        requested_date="2026-07-27",
        as_of_time=NOW,
        timezone_name="America/Los_Angeles",
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_metadata={"network_enabled": False},
        code_revision="f" * 40,
        run_id=RUN_ID,
        created_at=NOW.isoformat(),
    )
    pipeline.transition_pipeline_run(
        RUN_ID,
        PipelineRunStatus.RUNNING,
        transitioned_at=NOW.isoformat(),
    )
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.RUNNING,
        transitioned_at=NOW.isoformat(),
    )
    return (
        database,
        pipeline,
        DailySlateRepository(
            database,
            secret_values=secret_values,
            clock=lambda: NOW,
        ),
    )


def _insert_raw_snapshot(
    connection: sqlite3.Connection,
    *,
    snapshot_id: str,
    run_id: str,
    phase_key: str = "daily_slate",
    phase_attempt: int = 1,
    requested_date: str = "2026-07-27",
    sealed_at: str | None = None,
) -> None:
    slate = _slate()
    connection.execute(
        """
        INSERT INTO daily_slate_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,
            as_of_time,observed_at,sport,league,source_authority,
            source_version,contract_version,snapshot_checksum,
            artifact_relpath,artifact_checksum,provenance_json,canonical_json,
            game_count,sealed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)
        """,
        (
            snapshot_id,
            run_id,
            phase_key,
            phase_attempt,
            requested_date,
            slate.as_of_time.isoformat(),
            slate.observed_at.isoformat(),
            slate.sport,
            slate.league,
            slate.source_authority,
            slate.source_version,
            slate.contract_version,
            slate.checksum,
            None,
            None,
            json.dumps(slate.provenance.as_dict(), sort_keys=True),
            slate.canonical_json_bytes().decode("utf-8"),
            sealed_at,
            NOW.isoformat(),
        ),
    )


def test_persist_and_retrieve_complete_snapshot_in_canonical_order(
    tmp_path: Path,
) -> None:
    database, _, repository = _repositories(tmp_path)
    slate = _slate(
        _game("200", start=START + timedelta(hours=1)),
        _game("100"),
    )
    artifact = write_daily_slate_artifact(slate, tmp_path / "artifacts")
    persisted = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=slate,
        artifact=artifact,
    )

    assert persisted.run_id == RUN_ID
    assert persisted.phase_attempt == 1
    assert persisted.slate.checksum == slate.checksum
    assert persisted.artifact_relpath == artifact.relpath
    assert persisted.artifact_checksum == artifact.checksum
    assert [game.source_game_id for game in persisted.slate.games] == ["100", "200"]
    assert [game.source_game_id for game in repository.get_daily_slate_games(
        persisted.snapshot_id
    )] == ["100", "200"]
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_snapshots"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_games"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM collector_runs"
        ).fetchone()[0] == 0
        snapshot = connection.execute(
            "SELECT game_count, sealed_at FROM daily_slate_snapshots"
        ).fetchone()
        assert tuple(snapshot) == (2, NOW.isoformat())


def test_zero_game_snapshot_persists_seals_retrieves_and_regenerates_artifact(
    tmp_path: Path,
) -> None:
    database, _, repository = _repositories(tmp_path)
    slate = _slate()
    original = write_daily_slate_artifact(slate, tmp_path / "original")
    persisted = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=slate,
        artifact=original,
    )
    regenerated = write_daily_slate_artifact(
        repository.get_daily_slate_snapshot(persisted.snapshot_id).slate,
        tmp_path / "regenerated",
    )

    assert persisted.slate.games == ()
    assert repository.get_daily_slate_games(persisted.snapshot_id) == ()
    assert original == regenerated
    assert (
        tmp_path / "original" / original.relpath
    ).read_bytes() == (
        tmp_path / "regenerated" / regenerated.relpath
    ).read_bytes()
    with database.connect() as connection:
        assert connection.execute(
            "SELECT game_count FROM daily_slate_snapshots"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_games"
        ).fetchone()[0] == 0


def test_snapshot_and_all_game_rows_are_atomic(tmp_path: Path) -> None:
    database, _, repository = _repositories(tmp_path)
    with database.connect(write=True) as connection:
        connection.execute(
            """
            CREATE TRIGGER test_abort_second_daily_slate_game
            BEFORE INSERT ON daily_slate_games
            WHEN NEW.ordinal=2
            BEGIN
                SELECT RAISE(ABORT, 'synthetic second game failure');
            END
            """
        )

    with pytest.raises(DailySlatePersistenceConflict):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=1,
            slate=_slate(_game("100"), _game("200")),
        )

    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_snapshots"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_games"
        ).fetchone()[0] == 0


def test_repeated_attempt_inserts_new_snapshot_and_preserves_prior_history(
    tmp_path: Path,
) -> None:
    _, pipeline, repository = _repositories(tmp_path)
    first = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=_slate(_game("100")),
    )
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseStatus.SUCCEEDED,
        output_checksum=first.slate.checksum,
        transitioned_at=NOW.isoformat(),
    )
    refreshed = pipeline.force_refresh_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.DAILY_SLATE,
        reason="explicit synthetic refresh",
        transitioned_at=(NOW + timedelta(minutes=5)).isoformat(),
    )
    assert refreshed.attempt_count == 2
    second = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=2,
        slate=_slate(_game("100", status=DailySlateGameStatus.PREGAME)),
    )

    history = repository.list_daily_slate_snapshots_for_run(RUN_ID)
    assert [item.snapshot_id for item in history] == [
        first.snapshot_id,
        second.snapshot_id,
    ]
    assert history[0].slate.games[0].game_status is DailySlateGameStatus.SCHEDULED
    assert history[1].slate.games[0].game_status is DailySlateGameStatus.PREGAME
    assert repository.get_latest_daily_slate_for_run(RUN_ID) == second


def test_same_attempt_cannot_overwrite_retained_snapshot(tmp_path: Path) -> None:
    _, _, repository = _repositories(tmp_path)
    repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=_slate(_game("100")),
    )
    with pytest.raises(DailySlatePersistenceConflict):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=1,
            slate=_slate(_game("100", status=DailySlateGameStatus.PREGAME)),
        )


def test_sealed_snapshot_rejects_late_child_insertion(tmp_path: Path) -> None:
    database, _, repository = _repositories(tmp_path)
    persisted = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=_slate(_game("100")),
    )
    with database.connect(write=True) as connection:
        original = connection.execute(
            """
            SELECT *
            FROM daily_slate_games
            WHERE snapshot_id=?
            """,
            (persisted.snapshot_id,),
        ).fetchone()
        columns = [column[1] for column in connection.execute(
            "PRAGMA table_info(daily_slate_games)"
        ).fetchall()]
        values = dict(zip(columns, tuple(original), strict=True))
        values.update(
            ordinal=2,
            edge_event_id="edge:mlb:200",
            daily_mlb_game_id="game:mlb:200",
            source_game_id="200",
        )
        placeholders = ",".join("?" for _ in columns)
        with pytest.raises(sqlite3.IntegrityError, match="unsealed snapshot"):
            connection.execute(
                f"INSERT INTO daily_slate_games({','.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(values[column] for column in columns),
            )


def test_retrieval_fails_closed_on_child_envelope_mismatch(tmp_path: Path) -> None:
    database, _, repository = _repositories(tmp_path)
    persisted = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=_slate(_game("100")),
    )
    with database.connect(write=True) as connection:
        connection.execute("DROP TRIGGER daily_slate_games_reject_update")
        connection.execute(
            """
            UPDATE daily_slate_games
            SET canonical_json=?
            WHERE snapshot_id=?
            """,
            (json.dumps(_game("200").as_dict(), sort_keys=True), persisted.snapshot_id),
        )

    with pytest.raises(DailySlateRepositoryError, match="checksum"):
        repository.get_daily_slate_snapshot(persisted.snapshot_id)


def test_snapshot_requires_matching_active_pipeline_phase_attempt(
    tmp_path: Path,
) -> None:
    _, _, repository = _repositories(tmp_path)
    with pytest.raises(DailySlatePersistenceConflict, match="active phase attempt"):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=2,
            slate=_slate(_game("100")),
        )


@pytest.mark.parametrize(
    ("case", "run_id", "phase_key", "phase_attempt", "requested_date"),
    [
        ("missing-run", "run_20260727_99999999999999999999999999999999", "daily_slate", 1, "2026-07-27"),
        ("wrong-phase", RUN_ID, "game_state", 1, "2026-07-27"),
        ("attempt-zero", RUN_ID, "daily_slate", 0, "2026-07-27"),
        ("attempt-mismatch", RUN_ID, "daily_slate", 2, "2026-07-27"),
        ("date-mismatch", RUN_ID, "daily_slate", 1, "2026-07-28"),
    ],
)
def test_database_trigger_rejects_invalid_phase_attempt_lineage(
    tmp_path: Path,
    case: str,
    run_id: str,
    phase_key: str,
    phase_attempt: int,
    requested_date: str,
) -> None:
    database, _, _ = _repositories(tmp_path)
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert_raw_snapshot(
                connection,
                snapshot_id=f"slate:{hashlib.sha256(case.encode()).hexdigest()}",
                run_id=run_id,
                phase_key=phase_key,
                phase_attempt=phase_attempt,
                requested_date=requested_date,
            )


def test_database_trigger_rejects_presealed_snapshot_insert(tmp_path: Path) -> None:
    database, _, _ = _repositories(tmp_path)
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="active pipeline phase"):
            _insert_raw_snapshot(
                connection,
                snapshot_id=f"slate:{'d' * 64}",
                run_id=RUN_ID,
                sealed_at=NOW.isoformat(),
            )


def test_database_trigger_requires_daily_slate_phase_to_be_running(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "pending.db")
    pipeline = PipelineRunRepository(
        database, repository_root=tmp_path / "not-a-git-repository"
    )
    pipeline.create_pipeline_run(
        requested_date="2026-07-27",
        as_of_time=NOW,
        timezone_name="America/Los_Angeles",
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_metadata={"network_enabled": False},
        code_revision="f" * 40,
        run_id=RUN_ID,
        created_at=NOW.isoformat(),
    )
    with database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="active pipeline phase"):
            _insert_raw_snapshot(
                connection,
                snapshot_id=f"slate:{'c' * 64}",
                run_id=RUN_ID,
            )


def test_repository_rejects_configured_secret_before_persistence(
    tmp_path: Path,
) -> None:
    synthetic_secret = "synthetic-configured-value-for-test"
    database, _, repository = _repositories(
        tmp_path, secret_values=(synthetic_secret,)
    )
    slate = DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        source_authority="mlb",
        source_version=synthetic_secret,
        games=(_game("100"),),
        provenance=_provenance(None),
    )
    with pytest.raises(DailySlateContractError, match="credential"):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=1,
            slate=slate,
        )
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM daily_slate_snapshots"
        ).fetchone()[0] == 0


def test_probable_starter_resolver_reuses_verified_canonical_mapping(
    tmp_path: Path,
) -> None:
    database, _, repository = _repositories(tmp_path)
    database.create_run(
        "run_20260727_22222222222222222222222222222222",
        "2026-07-27",
    )
    stats = StatsRepository(database)
    stats.create_ingestion_run(
        stats_run_id="stats-daily-slate-identity",
        run_id="run_20260727_22222222222222222222222222222222",
        provider="mlb",
        scope_key="identity:2026",
        requested_through_date="2026-07-27",
        configuration_checksum=HASH_A,
        created_at=NOW.isoformat(),
    )
    stats.transition_ingestion_run(
        "stats-daily-slate-identity", "running", transitioned_at=NOW.isoformat()
    )
    stats.upsert_player_identity(
        {
            "player_identity_id": "player:mlb:660271",
            "provider": "mlb",
            "provider_player_id": "660271",
            "full_name": "Example Player",
            "active": True,
            "first_seen_at": NOW.isoformat(),
            "last_seen_at": NOW.isoformat(),
            "identity_checksum": HASH_A,
        }
    )
    stats.record_canonical_player(
        {
            "canonical_player_id": "mlb-player:example",
            "created_stats_run_id": "stats-daily-slate-identity",
            "display_name": "Example Player",
            "created_at": NOW.isoformat(),
            "canonical_checksum": HASH_A,
        }
    )
    stats.record_player_identifier_mapping(
        {
            "mapping_id": "mapping:daily-slate-example",
            "stats_run_id": "stats-daily-slate-identity",
            "canonical_player_id": "mlb-player:example",
            "player_identity_id": "player:mlb:660271",
            "mapping_method": "source_declared",
            "verification_status": "verified",
            "source_version": "schedule-v1",
            "adapter_version": "DSE_DAILY_SLATE_NORMALIZATION_V1",
            "observed_at": NOW.isoformat(),
            "provenance": {"source": "authoritative fixture"},
            "source_checksum": HASH_B,
        }
    )

    resolved = repository.resolve_probable_starter(
        source_provider="mlb",
        source_player_id="660271",
        full_name="Example Player",
        observed_at=NOW,
        provenance=_provenance("660271"),
    )
    unresolved = repository.resolve_probable_starter(
        source_provider="mlb",
        source_player_id="999999",
        full_name="Unresolved Player",
        observed_at=NOW,
        provenance=_provenance("999999"),
    )

    assert resolved.player_identity_id == "player:mlb:660271"
    assert resolved.canonical_player_id == "mlb-player:example"
    assert unresolved.player_identity_id is None
    assert unresolved.canonical_player_id is None


def test_persisted_artifact_checksum_is_exact_bytes_hash(tmp_path: Path) -> None:
    _, _, repository = _repositories(tmp_path)
    slate = _slate(_game("100"))
    content = slate.canonical_json_bytes()
    artifact = DailySlateArtifactV1(
        relpath=daily_slate_artifact_relpath(slate),
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
    persisted = repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=slate,
        artifact=artifact,
    )
    assert persisted.artifact_checksum == hashlib.sha256(content).hexdigest()


def test_repository_rejects_artifact_metadata_not_matching_canonical_bytes(
    tmp_path: Path,
) -> None:
    _, _, repository = _repositories(tmp_path)
    with pytest.raises(DailySlateContractError, match="canonical DailySlateV1 bytes"):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=1,
            slate=_slate(_game("100")),
            artifact=DailySlateArtifactV1(
                relpath=daily_slate_artifact_relpath(_slate(_game("100"))),
                checksum="0" * 64,
                byte_count=1,
            ),
        )


def test_repository_rejects_mutable_noncanonical_artifact_path(
    tmp_path: Path,
) -> None:
    _, _, repository = _repositories(tmp_path)
    slate = _slate(_game("100"))
    content = slate.canonical_json_bytes()
    with pytest.raises(DailySlateContractError, match="content-addressed"):
        repository.persist_daily_slate(
            run_id=RUN_ID,
            phase_attempt=1,
            slate=slate,
            artifact=DailySlateArtifactV1(
                relpath="daily_slate/daily_slate_v1.json",
                checksum=hashlib.sha256(content).hexdigest(),
                byte_count=len(content),
            ),
        )


def test_ds1a_contract_persistence_and_artifact_make_zero_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        calls.append("network")
        raise AssertionError("DS1-A must not perform network access")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(httpx.Client, "request", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    pybaseball_spec = importlib.util.find_spec("pybaseball")
    if pybaseball_spec is not None:
        pybaseball = importlib.import_module("pybaseball")
        for name in (
            "statcast",
            "batting_stats_range",
            "pitching_stats_range",
            "schedule_and_record",
        ):
            if hasattr(pybaseball, name):
                monkeypatch.setattr(pybaseball, name, forbidden)

    _, _, repository = _repositories(tmp_path)
    slate = _slate(_game("100"))
    artifact = write_daily_slate_artifact(slate, tmp_path / "artifacts")
    repository.persist_daily_slate(
        run_id=RUN_ID,
        phase_attempt=1,
        slate=slate,
        artifact=artifact,
    )
    assert calls == []
