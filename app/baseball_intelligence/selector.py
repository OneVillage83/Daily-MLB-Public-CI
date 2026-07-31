"""Read-only retained V3 candidate loading for Baseball Intelligence Assembly."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from app.baseball_intelligence.contracts import BaseballFeatureSnapshotV1
from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import parse_requested_date
from app.redaction import redact_value
from app.stats.features import FEATURE_VERSION_V3


class BaseballIntelligenceSelectorError(RuntimeError):
    """Raised when retained V3 feature evidence cannot be safely reconstructed."""


def _canonical_player_ids(values: Iterable[str]) -> tuple[str, ...]:
    identifiers: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("canonical_player_ids must be non-empty trimmed strings")
        identifiers.add(value)
    return tuple(sorted(identifiers))


@dataclass(frozen=True, slots=True)
class BaseballFeatureCandidateInventoryV1:
    """An immutable candidate inventory, deliberately distinct from selection."""

    requested_date: str
    feature_version: str
    canonical_player_ids: tuple[str, ...]
    candidates: tuple[BaseballFeatureSnapshotV1, ...]

    def __post_init__(self) -> None:
        parse_requested_date(self.requested_date)
        if self.feature_version != FEATURE_VERSION_V3:
            raise ValueError("candidate inventory requires frozen V3 feature_version")
        object.__setattr__(
            self,
            "canonical_player_ids",
            _canonical_player_ids(self.canonical_player_ids),
        )
        candidates = tuple(self.candidates)
        if any(not isinstance(item, BaseballFeatureSnapshotV1) for item in candidates):
            raise ValueError("candidate inventory must contain BaseballFeatureSnapshotV1 values")
        if any(
            item.entity_kind != "player"
            or item.feature_version != self.feature_version
            or item.feature_as_of != self.requested_date
            or item.entity_id not in self.canonical_player_ids
            for item in candidates
        ):
            raise ValueError("candidate inventory contains an out-of-domain snapshot")
        ordered = tuple(
            sorted(
                candidates,
                key=lambda item: (
                    item.entity_id,
                    item.feature_snapshot_id,
                    item.stats_run_id,
                    item.input_checksum,
                ),
            )
        )
        if len({item.feature_snapshot_id for item in ordered}) != len(ordered):
            raise ValueError("candidate inventory contains duplicate feature snapshot IDs")
        object.__setattr__(self, "candidates", ordered)

    @property
    def candidate_feature_snapshot_ids(self) -> tuple[str, ...]:
        return tuple(item.feature_snapshot_id for item in self.candidates)

    @property
    def candidate_stats_run_ids(self) -> tuple[str, ...]:
        return tuple(sorted({item.stats_run_id for item in self.candidates}))

    @property
    def candidate_feature_checksums(self) -> tuple[str, ...]:
        return tuple(sorted({item.feature_checksum for item in self.candidates}))

    @property
    def checksum(self) -> str:
        return canonical_sha256(
            {
                "candidate_feature_checksums": list(self.candidate_feature_checksums),
                "candidate_feature_snapshot_ids": list(self.candidate_feature_snapshot_ids),
                "candidate_stats_run_ids": list(self.candidate_stats_run_ids),
                "canonical_player_ids": list(self.canonical_player_ids),
                "feature_version": self.feature_version,
                "requested_date": self.requested_date,
            }
        )


class BaseballIntelligenceFeatureSelector:
    """Load all retained, relevant V3 candidates without making selection decisions."""

    def __init__(
        self,
        database: Database,
        *,
        secret_values: Iterable[str] = (),
    ) -> None:
        self.database = database
        self.secret_values = tuple(str(value) for value in secret_values if str(value))

    @staticmethod
    def _snapshot(row: sqlite3.Row, *, secret_values: tuple[str, ...]) -> BaseballFeatureSnapshotV1:
        try:
            raw_features: object = json.loads(str(row["features_json"]))
        except (TypeError, json.JSONDecodeError) as exc:
            raise BaseballIntelligenceSelectorError(
                "retained V3 features_json is malformed"
            ) from exc
        if not isinstance(raw_features, Mapping):
            raise BaseballIntelligenceSelectorError(
                "retained V3 features_json must be a JSON object"
            )
        if redact_value(raw_features, secret_values) != raw_features:
            raise BaseballIntelligenceSelectorError(
                "retained V3 features_json contains credential-bearing material"
            )
        try:
            snapshot = BaseballFeatureSnapshotV1(
                feature_snapshot_id=str(row["feature_snapshot_id"]),
                stats_run_id=str(row["stats_run_id"]),
                feature_version=str(row["feature_version"]),
                entity_kind=str(row["entity_kind"]),
                entity_id=str(row["canonical_player_id"]),
                feature_as_of=str(row["feature_as_of"]),
                completeness_state=str(row["completeness_state"]),
                input_checksum=str(row["input_checksum"]),
                feature_checksum=str(row["feature_checksum"]),
                features=dict(raw_features),
                created_at=_parse_datetime(str(row["created_at"])),
            )
        except (TypeError, ValueError) as exc:
            raise BaseballIntelligenceSelectorError(
                "retained V3 feature row violates the frozen feature contract"
            ) from exc
        if redact_value(snapshot.as_dict(), secret_values) != snapshot.as_dict():
            raise BaseballIntelligenceSelectorError(
                "retained V3 feature snapshot contains credential-bearing material"
            )
        return snapshot

    def load_candidates(
        self,
        *,
        requested_date: str,
        canonical_player_ids: Iterable[str],
        feature_version: str = FEATURE_VERSION_V3,
        connection: sqlite3.Connection | None = None,
    ) -> BaseballFeatureCandidateInventoryV1:
        date_text = parse_requested_date(requested_date).isoformat()
        identifiers = _canonical_player_ids(canonical_player_ids)
        if feature_version != FEATURE_VERSION_V3:
            raise ValueError("selector accepts only frozen V3 feature_version")
        if not identifiers:
            return BaseballFeatureCandidateInventoryV1(
                requested_date=date_text,
                feature_version=feature_version,
                canonical_player_ids=(),
                candidates=(),
            )
        placeholders = ",".join("?" for _ in identifiers)
        statement = f"""
            SELECT feature_snapshot_id, stats_run_id, feature_version, entity_kind,
                   canonical_player_id, feature_as_of, completeness_state,
                   input_checksum, feature_checksum, features_json, created_at
            FROM stats_feature_snapshots
            WHERE entity_kind='player'
              AND feature_version=?
              AND feature_as_of=?
              AND canonical_player_id IN ({placeholders})
            ORDER BY canonical_player_id, feature_snapshot_id, stats_run_id, input_checksum
        """
        params: tuple[object, ...] = (feature_version, date_text, *identifiers)
        if connection is None:
            with self.database.connect() as read_connection:
                rows = read_connection.execute(statement, params).fetchall()
        else:
            rows = connection.execute(statement, params).fetchall()
        return BaseballFeatureCandidateInventoryV1(
            requested_date=date_text,
            feature_version=feature_version,
            canonical_player_ids=identifiers,
            candidates=tuple(
                self._snapshot(row, secret_values=self.secret_values) for row in rows
            ),
        )


def _parse_datetime(value: str):
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BaseballIntelligenceSelectorError(
            "retained V3 feature created_at is not ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BaseballIntelligenceSelectorError(
            "retained V3 feature created_at must be timezone-aware"
        )
    return parsed.astimezone(timezone.utc)
