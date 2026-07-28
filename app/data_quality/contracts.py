from __future__ import annotations

from collections.abc import Iterable
from dataclasses import InitVar, dataclass
from datetime import datetime, timezone
from enum import StrEnum

from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.identifiers import parse_requested_date
from app.redaction import redact_value
from app.team_aliases import CANONICAL_TEAM_KEYS

DATA_QUALITY_CONTRACT_VERSION = "DSE_DATA_QUALITY_V1"
DATA_QUALITY_POLICY_VERSION = "DSE_DATA_QUALITY_POLICY_V1"
DATA_QUALITY_SPORT = "MLB"
DATA_QUALITY_LEAGUE = "MLB"


class DataQualityContractError(ValueError):
    """Raised when canonical Data Quality V1 evidence violates its contract."""


class DataQualityDisposition(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    INSUFFICIENT = "insufficient"


class QualityIssueSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class QualityDomain(StrEnum):
    SCHEDULE = "schedule"
    GAME_STATE = "game_state"
    STARTER = "starter"
    LINEUP = "lineup"
    BASEBALL_INTELLIGENCE = "baseball_intelligence"
    ODDS = "odds"
    WEATHER = "weather"


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise DataQualityContractError(f"{name} must be a non-empty trimmed string")
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
        raise DataQualityContractError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _optional_aware_utc(value: object, name: str) -> datetime | None:
    return None if value is None else _aware_utc(value, name)


def _sha256(value: object, name: str) -> str:
    text = _required_text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise DataQualityContractError(
            f"{name} must be 64 lowercase hexadecimal characters"
        )
    return text


def _expected_disposition(
    issues: tuple["QualityIssueV1", ...],
) -> DataQualityDisposition:
    severities = {issue.severity for issue in issues}
    if QualityIssueSeverity.CRITICAL in severities:
        return DataQualityDisposition.INSUFFICIENT
    if QualityIssueSeverity.WARNING in severities:
        return DataQualityDisposition.DEGRADED
    return DataQualityDisposition.READY


@dataclass(frozen=True, slots=True)
class QualityIssueV1:
    code: str
    domain: QualityDomain
    severity: QualityIssueSeverity
    message: str
    team_id: str | None = None
    provider: str | None = None
    expected: str | None = None
    actual: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "code", _required_text(self.code, "issue code"))
        object.__setattr__(self, "message", _required_text(self.message, "issue message"))
        if self.team_id is not None and self.team_id not in CANONICAL_TEAM_KEYS:
            raise DataQualityContractError("issue team_id must be canonical MLB")
        for name in ("provider", "expected", "actual"):
            object.__setattr__(
                self,
                name,
                _optional_text(getattr(self, name), f"issue {name}"),
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "actual": self.actual,
            "code": self.code,
            "domain": self.domain.value,
            "expected": self.expected,
            "message": self.message,
            "provider": self.provider,
            "severity": self.severity.value,
            "team_id": self.team_id,
        }


@dataclass(frozen=True, slots=True)
class DataQualityGameV1:
    edge_event_id: str
    daily_mlb_game_id: str
    source_game_id: str
    away_team_id: str
    home_team_id: str
    scheduled_start_time: datetime | None
    upstream_daily_slate_game_checksum: str
    upstream_game_state_game_checksum: str
    upstream_baseball_intelligence_game_checksum: str
    upstream_odds_weather_game_checksum: str
    disposition: DataQualityDisposition
    issues: tuple[QualityIssueV1, ...]

    def __post_init__(self) -> None:
        for name in ("edge_event_id", "daily_mlb_game_id", "source_game_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name))
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
        ):
            raise DataQualityContractError("game teams must be canonical MLB")
        if self.away_team_id == self.home_team_id:
            raise DataQualityContractError("home and away teams must differ")
        object.__setattr__(
            self,
            "scheduled_start_time",
            _optional_aware_utc(self.scheduled_start_time, "scheduled_start_time"),
        )
        for name in (
            "upstream_daily_slate_game_checksum",
            "upstream_game_state_game_checksum",
            "upstream_baseball_intelligence_game_checksum",
            "upstream_odds_weather_game_checksum",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        issues = tuple(
            sorted(
                self.issues,
                key=lambda issue: (
                    list(QualityIssueSeverity).index(issue.severity),
                    issue.domain.value,
                    issue.code,
                    issue.team_id or "",
                    issue.provider or "",
                    issue.message,
                    issue.expected or "",
                    issue.actual or "",
                ),
            )
        )
        if not all(isinstance(issue, QualityIssueV1) for issue in issues):
            raise DataQualityContractError("issues must contain QualityIssueV1 values")
        object.__setattr__(self, "issues", issues)
        expected_disposition = _expected_disposition(issues)
        if self.disposition is not expected_disposition:
            raise DataQualityContractError(
                "game disposition must be derived from issue severities"
            )

    @property
    def critical_issue_count(self) -> int:
        return sum(
            issue.severity is QualityIssueSeverity.CRITICAL for issue in self.issues
        )

    @property
    def warning_issue_count(self) -> int:
        return sum(
            issue.severity is QualityIssueSeverity.WARNING for issue in self.issues
        )

    @property
    def info_issue_count(self) -> int:
        return sum(issue.severity is QualityIssueSeverity.INFO for issue in self.issues)

    def _content_dict(self) -> dict[str, object]:
        return {
            "away_team_id": self.away_team_id,
            "critical_issue_count": self.critical_issue_count,
            "daily_mlb_game_id": self.daily_mlb_game_id,
            "disposition": self.disposition.value,
            "edge_event_id": self.edge_event_id,
            "home_team_id": self.home_team_id,
            "info_issue_count": self.info_issue_count,
            "issues": [issue.as_dict() for issue in self.issues],
            "scheduled_start_time": (
                None
                if self.scheduled_start_time is None
                else self.scheduled_start_time.isoformat()
            ),
            "source_game_id": self.source_game_id,
            "upstream_baseball_intelligence_game_checksum": self.upstream_baseball_intelligence_game_checksum,
            "upstream_daily_slate_game_checksum": self.upstream_daily_slate_game_checksum,
            "upstream_game_state_game_checksum": self.upstream_game_state_game_checksum,
            "upstream_odds_weather_game_checksum": self.upstream_odds_weather_game_checksum,
            "warning_issue_count": self.warning_issue_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}


@dataclass(frozen=True, slots=True)
class DataQualityV1:
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    upstream_daily_slate_checksum: str
    upstream_game_state_checksum: str
    upstream_baseball_intelligence_checksum: str
    upstream_odds_weather_checksum: str
    games: tuple[DataQualityGameV1, ...]
    secret_values: InitVar[Iterable[str]] = ()
    policy_version: str = DATA_QUALITY_POLICY_VERSION
    contract_version: str = DATA_QUALITY_CONTRACT_VERSION
    sport: str = DATA_QUALITY_SPORT
    league: str = DATA_QUALITY_LEAGUE

    def __post_init__(self, secret_values: Iterable[str]) -> None:
        parse_requested_date(self.requested_date)
        object.__setattr__(self, "as_of_time", _aware_utc(self.as_of_time, "as_of_time"))
        object.__setattr__(
            self, "observed_at", _aware_utc(self.observed_at, "observed_at")
        )
        for name in (
            "upstream_daily_slate_checksum",
            "upstream_game_state_checksum",
            "upstream_baseball_intelligence_checksum",
            "upstream_odds_weather_checksum",
        ):
            object.__setattr__(self, name, _sha256(getattr(self, name), name))
        if self.policy_version != DATA_QUALITY_POLICY_VERSION:
            raise DataQualityContractError("unsupported Data Quality policy_version")
        if self.contract_version != DATA_QUALITY_CONTRACT_VERSION:
            raise DataQualityContractError("unsupported Data Quality contract_version")
        if self.sport != "MLB" or self.league != "MLB":
            raise DataQualityContractError("sport and league must both be MLB")
        games = tuple(self.games)
        if not all(isinstance(game, DataQualityGameV1) for game in games):
            raise DataQualityContractError("games must contain DataQualityGameV1 values")
        if len({game.source_game_id for game in games}) != len(games):
            raise DataQualityContractError("DataQualityV1 contains duplicate games")
        object.__setattr__(self, "games", games)
        payload = self._content_dict()
        configured = tuple(str(value) for value in secret_values if str(value))
        if redact_value(payload, configured) != payload:
            raise DataQualityContractError(
                "DataQualityV1 contains credential-bearing material"
            )

    @property
    def ready_game_count(self) -> int:
        return sum(game.disposition is DataQualityDisposition.READY for game in self.games)

    @property
    def degraded_game_count(self) -> int:
        return sum(
            game.disposition is DataQualityDisposition.DEGRADED for game in self.games
        )

    @property
    def insufficient_game_count(self) -> int:
        return sum(
            game.disposition is DataQualityDisposition.INSUFFICIENT for game in self.games
        )

    def _content_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "contract_version": self.contract_version,
            "degraded_game_count": self.degraded_game_count,
            "games": [game.as_dict() for game in self.games],
            "insufficient_game_count": self.insufficient_game_count,
            "league": self.league,
            "observed_at": self.observed_at.isoformat(),
            "policy_version": self.policy_version,
            "ready_game_count": self.ready_game_count,
            "requested_date": self.requested_date,
            "sport": self.sport,
            "upstream_baseball_intelligence_checksum": self.upstream_baseball_intelligence_checksum,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
            "upstream_odds_weather_checksum": self.upstream_odds_weather_checksum,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self._content_dict())

    def as_dict(self) -> dict[str, object]:
        return {**self._content_dict(), "checksum": self.checksum}

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())
