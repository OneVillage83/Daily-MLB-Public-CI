from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone

from app.data_quality.contracts import DataQualityDisposition
from app.daily_slate.contracts import (
    canonical_authoritative_game_id,
    canonical_json_bytes,
    canonical_sha256,
    daily_mlb_game_id,
    edge_event_id,
)
from app.identifiers import parse_requested_date
from app.model_feature_set.schema import (
    MODEL_FEATURE_NAMES_V1,
    MODEL_FEATURE_SCHEMA_CHECKSUM,
    MODEL_FEATURE_SCHEMA_VERSION,
    MODEL_FEATURE_SET_CONTRACT_VERSION,
    MODEL_FEATURE_SET_LEAGUE,
    MODEL_FEATURE_SET_SPORT,
)
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS


class ModelFeatureSetContractError(ValueError):
    """Raised when ModelFeatureSet V1 violates its canonical contract."""


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ModelFeatureSetContractError(f"{name} must be a non-empty trimmed string")
    return value


def _aware_utc(value: object, name: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ModelFeatureSetContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise ModelFeatureSetContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


@dataclass(frozen=True, slots=True)
class ModelFeatureGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    upstream_matchup_packet_game_checksum: str
    quality_disposition: DataQualityDisposition
    quality_issue_codes: tuple[str, ...]
    market_reference_checksum: str | None
    feature_values: tuple[float | None, ...]
    schema_version: str = MODEL_FEATURE_SCHEMA_VERSION
    schema_checksum: str = MODEL_FEATURE_SCHEMA_CHECKSUM

    def __post_init__(self) -> None:
        source_game_id = canonical_authoritative_game_id(self.source_game_id)
        object.__setattr__(self, "source_game_id", source_game_id)
        if self.edge_event_id != edge_event_id(source_game_id):
            raise ModelFeatureSetContractError("edge_event_id identity mismatch")
        if self.daily_mlb_game_id != daily_mlb_game_id(source_game_id):
            raise ModelFeatureSetContractError("daily_mlb_game_id identity mismatch")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
        ):
            raise ModelFeatureSetContractError("game teams must be canonical MLB IDs")
        if self.away_team_id == self.home_team_id:
            raise ModelFeatureSetContractError("home and away teams must differ")
        object.__setattr__(
            self,
            "upstream_matchup_packet_game_checksum",
            _sha256(
                self.upstream_matchup_packet_game_checksum,
                "upstream_matchup_packet_game_checksum",
            ),
        )
        if self.market_reference_checksum is not None:
            object.__setattr__(
                self,
                "market_reference_checksum",
                _sha256(self.market_reference_checksum, "market_reference_checksum"),
            )
        if self.schema_version != MODEL_FEATURE_SCHEMA_VERSION:
            raise ModelFeatureSetContractError("unsupported model feature schema version")
        if self.schema_checksum != MODEL_FEATURE_SCHEMA_CHECKSUM:
            raise ModelFeatureSetContractError("model feature schema checksum mismatch")
        codes = tuple(
            sorted(
                {
                    _required_text(value, "quality issue code")
                    for value in self.quality_issue_codes
                }
            )
        )
        object.__setattr__(self, "quality_issue_codes", codes)
        values = tuple(self.feature_values)
        if len(values) != len(MODEL_FEATURE_NAMES_V1):
            raise ModelFeatureSetContractError(
                "feature_values length must exactly match frozen feature schema"
            )
        normalized: list[float | None] = []
        for value in values:
            if value is None:
                normalized.append(None)
                continue
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ModelFeatureSetContractError(
                    "feature_values must contain only finite numbers or null"
                )
            numeric = float(value)
            if not math.isfinite(numeric):
                raise ModelFeatureSetContractError("feature_values must be finite")
            normalized.append(numeric)
        object.__setattr__(self, "feature_values", tuple(normalized))

    @property
    def missing_feature_names(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in zip(
                MODEL_FEATURE_NAMES_V1,
                self.feature_values,
                strict=True,
            )
            if value is None
        )

    @property
    def available_feature_count(self) -> int:
        return len(self.feature_values) - len(self.missing_feature_names)

    def feature_map(self) -> dict[str, float | None]:
        return dict(
            zip(MODEL_FEATURE_NAMES_V1, self.feature_values, strict=True)
        )

    def _content_dict(self) -> dict[str, object]:
        return {
            "available_feature_count": self.available_feature_count,
            "away_team_id": self.away_team_id,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "edge_event_id": self.edge_event_id,
            "feature_values": list(self.feature_values),
            "home_team_id": self.home_team_id,
            "market_reference_checksum": self.market_reference_checksum,
            "missing_feature_names": list(self.missing_feature_names),
            "quality_disposition": self.quality_disposition.value,
            "quality_issue_codes": list(self.quality_issue_codes),
            "schema_checksum": self.schema_checksum,
            "schema_version": self.schema_version,
            "source_game_id": self.source_game_id,
            "upstream_matchup_packet_game_checksum": self.upstream_matchup_packet_game_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class ModelFeatureSetV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_matchup_packet_checksum: str
    games: tuple[ModelFeatureGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    schema_version: str = MODEL_FEATURE_SCHEMA_VERSION
    schema_checksum: str = MODEL_FEATURE_SCHEMA_CHECKSUM
    contract_version: str = MODEL_FEATURE_SET_CONTRACT_VERSION
    sport: str = MODEL_FEATURE_SET_SPORT
    league: str = MODEL_FEATURE_SET_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(
            self,
            "as_of_time",
            _aware_utc(self.as_of_time, "as_of_time"),
        )
        object.__setattr__(
            self,
            "observed_at",
            _aware_utc(self.observed_at, "observed_at"),
        )
        object.__setattr__(
            self,
            "upstream_matchup_packet_checksum",
            _sha256(
                self.upstream_matchup_packet_checksum,
                "upstream_matchup_packet_checksum",
            ),
        )
        if self.schema_version != MODEL_FEATURE_SCHEMA_VERSION:
            raise ModelFeatureSetContractError("unsupported model feature schema version")
        if self.schema_checksum != MODEL_FEATURE_SCHEMA_CHECKSUM:
            raise ModelFeatureSetContractError("model feature schema checksum mismatch")
        if self.contract_version != MODEL_FEATURE_SET_CONTRACT_VERSION:
            raise ModelFeatureSetContractError(
                "unsupported ModelFeatureSet contract version"
            )
        if self.sport != "MLB" or self.league != "MLB":
            raise ModelFeatureSetContractError("sport and league must both be MLB")
        games = tuple(self.games)
        if not all(isinstance(game, ModelFeatureGameV1) for game in games):
            raise ModelFeatureSetContractError(
                "games must contain ModelFeatureGameV1 values"
            )
        if len({game.source_game_id for game in games}) != len(games):
            raise ModelFeatureSetContractError(
                "ModelFeatureSetV1 contains duplicate games"
            )
        object.__setattr__(self, "games", games)
        payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if (
            redact_value(payload, configured, preserve_field_names=("key",))
            != payload
        ):
            raise ModelFeatureSetContractError(
                "ModelFeatureSetV1 contains credential-bearing material"
            )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "feature_names": list(MODEL_FEATURE_NAMES_V1),
            "games": [game.as_dict() for game in self.games],
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "requested_date": self.requested_date,
            "schema_checksum": self.schema_checksum,
            "schema_version": self.schema_version,
            "sport": self.sport,
            "upstream_matchup_packet_checksum": self.upstream_matchup_packet_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
