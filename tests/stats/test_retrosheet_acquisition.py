from __future__ import annotations

import json
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from app.database import Database
from app.stats.contracts import StatsRequest, StatsResponse, StatsTransport
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionExecutionError,
    AcquisitionMode,
    AcquisitionRequest,
    AcquisitionResumeError,
    StatsAcquisitionService,
    build_stats_transport,
)
from app.stats.providers.retrosheet import RETROSHEET_SEVEN_MEMBERS
from app.stats.providers.player_register import (
    PYBASEBALL_REGISTER_COMMIT_SHA,
    PYBASEBALL_REGISTER_ENDPOINT_CATEGORY,
    PYBASEBALL_REGISTER_URL,
    PybaseballPlayerRegisterProvider,
)
from app.stats.player_register_reconciliation import (
    LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY,
    PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT,
    PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
    PLAYER_REGISTER_RECONCILIATION_PROVIDER,
)
from app.stats.raw_store import RawArtifactStore


NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
SOURCE_FIXTURES = (
    Path(__file__).parents[1] / "fixtures" / "stats" / "retrosheet_normalization"
)


def _fixture_archive(
    root: Path,
    *,
    allplayers: str | None = None,
    gameinfo: str | None = None,
    teamstats: str | None = None,
    batting: str | None = None,
    fielding: str | None = None,
    register_rows: str = "known001,660271,known01,1,Known,Player\n",
) -> Path:
    root.mkdir(parents=True)
    archive_path = root / "retrosheet_regular_season_archive.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in RETROSHEET_SEVEN_MEMBERS:
            if name == "allplayers.csv" and allplayers is not None:
                archive.writestr(name, allplayers)
            elif name == "gameinfo.csv" and gameinfo is not None:
                archive.writestr(name, gameinfo)
            elif name == "teamstats.csv" and teamstats is not None:
                archive.writestr(name, teamstats)
            elif name == "batting.csv" and batting is not None:
                archive.writestr(name, batting)
            elif name == "fielding.csv" and fielding is not None:
                archive.writestr(name, fielding)
            else:
                archive.write(SOURCE_FIXTURES / name, arcname=name)
    register_path = root / "pybaseball_player_identifier_register.zip"
    with zipfile.ZipFile(
        register_path, "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        archive.writestr(
            "register-main/data/people-a.csv",
            "key_retro,key_mlbam,key_bbref,key_fangraphs,name_last,name_first\n"
            + register_rows,
        )
    return root


def _mark_register_checkpoint_as_legacy(
    database: Database,
    raw_root: Path,
    stats_run_id: str,
) -> tuple[dict[str, object], dict[str, object]]:
    with database.connect(write=True) as connection:
        source_run = dict(
            connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (stats_run_id,),
            ).fetchone()
        )
        checkpoint = connection.execute(
            "SELECT * FROM stats_checkpoints WHERE stats_run_id=? "
            "AND dataset_key='pybaseball_player_identifier_register'",
            (stats_run_id,),
        ).fetchone()
        assert checkpoint is not None
        checkpoint_before = dict(checkpoint)
        cursor = json.loads(str(checkpoint["cursor_after_json"]))
        cursor.pop("resource_revision")
        cursor.pop("resource_identity")
        cursor.pop("raw_endpoint_category")
        connection.execute(
            "UPDATE stats_checkpoints SET cursor_after_json=? WHERE checkpoint_id=?",
            (json.dumps(cursor, sort_keys=True), checkpoint["checkpoint_id"]),
        )
        raw = connection.execute(
            "SELECT * FROM stats_raw_payload_metadata WHERE checkpoint_id=?",
            (checkpoint["checkpoint_id"],),
        ).fetchone()
        assert raw is not None
        assert raw["endpoint_category"] == LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY
    assert raw_root.joinpath(str(raw["artifact_relpath"])).is_file()
    return source_run, checkpoint_before


class _LegacyRegisterEndpointTransport:
    def __init__(self, delegate: StatsTransport) -> None:
        self.delegate = delegate

    def fetch(self, request: StatsRequest) -> StatsResponse:
        if request.fixture_key != "pybaseball_player_identifier_register":
            return self.delegate.fetch(request)
        legacy_request = StatsRequest(
            provider=request.provider,
            endpoint_category=LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY,
            fixture_key=request.fixture_key,
            url=request.url,
            params=request.params,
            headers=request.headers,
            timeout_seconds=request.timeout_seconds,
            max_attempts=request.max_attempts,
            minimum_interval_seconds=request.minimum_interval_seconds,
            persistent_cache=request.persistent_cache,
        )
        return self.delegate.fetch(legacy_request)


def _legacy_source_service(
    root: Path,
    fixtures: Path,
) -> tuple[StatsAcquisitionService, Database]:
    database = Database(root / "stats.db")
    raw_store = RawArtifactStore(root / "raw")
    delegate = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/legacy-register-offline-test",
        clock=lambda: NOW,
    )
    return (
        StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=_LegacyRegisterEndpointTransport(delegate),
            report_path=root / "validation.json",
            clock=lambda: NOW,
        ),
        database,
    )


def test_retrosheet_name_history_is_preserved_with_deterministic_display_name(
    tmp_path: Path,
) -> None:
    base = (SOURCE_FIXTURES / "allplayers.csv").read_text(encoding="utf-8")
    fixtures = _fixture_archive(
        tmp_path / "fixtures",
        allplayers=(
            base
            + "known001,Player,Historical,LAN,2025\n"
            + "known001,Player,K.,LAN,2026\n"
            + "known001,Player,K.,NYA,2026\n"
            + "known001,Player,Zed,LAN,2026\n"
            + "known001,Player,Zed,NYA,2026\n"
        ),
    )
    service, database = _service(tmp_path / "run", fixtures)

    result = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )

    assert result.status == "completed"
    with database.connect() as connection:
        identity = connection.execute(
            "SELECT full_name FROM stats_player_identities "
            "WHERE provider='retrosheet' AND provider_player_id='known001'"
        ).fetchone()
        mapping = connection.execute(
            "SELECT provenance_json FROM stats_player_identifier_mappings AS mapping "
            "JOIN stats_player_identities AS identity "
            "ON identity.player_identity_id=mapping.player_identity_id "
            "WHERE identity.provider='retrosheet' "
            "AND identity.provider_player_id='known001' "
            "AND mapping.mapping_method='pybaseball_lookup'"
        ).fetchone()
        register_checkpoint = connection.execute(
            "SELECT cursor_after_json FROM stats_checkpoints "
            "WHERE stats_run_id=? AND dataset_key="
            "'pybaseball_player_identifier_register'",
            (result.stats_run_id,),
        ).fetchone()
        register_raw = connection.execute(
            "SELECT raw.endpoint_category FROM stats_raw_payload_metadata AS raw "
            "JOIN stats_checkpoints AS checkpoint "
            "ON checkpoint.checkpoint_id=raw.checkpoint_id "
            "WHERE checkpoint.stats_run_id=? AND checkpoint.dataset_key="
            "'pybaseball_player_identifier_register'",
            (result.stats_run_id,),
        ).fetchone()
    assert identity is not None and identity["full_name"] == "K. Player"
    assert mapping is not None
    provenance = json.loads(str(mapping["provenance_json"]))
    identifier_mapping = provenance["identifier_mapping"]
    assert (
        identifier_mapping["source_resource_revision"]
        == PYBASEBALL_REGISTER_COMMIT_SHA
    )
    assert identifier_mapping["source_resource_identity"] == PYBASEBALL_REGISTER_URL
    assert provenance["display_name_policy"] == (
        "latest_season_most_frequent_then_lexicographic"
    )
    assert provenance["name_observations"] == [
        {"name": "Historical Player", "row_count": 1, "season": 2025},
        {"name": "K. Player", "row_count": 2, "season": 2026},
        {"name": "Player Known", "row_count": 1, "season": 2026},
        {"name": "Zed Player", "row_count": 2, "season": 2026},
    ]
    assert register_checkpoint is not None
    register_cursor = json.loads(str(register_checkpoint["cursor_after_json"]))
    assert register_cursor["resource_revision"] == PYBASEBALL_REGISTER_COMMIT_SHA
    assert register_cursor["resource_identity"] == PYBASEBALL_REGISTER_URL
    assert (
        register_cursor["raw_endpoint_category"]
        == PYBASEBALL_REGISTER_ENDPOINT_CATEGORY
    )
    assert register_raw is not None
    assert register_raw["endpoint_category"] == PYBASEBALL_REGISTER_ENDPOINT_CATEGORY

    rerun_service, _ = _service(tmp_path / "run", fixtures)
    rerun = rerun_service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    assert rerun.status == "completed"
    with database.connect() as connection:
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_player_identities "
                "WHERE provider='retrosheet' AND provider_player_id='known001'"
            ).fetchone()[0]
        ) == 1
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_player_identifier_mappings AS mapping "
                "JOIN stats_player_identities AS identity "
                "ON identity.player_identity_id=mapping.player_identity_id "
                "WHERE identity.provider='retrosheet' "
                "AND identity.provider_player_id='known001'"
            ).fetchone()[0]
        ) == 1


def test_retrosheet_gid_team_exceptions_complete_with_auditable_warning(
    tmp_path: Path,
) -> None:
    base = (SOURCE_FIXTURES / "gameinfo.csv").read_text(encoding="utf-8")
    fixtures = _fixture_archive(
        tmp_path / "fixtures",
        gameinfo=(
            base
            + "NY6194905040,NY6,IN9,regular\n"
            + "CHA197907122,CHA,DET,regular\n"
        ),
    )
    service, database = _service(tmp_path / "run", fixtures)

    result = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )

    assert result.status == "completed_with_warnings"
    assert result.exit_code == 0
    assert result.warnings == ("retrosheet_gid_team_role_mismatch",)
    assert result.counts["retrosheet_gid_team_role_mismatches"] == 2
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT game.provider_game_id,home.provider_team_id AS home_team,"
            "away.provider_team_id AS away_team,status.status_json "
            "FROM stats_game_identities AS game "
            "JOIN stats_team_identities AS home "
            "ON home.team_identity_id=game.home_team_identity_id "
            "JOIN stats_team_identities AS away "
            "ON away.team_identity_id=game.away_team_identity_id "
            "JOIN stats_game_status_observations AS status "
            "ON status.game_identity_id=game.game_identity_id "
            "WHERE game.provider_game_id IN "
            "('LAN202607100','NY6194905040','CHA197907122') "
            "ORDER BY game.provider_game_id"
        ).fetchall()
    observed = {
        str(row["provider_game_id"]): {
            "home": str(row["home_team"]),
            "away": str(row["away_team"]),
            "status": json.loads(str(row["status_json"])),
        }
        for row in rows
    }
    assert observed["LAN202607100"]["home"] == "LAN"
    assert observed["LAN202607100"]["away"] == "SFN"
    assert observed["LAN202607100"]["status"]["dse_team_role_provenance"] == {
        "away_team_field": "visteam",
        "contract": "DSE_RETROSHEET_TEAM_ROLE_V1",
        "game_id_encoded_team_provider_id": "LAN",
        "game_id_team_role_matches": True,
        "home_team_field": "hometeam",
    }
    assert observed["NY6194905040"]["home"] == "IN9"
    assert observed["NY6194905040"]["away"] == "NY6"
    assert observed["NY6194905040"]["status"]["dse_team_role_provenance"] == {
        "away_team_field": "visteam",
        "contract": "DSE_RETROSHEET_TEAM_ROLE_V1",
        "game_id_encoded_team_provider_id": "NY6",
        "game_id_team_role_matches": False,
        "home_team_field": "hometeam",
    }
    assert observed["CHA197907122"]["home"] == "DET"
    assert observed["CHA197907122"]["away"] == "CHA"
    assert observed["CHA197907122"]["status"]["dse_team_role_provenance"] == {
        "away_team_field": "visteam",
        "contract": "DSE_RETROSHEET_TEAM_ROLE_V1",
        "game_id_encoded_team_provider_id": "CHA",
        "game_id_team_role_matches": False,
        "home_team_field": "hometeam",
    }


def test_retrosheet_duplicate_batter_slots_and_lineup_diagnostics_are_persisted(
    tmp_path: Path,
) -> None:
    gameinfo = (SOURCE_FIXTURES / "gameinfo.csv").read_text(encoding="utf-8")
    teamstats = (SOURCE_FIXTURES / "teamstats.csv").read_text(encoding="utf-8")
    fixtures = _fixture_archive(
        tmp_path / "fixtures",
        gameinfo=gameinfo + "WBS193805220,NY5,WBS,regular\n",
        teamstats=(
            teamstats
            + "WBS193805220,WBS,value,regular,,burbb102,johnt107,speah101,"
            "speah101,wrigz102,casem101,andrj104,hughc101,yokel101,yokel101,"
            "casem101,thomd104,johnt107,speah101,hughc101,andrj104,burbb102,"
            "wrigz102,\n"
        ),
    )
    service, database = _service(tmp_path / "run", fixtures)

    result = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )

    assert result.status == "completed"
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT entry.batting_order,player.provider_player_id,"
            "entry.position_code,entry.entry_json "
            "FROM stats_lineup_entries AS entry "
            "JOIN stats_lineup_snapshots AS lineup "
            "ON lineup.lineup_snapshot_id=entry.lineup_snapshot_id "
            "JOIN stats_game_identities AS game "
            "ON game.game_identity_id=lineup.game_identity_id "
            "JOIN stats_team_identities AS team "
            "ON team.team_identity_id=lineup.team_identity_id "
            "JOIN stats_player_identities AS player "
            "ON player.player_identity_id=entry.player_identity_id "
            "WHERE game.provider_game_id='WBS193805220' "
            "AND team.provider_team_id='WBS' ORDER BY entry.batting_order"
        ).fetchall()
    assert len(rows) == 9
    duplicate_slots = [
        row for row in rows if str(row["provider_player_id"]) == "speah101"
    ]
    assert [int(row["batting_order"]) for row in duplicate_slots] == [3, 4]
    assert [str(row["position_code"]) for row in duplicate_slots] == ["5", "5"]
    for row in duplicate_slots:
        entry = json.loads(str(row["entry_json"]))
        assert entry["fielding_positions"] == ["5"]
        assert entry["diagnostic_codes"] == ["duplicate_lineup_player"]


def _service(root: Path, fixtures: Path) -> tuple[StatsAcquisitionService, Database]:
    database = Database(root / "stats.db")
    raw_store = RawArtifactStore(root / "raw")
    transport = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/retrosheet-offline-test",
        clock=lambda: NOW,
    )
    return (
        StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=transport,
            report_path=root / "validation.json",
            clock=lambda: NOW,
        ),
        database,
    )


def _counts(database: Database) -> dict[str, int]:
    tables = (
        "stats_game_identities",
        "stats_game_status_observations",
        "stats_game_team_snapshots",
        "stats_game_player_snapshots",
        "stats_lineup_snapshots",
        "stats_lineup_entries",
        "stats_play_identities",
        "stats_play_revisions",
        "stats_excluded_source_rows",
        "stats_canonical_players",
        "stats_player_identifier_mappings",
        "stats_feature_snapshots",
    )
    with database.connect() as connection:
        return {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }


def test_offline_retrosheet_zip_import_is_complete_typed_and_idempotent(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _service(tmp_path / "run", fixtures)
    request = AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE)

    first = service.execute(AcquisitionCommand.BOOTSTRAP_RETROSHEET, request)

    assert first.status == "completed"
    assert first.completeness is not None
    assert first.completeness.latest_ingested_completed_game_date == date(2026, 7, 11)
    assert (
        first.completeness.contiguous_regular_season_complete_through_date
        == date(2026, 7, 11)
    )
    assert first.completeness.partial_date is None
    assert first.counts["games"] == 3
    assert first.counts["excluded_source_rows"] == 7
    assert first.counts.get("feature_snapshots", 0) == 0

    before = _counts(database)
    assert before["stats_game_identities"] == 3
    assert before["stats_game_team_snapshots"] == 3
    assert before["stats_game_player_snapshots"] == 6
    assert before["stats_lineup_snapshots"] == 3
    assert before["stats_lineup_entries"] == 4
    assert before["stats_play_identities"] == 2
    assert before["stats_play_revisions"] == 2
    assert before["stats_excluded_source_rows"] == 7
    assert before["stats_feature_snapshots"] == 0

    with database.connect() as connection:
        former = connection.execute(
            "SELECT canonical_team_key,active FROM stats_team_identities "
            "WHERE provider='retrosheet' AND provider_team_id='BRO'"
        ).fetchone()
        interleague = connection.execute(
            "SELECT away.canonical_team_key FROM stats_game_identities AS game "
            "JOIN stats_team_identities AS away "
            "ON away.team_identity_id=game.away_team_identity_id "
            "WHERE game.provider_game_id='LAN202607110'"
        ).fetchone()
        player_roles = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT role FROM stats_game_player_snapshots"
            ).fetchall()
        }
        play = connection.execute(
            "SELECT play_json FROM stats_play_revisions AS revision "
            "JOIN stats_play_identities AS identity "
            "ON identity.play_identity_id=revision.play_identity_id "
            "WHERE identity.provider_play_id='LAN202607100:1'"
        ).fetchone()
        former_mapping = connection.execute(
            "SELECT canonical.canonical_player_id "
            "FROM stats_player_identifier_mappings AS mapping "
            "JOIN stats_canonical_players AS canonical "
            "ON canonical.canonical_player_id=mapping.canonical_player_id "
            "JOIN stats_player_identities AS identity "
            "ON identity.player_identity_id=mapping.player_identity_id "
            "WHERE identity.provider='retrosheet' "
            "AND identity.provider_player_id='former01'"
        ).fetchone()
        known_mapping = connection.execute(
            "SELECT canonical.canonical_player_id,mapping.mapping_method,"
            "mapping.provenance_json "
            "FROM stats_player_identifier_mappings AS mapping "
            "JOIN stats_canonical_players AS canonical "
            "ON canonical.canonical_player_id=mapping.canonical_player_id "
            "JOIN stats_player_identities AS identity "
            "ON identity.player_identity_id=mapping.player_identity_id "
            "WHERE identity.provider='retrosheet' "
            "AND identity.provider_player_id='known001'"
        ).fetchone()
        checkpoints = connection.execute(
            "SELECT dataset_key,status,cursor_after_json FROM stats_checkpoints "
            "WHERE stats_run_id=? ORDER BY dataset_key",
            (first.stats_run_id,),
        ).fetchall()
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert former is not None
    assert former["canonical_team_key"] is None
    assert former["active"] == 0
    assert interleague is not None
    assert interleague["canonical_team_key"] == "CWS"
    assert player_roles == {"batting", "pitching", "fielding"}
    assert play is not None and '"event":"S7"' in str(play["play_json"])
    assert former_mapping is not None
    assert former_mapping["canonical_player_id"] == "retrosheet-player:former01"
    assert known_mapping is not None
    assert known_mapping["canonical_player_id"] == "mlb-player:mlbam:660271"
    assert known_mapping["mapping_method"] == "pybaseball_lookup"
    known_provenance = json.loads(str(known_mapping["provenance_json"]))
    assert known_provenance["identifier_mapping"]["mapping"] == (
        "exact_key_retro_to_key_mlbam"
    )
    assert known_provenance["identifier_mapping"]["fuzzy_matching"] is False
    assert integrity == "ok"
    assert foreign_keys == []
    assert checkpoints
    assert all(row["status"] == "completed" for row in checkpoints)
    assert any(
        row["dataset_key"] == "retrosheet:gameinfo:season:1955"
        for row in checkpoints
    )
    assert any(
        row["dataset_key"] == "retrosheet:fielding:season:1955"
        for row in checkpoints
    )

    second_service, database = _service(tmp_path / "run", fixtures)
    second = second_service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET, request
    )

    assert second.status == "completed"
    after = _counts(database)
    for table, count in before.items():
        if table != "stats_excluded_source_rows":
            assert after[table] == count
    assert after["stats_excluded_source_rows"] == 14
    with database.connect() as connection:
        assert int(
            connection.execute("SELECT COUNT(*) FROM stats_ingestion_runs").fetchone()[0]
        ) == 2
        assert int(
            connection.execute("SELECT COUNT(*) FROM stats_raw_payload_metadata").fetchone()[0]
        ) == 18
        assert str(connection.execute("PRAGMA integrity_check").fetchone()[0]) == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    validation = second_service.validate(request)
    assert validation.validation_details is not None
    player_lines = validation.validation_details["player_game_line_inventory"]
    assert player_lines == {
        "snapshot_rows": 6,
        "logical_identities": 6,
        "correction_revisions": 0,
        "duplicate_revision_rows": 0,
        "duplicate_normalized_rows": 0,
        "latest_logical_duplicates": 0,
    }
    assert validation.validation_details["duplicates"][
        "player_game_logical_duplicates"
    ] == 0



def test_retrosheet_fielding_uses_position_and_repeated_stint_grain(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(
        tmp_path / "fixtures",
        fielding=(
            "gid,id,team,stattype,gametype,d_pos,f_po\n"
            "LAN202607100,known001,LAN,value,regular,8,2\n"
            "LAN202607100,known001,LAN,value,regular,9,1\n"
            "LAN202607100,known001,LAN,value,regular,8,1\n"
        ),
    )
    service, database = _service(tmp_path / "run", fixtures)
    request = AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE)

    first = service.execute(AcquisitionCommand.BOOTSTRAP_RETROSHEET, request)

    assert first.status == "completed"
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT snapshot.position_code,snapshot.source_stint_key,"
            "snapshot.source_row_key,snapshot.revision_number,"
            "snapshot.revision_kind,player.provider_player_id "
            "FROM stats_game_player_snapshots AS snapshot "
            "JOIN stats_player_identities AS player "
            "ON player.player_identity_id=snapshot.player_identity_id "
            "JOIN stats_game_identities AS game "
            "ON game.game_identity_id=snapshot.game_identity_id "
            "WHERE snapshot.role='fielding' "
            "AND player.provider_player_id='known001' "
            "AND game.provider_game_id='LAN202607100' "
            "ORDER BY snapshot.player_snapshot_id"
        ).fetchall()
    assert [
        (
            row["position_code"],
            row["source_stint_key"],
            row["source_row_key"],
            row["revision_number"],
            row["revision_kind"],
        )
        for row in rows
    ] == [
        ("8", "000001", "fielding:8:000001", 1, "initial"),
        ("9", "000001", "fielding:9:000001", 1, "initial"),
        ("8", "000002", "fielding:8:000002", 1, "initial"),
    ]

    second_service, _ = _service(tmp_path / "run", fixtures)
    second = second_service.execute(AcquisitionCommand.BOOTSTRAP_RETROSHEET, request)
    assert second.status == "completed"
    with database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM stats_game_player_snapshots "
            "WHERE role='fielding'"
        ).fetchone()[0] == 3
        assert connection.execute(
            "SELECT COUNT(*) FROM stats_game_player_snapshots "
            "WHERE role='fielding' AND revision_kind='correction'"
        ).fetchone()[0] == 0

def test_retrosheet_checkpoint_is_not_completed_when_member_import_interrupts(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(
        tmp_path / "fixtures",
        batting=(
            "gid,id,team,stattype,gametype,b_pa,b_ab,b_h,b_bb\n"
            "BRO195509010,former01,BRO,value,regular,4,4,2,0\n"
            "LAN202607100,known001,LAN,value,regular,,0,0,\n"
            "LAN202610010,known001,LAN,value,postseason,4,4,1,0\n"
        ),
    )
    service, database = _service(tmp_path / "run", fixtures)
    original = service.repository.record_game_player_snapshots
    persistence_calls = 0

    def interrupt_once(records: object) -> object:
        nonlocal persistence_calls
        persistence_calls += 1
        if persistence_calls == 2:
            raise KeyboardInterrupt("fixture interruption")
        return original(records)  # type: ignore[arg-type]

    service.repository.record_game_player_snapshots = interrupt_once  # type: ignore[assignment]
    request = AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE)

    try:
        service.execute(AcquisitionCommand.BOOTSTRAP_RETROSHEET, request)
    except KeyboardInterrupt as exc:
        assert str(exc) == "fixture interruption"
    else:
        raise AssertionError("expected fixture interruption")

    with database.connect() as connection:
        running = connection.execute(
            "SELECT status FROM stats_checkpoints "
            "WHERE dataset_key='retrosheet:batting:season:2026'"
        ).fetchone()
        completed_prior_season = connection.execute(
            "SELECT status,records_seen,records_persisted,cursor_after_json "
            "FROM stats_checkpoints "
            "WHERE dataset_key='retrosheet:batting:season:1955'"
        ).fetchone()
        master = connection.execute(
            "SELECT status FROM stats_checkpoints "
            "WHERE dataset_key='retrosheet_regular_season'"
        ).fetchone()
        stats_run_id = str(
            connection.execute(
                "SELECT stats_run_id FROM stats_ingestion_runs "
                "WHERE status='running'"
            ).fetchone()[0]
        )
    assert master is not None and master["status"] == "completed"
    assert running is not None and running["status"] == "running"
    assert completed_prior_season is not None
    assert completed_prior_season["status"] == "completed"
    assert completed_prior_season["records_seen"] == 1
    assert completed_prior_season["records_persisted"] == 1
    assert json.loads(str(completed_prior_season["cursor_after_json"]))[
        "source_row_count"
    ] == 1

    resumed, database = _service(tmp_path / "run", fixtures)
    result = resumed.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(
            date(2026, 12, 31),
            mode=AcquisitionMode.OFFLINE,
            resume_run_id=stats_run_id,
        ),
    )
    assert result.status == "completed"
    with database.connect() as connection:
        assert {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT status FROM stats_checkpoints "
                "WHERE stats_run_id=?",
                (stats_run_id,),
            ).fetchall()
        } == {"completed"}
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_game_player_snapshots AS snapshot "
                "JOIN stats_game_identities AS game "
                "ON game.game_identity_id=snapshot.game_identity_id "
                "WHERE game.provider_game_id='BRO195509010' "
                "AND snapshot.role='batting'"
            ).fetchone()[0]
        ) == 1


def test_retrosheet_readiness_requires_every_raw_inventory_season_checkpoint(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _service(tmp_path / "run", fixtures)
    result = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM stats_checkpoints WHERE stats_run_id=?",
            (result.stats_run_id,),
        ).fetchall()
    checkpoints = {str(row["dataset_key"]): dict(row) for row in rows}

    missing, evidence_errors, expected, completed = (
        service._retrosheet_checkpoint_readiness(
            checkpoints,
            through_season=2026,
            register_artifact_verified=True,
        )
    )

    assert missing == []
    assert evidence_errors == []
    assert expected > len(RETROSHEET_SEVEN_MEMBERS)
    assert completed == expected
    integrated = service._validation_source_readiness(
        season=2027,
        requested_through_date=date(2027, 7, 16),
    )["retrosheet_through_prior_season"]
    assert integrated["ready"] is True
    assert integrated["expected_member_season_checkpoint_count"] == expected
    assert integrated["completed_member_season_checkpoint_count"] == expected
    assert integrated["checkpoint_evidence_errors"] == []
    assert integrated["analytical_inventory"]["ready"] is True
    assert integrated["analytical_inventory"]["contract"] == (
        "DSE_RETROSHEET_ANALYTICAL_INVENTORY_V1"
    )

    actual_inventory = service._retrosheet_analytical_inventory(2026)
    _, evidence_errors, _, _ = service._retrosheet_checkpoint_readiness(
        checkpoints,
        through_season=2026,
        actual_analytical_inventory=actual_inventory,
        register_artifact_verified=True,
    )
    assert evidence_errors == []

    floating_register = {key: dict(value) for key, value in checkpoints.items()}
    register = dict(floating_register["pybaseball_player_identifier_register"])
    register_cursor = json.loads(str(register["cursor_after_json"]))
    register_cursor["resource_revision"] = "refs/heads/master"
    register["cursor_after_json"] = json.dumps(register_cursor)
    floating_register["pybaseball_player_identifier_register"] = register
    _, evidence_errors, _, _ = service._retrosheet_checkpoint_readiness(
        floating_register,
        through_season=2026,
        register_artifact_verified=True,
    )
    assert len(evidence_errors) == 1
    assert evidence_errors[0].startswith("player_identifier_register_invalid:")

    incomplete = {key: dict(value) for key, value in checkpoints.items()}
    incomplete["retrosheet:fielding:season:1955"]["status"] = "running"
    missing, evidence_errors, _, completed = (
        service._retrosheet_checkpoint_readiness(
            incomplete,
            through_season=2026,
            register_artifact_verified=True,
        )
    )
    assert missing == ["retrosheet:fielding:season:1955"]
    assert evidence_errors == []
    assert completed == expected - 1

    invalid_manifest = {key: dict(value) for key, value in checkpoints.items()}
    manifest = dict(invalid_manifest["retrosheet_member_season_manifest"])
    manifest_cursor = json.loads(str(manifest["cursor_after_json"]))
    manifest_cursor["entries"] = [
        entry
        for entry in manifest_cursor["entries"]
        if entry["dataset_key"] != "retrosheet:fielding:season:1955"
    ]
    manifest["cursor_after_json"] = json.dumps(manifest_cursor)
    invalid_manifest["retrosheet_member_season_manifest"] = manifest
    missing, evidence_errors, _, completed = (
        service._retrosheet_checkpoint_readiness(
            invalid_manifest,
            through_season=2026,
            register_artifact_verified=True,
        )
    )
    assert missing == []
    assert evidence_errors == [
        "member_season_manifest_invalid:manifest does not match raw source "
        "season inventory"
    ]
    assert completed == expected

    invalid_inventory = {key: dict(value) for key, value in checkpoints.items()}
    inventory = dict(invalid_inventory["retrosheet_analytical_inventory"])
    inventory_cursor = json.loads(str(inventory["cursor_after_json"]))
    inventory_cursor["tables"]["games"]["row_count"] += 1
    inventory["cursor_after_json"] = json.dumps(inventory_cursor)
    invalid_inventory["retrosheet_analytical_inventory"] = inventory
    _, evidence_errors, _, _ = service._retrosheet_checkpoint_readiness(
        invalid_inventory,
        through_season=2026,
        actual_analytical_inventory=actual_inventory,
        register_artifact_verified=True,
    )
    assert evidence_errors == ["analytical_inventory_mismatch:games"]

    service.repository.upsert_team_identity(
        {
            "team_identity_id": "team:retrosheet:UNBACKED",
            "provider": "retrosheet",
            "provider_team_id": "UNBACKED",
            "canonical_team_key": None,
            "current_name": "Unbacked Team",
            "active": False,
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "identity_checksum": "a" * 64,
        }
    )
    tampered = service._validation_source_readiness(
        season=2027,
        requested_through_date=date(2027, 7, 16),
    )["retrosheet_through_prior_season"]
    assert tampered["ready"] is False
    assert tampered["analytical_inventory"]["ready"] is False
    assert (
        "analytical_inventory_mismatch:team_identities"
        in tampered["checkpoint_evidence_errors"]
    )


def test_exact_pin_bridge_preserves_terminal_source_and_unlocks_legacy_readiness(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _legacy_source_service(tmp_path / "run", fixtures)
    source = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    source_run_before, source_checkpoint_before = _mark_register_checkpoint_as_legacy(
        database,
        tmp_path / "run" / "raw",
        str(source.stats_run_id),
    )

    bridge_service, database = _service(tmp_path / "run", fixtures)
    bridge = bridge_service.execute(
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
        AcquisitionRequest(
            date(2026, 12, 31),
            mode=AcquisitionMode.OFFLINE,
            source_stats_run_id=str(source.stats_run_id),
        ),
    )

    assert bridge.status == "completed"
    assert bridge.validation_details is not None
    assert bridge.validation_details["contract"] == (
        PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT
    )
    assert bridge.validation_details["equivalent"] is True
    with database.connect() as connection:
        source_run_after = dict(
            connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (source.stats_run_id,),
            ).fetchone()
        )
        source_checkpoint_after = dict(
            connection.execute(
                "SELECT * FROM stats_checkpoints WHERE stats_run_id=? "
                "AND dataset_key='pybaseball_player_identifier_register'",
                (source.stats_run_id,),
            ).fetchone()
        )
        bridge_run = connection.execute(
            "SELECT provider,scope_key,status FROM stats_ingestion_runs "
            "WHERE stats_run_id=?",
            (bridge.stats_run_id,),
        ).fetchone()
        bridge_checkpoint = connection.execute(
            "SELECT cursor_after_json FROM stats_checkpoints WHERE stats_run_id=? "
            "AND dataset_key=?",
            (bridge.stats_run_id, PLAYER_REGISTER_RECONCILIATION_DATASET_KEY),
        ).fetchone()
        pinned_raw = connection.execute(
            "SELECT raw.artifact_relpath FROM stats_raw_payload_metadata AS raw "
            "JOIN stats_checkpoints AS checkpoint "
            "ON checkpoint.checkpoint_id=raw.checkpoint_id "
            "WHERE checkpoint.stats_run_id=? AND checkpoint.dataset_key=?",
            (bridge.stats_run_id, PLAYER_REGISTER_RECONCILIATION_DATASET_KEY),
        ).fetchone()
    assert source_run_after == source_run_before
    assert source_checkpoint_after["status"] == source_checkpoint_before["status"]
    assert source_checkpoint_after["completed_at"] == source_checkpoint_before[
        "completed_at"
    ]
    assert bridge_run is not None
    assert dict(bridge_run) == {
        "provider": PLAYER_REGISTER_RECONCILIATION_PROVIDER,
        "scope_key": f"player-register-pin:{source.stats_run_id}",
        "status": "completed",
    }
    assert bridge_checkpoint is not None
    cursor = json.loads(str(bridge_checkpoint["cursor_after_json"]))
    assert cursor["contract"] == PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT
    assert cursor["source_stats_run_id"] == source.stats_run_id
    assert cursor["pinned_stats_run_id"] == bridge.stats_run_id
    assert cursor["pinned_resource_revision"] == PYBASEBALL_REGISTER_COMMIT_SHA
    assert cursor["source_inventory"] == cursor["pinned_inventory"]
    bridge_readiness = bridge_service._player_register_pin_bridge_readiness(
        source_run_after
    )
    assert bridge_readiness["ready"] is True
    readiness = bridge_service._validation_source_readiness(
        season=2027,
        requested_through_date=date(2027, 7, 16),
    )["retrosheet_through_prior_season"]
    assert readiness["ready"] is True
    assert readiness["checkpoint_evidence_errors"] == []
    assert readiness["player_register_pin_reconciliation"]["ready"] is True
    assert pinned_raw is not None
    pinned_path = (tmp_path / "run" / "raw").joinpath(
        str(pinned_raw["artifact_relpath"])
    )
    pinned_path.write_bytes(b"tampered")
    tampered_bridge = bridge_service._player_register_pin_bridge_readiness(
        source_run_after
    )
    assert tampered_bridge["ready"] is False
    errors = tampered_bridge["errors"]
    assert isinstance(errors, list)
    assert str(errors[0]).startswith(
        "bridge_reverification_failed:"
    )


def test_exact_pin_bridge_fails_closed_on_content_drift(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _legacy_source_service(tmp_path / "run", fixtures)
    source = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    source_run_before, _ = _mark_register_checkpoint_as_legacy(
        database,
        tmp_path / "run" / "raw",
        str(source.stats_run_id),
    )
    register_path = fixtures / "pybaseball_player_identifier_register.zip"
    with zipfile.ZipFile(register_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "register-pinned/data/people-a.csv",
            "key_retro,key_mlbam,key_bbref,key_fangraphs,name_last,name_first\n"
            "known001,999999,known01,1,Known,Player\n",
        )
    bridge_service, database = _service(tmp_path / "run", fixtures)

    try:
        bridge_service.execute(
            AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
            AcquisitionRequest(
                date(2026, 12, 31),
                mode=AcquisitionMode.OFFLINE,
                source_stats_run_id=str(source.stats_run_id),
            ),
        )
    except AcquisitionExecutionError as exc:
        assert "inventories differ" in str(exc)
    else:
        raise AssertionError("expected register inventory drift to fail closed")

    with database.connect() as connection:
        source_run_after = dict(
            connection.execute(
                "SELECT * FROM stats_ingestion_runs WHERE stats_run_id=?",
                (source.stats_run_id,),
            ).fetchone()
        )
        failed_bridge_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_ingestion_runs WHERE provider=? "
                "AND status='failed'",
                (PLAYER_REGISTER_RECONCILIATION_PROVIDER,),
            ).fetchone()[0]
        )
    assert source_run_after == source_run_before
    assert failed_bridge_count == 1
    assert bridge_service._player_register_pin_bridge_readiness(source_run_after)[
        "ready"
    ] is False


def test_exact_pin_bridge_resume_reuses_checksum_verified_pinned_capture(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _legacy_source_service(tmp_path / "run", fixtures)
    source = service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    _mark_register_checkpoint_as_legacy(
        database,
        tmp_path / "run" / "raw",
        str(source.stats_run_id),
    )
    interrupted, database = _service(tmp_path / "run", fixtures)

    def interrupt_after_raw(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise KeyboardInterrupt("fixture interruption after pinned raw retention")

    interrupted._complete_checkpoint = interrupt_after_raw  # type: ignore[method-assign]
    request = AcquisitionRequest(
        date(2026, 12, 31),
        mode=AcquisitionMode.OFFLINE,
        source_stats_run_id=str(source.stats_run_id),
    )
    try:
        interrupted.execute(
            AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
            request,
        )
    except KeyboardInterrupt as exc:
        assert "after pinned raw retention" in str(exc)
    else:
        raise AssertionError("expected fixture interruption")
    with database.connect() as connection:
        interrupted_run = connection.execute(
            "SELECT stats_run_id FROM stats_ingestion_runs WHERE provider=? "
            "AND status='running'",
            (PLAYER_REGISTER_RECONCILIATION_PROVIDER,),
        ).fetchone()
        assert interrupted_run is not None
        resume_stats_run_id = str(interrupted_run["stats_run_id"])
        assert int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_raw_payload_metadata "
                "WHERE stats_run_id=?",
                (resume_stats_run_id,),
            ).fetchone()[0]
        ) == 1

    resumed, _ = _service(tmp_path / "run", fixtures)
    (fixtures / "pybaseball_player_identifier_register.zip").unlink()
    result = resumed.execute(
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
        AcquisitionRequest(
            date(2026, 12, 31),
            mode=AcquisitionMode.OFFLINE,
            source_stats_run_id=str(source.stats_run_id),
            resume_run_id=resume_stats_run_id,
        ),
    )

    assert result.status == "completed"
    assert result.stats_run_id == resume_stats_run_id
    assert result.counts["network_requests"] == 0


def test_exact_pin_bridge_resume_rejects_stale_checkpoint_scope(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    source_service, _ = _legacy_source_service(tmp_path / "run", fixtures)
    source = source_service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    _mark_register_checkpoint_as_legacy(
        source_service.database,
        tmp_path / "run" / "raw",
        str(source.stats_run_id),
    )
    service, _ = _service(tmp_path / "run", fixtures)
    request = AcquisitionRequest(
        date(2026, 12, 31),
        mode=AcquisitionMode.OFFLINE,
        source_stats_run_id=str(source.stats_run_id),
    )
    context = service._begin_run(  # noqa: SLF001 - intentional resume fixture
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
        request,
    )
    checkpoint_id = service.id_factory("checkpoint")
    service.repository.create_checkpoint(
        {
            "checkpoint_id": checkpoint_id,
            "stats_run_id": context.stats_run_id,
            "dataset_key": PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
            "scope_key": "regular-season:2026",
            "created_at": NOW,
        }
    )
    service.repository.transition_checkpoint(
        checkpoint_id,
        "running",
        transitioned_at=NOW,
    )

    try:
        service._checkpoint(  # noqa: SLF001 - validates legacy resume rejection
            context,
            PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
        )
    except AcquisitionResumeError as exc:
        assert "scope does not match" in str(exc)
    else:
        raise AssertionError("expected stale checkpoint scope to fail closed")


def test_exact_pin_bridge_resume_rejects_completed_checkpoint_cursor_drift(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    source_service, _ = _legacy_source_service(tmp_path / "run", fixtures)
    source = source_service.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET,
        AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE),
    )
    _mark_register_checkpoint_as_legacy(
        source_service.database,
        tmp_path / "run" / "raw",
        str(source.stats_run_id),
    )
    service, _ = _service(tmp_path / "run", fixtures)
    request = AcquisitionRequest(
        date(2026, 12, 31),
        mode=AcquisitionMode.OFFLINE,
        source_stats_run_id=str(source.stats_run_id),
    )
    context = service._begin_run(  # noqa: SLF001 - crash-window fixture
        AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
        request,
    )
    checkpoint_id = service._checkpoint(  # noqa: SLF001 - crash-window fixture
        context,
        PLAYER_REGISTER_RECONCILIATION_DATASET_KEY,
    )
    pinned = PybaseballPlayerRegisterProvider(service.transport).collect()
    service._persist_raw(  # noqa: SLF001 - crash-window fixture
        context,
        checkpoint_id,
        pinned.raw,
    )
    service._complete_checkpoint(  # noqa: SLF001 - simulates crash after completion
        checkpoint_id,
        seen=0,
        persisted=0,
        source_observed_at=pinned.raw.retrieved_at,
        cursor_after={"contract": "drifted"},
    )

    try:
        service.execute(
            AcquisitionCommand.RECONCILE_PLAYER_REGISTER_PIN,
            AcquisitionRequest(
                date(2026, 12, 31),
                mode=AcquisitionMode.OFFLINE,
                source_stats_run_id=str(source.stats_run_id),
                resume_run_id=context.stats_run_id,
            ),
        )
    except AcquisitionResumeError as exc:
        assert "failed exact completion validation" in str(exc)
    else:
        raise AssertionError("expected completed checkpoint drift to fail closed")


def test_retrosheet_failure_terminalizes_active_checkpoint_and_repairs_legacy_state(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, database = _service(tmp_path / "run", fixtures)
    original = service.repository.record_game_player_snapshots
    failed = False

    def fail_once(records: object) -> object:
        nonlocal failed
        if not failed:
            failed = True
            raise AcquisitionExecutionError("fixture member import failure")
        return original(records)  # type: ignore[arg-type]

    service.repository.record_game_player_snapshots = fail_once  # type: ignore[assignment]
    request = AcquisitionRequest(date(2026, 12, 31), mode=AcquisitionMode.OFFLINE)

    try:
        service.execute(AcquisitionCommand.BOOTSTRAP_RETROSHEET, request)
    except AcquisitionExecutionError as exc:
        assert str(exc) == "fixture member import failure"
        assert exc.run_id is not None
        assert exc.stats_run_id is not None
        stats_run_id = exc.stats_run_id
    else:
        raise AssertionError("expected fixture member import failure")

    with database.connect() as connection:
        parent = connection.execute(
            "SELECT status FROM stats_ingestion_runs WHERE stats_run_id=?",
            (stats_run_id,),
        ).fetchone()
        master = connection.execute(
            "SELECT status FROM stats_checkpoints "
            "WHERE stats_run_id=? AND dataset_key='retrosheet_regular_season'",
            (stats_run_id,),
        ).fetchone()
        child = connection.execute(
            "SELECT checkpoint_id,status,error_json FROM stats_checkpoints "
            "WHERE stats_run_id=? "
            "AND dataset_key='retrosheet:batting:season:2026'",
            (stats_run_id,),
        ).fetchone()
        active_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_checkpoints "
                "WHERE stats_run_id=? AND status IN ('pending','running')",
                (stats_run_id,),
            ).fetchone()[0]
        )
    assert parent is not None and parent["status"] == "failed"
    assert master is not None and master["status"] == "completed"
    assert child is not None and child["status"] == "failed"
    assert json.loads(str(child["error_json"]))["code"] == "parent_run_failed"
    assert active_count == 0

    legacy_run_id = "run_20261231_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    legacy_stats_run_id = "stats_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    legacy_checkpoint_id = "checkpoint-legacy-active"
    database.create_run(legacy_run_id, date(2026, 12, 31))
    service.repository.create_ingestion_run(
        stats_run_id=legacy_stats_run_id,
        run_id=legacy_run_id,
        provider="retrosheet",
        scope_key="regular-season:2026",
        requested_through_date=date(2026, 12, 31),
        configuration_checksum="a" * 64,
        created_at=NOW,
    )
    service.repository.transition_ingestion_run(
        legacy_stats_run_id, "running", transitioned_at=NOW
    )
    service.repository.create_checkpoint(
        {
            "checkpoint_id": legacy_checkpoint_id,
            "stats_run_id": legacy_stats_run_id,
            "dataset_key": "legacy_interrupted_member",
            "scope_key": "regular-season:2026",
            "created_at": NOW,
        }
    )
    service.repository.transition_checkpoint(
        legacy_checkpoint_id, "running", transitioned_at=NOW
    )
    service.repository.transition_ingestion_run(
        legacy_stats_run_id,
        "failed",
        transitioned_at=NOW,
        failure_stage="acquisition",
        error={"code": "legacy_failure"},
    )

    restarted, _ = _service(tmp_path / "run", fixtures)
    replacement = restarted.execute(
        AcquisitionCommand.BOOTSTRAP_RETROSHEET, request
    )
    assert replacement.status == "completed"
    with database.connect() as connection:
        repaired = connection.execute(
            "SELECT status,error_json FROM stats_checkpoints WHERE checkpoint_id=?",
            (legacy_checkpoint_id,),
        ).fetchone()
    assert repaired is not None and repaired["status"] == "failed"
    assert json.loads(str(repaired["error_json"])) == {
        "code": "terminal_parent_run_reconciliation",
        "details": {"parent_status": "failed"},
    }


def test_bootstrap_rejects_archive_without_requested_through_season(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_archive(tmp_path / "fixtures")
    service, _ = _service(tmp_path / "run", fixtures)

    try:
        service.execute(
            AcquisitionCommand.BOOTSTRAP_RETROSHEET,
            AcquisitionRequest(date(2025, 12, 31), mode=AcquisitionMode.OFFLINE),
        )
    except AcquisitionExecutionError as exc:
        assert "requested through-season 2025" in str(exc)
    else:
        raise AssertionError("expected missing through-season failure")
