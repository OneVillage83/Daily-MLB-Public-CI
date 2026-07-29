from __future__ import annotations

import copy
import hashlib
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone

import pytest

from app.baseball_intelligence.assembly import (
    BaseballIntelligenceAssemblyError,
    BaseballIntelligenceWarningCode,
    assemble_baseball_intelligence,
)
from app.baseball_intelligence.contracts import (
    BaseballFeatureSnapshotV1,
    BaseballIntelligenceContractError,
    BaseballIntelligenceRole,
    IntelligenceAvailability,
)
from app.daily_slate.contracts import (
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    VenueMappingStatus,
)
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
from app.stats.features import (
    FEATURE_VERSION_V3,
    BattingAggregateLine,
    build_aggregate_player_feature_payloads_v3,
)

REQUESTED_DATE = "2026-07-27"
AS_OF = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
SLATE_OBSERVED = datetime(2026, 7, 27, 14, 5, tzinfo=timezone.utc)
STATE_OBSERVED = datetime(2026, 7, 27, 14, 10, tzinfo=timezone.utc)
FEATURE_CREATED = datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc)
FEATURE_CUTOFF = datetime(2026, 7, 27, 13, 0, tzinfo=timezone.utc)
RAW_SLATE_CHECKSUM = "a" * 64
RAW_GAME_CHECKSUM = "b" * 64


def _player(source_id: int, *, resolved: bool = True) -> GameStatePlayerV1:
    source = str(source_id)
    return GameStatePlayerV1(
        source_player_id=source,
        full_name=f"Player {source}",
        player_identity_id=f"identity:mlb:{source}" if resolved else None,
        canonical_player_id=f"player:canonical:{source}" if resolved else None,
    )


def _team_state(
    *,
    team_id: str,
    source_team_id: int,
    lineup_start: int,
    starter_id: int,
    bench_id: int,
    bullpen_id: int,
    lineup_count: int = 2,
    unresolved_source_ids: set[int] | None = None,
) -> TeamGameStateV1:
    unresolved = unresolved_source_ids or set()
    lineup = tuple(
        _player(
            lineup_start + index,
            resolved=(lineup_start + index) not in unresolved,
        )
        for index in range(lineup_count)
    )
    starter = _player(starter_id, resolved=starter_id not in unresolved)
    bench = _player(bench_id, resolved=bench_id not in unresolved)
    bullpen = _player(bullpen_id, resolved=bullpen_id not in unresolved)
    availability = (
        LineupAvailability.POSTED
        if lineup_count == 9
        else LineupAvailability.PARTIAL
    )
    return TeamGameStateV1(
        team_id=team_id,
        source_team_id=str(source_team_id),
        starter=StarterStateV1(
            certainty=StarterCertainty.PROBABLE,
            player=starter,
            source_designation="gameData.probablePitchers",
        ),
        lineup=LineupStateV1(
            availability=availability,
            entries=tuple(
                LineupEntryV1(player=player, batting_order_slot=index)
                for index, player in enumerate(lineup, start=1)
            ),
        ),
        personnel=GamedayPersonnelV1(
            available=True,
            batters=(*lineup, bench),
            pitchers=(starter, bullpen),
            bench=(bench,),
            bullpen=(bullpen,),
        ),
    )


def _slate_game(game_pk: int = 900001) -> DailySlateGameV1:
    source_id = str(game_pk)
    return DailySlateGameV1(
        edge_event_id=f"edge:mlb:{source_id}",
        daily_mlb_game_id=f"game:mlb:{source_id}",
        official_date=REQUESTED_DATE,
        scheduled_start_time=datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc),
        away_team_id="SF",
        home_team_id="LAD",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        game_number=1,
        doubleheader_status=DailySlateDoubleheaderStatus.SINGLE,
        game_status=DailySlateGameStatus.SCHEDULED,
        source_game_id=source_id,
        source_provider="mlb",
        observed_at=SLATE_OBSERVED,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=source_id,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="Scheduled",
            upstream_checksum=RAW_SLATE_CHECKSUM,
        ),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name="Dodger Stadium",
    )


def _slate(*games: DailySlateGameV1) -> DailySlateV1:
    return DailySlateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=games,
        provenance=DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum=RAW_SLATE_CHECKSUM,
        ),
    )


def _game_state_game(
    slate_game: DailySlateGameV1,
    *,
    away: TeamGameStateV1 | None = None,
    home: TeamGameStateV1 | None = None,
) -> GameStateGameV1:
    return GameStateGameV1(
        edge_event_id=slate_game.edge_event_id,
        daily_mlb_game_id=slate_game.daily_mlb_game_id,
        source_game_id=slate_game.source_game_id,
        away_team_id=slate_game.away_team_id,
        home_team_id=slate_game.home_team_id,
        game_status=DailySlateGameStatus.PREGAME,
        away=away
        or _team_state(
            team_id="SF",
            source_team_id=137,
            lineup_start=700001,
            starter_id=710001,
            bench_id=710010,
            bullpen_id=710020,
        ),
        home=home
        or _team_state(
            team_id="LAD",
            source_team_id=119,
            lineup_start=730001,
            starter_id=720001,
            bench_id=720010,
            bullpen_id=720020,
        ),
        observed_at=STATE_OBSERVED,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=slate_game.source_game_id,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="Pre-Game",
            upstream_checksum=RAW_GAME_CHECKSUM,
        ),
    )


def _game_state(
    slate: DailySlateV1,
    *games: GameStateGameV1,
    upstream_slate_checksum: str | None = None,
) -> GameStateV1:
    checksum = upstream_slate_checksum or slate.checksum
    return GameStateV1(
        requested_date=slate.requested_date,
        as_of_time=slate.as_of_time,
        observed_at=STATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=checksum,
        games=games,
        provenance=GameStateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=STATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="game_state_snapshot",
            upstream_checksum=checksum,
        ),
    )


def _v3_feature(
    canonical_player_id: str,
    *,
    feature_date: str = REQUESTED_DATE,
    variant: int = 0,
    feature_snapshot_id: str | None = None,
    stats_run_id: str = "stats_feature_20260727",
    knowledge_cutoff: datetime = FEATURE_CUTOFF,
    created_at: datetime = FEATURE_CREATED,
) -> BaseballFeatureSnapshotV1:
    as_of = date.fromisoformat(feature_date)
    payload = build_aggregate_player_feature_payloads_v3(
        feature_as_of=as_of,
        knowledge_cutoff=knowledge_cutoff,
        batting_aggregates=(
            BattingAggregateLine(
                as_of - timedelta(days=1),
                canonical_player_id,
                "season_to_date",
                pa=20 + variant,
                ab=18 + variant,
                hits=6 + variant,
                home_runs=1,
                walks=2,
                strikeouts=4,
                available_at=min(
                    knowledge_cutoff - timedelta(minutes=1),
                    datetime.combine(
                        as_of - timedelta(days=1),
                        datetime.min.time(),
                        tzinfo=timezone.utc,
                    ),
                ),
            ),
        ),
        pitching_aggregates=(),
    )[canonical_player_id]
    checksum = str(payload["feature_checksum"])
    suffix = hashlib.sha256(
        f"{canonical_player_id}:{feature_date}:{variant}".encode()
    ).hexdigest()[:12]
    return BaseballFeatureSnapshotV1(
        feature_snapshot_id=feature_snapshot_id or f"feature:{suffix}",
        stats_run_id=stats_run_id,
        feature_version=FEATURE_VERSION_V3,
        entity_kind="player",
        entity_id=canonical_player_id,
        feature_as_of=feature_date,
        completeness_state="complete",
        input_checksum=hashlib.sha256(f"input:{suffix}".encode()).hexdigest(),
        feature_checksum=checksum,
        features=payload,
        created_at=created_at,
    )


def _unique_team_players(team: TeamGameStateV1) -> tuple[GameStatePlayerV1, ...]:
    players: dict[str, GameStatePlayerV1] = {}
    if team.starter.player is not None:
        players[team.starter.player.source_player_id] = team.starter.player
    for entry in team.lineup.entries:
        players[entry.player.source_player_id] = entry.player
    for bucket in (
        team.personnel.batters,
        team.personnel.pitchers,
        team.personnel.bench,
        team.personnel.bullpen,
    ):
        for player in bucket:
            players[player.source_player_id] = player
    return tuple(players[key] for key in sorted(players, key=int))


def _features_for_state(state: GameStateV1) -> tuple[BaseballFeatureSnapshotV1, ...]:
    features: list[BaseballFeatureSnapshotV1] = []
    seen: set[str] = set()
    for game in state.games:
        for team in (game.away, game.home):
            for player in _unique_team_players(team):
                if player.canonical_player_id is None or player.canonical_player_id in seen:
                    continue
                seen.add(player.canonical_player_id)
                features.append(_v3_feature(player.canonical_player_id))
    return tuple(features)


def _one_game_state(
    *,
    away: TeamGameStateV1 | None = None,
    home: TeamGameStateV1 | None = None,
) -> tuple[DailySlateV1, GameStateV1]:
    slate_game = _slate_game()
    slate = _slate(slate_game)
    state_game = _game_state_game(slate_game, away=away, home=home)
    return slate, _game_state(slate, state_game)


def test_zero_game_assembly_is_valid_deterministic_and_empty() -> None:
    slate = _slate()
    state = _game_state(slate)

    first = assemble_baseball_intelligence(slate=slate, game_state=state)
    second = assemble_baseball_intelligence(slate=slate, game_state=state)

    assert first.warnings == ()
    assert first.assembly.games == ()
    assert first.assembly.source_stats_run_ids == ()
    assert first.assembly.source_feature_checksums == ()
    assert first.assembly.checksum == second.assembly.checksum
    assert first.assembly.canonical_json_bytes() == second.assembly.canonical_json_bytes()


def test_complete_assembly_joins_v3_features_and_merges_player_roles() -> None:
    slate, state = _one_game_state()
    features = _features_for_state(state)

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
    )

    assert result.warnings == ()
    game = result.assembly.games[0]
    away = game.away
    assert away.coverage.gameday_player_count == 5
    assert away.coverage.resolved_player_count == 5
    assert away.coverage.player_feature_count == 5
    assert away.coverage.lineup_feature_count == 2
    assert away.coverage.bullpen_feature_count == 1
    assert away.coverage.bench_feature_count == 1
    assert away.coverage.starter_feature_available is True
    assert away.lineup_source_player_ids == ("700001", "700002")

    starter = next(
        player for player in away.players if player.source_player_id == "710001"
    )
    assert starter.availability is IntelligenceAvailability.AVAILABLE
    assert starter.roles == (
        BaseballIntelligenceRole.STARTER,
        BaseballIntelligenceRole.PITCHER,
    )
    lineup_player = next(
        player for player in away.players if player.source_player_id == "700001"
    )
    assert lineup_player.roles == (
        BaseballIntelligenceRole.LINEUP,
        BaseballIntelligenceRole.BATTER,
    )
    assert result.assembly.source_stats_run_ids == ("stats_feature_20260727",)
    assert len(result.assembly.source_feature_checksums) == len(features)
    assert game.upstream_daily_slate_game_checksum == slate.games[0].checksum
    assert game.upstream_game_state_game_checksum == state.games[0].checksum


def test_posted_lineup_order_is_preserved_as_structural_identity() -> None:
    away = _team_state(
        team_id="SF",
        source_team_id=137,
        lineup_start=700001,
        starter_id=710001,
        bench_id=710010,
        bullpen_id=710020,
        lineup_count=9,
    )
    slate, state = _one_game_state(away=away)

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=_features_for_state(state),
    )

    assert result.assembly.games[0].away.lineup_source_player_ids == tuple(
        str(value) for value in range(700001, 700010)
    )
    assert result.assembly.games[0].away.coverage.lineup_feature_count == 9


def test_missing_features_are_explicit_warnings_and_coverage_not_defaults() -> None:
    slate, state = _one_game_state()
    features = list(_features_for_state(state))
    missing_canonical_ids = {
        "player:canonical:710001",
        "player:canonical:700001",
        "player:canonical:710020",
    }
    features = [
        feature for feature in features if feature.entity_id not in missing_canonical_ids
    ]

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
    )

    away = result.assembly.games[0].away
    assert away.coverage.player_feature_count == 2
    assert away.coverage.starter_feature_available is False
    assert away.coverage.lineup_feature_count == 1
    assert away.coverage.bullpen_feature_count == 0
    codes = [warning.code for warning in result.warnings]
    assert codes.count(BaseballIntelligenceWarningCode.PLAYER_FEATURE_MISSING) == 3
    assert BaseballIntelligenceWarningCode.STARTER_FEATURE_MISSING in codes
    assert BaseballIntelligenceWarningCode.LINEUP_FEATURE_INCOMPLETE in codes
    assert BaseballIntelligenceWarningCode.BULLPEN_FEATURE_INCOMPLETE in codes


def test_unresolved_player_never_matches_feature_by_name() -> None:
    away = _team_state(
        team_id="SF",
        source_team_id=137,
        lineup_start=700001,
        starter_id=710001,
        bench_id=710010,
        bullpen_id=710020,
        unresolved_source_ids={710001},
    )
    slate, state = _one_game_state(away=away)
    features = list(_features_for_state(state))
    features.append(_v3_feature("player:canonical:999999"))

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
    )

    starter = next(
        player
        for player in result.assembly.games[0].away.players
        if player.source_player_id == "710001"
    )
    assert starter.canonical_player_id is None
    assert starter.feature is None
    assert starter.availability is IntelligenceAvailability.UNAVAILABLE
    codes = [warning.code for warning in result.warnings]
    assert BaseballIntelligenceWarningCode.UNRESOLVED_PLAYER_IDENTITY in codes
    assert BaseballIntelligenceWarningCode.STARTER_FEATURE_MISSING in codes


def test_historical_feature_inventory_is_ignored_when_requested_date_exists() -> None:
    slate, state = _one_game_state()
    features = list(_features_for_state(state))
    canonical_id = "player:canonical:700001"
    selected = next(feature for feature in features if feature.entity_id == canonical_id)
    historical = _v3_feature(
        canonical_id,
        feature_date="2026-07-26",
        variant=7,
        feature_snapshot_id="feature:historical",
        created_at=datetime(2026, 7, 26, 13, tzinfo=timezone.utc),
        knowledge_cutoff=datetime(2026, 7, 26, 12, tzinfo=timezone.utc),
    )
    features.append(historical)

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
    )

    player = next(
        player
        for player in result.assembly.games[0].away.players
        if player.source_player_id == "700001"
    )
    assert player.feature is not None
    assert player.feature.feature_snapshot_id == selected.feature_snapshot_id
    assert historical.feature_checksum not in result.assembly.source_feature_checksums


def test_equivalent_duplicate_feature_snapshots_select_deterministically() -> None:
    slate, state = _one_game_state()
    features = list(_features_for_state(state))
    canonical_id = "player:canonical:700001"
    original = next(feature for feature in features if feature.entity_id == canonical_id)
    duplicate = BaseballFeatureSnapshotV1(
        feature_snapshot_id="feature:000000000000",
        stats_run_id="stats_equivalent_rerun",
        feature_version=original.feature_version,
        entity_kind=original.entity_kind,
        entity_id=original.entity_id,
        feature_as_of=original.feature_as_of,
        completeness_state=original.completeness_state,
        input_checksum="c" * 64,
        feature_checksum=original.feature_checksum,
        features=original.features,
        created_at=original.created_at,
    )
    features.append(duplicate)

    result = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=features,
    )

    player = next(
        player
        for player in result.assembly.games[0].away.players
        if player.source_player_id == "700001"
    )
    assert player.feature is not None
    assert player.feature.feature_snapshot_id == "feature:000000000000"
    assert "stats_equivalent_rerun" in result.assembly.source_stats_run_ids


def test_conflicting_same_date_feature_evidence_fails_closed() -> None:
    slate, state = _one_game_state()
    features = list(_features_for_state(state))
    features.append(
        _v3_feature(
            "player:canonical:700001",
            variant=3,
            feature_snapshot_id="feature:conflict",
            stats_run_id="stats_conflict",
        )
    )

    with pytest.raises(BaseballIntelligenceAssemblyError, match="conflicting retained"):
        assemble_baseball_intelligence(
            slate=slate,
            game_state=state,
            feature_snapshots=features,
        )


def test_future_feature_knowledge_cutoff_fails_closed() -> None:
    slate, state = _one_game_state()
    features = [
        feature
        for feature in _features_for_state(state)
        if feature.entity_id != "player:canonical:700001"
    ]
    features.append(
        _v3_feature(
            "player:canonical:700001",
            knowledge_cutoff=datetime(2026, 7, 27, 16, tzinfo=timezone.utc),
            created_at=datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc),
        )
    )

    with pytest.raises(BaseballIntelligenceAssemblyError, match="knowledge cutoff"):
        assemble_baseball_intelligence(
            slate=slate,
            game_state=state,
            feature_snapshots=features,
            observed_at=datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc),
        )


def test_assembly_observation_cannot_precede_relevant_feature_creation() -> None:
    slate, state = _one_game_state()
    features = [
        feature
        for feature in _features_for_state(state)
        if feature.entity_id != "player:canonical:700001"
    ]
    features.append(
        _v3_feature(
            "player:canonical:700001",
            created_at=datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc),
        )
    )

    with pytest.raises(BaseballIntelligenceAssemblyError, match="cannot precede"):
        assemble_baseball_intelligence(
            slate=slate,
            game_state=state,
            feature_snapshots=features,
            observed_at=datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc),
        )


def test_wrong_game_state_daily_slate_checksum_fails_closed() -> None:
    slate_game = _slate_game()
    slate = _slate(slate_game)
    state = _game_state(
        slate,
        _game_state_game(slate_game),
        upstream_slate_checksum="d" * 64,
    )

    with pytest.raises(BaseballIntelligenceAssemblyError, match="supplied DailySlate"):
        assemble_baseball_intelligence(slate=slate, game_state=state)


def test_daily_slate_and_game_state_source_team_mismatch_fails_closed() -> None:
    slate_game = _slate_game()
    slate = _slate(slate_game)
    away = _team_state(
        team_id="SF",
        source_team_id=999,
        lineup_start=700001,
        starter_id=710001,
        bench_id=710010,
        bullpen_id=710020,
    )
    state = _game_state(slate, _game_state_game(slate_game, away=away))

    with pytest.raises(BaseballIntelligenceAssemblyError, match="away source team"):
        assemble_baseball_intelligence(slate=slate, game_state=state)


def test_feature_contract_rejects_payload_tampering() -> None:
    feature = _v3_feature("player:canonical:700001")
    payload = copy.deepcopy(feature.as_dict()["features"])
    assert isinstance(payload, dict)
    hitting = payload["hitting"]
    assert isinstance(hitting, dict)
    season = hitting["season_to_date"]
    assert isinstance(season, dict)
    season["h"] = 999

    with pytest.raises(BaseballIntelligenceContractError, match="feature_checksum"):
        BaseballFeatureSnapshotV1(
            feature_snapshot_id="feature:tampered",
            stats_run_id=feature.stats_run_id,
            feature_version=feature.feature_version,
            entity_kind=feature.entity_kind,
            entity_id=feature.entity_id,
            feature_as_of=feature.feature_as_of,
            completeness_state=feature.completeness_state,
            input_checksum=feature.input_checksum,
            feature_checksum=feature.feature_checksum,
            features=payload,
            created_at=feature.created_at,
        )


def test_feature_payload_is_deeply_immutable_after_contract_creation() -> None:
    feature = _v3_feature("player:canonical:700001")

    with pytest.raises(TypeError):
        feature.features["new"] = 1  # type: ignore[index]
    hitting = feature.features["hitting"]
    assert isinstance(hitting, Mapping)
    with pytest.raises(TypeError):
        hitting["new"] = 1  # type: ignore[index]
