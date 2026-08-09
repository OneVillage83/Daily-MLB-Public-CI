from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Mapping

from app.daily_slate.contracts import canonical_authoritative_game_id, canonical_sha256
from app.database import Database
from app.model_feature_set.repository import (
    ModelFeatureSetRepository,
    PersistedModelFeatureSetV1,
)
from app.predictions.training_data import FinalGameScoreV1

HISTORICAL_MODEL_FEATURE_INVENTORY_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_MODEL_FEATURE_INVENTORY_V1"
)
HISTORICAL_MODEL_FEATURE_SELECTION_POLICY = (
    "latest_verified_snapshot_strictly_pregame_per_requested_date_v1"
)
HISTORICAL_FEATURE_DATE_EXCLUSION_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_FEATURE_DATE_EXCLUSION_V1"
)
FINAL_GAME_SCORE_SOURCE_LINEAGE_CONTRACT_VERSION = (
    "DSE_MLB_FINAL_GAME_SCORE_SOURCE_LINEAGE_V1"
)
FINAL_GAME_SCORE_SOURCE_EXCLUSION_CONTRACT_VERSION = (
    "DSE_MLB_FINAL_GAME_SCORE_SOURCE_EXCLUSION_V1"
)
FINAL_GAME_SCORE_SOURCE_INVENTORY_CONTRACT_VERSION = (
    "DSE_MLB_FINAL_GAME_SCORE_SOURCE_INVENTORY_V1"
)
STATCAST_FINAL_COMPLETION_CONTRACTS = frozenset(
    {
        "DSE_STATCAST_FINAL_GAME_V1",
        "DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1",
    }
)


class HistoricalTrainingSourceError(ValueError):
    """Raised when retained historical training-source evidence is inconsistent."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HistoricalTrainingSourceError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise HistoricalTrainingSourceError(f"{name} must be lowercase SHA-256")
    return text


def _calendar_date(value: date | str, name: str) -> date:
    if isinstance(value, datetime):
        raise HistoricalTrainingSourceError(f"{name} must be a calendar date")
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or len(value) != 10:
        raise HistoricalTrainingSourceError(f"{name} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HistoricalTrainingSourceError(f"{name} must be a valid date") from exc
    if parsed.isoformat() != value:
        raise HistoricalTrainingSourceError(f"{name} must be exact YYYY-MM-DD")
    return parsed


def _utc(value: datetime | str, name: str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HistoricalTrainingSourceError(f"{name} must be ISO-8601") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise HistoricalTrainingSourceError(f"{name} must be a datetime")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalTrainingSourceError(f"{name} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _score(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise HistoricalTrainingSourceError(f"{name} must be a nonnegative integer")
    try:
        numeric = int(value)
    except (TypeError, ValueError) as exc:
        raise HistoricalTrainingSourceError(f"{name} must be a nonnegative integer") from exc
    if str(value).strip() not in {str(numeric), f"{numeric}.0"} or numeric < 0:
        raise HistoricalTrainingSourceError(f"{name} must be a nonnegative integer")
    return numeric


@dataclass(frozen=True, slots=True)
class HistoricalFeatureDateExclusionV1:
    requested_date: date
    reason: str
    candidate_count: int
    contract_version: str = HISTORICAL_FEATURE_DATE_EXCLUSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.requested_date, date):
            raise HistoricalTrainingSourceError("requested_date must be a date")
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        if (
            isinstance(self.candidate_count, bool)
            or not isinstance(self.candidate_count, int)
            or self.candidate_count < 1
        ):
            raise HistoricalTrainingSourceError("candidate_count must be positive")
        if self.contract_version != HISTORICAL_FEATURE_DATE_EXCLUSION_CONTRACT_VERSION:
            raise HistoricalTrainingSourceError("unsupported feature-date exclusion contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "candidate_count": self.candidate_count,
            "contract_version": self.contract_version,
            "reason": self.reason,
            "requested_date": self.requested_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class HistoricalModelFeatureSetInventoryV1:
    start_date: date
    end_date: date
    snapshots: tuple[PersistedModelFeatureSetV1, ...]
    exclusions: tuple[HistoricalFeatureDateExclusionV1, ...] = ()
    selection_policy: str = HISTORICAL_MODEL_FEATURE_SELECTION_POLICY
    contract_version: str = HISTORICAL_MODEL_FEATURE_INVENTORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.start_date, date) or not isinstance(self.end_date, date):
            raise HistoricalTrainingSourceError("inventory bounds must be dates")
        if self.end_date < self.start_date:
            raise HistoricalTrainingSourceError("end_date must not precede start_date")
        snapshots = tuple(self.snapshots)
        requested_dates = tuple(
            date.fromisoformat(item.feature_set.requested_date) for item in snapshots
        )
        if len(requested_dates) != len(set(requested_dates)):
            raise HistoricalTrainingSourceError(
                "historical feature inventory must retain at most one snapshot per date"
            )
        if requested_dates != tuple(sorted(requested_dates)):
            raise HistoricalTrainingSourceError("historical feature inventory must be date ordered")
        if any(not self.start_date <= item <= self.end_date for item in requested_dates):
            raise HistoricalTrainingSourceError("historical feature snapshot is outside bounds")
        exclusions = tuple(self.exclusions)
        excluded_dates = tuple(item.requested_date for item in exclusions)
        if len(excluded_dates) != len(set(excluded_dates)):
            raise HistoricalTrainingSourceError("feature-date exclusions must be unique")
        if set(excluded_dates).intersection(requested_dates):
            raise HistoricalTrainingSourceError(
                "a requested date cannot be both selected and excluded"
            )
        object.__setattr__(self, "snapshots", snapshots)
        object.__setattr__(self, "exclusions", exclusions)
        if self.selection_policy != HISTORICAL_MODEL_FEATURE_SELECTION_POLICY:
            raise HistoricalTrainingSourceError("unsupported historical feature selection policy")
        if self.contract_version != HISTORICAL_MODEL_FEATURE_INVENTORY_CONTRACT_VERSION:
            raise HistoricalTrainingSourceError("unsupported historical feature inventory contract")

    @property
    def feature_sets(self) -> tuple[object, ...]:
        return tuple(item.feature_set for item in self.snapshots)

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "end_date": self.end_date.isoformat(),
            "exclusions": [item.as_dict() for item in self.exclusions],
            "selected_snapshot_count": len(self.snapshots),
            "selection_policy": self.selection_policy,
            "snapshots": [
                {
                    "feature_set_checksum": item.feature_set.checksum,
                    "observed_at": item.feature_set.observed_at.isoformat(),
                    "phase_attempt": item.phase_attempt,
                    "requested_date": item.feature_set.requested_date,
                    "run_id": item.run_id,
                    "sealed_at": item.sealed_at.isoformat(),
                    "snapshot_id": item.snapshot_id,
                }
                for item in self.snapshots
            ],
            "start_date": self.start_date.isoformat(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class _DailySlateGameEvidence:
    snapshot_id: str
    row_checksum: str
    official_date: date
    scheduled_start_time: datetime | None
    away_team_id: str
    home_team_id: str
    game_number: int | None


@dataclass(frozen=True, slots=True)
class _FinalStatusCandidate:
    game_identity_id: str
    status_observation_id: int
    revision_number: int
    home_runs: int
    away_runs: int
    retrieved_at: datetime
    source_checksum: str
    normalized_checksum: str
    raw_payload_checksum: str
    completion_contract: str


@dataclass(frozen=True, slots=True)
class FinalGameScoreSourceLineageV1:
    source_game_id: str
    run_id: str
    model_feature_set_snapshot_id: str
    model_feature_set_checksum: str
    daily_slate_snapshot_id: str
    daily_slate_game_checksum: str
    result_provider: str
    result_game_identity_id: str
    result_status_observation_id: int
    result_revision_number: int
    result_source_checksum: str
    result_normalized_checksum: str
    raw_payload_checksum: str
    completion_contract: str
    final_observed_at: datetime
    contract_version: str = FINAL_GAME_SCORE_SOURCE_LINEAGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_game_id",
            canonical_authoritative_game_id(self.source_game_id),
        )
        for name in (
            "run_id",
            "model_feature_set_snapshot_id",
            "daily_slate_snapshot_id",
            "result_provider",
            "result_game_identity_id",
            "completion_contract",
        ):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        for name in (
            "model_feature_set_checksum",
            "daily_slate_game_checksum",
            "result_source_checksum",
            "result_normalized_checksum",
            "raw_payload_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in ("result_status_observation_id", "result_revision_number"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise HistoricalTrainingSourceError(f"{name} must be positive")
        object.__setattr__(self, "final_observed_at", _utc(self.final_observed_at, "final_observed_at"))
        if self.result_provider != "statcast":
            raise HistoricalTrainingSourceError("final-score training source must be statcast")
        if self.completion_contract not in STATCAST_FINAL_COMPLETION_CONTRACTS:
            raise HistoricalTrainingSourceError("unsupported Statcast completion contract")
        if self.contract_version != FINAL_GAME_SCORE_SOURCE_LINEAGE_CONTRACT_VERSION:
            raise HistoricalTrainingSourceError("unsupported final-score lineage contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "completion_contract": self.completion_contract,
            "contract_version": self.contract_version,
            "daily_slate_game_checksum": self.daily_slate_game_checksum,
            "daily_slate_snapshot_id": self.daily_slate_snapshot_id,
            "final_observed_at": self.final_observed_at.isoformat(),
            "model_feature_set_checksum": self.model_feature_set_checksum,
            "model_feature_set_snapshot_id": self.model_feature_set_snapshot_id,
            "raw_payload_checksum": self.raw_payload_checksum,
            "result_game_identity_id": self.result_game_identity_id,
            "result_normalized_checksum": self.result_normalized_checksum,
            "result_provider": self.result_provider,
            "result_revision_number": self.result_revision_number,
            "result_source_checksum": self.result_source_checksum,
            "result_status_observation_id": self.result_status_observation_id,
            "run_id": self.run_id,
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class FinalGameScoreSourceExclusionV1:
    source_game_id: str
    run_id: str
    model_feature_set_snapshot_id: str
    model_feature_set_checksum: str
    reason: str
    contract_version: str = FINAL_GAME_SCORE_SOURCE_EXCLUSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_game_id",
            canonical_authoritative_game_id(self.source_game_id),
        )
        for name in ("run_id", "model_feature_set_snapshot_id", "reason"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
        object.__setattr__(
            self,
            "model_feature_set_checksum",
            _sha(self.model_feature_set_checksum, "model_feature_set_checksum"),
        )
        if self.contract_version != FINAL_GAME_SCORE_SOURCE_EXCLUSION_CONTRACT_VERSION:
            raise HistoricalTrainingSourceError("unsupported final-score exclusion contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "model_feature_set_checksum": self.model_feature_set_checksum,
            "model_feature_set_snapshot_id": self.model_feature_set_snapshot_id,
            "reason": self.reason,
            "run_id": self.run_id,
            "source_game_id": self.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class FinalGameScoreSourceInventoryV1:
    feature_inventory_checksum: str
    scores: tuple[FinalGameScoreV1, ...]
    lineages: tuple[FinalGameScoreSourceLineageV1, ...]
    exclusions: tuple[FinalGameScoreSourceExclusionV1, ...]
    contract_version: str = FINAL_GAME_SCORE_SOURCE_INVENTORY_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "feature_inventory_checksum",
            _sha(self.feature_inventory_checksum, "feature_inventory_checksum"),
        )
        scores = tuple(self.scores)
        lineages = tuple(self.lineages)
        exclusions = tuple(self.exclusions)
        score_ids = tuple(item.source_game_id for item in scores)
        lineage_ids = tuple(item.source_game_id for item in lineages)
        if score_ids != lineage_ids:
            raise HistoricalTrainingSourceError("score and result-lineage inventories must match")
        if len(score_ids) != len(set(score_ids)):
            raise HistoricalTrainingSourceError("final-score inventory contains duplicate games")
        excluded_ids = {item.source_game_id for item in exclusions}
        if excluded_ids.intersection(score_ids):
            raise HistoricalTrainingSourceError("a game cannot be scored and excluded")
        if len(excluded_ids) != len(exclusions):
            raise HistoricalTrainingSourceError("final-score exclusions contain duplicate games")
        object.__setattr__(self, "scores", scores)
        object.__setattr__(self, "lineages", lineages)
        object.__setattr__(self, "exclusions", exclusions)
        if self.contract_version != FINAL_GAME_SCORE_SOURCE_INVENTORY_CONTRACT_VERSION:
            raise HistoricalTrainingSourceError("unsupported final-score source inventory contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "exclusions": [item.as_dict() for item in self.exclusions],
            "feature_inventory_checksum": self.feature_inventory_checksum,
            "lineages": [item.as_dict() for item in self.lineages],
            "scores": [item.as_dict() for item in self.scores],
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def _daily_slate_game_evidence(
    database: Database,
    *,
    run_id: str,
    source_game_id: str,
) -> _DailySlateGameEvidence:
    game_id = canonical_authoritative_game_id(source_game_id)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT snapshot.snapshot_id,snapshot.requested_date,snapshot.phase_attempt,
                   snapshot.sealed_at,game.official_date,game.scheduled_start_time,
                   game.away_team_id,game.home_team_id,game.game_number,
                   game.row_checksum
            FROM daily_slate_snapshots AS snapshot
            JOIN daily_slate_games AS game
              ON game.snapshot_id=snapshot.snapshot_id
            WHERE snapshot.run_id=?
              AND snapshot.sealed_at IS NOT NULL
              AND game.source_game_id=?
            ORDER BY snapshot.phase_attempt DESC,snapshot.sealed_at DESC,
                     snapshot.snapshot_id DESC
            """,
            (run_id, game_id),
        ).fetchall()
    if not rows:
        raise HistoricalTrainingSourceError(
            "Model Feature Set game lacks sealed DailySlate game evidence"
        )
    facts = {
        (
            str(row["official_date"]),
            None if row["scheduled_start_time"] is None else str(row["scheduled_start_time"]),
            str(row["away_team_id"]),
            str(row["home_team_id"]),
            None if row["game_number"] is None else int(row["game_number"]),
        )
        for row in rows
    }
    if len(facts) != 1:
        raise HistoricalTrainingSourceError(
            "retained DailySlate attempts disagree on immutable game scheduling facts"
        )
    row = rows[0]
    start = (
        None
        if row["scheduled_start_time"] is None
        else _utc(str(row["scheduled_start_time"]), "scheduled_start_time")
    )
    return _DailySlateGameEvidence(
        snapshot_id=_text(str(row["snapshot_id"]), "daily_slate_snapshot_id"),
        row_checksum=_sha(str(row["row_checksum"]), "daily_slate_game_checksum"),
        official_date=_calendar_date(str(row["official_date"]), "official_date"),
        scheduled_start_time=start,
        away_team_id=_text(str(row["away_team_id"]), "away_team_id"),
        home_team_id=_text(str(row["home_team_id"]), "home_team_id"),
        game_number=None if row["game_number"] is None else int(row["game_number"]),
    )


def _snapshot_is_strictly_pregame(
    database: Database,
    snapshot: PersistedModelFeatureSetV1,
) -> bool:
    for game in snapshot.feature_set.games:
        slate = _daily_slate_game_evidence(
            database,
            run_id=snapshot.run_id,
            source_game_id=game.source_game_id,
        )
        if (
            slate.official_date.isoformat() != snapshot.feature_set.requested_date
            or slate.away_team_id != game.away_team_id
            or slate.home_team_id != game.home_team_id
        ):
            raise HistoricalTrainingSourceError(
                "Model Feature Set game identity disagrees with retained DailySlate evidence"
            )
        start = slate.scheduled_start_time
        if start is None:
            return False
        if snapshot.feature_set.as_of_time >= start or snapshot.feature_set.observed_at >= start:
            return False
    return True


def load_historical_model_feature_sets(
    database: Database,
    repository: ModelFeatureSetRepository,
    *,
    start_date: date | str,
    end_date: date | str,
) -> HistoricalModelFeatureSetInventoryV1:
    start = _calendar_date(start_date, "start_date")
    end = _calendar_date(end_date, "end_date")
    if end < start:
        raise HistoricalTrainingSourceError("end_date must not precede start_date")
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT snapshot_id,requested_date,as_of_time,observed_at,sealed_at,
                   phase_attempt
            FROM model_feature_set_snapshots
            WHERE sealed_at IS NOT NULL
              AND requested_date BETWEEN ? AND ?
            ORDER BY requested_date,as_of_time DESC,observed_at DESC,sealed_at DESC,
                     phase_attempt DESC,snapshot_id DESC
            """,
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["requested_date"]), []).append(row)
    selected: list[PersistedModelFeatureSetV1] = []
    exclusions: list[HistoricalFeatureDateExclusionV1] = []
    for requested_date, candidates in grouped.items():
        retained: PersistedModelFeatureSetV1 | None = None
        for candidate in candidates:
            snapshot = repository.get_by_snapshot_id(str(candidate["snapshot_id"]))
            if (
                snapshot.feature_set.requested_date != requested_date
                or snapshot.phase_attempt != int(candidate["phase_attempt"])
                or snapshot.feature_set.as_of_time
                != _utc(str(candidate["as_of_time"]), "feature as_of_time")
                or snapshot.feature_set.observed_at
                != _utc(str(candidate["observed_at"]), "feature observed_at")
                or snapshot.sealed_at != _utc(str(candidate["sealed_at"]), "feature sealed_at")
            ):
                raise HistoricalTrainingSourceError(
                    "verified Model Feature Set does not match historical inventory row"
                )
            if _snapshot_is_strictly_pregame(database, snapshot):
                retained = snapshot
                break
        if retained is None:
            exclusions.append(
                HistoricalFeatureDateExclusionV1(
                    requested_date=date.fromisoformat(requested_date),
                    reason="no_fully_pregame_verified_snapshot",
                    candidate_count=len(candidates),
                )
            )
        else:
            selected.append(retained)
    selected.sort(key=lambda item: (item.feature_set.requested_date, item.snapshot_id))
    exclusions.sort(key=lambda item: item.requested_date)
    return HistoricalModelFeatureSetInventoryV1(
        start_date=start,
        end_date=end,
        snapshots=tuple(selected),
        exclusions=tuple(exclusions),
    )


def _statcast_final_candidate(
    database: Database,
    *,
    source_game_id: str,
    slate: _DailySlateGameEvidence,
) -> _FinalStatusCandidate | None:
    game_id = canonical_authoritative_game_id(source_game_id)
    with database.connect() as connection:
        rows = connection.execute(
            """
            SELECT game.game_identity_id,status.status_observation_id,
                   status.revision_number,status.abstract_state,status.status_code,
                   status.retrieved_at,status.status_json,status.source_checksum,
                   status.normalized_checksum,raw.checksum_sha256 AS raw_payload_checksum
            FROM stats_game_identities AS game
            JOIN stats_team_identities AS home
              ON home.team_identity_id=game.home_team_identity_id
            JOIN stats_team_identities AS away
              ON away.team_identity_id=game.away_team_identity_id
            JOIN stats_game_status_observations AS status
              ON status.game_identity_id=game.game_identity_id
            LEFT JOIN stats_raw_payload_metadata AS raw
              ON raw.raw_payload_id=status.raw_payload_id
            WHERE game.provider='statcast'
              AND game.provider_game_id=?
              AND game.official_date=?
              AND home.canonical_team_key=?
              AND away.canonical_team_key=?
            ORDER BY status.revision_number DESC,status.retrieved_at DESC,
                     status.status_observation_id DESC
            """,
            (
                game_id,
                slate.official_date.isoformat(),
                slate.home_team_id,
                slate.away_team_id,
            ),
        ).fetchall()
    for row in rows:
        if str(row["abstract_state"] or "").casefold() != "final":
            continue
        try:
            payload = json.loads(str(row["status_json"]))
        except json.JSONDecodeError as exc:
            raise HistoricalTrainingSourceError(
                "persisted Statcast final status JSON is malformed"
            ) from exc
        if not isinstance(payload, Mapping):
            raise HistoricalTrainingSourceError("persisted Statcast final status is not an object")
        completion_contract = str(payload.get("completion_contract") or "").strip()
        if completion_contract not in STATCAST_FINAL_COMPLETION_CONTRACTS:
            continue
        if payload.get("game_pk") is not None and str(payload["game_pk"]) != game_id:
            raise HistoricalTrainingSourceError(
                "Statcast final status game_pk disagrees with Daily MLB game identity"
            )
        if row["raw_payload_checksum"] is None:
            continue
        home_runs = _score(payload.get("home_score"), "home_score")
        away_runs = _score(payload.get("away_score"), "away_score")
        evidence = payload.get("completion_source_evidence")
        if isinstance(evidence, Mapping):
            for key, expected in (("home_score", home_runs), ("away_score", away_runs)):
                if evidence.get(key) is not None and _score(evidence[key], key) != expected:
                    raise HistoricalTrainingSourceError(
                        "Statcast final score disagrees with its completion-source evidence"
                    )
        return _FinalStatusCandidate(
            game_identity_id=_text(str(row["game_identity_id"]), "result_game_identity_id"),
            status_observation_id=int(row["status_observation_id"]),
            revision_number=int(row["revision_number"]),
            home_runs=home_runs,
            away_runs=away_runs,
            retrieved_at=_utc(str(row["retrieved_at"]), "result retrieved_at"),
            source_checksum=_sha(str(row["source_checksum"]), "result_source_checksum"),
            normalized_checksum=_sha(
                str(row["normalized_checksum"]), "result_normalized_checksum"
            ),
            raw_payload_checksum=_sha(
                str(row["raw_payload_checksum"]), "raw_payload_checksum"
            ),
            completion_contract=completion_contract,
        )
    return None


def load_final_game_score_inventory(
    database: Database,
    feature_inventory: HistoricalModelFeatureSetInventoryV1,
) -> FinalGameScoreSourceInventoryV1:
    scores_with_lineage: list[tuple[FinalGameScoreV1, FinalGameScoreSourceLineageV1]] = []
    exclusions: list[FinalGameScoreSourceExclusionV1] = []
    seen_game_ids: set[str] = set()
    for snapshot in feature_inventory.snapshots:
        for game in snapshot.feature_set.games:
            game_id = canonical_authoritative_game_id(game.source_game_id)
            if game_id in seen_game_ids:
                raise HistoricalTrainingSourceError(
                    "historical feature inventory contains a duplicate MLB game identity"
                )
            seen_game_ids.add(game_id)
            slate = _daily_slate_game_evidence(
                database,
                run_id=snapshot.run_id,
                source_game_id=game_id,
            )
            if (
                slate.official_date.isoformat() != snapshot.feature_set.requested_date
                or slate.away_team_id != game.away_team_id
                or slate.home_team_id != game.home_team_id
            ):
                raise HistoricalTrainingSourceError(
                    "Model Feature Set and DailySlate game identity disagree"
                )
            if slate.scheduled_start_time is None:
                exclusions.append(
                    FinalGameScoreSourceExclusionV1(
                        source_game_id=game_id,
                        run_id=snapshot.run_id,
                        model_feature_set_snapshot_id=snapshot.snapshot_id,
                        model_feature_set_checksum=snapshot.feature_set.checksum,
                        reason="missing_scheduled_start_time",
                    )
                )
                continue
            candidate = _statcast_final_candidate(
                database,
                source_game_id=game_id,
                slate=slate,
            )
            if candidate is None:
                exclusions.append(
                    FinalGameScoreSourceExclusionV1(
                        source_game_id=game_id,
                        run_id=snapshot.run_id,
                        model_feature_set_snapshot_id=snapshot.snapshot_id,
                        model_feature_set_checksum=snapshot.feature_set.checksum,
                        reason="missing_validated_statcast_final_score",
                    )
                )
                continue
            if candidate.retrieved_at <= slate.scheduled_start_time:
                raise HistoricalTrainingSourceError(
                    "final-score evidence cannot predate or equal scheduled first pitch"
                )
            score = FinalGameScoreV1(
                source_game_id=game_id,
                game_date=slate.official_date,
                away_team_id=slate.away_team_id,
                home_team_id=slate.home_team_id,
                away_runs=candidate.away_runs,
                home_runs=candidate.home_runs,
                scheduled_start_time=slate.scheduled_start_time,
                completed_at=candidate.retrieved_at,
                source_provider="statcast",
                source_payload_checksum=candidate.raw_payload_checksum,
            )
            lineage = FinalGameScoreSourceLineageV1(
                source_game_id=game_id,
                run_id=snapshot.run_id,
                model_feature_set_snapshot_id=snapshot.snapshot_id,
                model_feature_set_checksum=snapshot.feature_set.checksum,
                daily_slate_snapshot_id=slate.snapshot_id,
                daily_slate_game_checksum=slate.row_checksum,
                result_provider="statcast",
                result_game_identity_id=candidate.game_identity_id,
                result_status_observation_id=candidate.status_observation_id,
                result_revision_number=candidate.revision_number,
                result_source_checksum=candidate.source_checksum,
                result_normalized_checksum=candidate.normalized_checksum,
                raw_payload_checksum=candidate.raw_payload_checksum,
                completion_contract=candidate.completion_contract,
                final_observed_at=candidate.retrieved_at,
            )
            scores_with_lineage.append((score, lineage))
    scores_with_lineage.sort(key=lambda pair: (pair[0].game_date, pair[0].source_game_id))
    exclusions.sort(key=lambda item: item.source_game_id)
    return FinalGameScoreSourceInventoryV1(
        feature_inventory_checksum=feature_inventory.checksum,
        scores=tuple(pair[0] for pair in scores_with_lineage),
        lineages=tuple(pair[1] for pair in scores_with_lineage),
        exclusions=tuple(exclusions),
    )


def load_historical_training_sources(
    database: Database,
    repository: ModelFeatureSetRepository,
    *,
    start_date: date | str,
    end_date: date | str,
) -> tuple[HistoricalModelFeatureSetInventoryV1, FinalGameScoreSourceInventoryV1]:
    features = load_historical_model_feature_sets(
        database,
        repository,
        start_date=start_date,
        end_date=end_date,
    )
    scores = load_final_game_score_inventory(database, features)
    return features, scores
