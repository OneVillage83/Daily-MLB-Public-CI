from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from app.daily_slate.contracts import (
    DailySlateGameStatus,
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.redaction import redact_value
from app.stats.features import FEATURE_VERSION_V3
from app.team_aliases import CANONICAL_TEAM_KEYS

BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION = (
    "DSE_BASEBALL_INTELLIGENCE_ASSEMBLY_V1"
)
BASEBALL_INTELLIGENCE_SPORT = "MLB"
BASEBALL_INTELLIGENCE_LEAGUE = "MLB"


class BaseballIntelligenceContractError(ValueError):
    """Raised when a Baseball Intelligence Assembly V1 contract is invalid."""


class BaseballIntelligenceRole(StrEnum):
    STARTER = "starter"
    LINEUP = "lineup"
    BULLPEN = "bullpen"
    BENCH = "bench"
    BATTER = "batter"
    PITCHER = "pitcher"


class IntelligenceAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BaseballIntelligenceContractError(
            f"{name} must be a non-empty trimmed string"
        )
    return value


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name)


def _aware_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BaseballIntelligenceContractError(
            f"{name} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise BaseballIntelligenceContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


def _positive_decimal_id(value: object, name: str) -> str:
    if isinstance(value, bool):
        raise BaseballIntelligenceContractError(
            f"{name} must be a positive decimal identifier"
        )
    if isinstance(value, int):
        numeric = value
    elif isinstance(value, str):
        text = _required_text(value, name)
        if not text.isascii() or not text.isdecimal():
            raise BaseballIntelligenceContractError(
                f"{name} must be a positive decimal identifier"
            )
        numeric = int(text, 10)
    else:
        raise BaseballIntelligenceContractError(
            f"{name} must be an integer or decimal string"
        )
    if numeric <= 0:
        raise BaseballIntelligenceContractError(
            f"{name} must be a positive decimal identifier"
        )
    return str(numeric)


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise BaseballIntelligenceContractError(
        "feature payload must contain only JSON-compatible values"
    )


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _feature_v3_checksum(features: Mapping[str, Any]) -> str:
    payload = {
        str(key): _thaw_json(value)
        for key, value in features.items()
        if key != "feature_checksum"
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BaseballFeatureSnapshotV1:
    """Retained canonical Baseball Intelligence feature evidence.

    This is an assembly input/reference contract. It does not recalculate features.
    """

    feature_snapshot_id: str
    stats_run_id: str
    feature_version: str
    entity_kind: str
    entity_id: str
    feature_as_of: str
    completeness_state: str
    input_checksum: str
    feature_checksum: str
    features: Mapping[str, Any]
    created_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "feature_snapshot_id",
            _required_text(self.feature_snapshot_id, "feature_snapshot_id"),
        )
        object.__setattr__(
            self,
            "stats_run_id",
            _required_text(self.stats_run_id, "stats_run_id"),
        )
        if self.feature_version != FEATURE_VERSION_V3:
            raise BaseballIntelligenceContractError(
                f"feature_version must be {FEATURE_VERSION_V3}"
            )
        if self.entity_kind not in {"player", "team"}:
            raise BaseballIntelligenceContractError(
                "feature entity_kind must be player or team"
            )
        object.__setattr__(self, "entity_id", _required_text(self.entity_id, "entity_id"))
        parse_requested_date(self.feature_as_of)
        object.__setattr__(
            self,
            "completeness_state",
            _required_text(self.completeness_state, "completeness_state"),
        )
        object.__setattr__(
            self,
            "input_checksum",
            _sha256(self.input_checksum, "input_checksum"),
        )
        object.__setattr__(
            self,
            "feature_checksum",
            _sha256(self.feature_checksum, "feature_checksum"),
        )
        if not isinstance(self.features, Mapping):
            raise BaseballIntelligenceContractError("features must be a mapping")
        frozen_features = _freeze_json(dict(self.features))
        assert isinstance(frozen_features, Mapping)
        object.__setattr__(self, "features", frozen_features)
        object.__setattr__(
            self,
            "created_at",
            _aware_utc(self.created_at, "feature created_at"),
        )
        contract = frozen_features.get("contract_version")
        if contract is not None and contract != self.feature_version:
            raise BaseballIntelligenceContractError(
                "feature payload contract_version disagrees with feature_version"
            )
        payload_checksum = frozen_features.get("feature_checksum")
        if payload_checksum is not None and payload_checksum != self.feature_checksum:
            raise BaseballIntelligenceContractError(
                "feature payload checksum disagrees with stored feature_checksum"
            )
        payload_as_of = frozen_features.get("feature_as_of")
        if payload_as_of is not None and str(payload_as_of) != self.feature_as_of:
            raise BaseballIntelligenceContractError(
                "feature payload feature_as_of disagrees with snapshot"
            )
        expected_entity_key = "player_id" if self.entity_kind == "player" else "team_key"
        payload_entity = frozen_features.get(expected_entity_key)
        if payload_entity is not None and str(payload_entity) != self.entity_id:
            raise BaseballIntelligenceContractError(
                f"feature payload {expected_entity_key} disagrees with snapshot entity"
            )
        if _feature_v3_checksum(frozen_features) != self.feature_checksum:
            raise BaseballIntelligenceContractError(
                "feature payload does not reproduce frozen V3 feature_checksum"
            )
        serialized = self.as_dict()
        if redact_value(serialized) != serialized:
            raise BaseballIntelligenceContractError(
                "BaseballFeatureSnapshotV1 contains credential-bearing material"
            )

    @property
    def knowledge_cutoff(self) -> datetime | None:
        raw = self.features.get("knowledge_cutoff")
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise BaseballIntelligenceContractError(
                "feature knowledge_cutoff must be an ISO-8601 string when present"
            )
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BaseballIntelligenceContractError(
                "feature knowledge_cutoff is malformed"
            ) from exc
        return _aware_utc(parsed, "feature knowledge_cutoff")

    def as_dict(self) -> dict[str, object]:
        return {
            "completeness_state": self.completeness_state,
            "created_at": self.created_at.isoformat(),
            "entity_id": self.entity_id,
            "entity_kind": self.entity_kind,
            "feature_as_of": self.feature_as_of,
            "feature_checksum": self.feature_checksum,
            "feature_snapshot_id": self.feature_snapshot_id,
            "feature_version": self.feature_version,
            "features": _thaw_json(self.features),
            "input_checksum": self.input_checksum,
            "stats_run_id": self.stats_run_id,
        }


@dataclass(frozen=True, slots=True)
class PlayerIntelligenceV1:
    source_player_id: str
    full_name: str
    player_identity_id: str | None
    canonical_player_id: str | None
    roles: tuple[BaseballIntelligenceRole, ...]
    game_state_player_checksum: str
    availability: IntelligenceAvailability
    feature: BaseballFeatureSnapshotV1 | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_player_id",
            _positive_decimal_id(self.source_player_id, "source_player_id"),
        )
        object.__setattr__(self, "full_name", _required_text(self.full_name, "full_name"))
        if (self.player_identity_id is None) != (self.canonical_player_id is None):
            raise BaseballIntelligenceContractError(
                "resolved player identity requires both identity IDs"
            )
        if self.player_identity_id is not None:
            object.__setattr__(
                self,
                "player_identity_id",
                _required_text(self.player_identity_id, "player_identity_id"),
            )
            object.__setattr__(
                self,
                "canonical_player_id",
                _required_text(self.canonical_player_id, "canonical_player_id"),
            )
        roles = tuple(
            sorted(
                set(self.roles),
                key=lambda role: list(BaseballIntelligenceRole).index(role),
            )
        )
        if not roles:
            raise BaseballIntelligenceContractError(
                "player intelligence requires at least one GameState role"
            )
        object.__setattr__(self, "roles", roles)
        object.__setattr__(
            self,
            "game_state_player_checksum",
            _sha256(self.game_state_player_checksum, "game_state_player_checksum"),
        )
        if self.availability is IntelligenceAvailability.AVAILABLE:
            if self.feature is None or self.canonical_player_id is None:
                raise BaseballIntelligenceContractError(
                    "available player intelligence requires resolved identity and feature"
                )
            if self.feature.entity_kind != "player":
                raise BaseballIntelligenceContractError(
                    "player intelligence feature must be a player snapshot"
                )
            if self.feature.entity_id != self.canonical_player_id:
                raise BaseballIntelligenceContractError(
                    "player feature entity does not match canonical_player_id"
                )
        elif self.feature is not None:
            raise BaseballIntelligenceContractError(
                "unavailable player intelligence must not contain a feature"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "availability": self.availability.value,
            "canonical_player_id": self.canonical_player_id,
            "feature": None if self.feature is None else self.feature.as_dict(),
            "full_name": self.full_name,
            "game_state_player_checksum": self.game_state_player_checksum,
            "player_identity_id": self.player_identity_id,
            "roles": [role.value for role in self.roles],
            "source_player_id": self.source_player_id,
        }


@dataclass(frozen=True, slots=True)
class TeamIntelligenceCoverageV1:
    gameday_player_count: int
    resolved_player_count: int
    player_feature_count: int
    lineup_player_count: int
    lineup_feature_count: int
    bullpen_player_count: int
    bullpen_feature_count: int
    bench_player_count: int
    bench_feature_count: int
    starter_feature_available: bool

    def __post_init__(self) -> None:
        integer_fields = (
            "gameday_player_count",
            "resolved_player_count",
            "player_feature_count",
            "lineup_player_count",
            "lineup_feature_count",
            "bullpen_player_count",
            "bullpen_feature_count",
            "bench_player_count",
            "bench_feature_count",
        )
        for name in integer_fields:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise BaseballIntelligenceContractError(
                    f"coverage {name} must be a non-negative integer"
                )
        if self.resolved_player_count > self.gameday_player_count:
            raise BaseballIntelligenceContractError(
                "resolved player coverage cannot exceed gameday players"
            )
        if self.player_feature_count > self.resolved_player_count:
            raise BaseballIntelligenceContractError(
                "player feature coverage cannot exceed resolved players"
            )
        if self.lineup_feature_count > self.lineup_player_count:
            raise BaseballIntelligenceContractError(
                "lineup feature coverage cannot exceed lineup players"
            )
        if self.bullpen_feature_count > self.bullpen_player_count:
            raise BaseballIntelligenceContractError(
                "bullpen feature coverage cannot exceed bullpen players"
            )
        if self.bench_feature_count > self.bench_player_count:
            raise BaseballIntelligenceContractError(
                "bench feature coverage cannot exceed bench players"
            )
        if not isinstance(self.starter_feature_available, bool):
            raise BaseballIntelligenceContractError(
                "starter feature availability flag must be boolean"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "bench_feature_count": self.bench_feature_count,
            "bench_player_count": self.bench_player_count,
            "bullpen_feature_count": self.bullpen_feature_count,
            "bullpen_player_count": self.bullpen_player_count,
            "gameday_player_count": self.gameday_player_count,
            "lineup_feature_count": self.lineup_feature_count,
            "lineup_player_count": self.lineup_player_count,
            "player_feature_count": self.player_feature_count,
            "resolved_player_count": self.resolved_player_count,
            "starter_feature_available": self.starter_feature_available,
        }


@dataclass(frozen=True, slots=True)
class TeamBaseballIntelligenceV1:
    team_id: str
    source_team_id: str
    starter_source_player_id: str | None
    lineup_source_player_ids: tuple[str, ...]
    bullpen_source_player_ids: tuple[str, ...]
    bench_source_player_ids: tuple[str, ...]
    batter_source_player_ids: tuple[str, ...]
    pitcher_source_player_ids: tuple[str, ...]
    players: tuple[PlayerIntelligenceV1, ...]
    coverage: TeamIntelligenceCoverageV1

    def __post_init__(self) -> None:
        if self.team_id not in CANONICAL_TEAM_KEYS:
            raise BaseballIntelligenceContractError("team_id is not canonical MLB")
        object.__setattr__(
            self,
            "source_team_id",
            _positive_decimal_id(self.source_team_id, "source_team_id"),
        )
        if self.starter_source_player_id is not None:
            object.__setattr__(
                self,
                "starter_source_player_id",
                _positive_decimal_id(
                    self.starter_source_player_id, "starter_source_player_id"
                ),
            )
        for name in (
            "lineup_source_player_ids",
            "bullpen_source_player_ids",
            "bench_source_player_ids",
            "batter_source_player_ids",
            "pitcher_source_player_ids",
        ):
            values = tuple(
                _positive_decimal_id(value, f"{name} value")
                for value in getattr(self, name)
            )
            if len(values) != len(set(values)):
                raise BaseballIntelligenceContractError(f"{name} must be unique")
            object.__setattr__(self, name, values)
        players = tuple(sorted(self.players, key=lambda player: int(player.source_player_id)))
        if len({player.source_player_id for player in players}) != len(players):
            raise BaseballIntelligenceContractError(
                "team player intelligence source IDs must be unique"
            )
        object.__setattr__(self, "players", players)
        player_ids = {player.source_player_id for player in players}
        indexed_ids = set(self.lineup_source_player_ids)
        indexed_ids.update(self.bullpen_source_player_ids)
        indexed_ids.update(self.bench_source_player_ids)
        indexed_ids.update(self.batter_source_player_ids)
        indexed_ids.update(self.pitcher_source_player_ids)
        if self.starter_source_player_id is not None:
            indexed_ids.add(self.starter_source_player_id)
        if not indexed_ids.issubset(player_ids):
            raise BaseballIntelligenceContractError(
                "team structural player indexes must resolve to player intelligence records"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "batter_source_player_ids": list(self.batter_source_player_ids),
            "bench_source_player_ids": list(self.bench_source_player_ids),
            "bullpen_source_player_ids": list(self.bullpen_source_player_ids),
            "coverage": self.coverage.as_dict(),
            "lineup_source_player_ids": list(self.lineup_source_player_ids),
            "pitcher_source_player_ids": list(self.pitcher_source_player_ids),
            "players": [player.as_dict() for player in self.players],
            "source_team_id": self.source_team_id,
            "starter_source_player_id": self.starter_source_player_id,
            "team_id": self.team_id,
        }


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    venue_id: str | None
    game_status: DailySlateGameStatus
    away: TeamBaseballIntelligenceV1
    home: TeamBaseballIntelligenceV1
    upstream_daily_slate_game_checksum: str
    upstream_game_state_game_checksum: str

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise BaseballIntelligenceContractError("edge_event_id identity mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise BaseballIntelligenceContractError("daily_mlb_game_id identity mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
        ):
            raise BaseballIntelligenceContractError(
                "game teams must be canonical MLB IDs"
            )
        if self.away_team_id == self.home_team_id:
            raise BaseballIntelligenceContractError("home and away teams must differ")
        if (
            self.away.team_id != self.away_team_id
            or self.home.team_id != self.home_team_id
        ):
            raise BaseballIntelligenceContractError(
                "nested team intelligence identities must match game teams"
            )
        object.__setattr__(
            self,
            "venue_id",
            _optional_text(self.venue_id, "venue_id"),
        )
        object.__setattr__(
            self,
            "upstream_daily_slate_game_checksum",
            _sha256(
                self.upstream_daily_slate_game_checksum,
                "upstream_daily_slate_game_checksum",
            ),
        )
        object.__setattr__(
            self,
            "upstream_game_state_game_checksum",
            _sha256(
                self.upstream_game_state_game_checksum,
                "upstream_game_state_game_checksum",
            ),
        )

    def _content_dict(self) -> dict[str, object]:
        return {
            "away": self.away.as_dict(),
            "away_team_id": self.away_team_id,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "edge_event_id": self.edge_event_id,
            "game_status": self.game_status.value,
            "home": self.home.as_dict(),
            "home_team_id": self.home_team_id,
            "source_game_id": self.source_game_id,
            "upstream_daily_slate_game_checksum": self.upstream_daily_slate_game_checksum,
            "upstream_game_state_game_checksum": self.upstream_game_state_game_checksum,
            "venue_id": self.venue_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceAssemblyV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_daily_slate_checksum: str
    upstream_game_state_checksum: str
    source_stats_run_ids: tuple[str, ...]
    source_feature_checksums: tuple[str, ...]
    games: tuple[BaseballIntelligenceGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    feature_version: str = FEATURE_VERSION_V3
    contract_version: str = BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION
    sport: str = BASEBALL_INTELLIGENCE_SPORT
    league: str = BASEBALL_INTELLIGENCE_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "upstream_daily_slate_checksum",
            _sha256(self.upstream_daily_slate_checksum, "upstream_daily_slate_checksum"),
        )
        object.__setattr__(
            self,
            "upstream_game_state_checksum",
            _sha256(self.upstream_game_state_checksum, "upstream_game_state_checksum"),
        )
        stats_run_ids = tuple(
            sorted(
                {
                    _required_text(value, "source_stats_run_id")
                    for value in self.source_stats_run_ids
                }
            )
        )
        feature_checksums = tuple(
            sorted(
                {
                    _sha256(value, "source_feature_checksum")
                    for value in self.source_feature_checksums
                }
            )
        )
        object.__setattr__(self, "source_stats_run_ids", stats_run_ids)
        object.__setattr__(self, "source_feature_checksums", feature_checksums)
        if self.feature_version != FEATURE_VERSION_V3:
            raise BaseballIntelligenceContractError(
                f"feature_version must be {FEATURE_VERSION_V3}"
            )
        if self.contract_version != BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION:
            raise BaseballIntelligenceContractError(
                "invalid Baseball Intelligence Assembly contract_version"
            )
        if self.sport != "MLB" or self.league != "MLB":
            raise BaseballIntelligenceContractError("sport and league must both be MLB")
        games = tuple(self.games)
        if not all(isinstance(game, BaseballIntelligenceGameV1) for game in games):
            raise BaseballIntelligenceContractError(
                "games must contain BaseballIntelligenceGameV1 values"
            )
        object.__setattr__(self, "games", games)
        identities = [(game.edge_event_id, game.daily_mlb_game_id) for game in games]
        if len(identities) != len(set(identities)):
            raise BaseballIntelligenceContractError(
                "assembly contains duplicate canonical game identities"
            )
        semantic_payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(semantic_payload, configured) != semantic_payload:
            raise BaseballIntelligenceContractError(
                "BaseballIntelligenceAssemblyV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "feature_version": self.feature_version,
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "requested_date": self.requested_date,
            "source_feature_checksums": list(self.source_feature_checksums),
            "source_stats_run_ids": list(self.source_stats_run_ids),
            "sport": self.sport,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, Any]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
