from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.baseball_intelligence.contracts import (
    BaseballFeatureSnapshotV1,
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceContractError,
    BaseballIntelligenceGameV1,
    BaseballIntelligenceRole,
    IntelligenceAvailability,
    PlayerIntelligenceV1,
    TeamBaseballIntelligenceV1,
    TeamIntelligenceCoverageV1,
)
from app.daily_slate.contracts import DailySlateGameV1, DailySlateV1, canonical_sha256
from app.game_state.contracts import (
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateV1,
    TeamGameStateV1,
)
from app.stats.features import FEATURE_VERSION_V3


class BaseballIntelligenceAssemblyError(RuntimeError):
    """Fail-closed Baseball Intelligence Assembly error."""


class BaseballIntelligenceWarningCode(StrEnum):
    UNRESOLVED_PLAYER_IDENTITY = "unresolved_player_identity"
    PLAYER_FEATURE_MISSING = "player_feature_missing"
    STARTER_FEATURE_MISSING = "starter_feature_missing"
    LINEUP_FEATURE_INCOMPLETE = "lineup_feature_incomplete"
    BULLPEN_FEATURE_INCOMPLETE = "bullpen_feature_incomplete"
    BENCH_FEATURE_INCOMPLETE = "bench_feature_incomplete"


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceWarningV1:
    code: BaseballIntelligenceWarningCode
    message: str
    source_game_id: str
    team_id: str | None = None
    source_player_id: str | None = None
    missing_count: int | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code.value,
            "message": self.message,
            "missing_count": self.missing_count,
            "source_game_id": self.source_game_id,
            "source_player_id": self.source_player_id,
            "team_id": self.team_id,
        }


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceAssemblyResultV1:
    assembly: BaseballIntelligenceAssemblyV1
    warnings: tuple[BaseballIntelligenceWarningV1, ...]

    def warning_payload(self) -> list[dict[str, object]]:
        return [warning.as_dict() for warning in self.warnings]


def _select_feature(
    candidates: Iterable[BaseballFeatureSnapshotV1],
    *,
    entity_id: str,
    requested_date: str,
    assembly_observed_at: datetime,
) -> BaseballFeatureSnapshotV1 | None:
    accepted: list[BaseballFeatureSnapshotV1] = []
    for feature in candidates:
        if feature.entity_kind != "player" or feature.entity_id != entity_id:
            raise BaseballIntelligenceAssemblyError(
                "feature inventory index disagrees with feature entity identity"
            )
        if feature.feature_version != FEATURE_VERSION_V3:
            raise BaseballIntelligenceAssemblyError(
                "Baseball Intelligence Assembly accepts only frozen V3 features"
            )
        if feature.feature_as_of != requested_date:
            continue
        knowledge_cutoff = feature.knowledge_cutoff
        if knowledge_cutoff is not None and knowledge_cutoff > assembly_observed_at:
            raise BaseballIntelligenceAssemblyError(
                "feature knowledge cutoff is later than assembly observation time"
            )
        if feature.created_at > assembly_observed_at:
            raise BaseballIntelligenceAssemblyError(
                "feature snapshot was created after assembly observation time"
            )
        accepted.append(feature)
    if not accepted:
        return None

    feature_checksums = {feature.feature_checksum for feature in accepted}
    if len(feature_checksums) != 1:
        raise BaseballIntelligenceAssemblyError(
            "conflicting retained V3 feature evidence exists for one player/date"
        )
    return min(
        accepted,
        key=lambda feature: (
            feature.feature_snapshot_id,
            feature.stats_run_id,
            feature.input_checksum,
        ),
    )


def _index_player_features(
    features: Iterable[BaseballFeatureSnapshotV1],
) -> Mapping[str, tuple[BaseballFeatureSnapshotV1, ...]]:
    players: dict[str, list[BaseballFeatureSnapshotV1]] = defaultdict(list)
    for feature in features:
        if not isinstance(feature, BaseballFeatureSnapshotV1):
            raise BaseballIntelligenceAssemblyError(
                "feature inventory must contain BaseballFeatureSnapshotV1 values"
            )
        if feature.entity_kind == "player":
            players[feature.entity_id].append(feature)
    return {key: tuple(values) for key, values in players.items()}


def _merge_player(
    registry: dict[str, tuple[GameStatePlayerV1, set[BaseballIntelligenceRole]]],
    player: GameStatePlayerV1,
    role: BaseballIntelligenceRole,
) -> None:
    existing = registry.get(player.source_player_id)
    if existing is None:
        registry[player.source_player_id] = (player, {role})
        return
    existing_player, roles = existing
    if existing_player.as_dict() != player.as_dict():
        raise BaseballIntelligenceAssemblyError(
            "GameState repeats a source player with contradictory identity/details"
        )
    roles.add(role)


def _team_player_registry(
    team_state: TeamGameStateV1,
) -> dict[str, tuple[GameStatePlayerV1, set[BaseballIntelligenceRole]]]:
    registry: dict[
        str, tuple[GameStatePlayerV1, set[BaseballIntelligenceRole]]
    ] = {}
    if team_state.starter.player is not None:
        _merge_player(
            registry,
            team_state.starter.player,
            BaseballIntelligenceRole.STARTER,
        )
    for entry in team_state.lineup.entries:
        _merge_player(registry, entry.player, BaseballIntelligenceRole.LINEUP)
    for player in team_state.personnel.bullpen:
        _merge_player(registry, player, BaseballIntelligenceRole.BULLPEN)
    for player in team_state.personnel.bench:
        _merge_player(registry, player, BaseballIntelligenceRole.BENCH)
    for player in team_state.personnel.batters:
        _merge_player(registry, player, BaseballIntelligenceRole.BATTER)
    for player in team_state.personnel.pitchers:
        _merge_player(registry, player, BaseballIntelligenceRole.PITCHER)
    return registry


def _player_intelligence(
    *,
    game_state_player: GameStatePlayerV1,
    roles: set[BaseballIntelligenceRole],
    player_features: Mapping[str, tuple[BaseballFeatureSnapshotV1, ...]],
    requested_date: str,
    assembly_observed_at: datetime,
    source_game_id: str,
    team_id: str,
    warnings: list[BaseballIntelligenceWarningV1],
) -> PlayerIntelligenceV1:
    feature: BaseballFeatureSnapshotV1 | None = None
    if game_state_player.canonical_player_id is None:
        warnings.append(
            BaseballIntelligenceWarningV1(
                code=BaseballIntelligenceWarningCode.UNRESOLVED_PLAYER_IDENTITY,
                message="GameState player lacks a verified canonical player identity",
                source_game_id=source_game_id,
                team_id=team_id,
                source_player_id=game_state_player.source_player_id,
            )
        )
    else:
        feature = _select_feature(
            player_features.get(game_state_player.canonical_player_id, ()),
            entity_id=game_state_player.canonical_player_id,
            requested_date=requested_date,
            assembly_observed_at=assembly_observed_at,
        )
        if feature is None:
            warnings.append(
                BaseballIntelligenceWarningV1(
                    code=BaseballIntelligenceWarningCode.PLAYER_FEATURE_MISSING,
                    message="verified GameState player has no accepted V3 feature snapshot",
                    source_game_id=source_game_id,
                    team_id=team_id,
                    source_player_id=game_state_player.source_player_id,
                )
            )
    return PlayerIntelligenceV1(
        source_player_id=game_state_player.source_player_id,
        full_name=game_state_player.full_name,
        player_identity_id=game_state_player.player_identity_id,
        canonical_player_id=game_state_player.canonical_player_id,
        roles=tuple(roles),
        game_state_player_checksum=canonical_sha256(game_state_player.as_dict()),
        availability=(
            IntelligenceAvailability.AVAILABLE
            if feature is not None
            else IntelligenceAvailability.UNAVAILABLE
        ),
        feature=feature,
    )


def _team_intelligence(
    *,
    team_state: TeamGameStateV1,
    player_features: Mapping[str, tuple[BaseballFeatureSnapshotV1, ...]],
    requested_date: str,
    assembly_observed_at: datetime,
    source_game_id: str,
    warnings: list[BaseballIntelligenceWarningV1],
) -> TeamBaseballIntelligenceV1:
    registry = _team_player_registry(team_state)
    players = tuple(
        _player_intelligence(
            game_state_player=player,
            roles=roles,
            player_features=player_features,
            requested_date=requested_date,
            assembly_observed_at=assembly_observed_at,
            source_game_id=source_game_id,
            team_id=team_state.team_id,
            warnings=warnings,
        )
        for _, (player, roles) in sorted(
            registry.items(), key=lambda item: int(item[0])
        )
    )
    by_source_id = {player.source_player_id: player for player in players}

    lineup_ids = tuple(
        entry.player.source_player_id for entry in team_state.lineup.entries
    )
    bullpen_ids = tuple(
        player.source_player_id for player in team_state.personnel.bullpen
    )
    bench_ids = tuple(
        player.source_player_id for player in team_state.personnel.bench
    )
    batter_ids = tuple(
        player.source_player_id for player in team_state.personnel.batters
    )
    pitcher_ids = tuple(
        player.source_player_id for player in team_state.personnel.pitchers
    )
    starter_id = (
        None
        if team_state.starter.player is None
        else team_state.starter.player.source_player_id
    )

    def feature_available(source_id: str) -> bool:
        return (
            source_id in by_source_id
            and by_source_id[source_id].availability
            is IntelligenceAvailability.AVAILABLE
        )

    lineup_feature_count = sum(feature_available(source_id) for source_id in lineup_ids)
    bullpen_feature_count = sum(
        feature_available(source_id) for source_id in bullpen_ids
    )
    bench_feature_count = sum(feature_available(source_id) for source_id in bench_ids)
    starter_feature_available = (
        starter_id is not None and feature_available(starter_id)
    )

    if starter_id is not None and not starter_feature_available:
        warnings.append(
            BaseballIntelligenceWarningV1(
                code=BaseballIntelligenceWarningCode.STARTER_FEATURE_MISSING,
                message="GameState starter lacks accepted V3 player intelligence",
                source_game_id=source_game_id,
                team_id=team_state.team_id,
                source_player_id=starter_id,
            )
        )
    lineup_missing = len(lineup_ids) - lineup_feature_count
    if lineup_missing:
        warnings.append(
            BaseballIntelligenceWarningV1(
                code=BaseballIntelligenceWarningCode.LINEUP_FEATURE_INCOMPLETE,
                message="GameState lineup has incomplete V3 player intelligence coverage",
                source_game_id=source_game_id,
                team_id=team_state.team_id,
                missing_count=lineup_missing,
            )
        )
    bullpen_missing = len(bullpen_ids) - bullpen_feature_count
    if bullpen_missing:
        warnings.append(
            BaseballIntelligenceWarningV1(
                code=BaseballIntelligenceWarningCode.BULLPEN_FEATURE_INCOMPLETE,
                message="GameState bullpen has incomplete V3 player intelligence coverage",
                source_game_id=source_game_id,
                team_id=team_state.team_id,
                missing_count=bullpen_missing,
            )
        )
    bench_missing = len(bench_ids) - bench_feature_count
    if bench_missing:
        warnings.append(
            BaseballIntelligenceWarningV1(
                code=BaseballIntelligenceWarningCode.BENCH_FEATURE_INCOMPLETE,
                message="GameState bench has incomplete V3 player intelligence coverage",
                source_game_id=source_game_id,
                team_id=team_state.team_id,
                missing_count=bench_missing,
            )
        )

    coverage = TeamIntelligenceCoverageV1(
        gameday_player_count=len(players),
        resolved_player_count=sum(
            player.canonical_player_id is not None for player in players
        ),
        player_feature_count=sum(
            player.availability is IntelligenceAvailability.AVAILABLE
            for player in players
        ),
        lineup_player_count=len(lineup_ids),
        lineup_feature_count=lineup_feature_count,
        bullpen_player_count=len(bullpen_ids),
        bullpen_feature_count=bullpen_feature_count,
        bench_player_count=len(bench_ids),
        bench_feature_count=bench_feature_count,
        starter_feature_available=starter_feature_available,
    )
    return TeamBaseballIntelligenceV1(
        team_id=team_state.team_id,
        source_team_id=team_state.source_team_id,
        starter_source_player_id=starter_id,
        lineup_source_player_ids=lineup_ids,
        bullpen_source_player_ids=bullpen_ids,
        bench_source_player_ids=bench_ids,
        batter_source_player_ids=batter_ids,
        pitcher_source_player_ids=pitcher_ids,
        players=players,
        coverage=coverage,
    )


def _validate_upstream_game_identity(
    slate_game: DailySlateGameV1,
    state_game: GameStateGameV1,
) -> None:
    comparisons: tuple[tuple[str, object, object], ...] = (
        ("edge_event_id", slate_game.edge_event_id, state_game.edge_event_id),
        (
            "daily_mlb_game_id",
            slate_game.daily_mlb_game_id,
            state_game.daily_mlb_game_id,
        ),
        ("source_game_id", slate_game.source_game_id, state_game.source_game_id),
        ("away_team_id", slate_game.away_team_id, state_game.away_team_id),
        ("home_team_id", slate_game.home_team_id, state_game.home_team_id),
    )
    mismatches = [
        name
        for name, slate_value, state_value in comparisons
        if slate_value != state_value
    ]
    if mismatches:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate/GameState game identity mismatch: " + ", ".join(mismatches)
        )
    if slate_game.source_away_team_id != state_game.away.source_team_id:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate/GameState away source team identity mismatch"
        )
    if slate_game.source_home_team_id != state_game.home.source_team_id:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate/GameState home source team identity mismatch"
        )


def _relevant_canonical_player_ids(game_state: GameStateV1) -> set[str]:
    result: set[str] = set()
    for game in game_state.games:
        for team in (game.away, game.home):
            registry = _team_player_registry(team)
            for player, _ in registry.values():
                if player.canonical_player_id is not None:
                    result.add(player.canonical_player_id)
    return result


def _selected_features(
    games: Iterable[BaseballIntelligenceGameV1],
) -> tuple[BaseballFeatureSnapshotV1, ...]:
    selected: dict[tuple[str, str], BaseballFeatureSnapshotV1] = {}
    for game in games:
        for team in (game.away, game.home):
            for player in team.players:
                if player.feature is None:
                    continue
                key = (player.feature.entity_id, player.feature.feature_checksum)
                selected[key] = player.feature
    return tuple(
        selected[key]
        for key in sorted(selected, key=lambda item: (item[0], item[1]))
    )


def assemble_baseball_intelligence(
    *,
    slate: DailySlateV1,
    game_state: GameStateV1,
    feature_snapshots: Iterable[BaseballFeatureSnapshotV1] = (),
    observed_at: datetime | None = None,
) -> BaseballIntelligenceAssemblyResultV1:
    if slate.requested_date != game_state.requested_date:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate and GameState requested dates disagree"
        )
    if game_state.upstream_daily_slate_checksum != slate.checksum:
        raise BaseballIntelligenceAssemblyError(
            "GameState does not reference the supplied DailySlate checksum"
        )
    if slate.as_of_time != game_state.as_of_time:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate and GameState as_of_time values disagree"
        )
    slate_ids = tuple(game.source_game_id for game in slate.games)
    state_ids = tuple(game.source_game_id for game in game_state.games)
    if slate_ids != state_ids:
        raise BaseballIntelligenceAssemblyError(
            "DailySlate and GameState game ordering/set must match exactly"
        )

    feature_inventory = tuple(feature_snapshots)
    player_features = _index_player_features(feature_inventory)
    relevant_player_ids = _relevant_canonical_player_ids(game_state)
    relevant_feature_times = [
        feature.created_at
        for feature in feature_inventory
        if feature.entity_kind == "player"
        and feature.entity_id in relevant_player_ids
        and feature.feature_as_of == slate.requested_date
    ]
    latest_input_time = max(
        [slate.observed_at, game_state.observed_at, *relevant_feature_times]
    )
    selected_observed_at = observed_at or latest_input_time
    if (
        selected_observed_at.tzinfo is None
        or selected_observed_at.utcoffset() is None
    ):
        raise BaseballIntelligenceAssemblyError(
            "assembly observed_at must be timezone-aware"
        )
    selected_observed_at = selected_observed_at.astimezone(timezone.utc)
    if selected_observed_at < latest_input_time:
        raise BaseballIntelligenceAssemblyError(
            "assembly observed_at cannot precede retained input evidence"
        )

    warnings: list[BaseballIntelligenceWarningV1] = []
    games: list[BaseballIntelligenceGameV1] = []
    for slate_game, state_game in zip(
        slate.games, game_state.games, strict=True
    ):
        _validate_upstream_game_identity(slate_game, state_game)
        away = _team_intelligence(
            team_state=state_game.away,
            player_features=player_features,
            requested_date=slate.requested_date,
            assembly_observed_at=selected_observed_at,
            source_game_id=state_game.source_game_id,
            warnings=warnings,
        )
        home = _team_intelligence(
            team_state=state_game.home,
            player_features=player_features,
            requested_date=slate.requested_date,
            assembly_observed_at=selected_observed_at,
            source_game_id=state_game.source_game_id,
            warnings=warnings,
        )
        games.append(
            BaseballIntelligenceGameV1(
                edge_event_id=slate_game.edge_event_id,
                daily_mlb_game_id=slate_game.daily_mlb_game_id,
                source_game_id=slate_game.source_game_id,
                away_team_id=slate_game.away_team_id,
                home_team_id=slate_game.home_team_id,
                venue_id=slate_game.venue_id,
                game_status=state_game.game_status,
                away=away,
                home=home,
                upstream_daily_slate_game_checksum=slate_game.checksum,
                upstream_game_state_game_checksum=state_game.checksum,
            )
        )

    selected_features = _selected_features(games)
    try:
        assembly = BaseballIntelligenceAssemblyV1(
            requested_date=slate.requested_date,
            as_of_time=slate.as_of_time,
            observed_at=selected_observed_at,
            upstream_daily_slate_checksum=slate.checksum,
            upstream_game_state_checksum=game_state.checksum,
            source_stats_run_ids=tuple(
                feature.stats_run_id for feature in selected_features
            ),
            source_feature_checksums=tuple(
                feature.feature_checksum for feature in selected_features
            ),
            games=tuple(games),
        )
    except BaseballIntelligenceContractError as exc:
        raise BaseballIntelligenceAssemblyError(
            "assembled baseball intelligence violates V1 contract"
        ) from exc
    return BaseballIntelligenceAssemblyResultV1(
        assembly=assembly,
        warnings=tuple(warnings),
    )
