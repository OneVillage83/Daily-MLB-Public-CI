from __future__ import annotations

import json
import os
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.model_feature_set.repository import ModelFeatureSetRepository
from app.model_feature_set.schema import MODEL_FEATURE_NAMES_V1
from app.predictions.historical_sources import (
    FinalGameScoreSourceInventoryV1,
    HistoricalModelFeatureSetInventoryV1,
    load_historical_training_sources,
)
from app.predictions.training import (
    HistoricalFeatureValueV1,
    HistoricalScoringRowV1,
    ScoringTrainingDatasetV1,
)
from app.predictions.training_data import (
    ScoringTrainingMaterializationV1,
    materialize_scoring_training_data,
)

HISTORICAL_SCORING_MATERIALIZATION_ARTIFACT_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_SCORING_MATERIALIZATION_ARTIFACT_V1"
)
HISTORICAL_SCORING_FEATURE_COVERAGE_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_SCORING_FEATURE_COVERAGE_V1"
)
HISTORICAL_SCORING_MATERIALIZATION_DOCUMENT_CONTRACT_VERSION = (
    "DSE_MLB_HISTORICAL_SCORING_MATERIALIZATION_DOCUMENT_V1"
)


class HistoricalScoringMaterializationError(ValueError):
    """Raised when deterministic historical scoring materialization is inconsistent."""


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise HistoricalScoringMaterializationError(
            f"{name} must be non-empty trimmed text"
        )
    return value


def _sha(value: object, name: str) -> str:
    text = _text(value, name)
    if len(text) != 64 or any(
        character not in "0123456789abcdef" for character in text
    ):
        raise HistoricalScoringMaterializationError(
            f"{name} must be lowercase SHA-256"
        )
    return text


def _nonnegative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalScoringMaterializationError(
            f"{name} must be a nonnegative integer"
        )
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _reason_counts(values: tuple[object, ...]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for value in values:
        reason = _text(getattr(value, "reason"), "exclusion reason")
        counter[reason] += 1
    return dict(sorted(counter.items()))


@dataclass(frozen=True, slots=True)
class HistoricalScoringFeatureCoverageV1:
    feature_name: str
    observed_game_count: int
    missing_game_count: int
    distinct_observed_value_count: int
    contract_version: str = HISTORICAL_SCORING_FEATURE_COVERAGE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "feature_name",
            _text(self.feature_name, "feature_name"),
        )
        for name in (
            "observed_game_count",
            "missing_game_count",
            "distinct_observed_value_count",
        ):
            _nonnegative_int(getattr(self, name), name)
        if self.distinct_observed_value_count > self.observed_game_count:
            raise HistoricalScoringMaterializationError(
                "distinct observed feature values exceed observed games"
            )
        if (
            self.contract_version
            != HISTORICAL_SCORING_FEATURE_COVERAGE_CONTRACT_VERSION
        ):
            raise HistoricalScoringMaterializationError(
                "unsupported historical feature-coverage contract"
            )

    @property
    def total_game_count(self) -> int:
        return self.observed_game_count + self.missing_game_count

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "distinct_observed_value_count": self.distinct_observed_value_count,
            "feature_name": self.feature_name,
            "missing_game_count": self.missing_game_count,
            "observed_game_count": self.observed_game_count,
            "total_game_count": self.total_game_count,
        }


@dataclass(frozen=True, slots=True)
class HistoricalScoringMaterializationArtifactV1:
    feature_inventory: HistoricalModelFeatureSetInventoryV1
    final_score_inventory: FinalGameScoreSourceInventoryV1
    materialization: ScoringTrainingMaterializationV1
    feature_coverage: tuple[HistoricalScoringFeatureCoverageV1, ...]
    contract_version: str = HISTORICAL_SCORING_MATERIALIZATION_ARTIFACT_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if (
            self.final_score_inventory.feature_inventory_checksum
            != self.feature_inventory.checksum
        ):
            raise HistoricalScoringMaterializationError(
                "final-score inventory is not bound to the selected feature inventory"
            )
        feature_coverage = tuple(self.feature_coverage)
        coverage_names = tuple(item.feature_name for item in feature_coverage)
        if coverage_names != self.materialization.feature_names:
            raise HistoricalScoringMaterializationError(
                "feature coverage must exactly match materialization feature order"
            )
        if len(coverage_names) != len(set(coverage_names)):
            raise HistoricalScoringMaterializationError(
                "feature coverage contains duplicate feature names"
            )
        materialized_count = self.materialization.materialized_game_count
        if any(
            item.total_game_count != materialized_count
            for item in feature_coverage
        ):
            raise HistoricalScoringMaterializationError(
                "feature coverage totals must match materialized game count"
            )
        source_game_ids = tuple(
            game.source_game_id
            for snapshot in self.feature_inventory.snapshots
            for game in snapshot.feature_set.games
        )
        if len(source_game_ids) != len(set(source_game_ids)):
            raise HistoricalScoringMaterializationError(
                "historical feature inventory contains duplicate source games"
            )
        source_ids = set(source_game_ids)
        scored_ids = {
            item.source_game_id for item in self.final_score_inventory.scores
        }
        score_excluded_ids = {
            item.source_game_id
            for item in self.final_score_inventory.exclusions
        }
        if scored_ids | score_excluded_ids != source_ids:
            raise HistoricalScoringMaterializationError(
                "final-score source inventory does not account for every feature game"
            )
        materialized_ids = {
            item.source_game_id for item in self.materialization.dataset.rows
        }
        materialization_excluded_ids = {
            item.source_game_id for item in self.materialization.exclusions
        }
        if materialized_ids | materialization_excluded_ids != source_ids:
            raise HistoricalScoringMaterializationError(
                "training materialization does not account for every feature game"
            )
        object.__setattr__(self, "feature_coverage", feature_coverage)
        if (
            self.contract_version
            != HISTORICAL_SCORING_MATERIALIZATION_ARTIFACT_CONTRACT_VERSION
        ):
            raise HistoricalScoringMaterializationError(
                "unsupported historical scoring materialization artifact contract"
            )

    @property
    def source_game_count(self) -> int:
        return sum(
            len(snapshot.feature_set.games)
            for snapshot in self.feature_inventory.snapshots
        )

    @property
    def first_materialized_date(self) -> date | None:
        rows = self.materialization.dataset.rows
        return None if not rows else rows[0].game_date

    @property
    def last_materialized_date(self) -> date | None:
        rows = self.materialization.dataset.rows
        return None if not rows else rows[-1].game_date

    @property
    def selected_dimension_minimum_total_rows_with_holdout(self) -> int:
        # The trainer needs at least feature_count + 2 rows in its training
        # partition and at least one strictly later validation row. This is only
        # a necessary total-row floor; it is intentionally not a readiness claim.
        return len(self.materialization.feature_names) + 3

    @property
    def selected_dimension_total_row_floor_met(self) -> bool:
        return (
            self.materialization.materialized_game_count
            >= self.selected_dimension_minimum_total_rows_with_holdout
        )

    def coverage_as_dict(self) -> dict[str, object]:
        return {
            "feature_date_exclusion_count": len(
                self.feature_inventory.exclusions
            ),
            "feature_date_exclusion_reasons": _reason_counts(
                self.feature_inventory.exclusions
            ),
            "feature_snapshot_count": len(self.feature_inventory.snapshots),
            "feature_coverage": [
                item.as_dict() for item in self.feature_coverage
            ],
            "final_score_count": len(self.final_score_inventory.scores),
            "final_score_source_exclusion_count": len(
                self.final_score_inventory.exclusions
            ),
            "final_score_source_exclusion_reasons": _reason_counts(
                self.final_score_inventory.exclusions
            ),
            "first_materialized_date": (
                self.first_materialized_date.isoformat()
                if self.first_materialized_date is not None
                else None
            ),
            "last_materialized_date": (
                self.last_materialized_date.isoformat()
                if self.last_materialized_date is not None
                else None
            ),
            "materialization_exclusion_count": len(
                self.materialization.exclusions
            ),
            "materialization_exclusion_reasons": _reason_counts(
                self.materialization.exclusions
            ),
            "materialized_game_count": (
                self.materialization.materialized_game_count
            ),
            "selected_dimension_minimum_total_rows_with_holdout": (
                self.selected_dimension_minimum_total_rows_with_holdout
            ),
            "selected_dimension_total_row_floor_met": (
                self.selected_dimension_total_row_floor_met
            ),
            "selected_feature_count": len(self.materialization.feature_names),
            "source_game_count": self.source_game_count,
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "coverage": self.coverage_as_dict(),
            "end_date": self.feature_inventory.end_date.isoformat(),
            "feature_inventory": self.feature_inventory.as_dict(),
            "feature_inventory_checksum": self.feature_inventory.checksum,
            "final_score_inventory": self.final_score_inventory.as_dict(),
            "final_score_inventory_checksum": self.final_score_inventory.checksum,
            "materialization": self.materialization.as_dict(),
            "materialization_checksum": self.materialization.checksum,
            "start_date": self.feature_inventory.start_date.isoformat(),
        }

    @property
    def checksum(self) -> str:
        return canonical_sha256(self.as_dict())

    def summary_as_dict(self) -> dict[str, object]:
        return {
            "artifact_checksum": self.checksum,
            "contract_version": self.contract_version,
            "coverage": self.coverage_as_dict(),
            "end_date": self.feature_inventory.end_date.isoformat(),
            "feature_inventory_checksum": self.feature_inventory.checksum,
            "final_score_inventory_checksum": self.final_score_inventory.checksum,
            "materialization_checksum": self.materialization.checksum,
            "start_date": self.feature_inventory.start_date.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class LoadedHistoricalScoringDatasetV1:
    dataset: ScoringTrainingDatasetV1
    feature_names: tuple[str, ...]
    artifact_checksum: str
    materialization_checksum: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "artifact_checksum",
            _sha(self.artifact_checksum, "artifact_checksum"),
        )
        object.__setattr__(
            self,
            "materialization_checksum",
            _sha(self.materialization_checksum, "materialization_checksum"),
        )
        feature_names = tuple(self.feature_names)
        if (
            not feature_names
            or feature_names != tuple(sorted(set(feature_names)))
        ):
            raise HistoricalScoringMaterializationError(
                "loaded feature_names must be non-empty, unique, and sorted"
            )
        for row in self.dataset.rows:
            if (
                tuple(item.feature_name for item in row.features)
                != feature_names
            ):
                raise HistoricalScoringMaterializationError(
                    "loaded training row feature inventory differs from artifact feature_names"
                )
        object.__setattr__(self, "feature_names", feature_names)


def _build_feature_coverage(
    materialization: ScoringTrainingMaterializationV1,
) -> tuple[HistoricalScoringFeatureCoverageV1, ...]:
    rows = materialization.dataset.rows
    mappings = tuple(row.feature_map() for row in rows)
    coverage: list[HistoricalScoringFeatureCoverageV1] = []
    for feature_name in materialization.feature_names:
        observed = [mapping.get(feature_name) for mapping in mappings]
        numeric = [float(value) for value in observed if value is not None]
        coverage.append(
            HistoricalScoringFeatureCoverageV1(
                feature_name=feature_name,
                observed_game_count=len(numeric),
                missing_game_count=len(rows) - len(numeric),
                distinct_observed_value_count=len(set(numeric)),
            )
        )
    return tuple(coverage)


def build_historical_scoring_materialization(
    database: Database,
    repository: ModelFeatureSetRepository,
    *,
    start_date: date | str,
    end_date: date | str,
    feature_names: tuple[str, ...] = MODEL_FEATURE_NAMES_V1,
) -> HistoricalScoringMaterializationArtifactV1:
    selected_features = tuple(sorted(set(feature_names)))
    if not selected_features:
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization requires at least one feature"
        )
    feature_inventory, score_inventory = load_historical_training_sources(
        database,
        repository,
        start_date=start_date,
        end_date=end_date,
    )
    materialization = materialize_scoring_training_data(
        tuple(
            snapshot.feature_set
            for snapshot in feature_inventory.snapshots
        ),
        score_inventory.scores,
        feature_names=selected_features,
    )
    return HistoricalScoringMaterializationArtifactV1(
        feature_inventory=feature_inventory,
        final_score_inventory=score_inventory,
        materialization=materialization,
        feature_coverage=_build_feature_coverage(materialization),
    )


def default_historical_scoring_materialization_path(
    artifact_root: Path,
    artifact: HistoricalScoringMaterializationArtifactV1,
) -> Path:
    root = Path(artifact_root)
    return (
        root
        / "training"
        / "scoring"
        / (
            "historical_scoring_materialization_"
            f"{artifact.feature_inventory.start_date.isoformat()}_"
            f"{artifact.feature_inventory.end_date.isoformat()}_"
            f"{artifact.checksum[:16]}.json"
        )
    )


def write_historical_scoring_materialization_artifact(
    artifact: HistoricalScoringMaterializationArtifactV1,
    path: Path,
) -> Path:
    target = Path(path)
    if target.exists() and target.is_symlink():
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization artifact must not be a symbolic link"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "artifact": artifact.as_dict(),
        "artifact_checksum": artifact.checksum,
        "document_contract_version": (
            HISTORICAL_SCORING_MATERIALIZATION_DOCUMENT_CONTRACT_VERSION
        ),
    }
    payload = (_canonical_json(document) + "\n").encode("utf-8")
    if target.exists():
        if not target.is_file():
            raise HistoricalScoringMaterializationError(
                "historical scoring materialization target is not a regular file"
            )
        if target.read_bytes() != payload:
            raise HistoricalScoringMaterializationError(
                "historical scoring materialization path already contains different bytes"
            )
        return target
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        descriptor = os.open(target, flags, 0o600)
    except FileExistsError:
        if target.is_symlink() or target.read_bytes() != payload:
            raise HistoricalScoringMaterializationError(
                "historical scoring materialization path changed during publication"
            )
        return target
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise HistoricalScoringMaterializationError(
            f"{name} must be a JSON object"
        )
    return value


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        raise HistoricalScoringMaterializationError(
            f"{name} must be a JSON array"
        )
    return value


def load_historical_scoring_dataset(
    path: Path,
) -> LoadedHistoricalScoringDatasetV1:
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization artifact must be a regular file"
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization artifact is malformed"
        ) from exc
    root = _mapping(document, "materialization document")
    if (
        root.get("document_contract_version")
        != HISTORICAL_SCORING_MATERIALIZATION_DOCUMENT_CONTRACT_VERSION
    ):
        raise HistoricalScoringMaterializationError(
            "unsupported historical scoring materialization document contract"
        )
    artifact_document = _mapping(root.get("artifact"), "artifact")
    artifact_checksum = _sha(
        root.get("artifact_checksum"),
        "artifact_checksum",
    )
    if canonical_sha256(artifact_document) != artifact_checksum:
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization artifact checksum mismatch"
        )
    if (
        artifact_document.get("contract_version")
        != HISTORICAL_SCORING_MATERIALIZATION_ARTIFACT_CONTRACT_VERSION
    ):
        raise HistoricalScoringMaterializationError(
            "unsupported historical scoring materialization artifact contract"
        )
    materialization = _mapping(
        artifact_document.get("materialization"),
        "materialization",
    )
    materialization_checksum = _sha(
        artifact_document.get("materialization_checksum"),
        "materialization_checksum",
    )
    if canonical_sha256(materialization) != materialization_checksum:
        raise HistoricalScoringMaterializationError(
            "historical scoring materialization checksum mismatch"
        )
    feature_names = tuple(
        _text(value, "feature_name")
        for value in _list(
            materialization.get("feature_names"),
            "feature_names",
        )
    )
    dataset_document = _mapping(
        materialization.get("dataset"),
        "dataset",
    )
    rows: list[HistoricalScoringRowV1] = []
    for raw_row in _list(dataset_document.get("rows"), "dataset.rows"):
        row = _mapping(raw_row, "training row")
        features: list[HistoricalFeatureValueV1] = []
        for raw_feature in _list(
            row.get("features"),
            "training row features",
        ):
            feature = _mapping(raw_feature, "historical feature")
            features.append(
                HistoricalFeatureValueV1(
                    feature_name=_text(
                        feature.get("feature_name"),
                        "feature_name",
                    ),
                    value=feature.get("value"),
                )
            )
        rows.append(
            HistoricalScoringRowV1(
                source_game_id=_text(
                    row.get("source_game_id"),
                    "source_game_id",
                ),
                game_date=date.fromisoformat(
                    _text(row.get("game_date"), "game_date")
                ),
                away_team_id=_text(
                    row.get("away_team_id"),
                    "away_team_id",
                ),
                home_team_id=_text(
                    row.get("home_team_id"),
                    "home_team_id",
                ),
                away_runs=_nonnegative_int(
                    row.get("away_runs"),
                    "away_runs",
                ),
                home_runs=_nonnegative_int(
                    row.get("home_runs"),
                    "home_runs",
                ),
                features=tuple(features),
            )
        )
    dataset = ScoringTrainingDatasetV1(rows=tuple(rows))
    dataset_checksum = _sha(
        materialization.get("dataset_checksum"),
        "dataset_checksum",
    )
    if dataset.checksum != dataset_checksum:
        raise HistoricalScoringMaterializationError(
            "loaded scoring training dataset checksum mismatch"
        )
    return LoadedHistoricalScoringDatasetV1(
        dataset=dataset,
        feature_names=feature_names,
        artifact_checksum=artifact_checksum,
        materialization_checksum=materialization_checksum,
    )
