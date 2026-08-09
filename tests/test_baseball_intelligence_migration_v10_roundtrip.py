from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import pytest

from app.baseball_intelligence.assembly import assemble_baseball_intelligence
from app.baseball_intelligence.contracts import (
    BaseballFeatureSnapshotV1,
    BaseballIntelligenceAssemblyV1,
    FeatureCompletenessState,
    IntelligenceAvailability,
    PlayerIntelligenceV1,
)
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
    canonical_sha256,
)
from app.database import Database
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupEntryV1,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)
from app.migrations import CURRENT_SCHEMA_VERSION
from app.stats.features import (
    FEATURE_VERSION_V3,
    BattingAggregateLine,
    build_aggregate_player_feature_payloads_v3,
)


REQUESTED_DATE = "2026-07-30"
AS_OF = datetime(2026, 7, 30, 14, 0, tzinfo=timezone.utc)
SLATE_OBSERVED = datetime(2026, 7, 30, 14, 5, tzinfo=timezone.utc)
STATE_OBSERVED = datetime(2026, 7, 30, 14, 10, tzinfo=timezone.utc)
SELECTION_OBSERVED = datetime(2026, 7, 30, 14, 15, tzinfo=timezone.utc)
FEATURE_CREATED = datetime(2026, 7, 30, 13, 30, tzinfo=timezone.utc)
LATE_FEATURE_CREATED = datetime(2026, 7, 30, 14, 16, tzinfo=timezone.utc)
FEATURE_CUTOFF = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)
SEALED_AT = datetime(2026, 7, 30, 14, 20, tzinfo=timezone.utc)
RUN_ID = "run_20260730_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GAME_PK = "900001"
CONFIG_CHECKSUM = "f" * 64
RAW_CHECKSUM = "a" * 64
ATTEMPT_MANIFEST = b'{"contract":"fixture-v10","outcome":"assembled"}'


@dataclass(frozen=True, slots=True)
class Fixture:
    slate: DailySlateV1
    state: GameStateV1
    assembly: BaseballIntelligenceAssemblyV1
    features: tuple[BaseballFeatureSnapshotV1, ...]
    snapshot_id: str


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _player(source_id: int, *, resolved: bool = True) -> GameStatePlayerV1:
    source = str(source_id)
    return GameStatePlayerV1(
        source_player_id=source,
        full_name=f"Fixture Player {source}",
        player_identity_id=f"identity:mlb:{source}" if resolved else None,
        canonical_player_id=f"player:canonical:{source}" if resolved else None,
    )


def _away_team() -> TeamGameStateV1:
    unresolved = _player(1001, resolved=False)
    no_feature = _player(1002)
    blocked = _player(1003)
    late = _player(1004)
    complete = _player(1005)
    degraded = _player(1006)
    return TeamGameStateV1(
        team_id="SF",
        source_team_id="137",
        starter=StarterStateV1(
            certainty=StarterCertainty.PROBABLE,
            player=complete,
            source_designation="gameData.probablePitchers",
        ),
        lineup=LineupStateV1(
            availability=LineupAvailability.PARTIAL,
            entries=(
                LineupEntryV1(player=unresolved, batting_order_slot=1),
                LineupEntryV1(player=no_feature, batting_order_slot=2),
                LineupEntryV1(player=degraded, batting_order_slot=3),
            ),
        ),
        personnel=GamedayPersonnelV1(
            available=True,
            batters=(unresolved, no_feature, late, degraded),
            pitchers=(complete, blocked),
            bench=(late,),
            bullpen=(blocked,),
        ),
    )


def _home_team() -> TeamGameStateV1:
    return TeamGameStateV1(
        team_id="LAD",
        source_team_id="119",
        starter=StarterStateV1(
            certainty=StarterCertainty.UNAVAILABLE,
            player=None,
            source_designation=None,
        ),
        lineup=LineupStateV1(availability=LineupAvailability.UNAVAILABLE, entries=()),
        personnel=GamedayPersonnelV1(
            available=False,
            batters=(),
            pitchers=(),
            bench=(),
            bullpen=(),
        ),
    )


def _slate() -> DailySlateV1:
    game = DailySlateGameV1(
        edge_event_id=f"edge:mlb:{GAME_PK}",
        daily_mlb_game_id=f"game:mlb:{GAME_PK}",
        official_date=REQUESTED_DATE,
        scheduled_start_time=datetime(2026, 7, 30, 23, 10, tzinfo=timezone.utc),
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id=GAME_PK,
        source_provider="mlb",
        observed_at=SLATE_OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=GAME_PK,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum=RAW_CHECKSUM,
        ),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
    )
    return DailySlateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=(game,),
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum=RAW_CHECKSUM,
        ),
    )


def _state(slate: DailySlateV1) -> GameStateV1:
    slate_game = slate.games[0]
    game = GameStateGameV1(
        edge_event_id=slate_game.edge_event_id,
        daily_mlb_game_id=slate_game.daily_mlb_game_id,
        source_game_id=slate_game.source_game_id,
        away_team_id=slate_game.away_team_id,
        home_team_id=slate_game.home_team_id,
        game_status=DailySlateGameStatus.PREGAME,
        away=_away_team(),
        home=_home_team(),
        observed_at=STATE_OBSERVED,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=slate_game.source_game_id,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="Pre-Game",
            upstream_checksum=RAW_CHECKSUM,
        ),
    )
    return GameStateV1(
        requested_date=slate.requested_date,
        as_of_time=slate.as_of_time,
        observed_at=STATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=slate.checksum,
        games=(game,),
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="game_state_snapshot",
            upstream_checksum=slate.checksum,
        ),
    )


def _feature(
    canonical_player_id: str,
    *,
    feature_snapshot_id: str,
    stats_run_id: str,
    completeness_state: FeatureCompletenessState,
    created_at: datetime = FEATURE_CREATED,
    input_salt: str,
) -> BaseballFeatureSnapshotV1:
    as_of = date.fromisoformat(REQUESTED_DATE)
    payload = build_aggregate_player_feature_payloads_v3(
        feature_as_of=as_of,
        knowledge_cutoff=FEATURE_CUTOFF,
        batting_aggregates=(
            BattingAggregateLine(
                as_of - timedelta(days=1),
                canonical_player_id,
                "season_to_date",
                pa=20,
                ab=18,
                hits=6,
                home_runs=1,
                walks=2,
                strikeouts=4,
                available_at=datetime(2026, 7, 29, tzinfo=timezone.utc),
            ),
        ),
        pitching_aggregates=(),
    )[canonical_player_id]
    return BaseballFeatureSnapshotV1(
        feature_snapshot_id=feature_snapshot_id,
        stats_run_id=stats_run_id,
        feature_version=FEATURE_VERSION_V3,
        entity_kind="player",
        entity_id=canonical_player_id,
        feature_as_of=REQUESTED_DATE,
        completeness_state=completeness_state,
        input_checksum=_hash(f"input:{input_salt}"),
        feature_checksum=str(payload["feature_checksum"]),
        features=payload,
        created_at=created_at,
    )


def _fixture() -> Fixture:
    slate = _slate()
    state = _state(slate)
    features = (
        _feature(
            "player:canonical:1003",
            feature_snapshot_id="feature:blocked",
            stats_run_id="stats-run-blocked",
            completeness_state=FeatureCompletenessState.BLOCKED,
            input_salt="blocked",
        ),
        _feature(
            "player:canonical:1004",
            feature_snapshot_id="feature:late",
            stats_run_id="stats-run-late",
            completeness_state=FeatureCompletenessState.COMPLETE,
            created_at=LATE_FEATURE_CREATED,
            input_salt="late",
        ),
        _feature(
            "player:canonical:1005",
            feature_snapshot_id="feature:complete:a",
            stats_run_id="stats-run-complete-a",
            completeness_state=FeatureCompletenessState.COMPLETE,
            input_salt="complete-a",
        ),
        _feature(
            "player:canonical:1005",
            feature_snapshot_id="feature:complete:b",
            stats_run_id="stats-run-complete-b",
            completeness_state=FeatureCompletenessState.COMPLETE,
            input_salt="complete-b",
        ),
        _feature(
            "player:canonical:1006",
            feature_snapshot_id="feature:degraded",
            stats_run_id="stats-run-degraded",
            completeness_state=FeatureCompletenessState.DEGRADED,
            input_salt="degraded",
        ),
    )
    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
        observed_at=SELECTION_OBSERVED,
    )
    return Fixture(
        slate=slate,
        state=state,
        assembly=result.assembly,
        features=features,
        snapshot_id=f"bia:{result.assembly.checksum}",
    )


def _insert_pipeline(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id,sport,run_type,requested_date,as_of_time,timezone,
            pipeline_version,configuration_version,configuration_fingerprint,
            configuration_metadata_json,code_revision,database_schema_version,
            force_refresh,status,failure_phase,error_message,final_summary_json,
            created_at,started_at,completed_at,updated_at
        ) VALUES (?
            ,'MLB','manual_daily',?,'2026-07-30T14:00:00+00:00','America/Los_Angeles',
            'DSE_MANUAL_RUN_CONTROLLER_V1','DSE_DAILY_MLB_CONFIG_V1',?,
            '{}','fixture-v10',10,0,'running',NULL,NULL,NULL,?,?,NULL,?
        )
        """,
        (RUN_ID, REQUESTED_DATE, CONFIG_CHECKSUM, AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat()),
    )
    phase_keys = (
        "daily_slate",
        "game_state",
        "baseball_intelligence_assembly",
        "odds_weather",
        "data_quality",
        "matchup_packet",
        "model_feature_set",
        "predictions",
        "value_engine",
        "recommendation_gate",
        "rankings",
        "pdf_report",
        "infographic",
        "final_qc",
        "human_review",
    )
    for ordinal, key in enumerate(phase_keys, start=1):
        connection.execute(
            """
            INSERT INTO pipeline_run_phases(
                run_id,phase_key,ordinal,status,attempt_count,started_at,
                completed_at,updated_at,input_checksum,output_checksum,
                artifact_relpath,warnings_json,error_json,reused_from_run_id
            ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, NULL, NULL, NULL, NULL, NULL)
            """,
            (RUN_ID, key, ordinal, AS_OF.isoformat()),
        )


def _persist_upstream(connection: sqlite3.Connection, fixture: Fixture) -> tuple[str, str]:
    slate = fixture.slate
    state = fixture.state
    slate_id = f"slate:{slate.checksum}"
    state_id = f"state:{state.checksum}"
    _insert_pipeline(connection)
    connection.execute(
        "UPDATE pipeline_run_phases SET status='running',attempt_count=1,started_at=? WHERE run_id=? AND phase_key='daily_slate'",
        (AS_OF.isoformat(), RUN_ID),
    )
    connection.execute(
        """
        INSERT INTO daily_slate_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
            sport,league,source_authority,source_version,contract_version,snapshot_checksum,
            artifact_relpath,artifact_checksum,provenance_json,canonical_json,game_count,sealed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,?,?,?,NULL,?)
        """,
        (
            slate_id, RUN_ID, "daily_slate", 1, slate.requested_date, slate.as_of_time.isoformat(),
            slate.observed_at.isoformat(), slate.sport, slate.league, slate.source_authority,
            slate.source_version, slate.contract_version, slate.checksum,
            _json(slate.provenance.as_dict()), slate.canonical_json_bytes().decode("utf-8"),
            len(slate.games), slate.observed_at.isoformat(),
        ),
    )
    for ordinal, game in enumerate(slate.games, start=1):
        connection.execute(
            """
            INSERT INTO daily_slate_games(
                snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,official_date,scheduled_start_time,
                away_team_id,home_team_id,venue_id,venue_mapping_status,game_number,doubleheader_status,
                game_status,source_game_id,source_provider,source_home_team_id,source_away_team_id,
                source_venue_id,source_venue_name,observed_at,source_updated_at,
                away_probable_player_identity_id,away_probable_canonical_player_id,
                home_probable_player_identity_id,home_probable_canonical_player_id,
                provenance_json,canonical_json,row_checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                slate_id, ordinal, game.edge_event_id, game.daily_mlb_game_id, game.official_date,
                game.scheduled_start_time.isoformat() if game.scheduled_start_time else None,
                game.away_team_id, game.home_team_id, game.venue_id, game.venue_mapping_status.value,
                game.game_number, game.doubleheader_status.value, game.game_status.value,
                game.source_game_id, game.source_provider, game.source_home_team_id,
                game.source_away_team_id, game.source_venue_id, game.source_venue_name,
                game.observed_at.isoformat(),
                game.source_updated_at.isoformat() if game.source_updated_at else None,
                None, None, None, None, _json(game.provenance.as_dict()),
                _json(game.as_dict()), game.checksum,
            ),
        )
    connection.execute("UPDATE daily_slate_snapshots SET sealed_at=? WHERE snapshot_id=?", (SLATE_OBSERVED.isoformat(), slate_id))
    connection.execute(
        "UPDATE pipeline_run_phases SET status='succeeded',completed_at=?,updated_at=?,output_checksum=? WHERE run_id=? AND phase_key='daily_slate'",
        (SLATE_OBSERVED.isoformat(), SLATE_OBSERVED.isoformat(), slate.checksum, RUN_ID),
    )
    connection.execute(
        "UPDATE pipeline_run_phases SET status='running',attempt_count=1,started_at=?,updated_at=? WHERE run_id=? AND phase_key='game_state'",
        (STATE_OBSERVED.isoformat(), STATE_OBSERVED.isoformat(), RUN_ID),
    )
    raw_link_checksum = hashlib.sha256(b"fixture game-state raw link").hexdigest()
    connection.execute(
        """
        INSERT INTO game_state_attempt_evidence(
            run_id,phase_key,phase_attempt,requested_date,upstream_daily_slate_checksum,outcome,
            normalized_snapshot_checksum,raw_link_relpath,raw_link_checksum,raw_link_byte_count,
            warnings_json,warning_count,created_at
        ) VALUES (?, 'game_state', 1, ?, ?, 'normalized', ?, 'game_state/raw_links/fixture.json', ?, 27, '[]', 0, ?)
        """,
        (RUN_ID, state.requested_date, slate.checksum, state.checksum, raw_link_checksum, STATE_OBSERVED.isoformat()),
    )
    connection.execute(
        """
        INSERT INTO game_state_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
            sport,league,source_authority,source_version,contract_version,
            upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,snapshot_checksum,
            artifact_relpath,artifact_checksum,artifact_byte_count,provenance_json,canonical_json,
            game_count,sealed_at,created_at
        ) VALUES (?,?, 'game_state',1,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,?,?,?,NULL,?)
        """,
        (
            state_id, RUN_ID, state.requested_date, state.as_of_time.isoformat(), state.observed_at.isoformat(),
            state.sport, state.league, state.source_authority, state.source_version, state.contract_version,
            slate_id, slate.checksum, state.checksum, _json(state.provenance.as_dict()),
            state.canonical_json_bytes().decode("utf-8"), len(state.games), state.observed_at.isoformat(),
        ),
    )
    for ordinal, state_game in enumerate(state.games, start=1):
        connection.execute(
            """
            INSERT INTO game_state_games(
                snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,source_game_id,away_team_id,home_team_id,
                game_status,away_starter_certainty,home_starter_certainty,away_lineup_availability,
                home_lineup_availability,observed_at,provenance_json,canonical_json,row_checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                state_id, ordinal, state_game.edge_event_id, state_game.daily_mlb_game_id,
                state_game.source_game_id, state_game.away_team_id, state_game.home_team_id,
                state_game.game_status.value, state_game.away.starter.certainty.value,
                state_game.home.starter.certainty.value,
                state_game.away.lineup.availability.value,
                state_game.home.lineup.availability.value, state_game.observed_at.isoformat(),
                _json(state_game.provenance.as_dict()), _json(state_game.as_dict()), state_game.checksum,
            ),
        )
    connection.execute("UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?", (STATE_OBSERVED.isoformat(), state_id))
    connection.execute(
        "UPDATE pipeline_run_phases SET status='succeeded',completed_at=?,updated_at=?,output_checksum=? WHERE run_id=? AND phase_key='game_state'",
        (STATE_OBSERVED.isoformat(), STATE_OBSERVED.isoformat(), state.checksum, RUN_ID),
    )
    connection.execute(
        "UPDATE pipeline_run_phases SET status='running',attempt_count=1,started_at=?,updated_at=? WHERE run_id=? AND phase_key='baseball_intelligence_assembly'",
        (SELECTION_OBSERVED.isoformat(), SELECTION_OBSERVED.isoformat(), RUN_ID),
    )
    return slate_id, state_id


def _persist_feature_inventory(connection: sqlite3.Connection, fixture: Fixture) -> None:
    connection.execute(
        """
        INSERT INTO collector_runs(run_id,requested_date,status,created_at,queued_at,started_at,completed_at,updated_at,
            failure_stage,error_message,artifact_relpath,app_version,schema_version)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "collector_fixture_v10", REQUESTED_DATE, "completed", AS_OF.isoformat(),
            AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat(), AS_OF.isoformat(),
            None, None, None, "test", 10,
        ),
    )
    canonical_ids = sorted(
        {feature.entity_id for feature in fixture.features}
        | {"player:canonical:1002"}
    )
    stats_runs = sorted({feature.stats_run_id for feature in fixture.features})
    for stats_run_id in stats_runs:
        connection.execute(
            """
            INSERT INTO stats_ingestion_runs(stats_run_id,run_id,provider,source_version,adapter_version,scope_key,
                requested_through_date,source_observed_at,status,created_at,started_at,completed_at,updated_at,configuration_checksum)
            VALUES (?, 'collector_fixture_v10', 'fixture', 'v3', 'fixture-v10', 'regular-season:2026', ?, ?,
                'completed', ?, ?, ?, ?, ?)
            """,
            (stats_run_id, REQUESTED_DATE, FEATURE_CREATED.isoformat(), FEATURE_CREATED.isoformat(), FEATURE_CREATED.isoformat(), FEATURE_CREATED.isoformat(), FEATURE_CREATED.isoformat(), _hash(stats_run_id)),
        )
    created_run = stats_runs[0]
    for canonical_player_id in canonical_ids:
        connection.execute(
            """
            INSERT INTO stats_canonical_players(canonical_player_id,created_stats_run_id,display_name,birth_date,created_at,canonical_checksum)
            VALUES (?, ?, ?, NULL, ?, ?)
            """,
            (canonical_player_id, created_run, canonical_player_id, FEATURE_CREATED.isoformat(), _hash(canonical_player_id)),
        )
    for feature in fixture.features:
        connection.execute(
            """
            INSERT INTO stats_feature_snapshots(
                feature_snapshot_id,stats_run_id,feature_version,entity_kind,game_identity_id,team_identity_id,
                player_identity_id,canonical_player_id,feature_as_of,completeness_state,observed_through,
                complete_through,input_checksum,feature_checksum,features_json,created_at
            ) VALUES (?, ?, ?, 'player', NULL, NULL, NULL, ?, ?, ?, NULL, NULL, ?, ?, ?, ?)
            """,
            (
                feature.feature_snapshot_id, feature.stats_run_id, feature.feature_version, feature.entity_id,
                feature.feature_as_of, FeatureCompletenessState(feature.completeness_state).value,
                feature.input_checksum, feature.feature_checksum, _json(feature.as_dict()["features"]), feature.created_at.isoformat(),
            ),
        )


def _players(assembly: BaseballIntelligenceAssemblyV1) -> tuple[tuple[str, str, PlayerIntelligenceV1], ...]:
    rows: list[tuple[str, str, PlayerIntelligenceV1]] = []
    for game in assembly.games:
        rows.extend((game.edge_event_id, "away", player) for player in game.away.players)
        rows.extend((game.edge_event_id, "home", player) for player in game.home.players)
    return tuple(rows)


def _insert_bia(
    connection: sqlite3.Connection,
    fixture: Fixture,
    slate_id: str,
    state_id: str,
    *,
    mutation: Literal[
        "none", "snapshot_id", "source_team", "identity_left", "identity_right",
        "wrong_representative", "omit_representative", "player_ordinal_gap",
        "equivalent_ordinal_gap", "wrong_game_player_count", "wrong_game_available_count",
    ] = "none",
) -> None:
    assembly = fixture.assembly
    warning_payload = [warning.as_dict() for warning in assemble_baseball_intelligence(
        slate=fixture.slate, game_state=fixture.state, feature_snapshots=fixture.features,
        observed_at=SELECTION_OBSERVED,
    ).warnings]
    manifest_checksum = hashlib.sha256(ATTEMPT_MANIFEST).hexdigest()
    connection.execute(
        """
        INSERT INTO baseball_intelligence_attempt_evidence(
            run_id,phase_key,phase_attempt,requested_date,upstream_daily_slate_snapshot_id,
            upstream_daily_slate_checksum,upstream_game_state_snapshot_id,upstream_game_state_checksum,
            selection_observed_at,outcome,assembly_checksum,evidence_manifest_relpath,
            evidence_manifest_checksum,evidence_manifest_byte_count,warnings_json,warning_count,created_at
        ) VALUES (?, 'baseball_intelligence_assembly', 1, ?, ?, ?, ?, ?, ?, 'assembled', ?,
            'baseball_intelligence/attempts/fixture.json', ?, ?, ?, ?, ?)
        """,
        (RUN_ID, assembly.requested_date, slate_id, assembly.upstream_daily_slate_checksum,
         state_id, assembly.upstream_game_state_checksum, assembly.observed_at.isoformat(), assembly.checksum,
         manifest_checksum, len(ATTEMPT_MANIFEST), _json(warning_payload), len(warning_payload), assembly.observed_at.isoformat()),
    )
    artifact = assembly.canonical_json_bytes()
    snapshot_id = fixture.snapshot_id if mutation != "snapshot_id" else "bia:" + ("e" * 64)
    connection.execute(
        """
        INSERT INTO baseball_intelligence_snapshots(
            snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,sport,league,
            contract_version,feature_version,upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
            upstream_game_state_snapshot_id,upstream_game_state_checksum,assembly_checksum,artifact_relpath,
            artifact_checksum,artifact_byte_count,source_stats_run_ids_json,source_feature_checksums_json,
            warnings_json,warning_count,canonical_json,game_count,player_count,available_feature_count,
            equivalent_feature_row_count,sealed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id, RUN_ID, "baseball_intelligence_assembly", 1, assembly.requested_date,
            assembly.as_of_time.isoformat(), assembly.observed_at.isoformat(),
            assembly.sport, assembly.league, assembly.contract_version, assembly.feature_version,
            slate_id, assembly.upstream_daily_slate_checksum, state_id, assembly.upstream_game_state_checksum,
            assembly.checksum, f"baseball_intelligence/snapshots/{assembly.checksum}/baseball_intelligence_v1.json",
            hashlib.sha256(artifact).hexdigest(), len(artifact), _json(list(assembly.source_stats_run_ids)),
            _json(list(assembly.source_feature_checksums)), _json(warning_payload), len(warning_payload),
            artifact.decode("utf-8"), len(assembly.games), len(_players(assembly)),
            sum(player.availability is IntelligenceAvailability.AVAILABLE for _, _, player in _players(assembly)),
            sum(len(player.equivalent_feature_snapshot_ids) for _, _, player in _players(assembly)),
            None, assembly.observed_at.isoformat(),
        ),
    )
    inventory = {feature.feature_snapshot_id: feature for feature in fixture.features}
    for ordinal, game in enumerate(assembly.games, start=1):
        players = (*game.away.players, *game.home.players)
        player_count = len(players)
        available_count = sum(player.availability is IntelligenceAvailability.AVAILABLE for player in players)
        if mutation == "wrong_game_player_count":
            player_count += 1
        if mutation == "wrong_game_available_count":
            available_count += 1
        connection.execute(
            """
            INSERT INTO baseball_intelligence_games(
                snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,source_game_id,away_team_id,home_team_id,
                venue_id,game_status,upstream_daily_slate_game_checksum,upstream_game_state_game_checksum,
                player_count,available_feature_count,canonical_json,row_checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (snapshot_id, ordinal, game.edge_event_id, game.daily_mlb_game_id, game.source_game_id,
             game.away_team_id, game.home_team_id, game.venue_id, game.game_status.value,
             game.upstream_daily_slate_game_checksum, game.upstream_game_state_game_checksum,
             player_count, available_count, _json(game.as_dict()), game.checksum),
        )
        for team_side, team in (("away", game.away), ("home", game.home)):
            for player_ordinal, player in enumerate(team.players, start=1):
                identity_id = player.player_identity_id
                canonical_id = player.canonical_player_id
                source_team_id = team.source_team_id
                if mutation == "source_team" and player.source_player_id == "1002":
                    source_team_id = "119"
                if mutation == "identity_left" and player.source_player_id == "1002":
                    canonical_id = None
                if mutation == "identity_right" and player.source_player_id == "1002":
                    identity_id = None
                row_ordinal = player_ordinal
                if mutation == "player_ordinal_gap" and player.source_player_id == "1006":
                    row_ordinal += 1
                connection.execute(
                    """
                    INSERT INTO baseball_intelligence_players(
                        snapshot_id,edge_event_id,team_side,team_id,source_team_id,source_player_id,
                        player_identity_id,canonical_player_id,availability,representative_feature_snapshot_id,
                        representative_stats_run_id,representative_feature_checksum,representative_completeness_state,
                        game_state_player_checksum,roles_json,canonical_json,row_checksum,ordinal
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        snapshot_id, game.edge_event_id, team_side, team.team_id, source_team_id,
                        player.source_player_id, identity_id, canonical_id, player.availability.value,
                        None if player.feature is None else player.feature.feature_snapshot_id,
                        None if player.feature is None else player.feature.stats_run_id,
                        None if player.feature is None else player.feature.feature_checksum,
                        None if player.feature is None else FeatureCompletenessState(player.feature.completeness_state).value,
                        player.game_state_player_checksum, _json([role.value for role in player.roles]),
                        _json(player.as_dict()), canonical_sha256(player.as_dict()), row_ordinal,
                    ),
                )
                if player.feature is None:
                    continue
                for equivalent_ordinal, feature_snapshot_id in enumerate(player.equivalent_feature_snapshot_ids, start=1):
                    feature = inventory[feature_snapshot_id]
                    is_representative = int(feature_snapshot_id == player.feature.feature_snapshot_id)
                    if mutation == "wrong_representative" and player.source_player_id == "1005":
                        is_representative = int(not is_representative)
                    if mutation == "omit_representative" and player.source_player_id == "1005" and is_representative:
                        continue
                    if mutation == "equivalent_ordinal_gap" and player.source_player_id == "1005" and equivalent_ordinal == 2:
                        equivalent_ordinal = 3
                    connection.execute(
                        """
                        INSERT INTO baseball_intelligence_feature_equivalents(
                            snapshot_id,edge_event_id,team_id,source_player_id,ordinal,feature_snapshot_id,
                            stats_run_id,canonical_player_id,feature_checksum,completeness_state,is_representative
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot_id, game.edge_event_id, team.team_id, player.source_player_id,
                            equivalent_ordinal, feature.feature_snapshot_id, feature.stats_run_id,
                            feature.entity_id, feature.feature_checksum,
                            FeatureCompletenessState(feature.completeness_state).value, is_representative,
                        ),
                    )


def _build_database(tmp_path: Path, *, mutation: str = "none") -> tuple[sqlite3.Connection, Fixture]:
    path = tmp_path / f"bia-roundtrip-{mutation}.sqlite3"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    fixture = _fixture()
    slate_id, state_id = _persist_upstream(connection, fixture)
    _persist_feature_inventory(connection, fixture)
    _insert_bia(connection, fixture, slate_id, state_id, mutation=mutation)  # type: ignore[arg-type]
    return connection, fixture


def _seal(connection: sqlite3.Connection, fixture: Fixture) -> None:
    connection.execute(
        "UPDATE baseball_intelligence_snapshots SET sealed_at=? WHERE snapshot_id=?",
        (SEALED_AT.isoformat(), fixture.snapshot_id),
    )


def _assert_assembly_contract(fixture: Fixture) -> None:
    assembly = fixture.assembly
    assert assembly.upstream_daily_slate_checksum == fixture.slate.checksum
    assert assembly.upstream_game_state_checksum == fixture.state.checksum
    assert tuple(game.source_game_id for game in assembly.games) == (GAME_PK,)
    players = {player.source_player_id: player for _, _, player in _players(assembly)}
    assert set(players) == {"1001", "1002", "1003", "1004", "1005", "1006"}
    assert players["1001"].player_identity_id is None and players["1001"].availability is IntelligenceAvailability.UNAVAILABLE
    for source_id in ("1002", "1003", "1004"):
        assert players[source_id].player_identity_id is not None
        assert players[source_id].canonical_player_id is not None
        assert players[source_id].availability is IntelligenceAvailability.UNAVAILABLE
    assert players["1005"].availability is IntelligenceAvailability.AVAILABLE
    assert players["1005"].equivalent_feature_snapshot_ids == ("feature:complete:a", "feature:complete:b")
    assert players["1006"].availability is IntelligenceAvailability.AVAILABLE
    assert players["1006"].feature is not None
    assert players["1006"].feature.completeness_state is FeatureCompletenessState.DEGRADED
    roles = {source_id: {role.value for role in player.roles} for source_id, player in players.items()}
    assert roles["1005"] == {"starter", "pitcher"}
    assert roles["1003"] == {"bullpen", "pitcher"}
    assert roles["1004"] == {"bench", "batter"}
    assert {"lineup", "batter"}.issubset(roles["1001"])
    warning_codes = {warning["code"] for warning in assemble_baseball_intelligence(
        slate=fixture.slate, game_state=fixture.state, feature_snapshots=fixture.features,
        observed_at=SELECTION_OBSERVED,
    ).warning_payload()}
    assert {"blocked_feature_excluded", "feature_after_selection_boundary"}.issubset(warning_codes)
    assert set(assembly.source_stats_run_ids) == {
        "stats-run-complete-a", "stats-run-complete-b", "stats-run-degraded"
    }
    assert len(assembly.source_feature_checksums) == 2


def test_bia_v10_six_category_contract_round_trip_and_immutable_seal(tmp_path: Path) -> None:
    connection, fixture = _build_database(tmp_path)
    try:
        _assert_assembly_contract(fixture)
        _seal(connection, fixture)
        connection.commit()

        attempt = connection.execute("SELECT * FROM baseball_intelligence_attempt_evidence").fetchone()
        snapshot = connection.execute("SELECT * FROM baseball_intelligence_snapshots").fetchone()
        games = connection.execute("SELECT * FROM baseball_intelligence_games").fetchall()
        players = connection.execute("SELECT * FROM baseball_intelligence_players ORDER BY team_id,ordinal").fetchall()
        equivalents = connection.execute("SELECT * FROM baseball_intelligence_feature_equivalents ORDER BY source_player_id,ordinal").fetchall()
        assert attempt is not None and attempt[9] == "assembled" and attempt[10] == fixture.assembly.checksum
        assert snapshot is not None and snapshot[0] == fixture.snapshot_id and snapshot[28] is not None
        assert json.loads(snapshot[23]) == fixture.assembly.as_dict()
        assert json.loads(snapshot[19]) == list(fixture.assembly.source_stats_run_ids)
        assert json.loads(snapshot[20]) == list(fixture.assembly.source_feature_checksums)
        assert len(games) == 1 and json.loads(games[0][13]) == fixture.assembly.games[0].as_dict()
        assert games[0][14] == fixture.assembly.games[0].checksum
        assert len(players) == len(_players(fixture.assembly)) == 6
        persisted = {row[5]: row for row in players}
        assert persisted["1001"][6:9] == (None, None, "unavailable")
        for source_id in ("1002", "1003", "1004"):
            assert persisted[source_id][6] is not None and persisted[source_id][7] is not None
            assert persisted[source_id][8] == "unavailable"
            assert persisted[source_id][9:13] == (None, None, None, None)
        assert persisted["1005"][8] == "available"
        assert persisted["1006"][12] == "degraded"
        assert len([row for row in equivalents if row[3] == "1005"]) == 2
        assert len([row for row in equivalents if row[3] == "1006"]) == 1
        assert not [row for row in equivalents if row[3] in {"1001", "1002", "1003", "1004"}]
        for source_id in ("1005", "1006"):
            selected = [row for row in equivalents if row[3] == source_id]
            assert [row[4] for row in selected] == list(range(1, len(selected) + 1))
            assert sum(row[10] for row in selected) == 1
            parent = persisted[source_id]
            representative = next(row for row in selected if row[10] == 1)
            assert (representative[5], representative[6], representative[8]) == parent[9:12]
        assert snapshot[24:28] == (1, 6, 2, 3)
        for sql, params in (
            ("INSERT INTO baseball_intelligence_games(snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,source_game_id,away_team_id,home_team_id,venue_id,game_status,upstream_daily_slate_game_checksum,upstream_game_state_game_checksum,player_count,available_feature_count,canonical_json,row_checksum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (fixture.snapshot_id, 2, "edge:mlb:900002", "game:mlb:900002", "900002", "SF", "LAD", None, "pregame", "a" * 64, "b" * 64, 0, 0, "{}", "c" * 64)),
            ("DELETE FROM baseball_intelligence_players WHERE snapshot_id=? AND source_player_id='1001'", (fixture.snapshot_id,)),
            ("DELETE FROM baseball_intelligence_snapshots WHERE snapshot_id=?", (fixture.snapshot_id,)),
            ("UPDATE baseball_intelligence_snapshots SET sealed_at=? WHERE snapshot_id=?", (SEALED_AT.isoformat(), fixture.snapshot_id)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "mutation",
    (
        "snapshot_id", "source_team", "identity_left", "identity_right", "wrong_representative",
        "omit_representative", "player_ordinal_gap", "equivalent_ordinal_gap",
        "wrong_game_player_count", "wrong_game_available_count",
    ),
)
def test_bia_v10_round_trip_fixture_rejects_relational_contract_violations(
    tmp_path: Path, mutation: str
) -> None:
    if mutation in {"snapshot_id", "source_team", "identity_left", "identity_right"}:
        with pytest.raises(sqlite3.IntegrityError):
            _build_database(tmp_path, mutation=mutation)
        return
    connection, fixture = _build_database(tmp_path, mutation=mutation)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            _seal(connection, fixture)
    finally:
        connection.close()


def test_bia_v10_zero_game_relational_snapshot_seals(tmp_path: Path) -> None:
    slate = DailySlateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=(),
        provenance=DailySlateProvenanceV1("mlb", None, SLATE_OBSERVED, source_version="statsapi-v1", raw_status="schedule", upstream_checksum=RAW_CHECKSUM),
    )
    state = GameStateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=slate.checksum,
        games=(),
        provenance=GameStateProvenanceV1("mlb", None, SLATE_OBSERVED, source_version="statsapi-game-feed-v1.1", raw_status="snapshot", upstream_checksum=slate.checksum),
    )
    assembly = assemble_baseball_intelligence(slate=slate, game_state=state, observed_at=SLATE_OBSERVED).assembly
    fixture = Fixture(slate, state, assembly, (), f"bia:{assembly.checksum}")
    path = tmp_path / "bia-zero-game.sqlite3"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        slate_id, state_id = _persist_upstream(connection, fixture)
        _insert_bia(connection, fixture, slate_id, state_id)
        _seal(connection, fixture)
        row = connection.execute("SELECT canonical_json,game_count,player_count,equivalent_feature_row_count,sealed_at FROM baseball_intelligence_snapshots WHERE snapshot_id=?", (fixture.snapshot_id,)).fetchone()
        assert row is not None
        assert json.loads(row[0]) == assembly.as_dict()
        assert row[1:4] == (0, 0, 0)
        assert row[4] is not None
    finally:
        connection.close()


def test_bia_v10_roundtrip_uses_current_schema_only() -> None:
    assert CURRENT_SCHEMA_VERSION == 14
