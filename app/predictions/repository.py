from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.data_quality.repository import DataQualityRepository, PersistedDataQualityV1
from app.database import Database
from app.identifiers import validate_run_id
from app.matchup_packet.repository import MatchupPacketRepository, PersistedMatchupPacketV1
from app.model_feature_set.repository import ModelFeatureSetRepository, PersistedModelFeatureSetV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelAttemptManifestV1,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
)
from app.predictions.artifact_v1 import verify_predictions_artifact, write_predictions_artifact
from app.predictions.attempt_manifest import (
    create_predictions_attempt_manifest,
    publish_predictions_attempt_manifest,
    verify_predictions_attempt_manifest,
)
from app.predictions.production import (
    MoneylinePredictionV1,
    PredictionProviderPolicyV1,
    PredictionsV1,
    ReviewedPredictionInputV1,
)


class PredictionsAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    VALIDATION_FAILED = "validation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class PredictionsRepositoryError(RuntimeError):
    pass


class PredictionsNotFoundError(PredictionsRepositoryError):
    pass


class PredictionsPersistenceConflict(PredictionsRepositoryError):
    pass


class PredictionsIntegrityError(PredictionsRepositoryError):
    pass


class PredictionsInputInventoryError(PredictionsIntegrityError):
    def __init__(self, message: str, inventory: "PredictionsInputInventoryV1") -> None:
        super().__init__(message)
        self.inventory = inventory


@dataclass(frozen=True, slots=True)
class PredictionsUpstreamV1:
    model_feature_set: PersistedModelFeatureSetV1
    data_quality: PersistedDataQualityV1
    matchup_packet: PersistedMatchupPacketV1

    def identities(self) -> tuple[PreModelUpstreamIdentityV1, ...]:
        return (
            PreModelUpstreamIdentityV1(
                "model_feature_set",
                self.model_feature_set.snapshot_id,
                self.model_feature_set.feature_set.checksum,
            ),
            PreModelUpstreamIdentityV1(
                "data_quality",
                self.data_quality.snapshot_id,
                self.data_quality.snapshot.checksum,
            ),
        )


@dataclass(frozen=True, slots=True)
class InvalidPredictionInputV1:
    source_game_id: str
    reason_code: str
    retained_input_checksum: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "reason_code": self.reason_code,
            "retained_input_checksum": self.retained_input_checksum,
            "source_game_id": self.source_game_id,
        }


@dataclass(frozen=True, slots=True)
class PredictionsInputInventoryV1:
    expected_game_ids: tuple[str, ...]
    values: tuple[ReviewedPredictionInputV1, ...]
    missing_game_ids: tuple[str, ...]
    invalid_inputs: tuple[InvalidPredictionInputV1, ...]
    validation_error: Exception | None = field(default=None, compare=False, repr=False)

    @property
    def checksum(self) -> str:
        values = {value.source_game_id: value.checksum for value in self.values}
        invalid = {value.source_game_id: value.as_dict() for value in self.invalid_inputs}
        return canonical_sha256(
            [
                {"input_checksum": values[source_game_id], "source_game_id": source_game_id}
                if source_game_id in values
                else {"invalid_input": invalid[source_game_id], "source_game_id": source_game_id}
                if source_game_id in invalid
                else {"input_checksum": None, "source_game_id": source_game_id}
                for source_game_id in self.expected_game_ids
            ]
        )


@dataclass(frozen=True, slots=True)
class PredictionsAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    outcome: PredictionsAttemptOutcome
    snapshot_checksum: str | None
    phase_input_checksum: str
    input_inventory_checksum: str
    warnings: tuple[Mapping[str, object], ...]
    manifest: PreModelArtifactV1
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedPredictionsV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    predictions: PredictionsV1
    artifact: PreModelArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise PredictionsIntegrityError(f"persisted {name} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), name)
    except ValueError as exc:
        raise PredictionsIntegrityError(f"persisted {name} is invalid") from exc


def _policy(payload: object) -> PredictionProviderPolicyV1:
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise PredictionsIntegrityError("persisted provider policy is invalid") from exc
    if not isinstance(payload, dict):
        raise PredictionsIntegrityError("persisted provider policy is not an object")
    identity = {key: value for key, value in payload.items() if key != "checksum"}
    policy = PredictionProviderPolicyV1(**identity)
    if payload.get("checksum") != policy.checksum:
        raise PredictionsIntegrityError("persisted provider policy checksum mismatch")
    return policy


class PredictionsRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _now,
        provider_policy: PredictionProviderPolicyV1 = PredictionProviderPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.provider_policy = provider_policy
        self.model_feature_set = ModelFeatureSetRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.data_quality = DataQualityRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )
        self.matchup_packet = MatchupPacketRepository(
            database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=clock
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> PredictionsUpstreamV1:
        model = self.model_feature_set.get_latest_for_run(validate_run_id(run_id))
        if model is None:
            raise PredictionsIntegrityError("Predictions requires a sealed Model Feature Set")
        return self.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=model.snapshot_id,
        )

    def resolve_upstream_by_snapshot_ids(
        self,
        *,
        model_feature_set_snapshot_id: str,
        data_quality_snapshot_id: str | None = None,
    ) -> PredictionsUpstreamV1:
        """Resolve the exact stored pre-model chain without consulting latest-for-run."""
        model = self.model_feature_set.get_by_snapshot_id(model_feature_set_snapshot_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT upstream_data_quality_snapshot_id,upstream_data_quality_checksum,upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum FROM model_feature_set_snapshots WHERE snapshot_id=?",
                (model.snapshot_id,),
            ).fetchone()
        if row is None:
            raise PredictionsIntegrityError("Model Feature Set lineage row is missing")
        stored_quality_id = str(row["upstream_data_quality_snapshot_id"])
        if data_quality_snapshot_id is not None and data_quality_snapshot_id != stored_quality_id:
            raise PredictionsIntegrityError("Predictions Data Quality snapshot identity mismatch")
        quality = self.data_quality.get_by_snapshot_id(stored_quality_id)
        packet = self.matchup_packet.get_by_snapshot_id(str(row["upstream_matchup_packet_snapshot_id"]))
        if quality.snapshot.checksum != str(row["upstream_data_quality_checksum"]):
            raise PredictionsIntegrityError("Model Feature Set Data Quality checksum mismatch")
        if packet.packet.checksum != str(row["upstream_matchup_packet_checksum"]):
            raise PredictionsIntegrityError("Model Feature Set Matchup Packet checksum mismatch")
        if (
            model.feature_set.requested_date != quality.snapshot.requested_date
            or model.feature_set.as_of_time != quality.snapshot.as_of_time
            or [game.source_game_id for game in model.feature_set.games]
            != [game.source_game_id for game in packet.packet.games]
        ):
            raise PredictionsIntegrityError("Predictions upstream chain is inconsistent")
        return PredictionsUpstreamV1(model, quality, packet)

    def seal_authoring_input(self, value: ReviewedPredictionInputV1) -> ReviewedPredictionInputV1:
        upstream = self.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=value.upstream_model_feature_set_snapshot_id
        )
        if (
            value.run_id != upstream.model_feature_set.run_id
            or
            value.upstream_model_feature_set_snapshot_id != upstream.model_feature_set.snapshot_id
            or value.upstream_model_feature_set_checksum != upstream.model_feature_set.feature_set.checksum
        ):
            raise PredictionsIntegrityError("authoring input Model Feature Set lineage mismatch")
        games = {game.source_game_id: game for game in upstream.model_feature_set.feature_set.games}
        game = games.get(value.source_game_id)
        if game is None:
            raise PredictionsIntegrityError("authoring input source game is unknown")
        if (
            value.upstream_model_feature_game_checksum != game.checksum
            or value.predictive_feature_checksum != game.predictive_feature_checksum
            or value.provider_policy != self.provider_policy
        ):
            raise PredictionsIntegrityError("authoring input feature or provider identity mismatch")
        try:
            with self.database.connect(write=True) as connection:
                connection.execute(
                    """
                    INSERT INTO prediction_authoring_inputs(
                      input_id,run_id,source_game_id,upstream_model_feature_set_snapshot_id,
                      upstream_model_feature_set_checksum,upstream_model_feature_game_checksum,
                      predictive_feature_checksum,provider_policy_json,provider_policy_checksum,
                      home_probability,home_lower,home_upper,generated_at,sealed_at,market_independence_attested,
                      authoring_evidence_json,evidence_checksum,input_checksum,canonical_json,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        f"reviewed-analyst:{value.checksum}",
                        value.run_id,
                        value.source_game_id,
                        value.upstream_model_feature_set_snapshot_id,
                        value.upstream_model_feature_set_checksum,
                        value.upstream_model_feature_game_checksum,
                        value.predictive_feature_checksum,
                        canonical_text(value.provider_policy.as_dict()),
                        value.provider_policy.checksum,
                        value.home_probability,
                        value.home_lower,
                        value.home_upper,
                        value.generated_at.isoformat(),
                        value.sealed_at.isoformat(),
                        int(value.market_independence_attested),
                        canonical_text(dict(value.authoring_evidence)),
                        value.evidence_checksum,
                        value.checksum,
                        value.canonical_json_bytes().decode("utf-8"),
                        self._now().isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            try:
                existing = self.get_authoring_input(
                    value.run_id,
                    value.source_game_id,
                    value.upstream_model_feature_set_snapshot_id,
                )
            except PredictionsNotFoundError:
                raise PredictionsPersistenceConflict("conflicting reviewed prediction input") from exc
            if existing.canonical_json_bytes() == value.canonical_json_bytes():
                return existing
            raise PredictionsPersistenceConflict("conflicting reviewed prediction input") from exc
        return self.get_authoring_input(
            value.run_id,
            value.source_game_id,
            value.upstream_model_feature_set_snapshot_id,
        )

    def get_authoring_input(
        self,
        run_id: str,
        source_game_id: str,
        model_feature_set_snapshot_id: str,
    ) -> ReviewedPredictionInputV1:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM prediction_authoring_inputs WHERE run_id=? AND source_game_id=? AND upstream_model_feature_set_snapshot_id=?",
                (validate_run_id(run_id), source_game_id, model_feature_set_snapshot_id),
            ).fetchone()
        if row is None:
            raise PredictionsNotFoundError("reviewed prediction input not found")
        return self._authoring_from_row(row)

    def _authoring_from_row(self, row: sqlite3.Row) -> ReviewedPredictionInputV1:
        value = ReviewedPredictionInputV1(
            run_id=str(row["run_id"]),
            source_game_id=str(row["source_game_id"]),
            upstream_model_feature_set_snapshot_id=str(row["upstream_model_feature_set_snapshot_id"]),
            upstream_model_feature_set_checksum=str(row["upstream_model_feature_set_checksum"]),
            upstream_model_feature_game_checksum=str(row["upstream_model_feature_game_checksum"]),
            predictive_feature_checksum=str(row["predictive_feature_checksum"]),
            provider_policy=_policy(row["provider_policy_json"]),
            home_probability=float(row["home_probability"]),
            home_lower=float(row["home_lower"]),
            home_upper=float(row["home_upper"]),
            generated_at=_time(row["generated_at"], "generated_at"),
            sealed_at=_time(row["sealed_at"], "sealed_at"),
            authoring_evidence=json.loads(str(row["authoring_evidence_json"])),
            market_independence_attested=bool(row["market_independence_attested"])
            if isinstance(row["market_independence_attested"], int)
            and row["market_independence_attested"] in {0, 1}
            else row["market_independence_attested"],
            secret_values=self.secret_values,
        )
        if value.checksum != str(row["input_checksum"]) or value.canonical_json_bytes().decode("utf-8") != str(
            row["canonical_json"]
        ):
            raise PredictionsIntegrityError("reviewed prediction input evidence mismatch")
        return value

    def discover_input_inventory(self, upstream: PredictionsUpstreamV1) -> PredictionsInputInventoryV1:
        expected = tuple(game.source_game_id for game in upstream.model_feature_set.feature_set.games)
        values: list[ReviewedPredictionInputV1] = []
        missing: list[str] = []
        invalid: list[InvalidPredictionInputV1] = []
        validation_error: Exception | None = None
        with self.database.connect() as connection:
            for source_game_id in expected:
                rows = connection.execute(
                    "SELECT * FROM prediction_authoring_inputs WHERE run_id=? AND source_game_id=? AND upstream_model_feature_set_snapshot_id=? ORDER BY input_id",
                    (upstream.model_feature_set.run_id, source_game_id, upstream.model_feature_set.snapshot_id),
                ).fetchall()
                if not rows:
                    missing.append(source_game_id)
                    continue
                if len(rows) != 1:
                    invalid.append(InvalidPredictionInputV1(source_game_id, "ambiguous_authoring_identity"))
                    continue
                row = rows[0]
                retained = str(row["input_checksum"])
                retained_checksum = (
                    retained
                    if len(retained) == 64 and all(character in "0123456789abcdef" for character in retained)
                    else None
                )
                try:
                    value = self._authoring_from_row(row)
                    if (
                        value.upstream_model_feature_set_snapshot_id != upstream.model_feature_set.snapshot_id
                        or value.upstream_model_feature_set_checksum != upstream.model_feature_set.feature_set.checksum
                    ):
                        raise PredictionsIntegrityError("authoring input Model Feature Set identity mismatch")
                    game = next(
                        game
                        for game in upstream.model_feature_set.feature_set.games
                        if game.source_game_id == source_game_id
                    )
                    if (
                        value.upstream_model_feature_game_checksum != game.checksum
                        or value.predictive_feature_checksum != game.predictive_feature_checksum
                        or value.provider_policy != self.provider_policy
                    ):
                        raise PredictionsIntegrityError("authoring input retained lineage mismatch")
                except Exception as exc:
                    if validation_error is None:
                        validation_error = exc
                    invalid.append(
                        InvalidPredictionInputV1(source_game_id, "invalid_authoring_evidence", retained_checksum)
                    )
                else:
                    values.append(value)
        return PredictionsInputInventoryV1(
            expected,
            tuple(values),
            tuple(missing),
            tuple(invalid),
            validation_error,
        )

    def load_input_inventory(
        self, upstream: PredictionsUpstreamV1
    ) -> tuple[tuple[ReviewedPredictionInputV1, ...], tuple[str, ...]]:
        inventory = self.discover_input_inventory(upstream)
        if inventory.invalid_inputs:
            raise PredictionsInputInventoryError("retained reviewed prediction input is invalid", inventory)
        return inventory.values, inventory.missing_game_ids

    @staticmethod
    def input_inventory_checksum(values: tuple[ReviewedPredictionInputV1, ...], expected: tuple[str, ...]) -> str:
        by_game = {value.source_game_id: value for value in values}
        return canonical_sha256(
            [
                {"input_checksum": by_game[game_id].checksum, "source_game_id": game_id}
                if game_id in by_game
                else {"input_checksum": None, "source_game_id": game_id}
                for game_id in expected
            ]
        )

    def assemble(
        self,
        upstream: PredictionsUpstreamV1,
        values: tuple[ReviewedPredictionInputV1, ...],
        *,
        observed_at: datetime,
        provider_policy: PredictionProviderPolicyV1 | None = None,
    ) -> PredictionsV1:
        active_policy = self.provider_policy if provider_policy is None else provider_policy
        feature_games = tuple(upstream.model_feature_set.feature_set.games)
        expected = tuple(game.source_game_id for game in feature_games)
        by_game = {value.source_game_id: value for value in values}
        if set(by_game) != set(expected) or len(by_game) != len(values):
            raise PredictionsIntegrityError("exactly one reviewed prediction is required per game")
        packet_by_game = {game.source_game_id: game for game in upstream.matchup_packet.packet.games}
        games: list[MoneylinePredictionV1] = []
        for ordinal, feature_game in enumerate(feature_games, 1):
            authored = by_game[feature_game.source_game_id]
            packet = packet_by_game[feature_game.source_game_id]
            if (
                authored.upstream_model_feature_game_checksum != feature_game.checksum
                or authored.predictive_feature_checksum != feature_game.predictive_feature_checksum
                or authored.provider_policy != active_policy
            ):
                raise PredictionsIntegrityError("reviewed prediction lineage is invalid")
            scheduled_start = packet.schedule.scheduled_start_time
            if scheduled_start is None:
                raise PredictionsIntegrityError("prediction game requires a scheduled start time")
            games.append(
                MoneylinePredictionV1(
                    source_game_id=feature_game.source_game_id,
                    ordinal=ordinal,
                    away_team_id=feature_game.away_team_id,
                    home_team_id=feature_game.home_team_id,
                    scheduled_start_time=scheduled_start,
                    predictive_feature_checksum=feature_game.predictive_feature_checksum,
                    upstream_model_feature_game_checksum=feature_game.checksum,
                    provider_policy=authored.provider_policy,
                    home_probability=authored.home_probability,
                    home_lower=authored.home_lower,
                    home_upper=authored.home_upper,
                    generated_at=authored.generated_at,
                    sealed_at=authored.sealed_at,
                    evidence_checksum=authored.evidence_checksum,
                    market_independence_attested=authored.market_independence_attested,
                    completeness_state="degraded"
                    if feature_game.quality_disposition.value in {"degraded", "insufficient"}
                    else "complete",
                )
            )
        return PredictionsV1(
            run_id=upstream.model_feature_set.run_id,
            requested_date=upstream.model_feature_set.feature_set.requested_date,
            as_of_time=upstream.model_feature_set.feature_set.as_of_time,
            observed_at=aware_utc(observed_at, "observed_at"),
            provider_policy=active_policy,
            upstream_model_feature_set_snapshot_id=upstream.model_feature_set.snapshot_id,
            upstream_model_feature_set_checksum=upstream.model_feature_set.feature_set.checksum,
            upstream_data_quality_snapshot_id=upstream.data_quality.snapshot_id,
            upstream_data_quality_checksum=upstream.data_quality.snapshot.checksum,
            input_inventory_checksum=self.input_inventory_checksum(values, expected),
            games=tuple(games),
            secret_values=self.secret_values,
        )

    @staticmethod
    def _active(connection: sqlite3.Connection, run_id: str, attempt: int) -> None:
        row = connection.execute(
            "SELECT status,attempt_count FROM pipeline_run_phases WHERE run_id=? AND phase_key='predictions'",
            (run_id,),
        ).fetchone()
        if row is None or str(row["status"]) != "running" or int(row["attempt_count"]) != attempt:
            raise PredictionsIntegrityError("Predictions attempt is not the active controller attempt")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        requested_date: str,
        as_of_time: datetime,
        observed_at: datetime,
        phase_input_checksum: str,
        upstream: PredictionsUpstreamV1,
        outcome: PredictionsAttemptOutcome,
        snapshot_checksum: str | None,
        inventory: PredictionsInputInventoryV1,
        warnings: tuple[Mapping[str, object], ...],
        created_at: datetime,
    ) -> PreModelAttemptManifestV1:
        return create_predictions_attempt_manifest(
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=requested_date,
            as_of_time=as_of_time,
            observed_at=observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream.identities(),
            outcome=outcome.value,
            snapshot_checksum=snapshot_checksum,
            warnings=warnings,
            created_at=created_at,
            completed_at=created_at,
            provider_policy=self.provider_policy,
            expected_game_ids=inventory.expected_game_ids,
            present_input_checksums=tuple(value.checksum for value in inventory.values),
            missing_game_ids=inventory.missing_game_ids,
            invalid_inputs=tuple(value.as_dict() for value in inventory.invalid_inputs),
            input_inventory_checksum=inventory.checksum,
            secret_values=self.secret_values,
        )

    @staticmethod
    def _insert_attempt(
        connection: sqlite3.Connection, manifest: PreModelAttemptManifestV1, artifact: PreModelArtifactV1
    ) -> None:
        upstream = {value.phase_key: value for value in manifest.upstream}
        evidence = manifest.phase_input_evidence
        policy = evidence["provider_policy"]
        if not isinstance(policy, Mapping):
            raise PredictionsIntegrityError("manifest provider policy is missing")
        invalid_inputs = evidence["invalid_inputs"]
        if not isinstance(invalid_inputs, list):
            raise PredictionsIntegrityError("manifest invalid input inventory is missing")
        connection.execute(
            """
            INSERT INTO predictions_attempt_evidence(
              run_id,phase_attempt,requested_date,as_of_time,observed_at,phase_input_checksum,
              provider_policy_json,provider_policy_checksum,
              upstream_model_feature_set_snapshot_id,upstream_model_feature_set_checksum,
              upstream_data_quality_snapshot_id,upstream_data_quality_checksum,
              expected_game_ids_json,present_input_checksums_json,missing_game_ids_json,invalid_inputs_json,invalid_input_count,input_inventory_checksum,
              outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,evidence_manifest_byte_count,
              warnings_json,warning_count,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                manifest.run_id,
                manifest.phase_attempt,
                manifest.requested_date,
                manifest.as_of_time.isoformat(),
                manifest.observed_at.isoformat(),
                manifest.phase_input_checksum,
                canonical_text(policy),
                str(policy["checksum"]),
                upstream["model_feature_set"].snapshot_id,
                upstream["model_feature_set"].checksum,
                upstream["data_quality"].snapshot_id,
                upstream["data_quality"].checksum,
                canonical_text(evidence["expected_game_ids"]),
                canonical_text(evidence["present_input_checksums"]),
                canonical_text(evidence["missing_game_ids"]),
                canonical_text(evidence["invalid_inputs"]),
                len(invalid_inputs),
                evidence["input_inventory_checksum"],
                manifest.outcome,
                manifest.snapshot_checksum,
                artifact.relpath,
                artifact.checksum,
                artifact.byte_count,
                canonical_text(list(manifest.warnings)),
                len(manifest.warnings),
                manifest.created_at.isoformat(),
                manifest.completed_at.isoformat(),
            ),
        )

    def persist_assembly(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        predictions: PredictionsV1,
        values: tuple[ReviewedPredictionInputV1, ...],
    ) -> PersistedPredictionsV1:
        try:
            existing = self.get_for_run_attempt(run_id, phase_attempt)
        except PredictionsNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.predictions.canonical_json_bytes() == predictions.canonical_json_bytes()
            ):
                return existing
            raise PredictionsPersistenceConflict("conflicting immutable Predictions attempt")
        upstream = self.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=predictions.upstream_model_feature_set_snapshot_id,
            data_quality_snapshot_id=predictions.upstream_data_quality_snapshot_id,
        )
        if upstream.model_feature_set.run_id != validate_run_id(run_id):
            raise PredictionsIntegrityError("Predictions replay run identity mismatch")
        replay = self.assemble(upstream, values, observed_at=predictions.observed_at)
        if replay.canonical_json_bytes() != predictions.canonical_json_bytes():
            raise PredictionsIntegrityError("Predictions snapshot is not deterministically reproducible")
        now = self._now()
        expected = tuple(game.source_game_id for game in upstream.model_feature_set.feature_set.games)
        inventory = PredictionsInputInventoryV1(expected, values, (), ())
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            requested_date=predictions.requested_date,
            as_of_time=predictions.as_of_time,
            observed_at=predictions.observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream,
            outcome=PredictionsAttemptOutcome.ASSEMBLED,
            snapshot_checksum=predictions.checksum,
            inventory=inventory,
            warnings=predictions.warnings,
            created_at=now,
        )
        manifest_artifact: PreModelArtifactV1 | None = None
        snapshot_artifact: PreModelArtifactV1 | None = None
        try:
            with self.database.connect() as connection:
                self._active(connection, run_id, phase_attempt)
            manifest_artifact = publish_predictions_attempt_manifest(
                manifest, self.artifact_root, secret_values=self.secret_values
            )
            snapshot_artifact = write_predictions_artifact(
                predictions, self.artifact_root, secret_values=self.secret_values
            )
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt)
                self._insert_attempt(connection, manifest, manifest_artifact)
                snapshot_id = f"predictions:{predictions.checksum}"
                connection.execute(
                    """
                    INSERT INTO prediction_snapshots(
                      snapshot_id,run_id,phase_attempt,requested_date,as_of_time,observed_at,contract_version,
                      phase_input_checksum,provider_policy_json,provider_policy_checksum,
                      upstream_model_feature_set_snapshot_id,upstream_model_feature_set_checksum,
                      upstream_data_quality_snapshot_id,upstream_data_quality_checksum,input_inventory_checksum,
                      snapshot_checksum,game_count,warning_count,warnings_json,canonical_json,
                      artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        phase_attempt,
                        predictions.requested_date,
                        predictions.as_of_time.isoformat(),
                        predictions.observed_at.isoformat(),
                        predictions.contract_version,
                        phase_input_checksum,
                        canonical_text(predictions.provider_policy.as_dict()),
                        predictions.provider_policy.checksum,
                        predictions.upstream_model_feature_set_snapshot_id,
                        predictions.upstream_model_feature_set_checksum,
                        predictions.upstream_data_quality_snapshot_id,
                        predictions.upstream_data_quality_checksum,
                        predictions.input_inventory_checksum,
                        predictions.checksum,
                        len(predictions.games),
                        len(predictions.warnings),
                        canonical_text(list(predictions.warnings)),
                        predictions.canonical_json_bytes().decode("utf-8"),
                        snapshot_artifact.relpath,
                        snapshot_artifact.checksum,
                        snapshot_artifact.byte_count,
                        now.isoformat(),
                    ),
                )
                for game in predictions.games:
                    connection.execute(
                        """
                        INSERT INTO prediction_games(
                          snapshot_id,run_id,phase_attempt,ordinal,source_game_id,away_team_id,home_team_id,
                          scheduled_start_time,predictive_feature_checksum,upstream_model_feature_game_checksum,
                          provider_identity_json,provider_identity_checksum,home_probability,away_probability,
                          home_lower,home_upper,away_lower,away_upper,generated_at,prediction_sealed_at,
                          evidence_checksum,market_independence_attested,prediction_checksum,canonical_json
                        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        """,
                        (
                            snapshot_id,
                            run_id,
                            phase_attempt,
                            game.ordinal,
                            game.source_game_id,
                            game.away_team_id,
                            game.home_team_id,
                            game.scheduled_start_time.isoformat(),
                            game.predictive_feature_checksum,
                            game.upstream_model_feature_game_checksum,
                            canonical_text(game.provider_policy.as_dict()),
                            game.provider_policy.checksum,
                            game.home_probability,
                            game.away_probability,
                            game.home_lower,
                            game.home_upper,
                            game.away_lower,
                            game.away_upper,
                            game.generated_at.isoformat(),
                            game.sealed_at.isoformat(),
                            game.evidence_checksum,
                            int(game.market_independence_attested),
                            game.checksum,
                            canonical_text(game.as_dict()),
                        ),
                    )
                self._verify_relational(connection, snapshot_id, predictions)
                connection.execute(
                    "UPDATE prediction_snapshots SET sealed_at=? WHERE snapshot_id=?",
                    (self._now().isoformat(), snapshot_id),
                )
        except sqlite3.IntegrityError as exc:
            self._cleanup(snapshot_artifact, manifest_artifact)
            raise PredictionsPersistenceConflict("conflicting immutable Predictions evidence") from exc
        except Exception:
            self._cleanup(snapshot_artifact, manifest_artifact)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    @staticmethod
    def _verify_relational(connection: sqlite3.Connection, snapshot_id: str, predictions: PredictionsV1) -> None:
        rows = connection.execute(
            "SELECT * FROM prediction_games WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)
        ).fetchall()
        if len(rows) != len(predictions.games):
            raise PredictionsIntegrityError("prediction game row count mismatch")
        for row, game in zip(rows, predictions.games, strict=True):
            if (
                int(row["ordinal"]) != game.ordinal
                or str(row["prediction_checksum"]) != game.checksum
                or str(row["canonical_json"]) != canonical_text(game.as_dict())
            ):
                raise PredictionsIntegrityError("prediction relational row mismatch")

    def _cleanup(self, *artifacts: PreModelArtifactV1 | None) -> None:
        for artifact in artifacts:
            if artifact is not None:
                cleanup_owned_artifact(self.artifact_root, artifact)

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        observed_at: datetime,
        outcome: PredictionsAttemptOutcome,
        upstream: PredictionsUpstreamV1,
        inventory: PredictionsInputInventoryV1,
        warnings: tuple[Mapping[str, object], ...],
    ) -> PredictionsAttemptEvidenceV1:
        if outcome is PredictionsAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt cannot be assembled")
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except PredictionsNotFoundError:
            existing = None
        inventory_checksum = inventory.checksum
        if existing is not None:
            if (
                existing.outcome is outcome
                and existing.phase_input_checksum == phase_input_checksum
                and existing.input_inventory_checksum == inventory_checksum
                and existing.warnings == warnings
            ):
                return existing
            raise PredictionsPersistenceConflict("conflicting failed Predictions attempt")
        now = self._now()
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            requested_date=upstream.model_feature_set.feature_set.requested_date,
            as_of_time=upstream.model_feature_set.feature_set.as_of_time,
            observed_at=observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream,
            outcome=outcome,
            snapshot_checksum=None,
            inventory=inventory,
            warnings=warnings,
            created_at=now,
        )
        artifact = publish_predictions_attempt_manifest(manifest, self.artifact_root, secret_values=self.secret_values)
        try:
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt)
                self._insert_attempt(connection, manifest, artifact)
        except sqlite3.IntegrityError as exc:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise PredictionsPersistenceConflict("conflicting failed Predictions attempt") from exc
        except Exception:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _attempt_row(self, run_id: str, attempt: int) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM predictions_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), attempt),
            ).fetchone()
        if row is None:
            raise PredictionsNotFoundError("Predictions attempt not found")
        return row

    def _manifest_from_row(self, row: sqlite3.Row) -> PreModelAttemptManifestV1:
        expected = tuple(json.loads(str(row["expected_game_ids_json"])))
        present = tuple(json.loads(str(row["present_input_checksums_json"])))
        missing = tuple(json.loads(str(row["missing_game_ids_json"])))
        invalid = tuple(dict(value) for value in json.loads(str(row["invalid_inputs_json"])))
        warnings = tuple(dict(value) for value in json.loads(str(row["warnings_json"])))
        return create_predictions_attempt_manifest(
            run_id=str(row["run_id"]),
            phase_attempt=int(row["phase_attempt"]),
            requested_date=str(row["requested_date"]),
            as_of_time=_time(row["as_of_time"], "as_of_time"),
            observed_at=_time(row["observed_at"], "observed_at"),
            phase_input_checksum=str(row["phase_input_checksum"]),
            upstream=(
                PreModelUpstreamIdentityV1(
                    "model_feature_set",
                    str(row["upstream_model_feature_set_snapshot_id"]),
                    str(row["upstream_model_feature_set_checksum"]),
                ),
                PreModelUpstreamIdentityV1(
                    "data_quality",
                    str(row["upstream_data_quality_snapshot_id"]),
                    str(row["upstream_data_quality_checksum"]),
                ),
            ),
            outcome=str(row["outcome"]),
            snapshot_checksum=None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            warnings=warnings,
            created_at=_time(row["created_at"], "created_at"),
            completed_at=_time(row["completed_at"], "completed_at"),
            provider_policy=_policy(row["provider_policy_json"]),
            expected_game_ids=expected,
            present_input_checksums=present,
            missing_game_ids=missing,
            invalid_inputs=invalid,
            input_inventory_checksum=str(row["input_inventory_checksum"]),
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> PreModelAttemptManifestV1:
        row = self._attempt_row(run_id, attempt)
        manifest = self._manifest_from_row(row)
        verify_predictions_attempt_manifest(
            manifest,
            PreModelArtifactV1(
                str(row["evidence_manifest_relpath"]),
                str(row["evidence_manifest_checksum"]),
                int(row["evidence_manifest_byte_count"]),
            ),
            self.artifact_root,
            secret_values=self.secret_values,
        )
        return manifest

    def get_attempt_evidence(self, run_id: str, attempt: int) -> PredictionsAttemptEvidenceV1:
        row = self._attempt_row(run_id, attempt)
        manifest = self.get_attempt_manifest(run_id, attempt)
        return PredictionsAttemptEvidenceV1(
            manifest.run_id,
            manifest.phase_attempt,
            PredictionsAttemptOutcome(manifest.outcome),
            manifest.snapshot_checksum,
            manifest.phase_input_checksum,
            str(row["input_inventory_checksum"]),
            manifest.warnings,
            PreModelArtifactV1(
                str(row["evidence_manifest_relpath"]),
                str(row["evidence_manifest_checksum"]),
                int(row["evidence_manifest_byte_count"]),
            ),
            manifest.created_at,
            manifest.completed_at,
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[PredictionsAttemptEvidenceV1, ...]:
        with self.database.connect() as connection:
            attempts = tuple(
                int(row[0])
                for row in connection.execute(
                    "SELECT phase_attempt FROM predictions_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            )
        return tuple(self.get_attempt_evidence(run_id, attempt) for attempt in attempts)

    def _snapshot_row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT * FROM prediction_snapshots WHERE {where}", values).fetchone()
        if row is None or row["sealed_at"] is None:
            raise PredictionsNotFoundError("sealed Predictions snapshot not found")
        return row

    def _verify_snapshot(self, row: sqlite3.Row) -> PersistedPredictionsV1:
        exact_upstream = self.resolve_upstream_by_snapshot_ids(
            model_feature_set_snapshot_id=str(row["upstream_model_feature_set_snapshot_id"]),
            data_quality_snapshot_id=str(row["upstream_data_quality_snapshot_id"]),
        )
        upstream_model = exact_upstream.model_feature_set
        quality = exact_upstream.data_quality
        if upstream_model.feature_set.checksum != str(
            row["upstream_model_feature_set_checksum"]
        ) or quality.snapshot.checksum != str(row["upstream_data_quality_checksum"]):
            raise PredictionsIntegrityError("historical Predictions upstream mismatch")
        policy = _policy(row["provider_policy_json"])
        with self.database.connect() as connection:
            game_rows = connection.execute(
                "SELECT * FROM prediction_games WHERE snapshot_id=? ORDER BY ordinal", (str(row["snapshot_id"]),)
            ).fetchall()
        games = tuple(
            MoneylinePredictionV1(
                source_game_id=str(game["source_game_id"]),
                ordinal=int(game["ordinal"]),
                away_team_id=str(game["away_team_id"]),
                home_team_id=str(game["home_team_id"]),
                scheduled_start_time=_time(game["scheduled_start_time"], "scheduled_start_time"),
                predictive_feature_checksum=str(game["predictive_feature_checksum"]),
                upstream_model_feature_game_checksum=str(game["upstream_model_feature_game_checksum"]),
                provider_policy=_policy(game["provider_identity_json"]),
                home_probability=float(game["home_probability"]),
                home_lower=float(game["home_lower"]),
                home_upper=float(game["home_upper"]),
                generated_at=_time(game["generated_at"], "generated_at"),
                sealed_at=_time(game["prediction_sealed_at"], "prediction_sealed_at"),
                evidence_checksum=str(game["evidence_checksum"]),
                market_independence_attested=bool(game["market_independence_attested"])
                if isinstance(game["market_independence_attested"], int)
                and game["market_independence_attested"] in {0, 1}
                else game["market_independence_attested"],
                completeness_state=str(json.loads(str(game["canonical_json"])).get("completeness_state", "complete")),
            )
            for game in game_rows
        )
        snapshot = PredictionsV1(
            run_id=str(row["run_id"]),
            requested_date=str(row["requested_date"]),
            as_of_time=_time(row["as_of_time"], "as_of_time"),
            observed_at=_time(row["observed_at"], "observed_at"),
            provider_policy=policy,
            upstream_model_feature_set_snapshot_id=str(row["upstream_model_feature_set_snapshot_id"]),
            upstream_model_feature_set_checksum=str(row["upstream_model_feature_set_checksum"]),
            upstream_data_quality_snapshot_id=str(row["upstream_data_quality_snapshot_id"]),
            upstream_data_quality_checksum=str(row["upstream_data_quality_checksum"]),
            input_inventory_checksum=str(row["input_inventory_checksum"]),
            games=games,
            warnings=tuple(dict(value) for value in json.loads(str(row["warnings_json"]))),
            secret_values=self.secret_values,
        )
        if snapshot.checksum != str(row["snapshot_checksum"]) or snapshot.canonical_json_bytes().decode("utf-8") != str(
            row["canonical_json"]
        ):
            raise PredictionsIntegrityError("Predictions canonical reconstruction mismatch")
        authored, missing = self.load_input_inventory(exact_upstream)
        if missing:
            raise PredictionsIntegrityError("historical Predictions authoring inventory is incomplete")
        replay = self.assemble(
            exact_upstream,
            authored,
            observed_at=snapshot.observed_at,
            provider_policy=policy,
        )
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise PredictionsIntegrityError("historical Predictions deterministic replay mismatch")
        self.get_attempt_evidence(str(row["run_id"]), int(row["phase_attempt"]))
        artifact = PreModelArtifactV1(
            str(row["artifact_relpath"]), str(row["artifact_checksum"]), int(row["artifact_byte_count"])
        )
        verify_predictions_artifact(snapshot, artifact, self.artifact_root, secret_values=self.secret_values)
        return PersistedPredictionsV1(
            str(row["snapshot_id"]),
            str(row["run_id"]),
            int(row["phase_attempt"]),
            snapshot,
            artifact,
            str(row["phase_input_checksum"]),
            _time(row["created_at"], "created_at"),
            _time(row["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedPredictionsV1:
        return self._verify_snapshot(self._snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedPredictionsV1:
        return self._verify_snapshot(
            self._snapshot_row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt))
        )

    def get_latest_for_run(self, run_id: str) -> PersistedPredictionsV1 | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM prediction_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if row is None else self._verify_snapshot(row)
