from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

from app.daily_slate.contracts import canonical_authoritative_game_id, canonical_sha256
from app.model_feature_set.contracts import ModelFeatureSetV1
from app.model_feature_set.schema import FEATURE_NAME_SET_V1
from app.predictions.training import (
    HistoricalFeatureValueV1,
    HistoricalScoringRowV1,
    ScoringTrainingDatasetV1,
)
from app.team_aliases import CANONICAL_TEAM_KEYS

FINAL_GAME_SCORE_CONTRACT_VERSION = "DSE_MLB_FINAL_GAME_SCORE_V1"
TRAINING_ROW_LINEAGE_CONTRACT_VERSION = "DSE_MLB_SCORING_TRAINING_ROW_LINEAGE_V1"
TRAINING_MATERIALIZATION_EXCLUSION_CONTRACT_VERSION = "DSE_MLB_SCORING_TRAINING_EXCLUSION_V1"
TRAINING_MATERIALIZATION_CONTRACT_VERSION = "DSE_MLB_SCORING_TRAINING_MATERIALIZATION_V1"


class ScoringTrainingMaterializationError(ValueError):
    """Raised when historical feature/score evidence cannot be joined safely."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ScoringTrainingMaterializationError(f"{name} must be non-empty trimmed text")
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ScoringTrainingMaterializationError(f"{name} must be lowercase SHA-256")
    return text


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScoringTrainingMaterializationError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class FinalGameScoreV1:
    source_game_id: str
    game_date: date
    away_team_id: str
    home_team_id: str
    away_runs: int
    home_runs: int
    scheduled_start_time: datetime
    completed_at: datetime
    source_provider: str
    source_payload_checksum: str
    status: str = "final"
    contract_version: str = FINAL_GAME_SCORE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", canonical_authoritative_game_id(self.source_game_id))
        if not isinstance(self.game_date, date):
            raise ScoringTrainingMaterializationError("game_date must be a date")
        if (
            self.away_team_id not in CANONICAL_TEAM_KEYS
            or self.home_team_id not in CANONICAL_TEAM_KEYS
            or self.away_team_id == self.home_team_id
        ):
            raise ScoringTrainingMaterializationError("final-score team identity is invalid")
        for name in ("away_runs", "home_runs"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ScoringTrainingMaterializationError(f"{name} must be a nonnegative integer")
        scheduled = _utc(self.scheduled_start_time, "scheduled_start_time")
        completed = _utc(self.completed_at, "completed_at")
        if completed <= scheduled:
            raise ScoringTrainingMaterializationError("completed_at must be after scheduled_start_time")
        object.__setattr__(self, "scheduled_start_time", scheduled)
        object.__setattr__(self, "completed_at", completed)
        object.__setattr__(self, "source_provider", _text(self.source_provider, "source_provider"))
        object.__setattr__(self, "source_payload_checksum", _sha(self.source_payload_checksum, "source_payload_checksum"))
        if self.status != "final":
            raise ScoringTrainingMaterializationError("training score evidence must be final")
        if self.contract_version != FINAL_GAME_SCORE_CONTRACT_VERSION:
            raise ScoringTrainingMaterializationError("unsupported final-game-score contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "away_runs": self.away_runs,
            "away_team_id": self.away_team_id,
            "completed_at": self.completed_at.isoformat(),
            "contract_version": self.contract_version,
            "game_date": self.game_date.isoformat(),
            "home_runs": self.home_runs,
            "home_team_id": self.home_team_id,
            "scheduled_start_time": self.scheduled_start_time.isoformat(),
            "source_game_id": self.source_game_id,
            "source_payload_checksum": self.source_payload_checksum,
            "source_provider": self.source_provider,
            "status": self.status,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class TrainingRowLineageV1:
    source_game_id: str
    model_feature_set_checksum: str
    model_feature_game_checksum: str
    predictive_feature_checksum: str
    feature_as_of_time: datetime
    feature_observed_at: datetime
    final_score_checksum: str
    scheduled_start_time: datetime
    score_completed_at: datetime
    contract_version: str = TRAINING_ROW_LINEAGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", canonical_authoritative_game_id(self.source_game_id))
        for name in (
            "model_feature_set_checksum",
            "model_feature_game_checksum",
            "predictive_feature_checksum",
            "final_score_checksum",
        ):
            object.__setattr__(self, name, _sha(getattr(self, name), name))
        for name in (
            "feature_as_of_time",
            "feature_observed_at",
            "scheduled_start_time",
            "score_completed_at",
        ):
            object.__setattr__(self, name, _utc(getattr(self, name), name))
        if self.feature_as_of_time >= self.scheduled_start_time:
            raise ScoringTrainingMaterializationError("feature as_of_time must be strictly pregame")
        if self.feature_observed_at >= self.scheduled_start_time:
            raise ScoringTrainingMaterializationError("feature observed_at must be strictly pregame")
        if self.score_completed_at <= self.scheduled_start_time:
            raise ScoringTrainingMaterializationError("score completion must follow scheduled start")
        if self.contract_version != TRAINING_ROW_LINEAGE_CONTRACT_VERSION:
            raise ScoringTrainingMaterializationError("unsupported training-row-lineage contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "feature_as_of_time": self.feature_as_of_time.isoformat(),
            "feature_observed_at": self.feature_observed_at.isoformat(),
            "final_score_checksum": self.final_score_checksum,
            "model_feature_game_checksum": self.model_feature_game_checksum,
            "model_feature_set_checksum": self.model_feature_set_checksum,
            "predictive_feature_checksum": self.predictive_feature_checksum,
            "scheduled_start_time": self.scheduled_start_time.isoformat(),
            "score_completed_at": self.score_completed_at.isoformat(),
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class TrainingMaterializationExclusionV1:
    source_game_id: str
    reason: str
    model_feature_set_checksum: str
    model_feature_game_checksum: str
    contract_version: str = TRAINING_MATERIALIZATION_EXCLUSION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_game_id", canonical_authoritative_game_id(self.source_game_id))
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "model_feature_set_checksum", _sha(self.model_feature_set_checksum, "model_feature_set_checksum"))
        object.__setattr__(self, "model_feature_game_checksum", _sha(self.model_feature_game_checksum, "model_feature_game_checksum"))
        if self.contract_version != TRAINING_MATERIALIZATION_EXCLUSION_CONTRACT_VERSION:
            raise ScoringTrainingMaterializationError("unsupported training-materialization exclusion contract")

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "model_feature_game_checksum": self.model_feature_game_checksum,
            "model_feature_set_checksum": self.model_feature_set_checksum,
            "reason": self.reason,
            "source_game_id": self.source_game_id,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


@dataclass(frozen=True, slots=True)
class ScoringTrainingMaterializationV1:
    dataset: ScoringTrainingDatasetV1
    lineages: tuple[TrainingRowLineageV1, ...]
    exclusions: tuple[TrainingMaterializationExclusionV1, ...]
    feature_names: tuple[str, ...]
    contract_version: str = TRAINING_MATERIALIZATION_CONTRACT_VERSION

    def __post_init__(self) -> None:
        feature_names = tuple(self.feature_names)
        if not feature_names or feature_names != tuple(sorted(set(feature_names))):
            raise ScoringTrainingMaterializationError("feature_names must be non-empty, unique, and sorted")
        if any(feature not in FEATURE_NAME_SET_V1 for feature in feature_names):
            raise ScoringTrainingMaterializationError("materialization contains unsupported feature_names")
        object.__setattr__(self, "feature_names", feature_names)
        lineages = tuple(self.lineages)
        exclusions = tuple(self.exclusions)
        row_ids = tuple(row.source_game_id for row in self.dataset.rows)
        lineage_ids = tuple(lineage.source_game_id for lineage in lineages)
        if lineage_ids != row_ids:
            raise ScoringTrainingMaterializationError("training row lineage inventory must exactly match dataset order")
        excluded_ids = {item.source_game_id for item in exclusions}
        if excluded_ids.intersection(row_ids):
            raise ScoringTrainingMaterializationError("a game cannot be both materialized and excluded")
        if len(excluded_ids) != len(exclusions):
            raise ScoringTrainingMaterializationError("training materialization contains duplicate exclusions")
        object.__setattr__(self, "lineages", lineages)
        object.__setattr__(self, "exclusions", exclusions)
        if self.contract_version != TRAINING_MATERIALIZATION_CONTRACT_VERSION:
            raise ScoringTrainingMaterializationError("unsupported scoring-training materialization contract")

    @property
    def materialized_game_count(self) -> int:
        return len(self.dataset.rows)

    @property
    def excluded_game_count(self) -> int:
        return len(self.exclusions)

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "dataset": self.dataset.as_dict(),
            "dataset_checksum": self.dataset.checksum,
            "excluded_game_count": self.excluded_game_count,
            "exclusions": [item.as_dict() for item in self.exclusions],
            "feature_names": list(self.feature_names),
            "lineages": [item.as_dict() for item in self.lineages],
            "materialized_game_count": self.materialized_game_count,
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())


def materialize_scoring_training_data(
    feature_sets: tuple[ModelFeatureSetV1, ...],
    final_scores: tuple[FinalGameScoreV1, ...],
    *,
    feature_names: tuple[str, ...],
) -> ScoringTrainingMaterializationV1:
    selected_features = tuple(sorted(set(feature_names)))
    if not selected_features or any(feature not in FEATURE_NAME_SET_V1 for feature in selected_features):
        raise ScoringTrainingMaterializationError("feature_names must contain supported ModelFeatureSet V1 names")
    score_map: dict[str, FinalGameScoreV1] = {}
    for final_score in final_scores:
        if final_score.source_game_id in score_map:
            raise ScoringTrainingMaterializationError("duplicate final-score source_game_id")
        score_map[final_score.source_game_id] = final_score
    rows_with_lineage: list[tuple[HistoricalScoringRowV1, TrainingRowLineageV1]] = []
    exclusions: list[TrainingMaterializationExclusionV1] = []
    seen_feature_games: set[str] = set()
    for feature_set in sorted(feature_sets, key=lambda item: (item.requested_date, item.checksum)):
        requested_date = date.fromisoformat(feature_set.requested_date)
        for game in feature_set.games:
            if game.source_game_id in seen_feature_games:
                raise ScoringTrainingMaterializationError("duplicate feature game across historical feature sets")
            seen_feature_games.add(game.source_game_id)
            score = score_map.get(game.source_game_id)
            if score is None:
                exclusions.append(
                    TrainingMaterializationExclusionV1(
                        source_game_id=game.source_game_id,
                        reason="missing_final_score",
                        model_feature_set_checksum=feature_set.checksum,
                        model_feature_game_checksum=game.checksum,
                    )
                )
                continue
            if score.game_date != requested_date:
                raise ScoringTrainingMaterializationError("feature-set requested date and final-score game date disagree")
            if score.away_team_id != game.away_team_id or score.home_team_id != game.home_team_id:
                raise ScoringTrainingMaterializationError("feature game and final score team identity disagree")
            if feature_set.as_of_time >= score.scheduled_start_time:
                raise ScoringTrainingMaterializationError("feature-set as_of_time is not strictly pregame")
            if feature_set.observed_at >= score.scheduled_start_time:
                raise ScoringTrainingMaterializationError("feature-set observed_at is not strictly pregame")
            feature_map = game.feature_map()
            features = tuple(
                HistoricalFeatureValueV1(feature_name, feature_map[feature_name])
                for feature_name in selected_features
            )
            row = HistoricalScoringRowV1(
                source_game_id=game.source_game_id,
                game_date=score.game_date,
                away_team_id=score.away_team_id,
                home_team_id=score.home_team_id,
                away_runs=score.away_runs,
                home_runs=score.home_runs,
                features=features,
            )
            lineage = TrainingRowLineageV1(
                source_game_id=game.source_game_id,
                model_feature_set_checksum=feature_set.checksum,
                model_feature_game_checksum=game.checksum,
                predictive_feature_checksum=game.predictive_feature_checksum,
                feature_as_of_time=feature_set.as_of_time,
                feature_observed_at=feature_set.observed_at,
                final_score_checksum=score.checksum,
                scheduled_start_time=score.scheduled_start_time,
                score_completed_at=score.completed_at,
            )
            rows_with_lineage.append((row, lineage))
    rows_with_lineage.sort(key=lambda pair: (pair[0].game_date, pair[0].source_game_id))
    exclusions.sort(key=lambda item: item.source_game_id)
    dataset = ScoringTrainingDatasetV1(rows=tuple(pair[0] for pair in rows_with_lineage))
    return ScoringTrainingMaterializationV1(
        dataset=dataset,
        lineages=tuple(pair[1] for pair in rows_with_lineage),
        exclusions=tuple(exclusions),
        feature_names=selected_features,
    )
