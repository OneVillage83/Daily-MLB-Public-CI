from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.daily_slate.contracts import canonical_sha256
from app.database import Database
from app.identifiers import validate_run_id
from app.matchup_packet.repository import MatchupPacketRepository, PersistedMatchupPacketV1
from app.model_feature_set.artifact import (
    ModelFeatureSetArtifactV1,
    verify_model_feature_set_artifact,
    write_model_feature_set_artifact,
)
from app.model_feature_set.attempt_manifest import (
    ModelFeatureSetAttemptManifestV1,
    create_model_feature_set_attempt_manifest,
    publish_model_feature_set_attempt_manifest,
    verify_model_feature_set_attempt_manifest,
)
from app.model_feature_set.builder import build_model_feature_set
from app.model_feature_set.contracts import (
    MODEL_FEATURE_ENCODING_POLICY_VERSION,
    MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
    MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
    ModelFeatureSetV1,
    ModelFeatureSourceV1,
)
from app.odds_weather.contracts import thaw_mapping
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelEvidenceError,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
    json_array,
)
from app.stats.features import FEATURE_VERSION_V3


class ModelFeatureSetAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    TRANSFORMATION_FAILED = "transformation_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class ModelFeatureSetRepositoryError(RuntimeError):
    pass


class ModelFeatureSetNotFoundError(ModelFeatureSetRepositoryError):
    pass


class ModelFeatureSetPersistenceConflict(ModelFeatureSetRepositoryError):
    pass


class ModelFeatureSetIntegrityError(ModelFeatureSetRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class ModelFeatureSetAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    outcome: ModelFeatureSetAttemptOutcome
    snapshot_checksum: str | None
    selected_feature_inventory: tuple[ModelFeatureSourceV1, ...]
    selected_feature_inventory_checksum: str
    manifest: PreModelArtifactV1
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedModelFeatureSetV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    feature_set: ModelFeatureSetV1
    artifact: ModelFeatureSetArtifactV1
    phase_input_checksum: str
    selected_feature_inventory_checksum: str
    created_at: datetime
    sealed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise ModelFeatureSetIntegrityError(f"persisted {field} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise ModelFeatureSetIntegrityError(f"persisted {field} is invalid") from exc


def selected_feature_inventory(feature_set: ModelFeatureSetV1) -> tuple[ModelFeatureSourceV1, ...]:
    return tuple(
        sorted(
            {source for game in feature_set.games for source in game.source_features},
            key=lambda value: (value.feature_snapshot_id, value.feature_checksum),
        )
    )


def selected_feature_inventory_checksum(values: tuple[ModelFeatureSourceV1, ...]) -> str:
    return canonical_sha256([value.as_dict() for value in values])


class ModelFeatureSetRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.matchup_packet = MatchupPacketRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def _upstream(self, run_id: str) -> tuple[PersistedMatchupPacketV1, PreModelUpstreamIdentityV1, PreModelUpstreamIdentityV1]:
        packet = self.matchup_packet.get_latest_for_run(run_id)
        if packet is None:
            raise ModelFeatureSetIntegrityError("Model Feature Set requires sealed Matchup Packet")
        quality = self.matchup_packet.data_quality.get_latest_for_run(run_id)
        if quality is None or packet.packet.upstream_data_quality_checksum != quality.snapshot.checksum:
            raise ModelFeatureSetIntegrityError("Matchup Packet/Data Quality lineage mismatch")
        return (
            packet,
            PreModelUpstreamIdentityV1("data_quality", quality.snapshot_id, quality.snapshot.checksum),
            PreModelUpstreamIdentityV1("matchup_packet", packet.snapshot_id, packet.packet.checksum),
        )

    def build_for_run(self, run_id: str, *, observed_at: datetime) -> ModelFeatureSetV1:
        packet, _, _ = self._upstream(run_id)
        feature_set = build_model_feature_set(packet.packet, observed_at=observed_at)
        if feature_set.upstream_data_quality_checksum is None:
            raise ModelFeatureSetIntegrityError("Model Feature Set must bind Data Quality")
        self._verify_selected_features(feature_set, packet)
        return feature_set

    def _verify_selected_features(self, feature_set: ModelFeatureSetV1, packet: PersistedMatchupPacketV1) -> None:
        inventory = selected_feature_inventory(feature_set)
        with self.database.connect() as connection:
            for source in inventory:
                row = connection.execute(
                    """
                    SELECT feature_checksum,feature_version,entity_kind,feature_as_of,
                           completeness_state,created_at
                    FROM stats_feature_snapshots WHERE feature_snapshot_id=?
                    """,
                    (source.feature_snapshot_id,),
                ).fetchone()
                if row is None:
                    raise ModelFeatureSetIntegrityError("referenced feature snapshot is missing")
                if (
                    str(row["feature_checksum"]) != source.feature_checksum
                    or str(row["feature_version"]) != FEATURE_VERSION_V3
                    or str(row["entity_kind"]) != "player"
                    or str(row["feature_as_of"]) != feature_set.requested_date
                    or str(row["completeness_state"]) not in {"complete", "degraded"}
                    or _time(row["created_at"], "feature created_at") > packet.packet.observed_at
                ):
                    raise ModelFeatureSetIntegrityError("referenced feature snapshot violates frozen PIT lineage")

    @staticmethod
    def _active(connection: sqlite3.Connection, run_id: str, attempt: int, requested_date: str, as_of_time: datetime) -> None:
        if connection.execute(
            """
            SELECT 1 FROM pipeline_runs run JOIN pipeline_run_phases phase ON phase.run_id=run.run_id
            WHERE run.run_id=? AND run.requested_date=? AND run.as_of_time=?
              AND phase.phase_key='model_feature_set' AND phase.status='running' AND phase.attempt_count=?
            """,
            (run_id, requested_date, as_of_time.isoformat(), attempt),
        ).fetchone() is None:
            raise ModelFeatureSetPersistenceConflict("Model Feature Set attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        feature_set: ModelFeatureSetV1,
        phase_input_checksum: str,
        identities: tuple[PreModelUpstreamIdentityV1, ...],
        outcome: ModelFeatureSetAttemptOutcome,
        snapshot_checksum: str | None,
        warnings: tuple[Mapping[str, object], ...],
        now: datetime,
    ) -> ModelFeatureSetAttemptManifestV1:
        return create_model_feature_set_attempt_manifest(
            run_id=run_id, phase_attempt=attempt, requested_date=feature_set.requested_date,
            as_of_time=feature_set.as_of_time, observed_at=feature_set.observed_at,
            phase_input_checksum=phase_input_checksum, upstream=identities,
            outcome=outcome.value, snapshot_checksum=snapshot_checksum,
            warnings=warnings, created_at=now, completed_at=now,
            secret_values=self.secret_values,
        )

    def persist_feature_set(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        feature_set: ModelFeatureSetV1,
    ) -> PersistedModelFeatureSetV1:
        try:
            retained = self.get_for_run_attempt(run_id, phase_attempt)
        except ModelFeatureSetNotFoundError:
            retained = None
        if retained is not None:
            if (
                retained.phase_input_checksum == phase_input_checksum
                and retained.feature_set.canonical_json_bytes()
                == feature_set.canonical_json_bytes()
            ):
                return retained
            raise ModelFeatureSetPersistenceConflict(
                "conflicting immutable Model Feature Set attempt"
            )
        packet, quality_identity, packet_identity = self._upstream(run_id)
        replay = self.build_for_run(run_id, observed_at=feature_set.observed_at)
        if replay.canonical_json_bytes() != feature_set.canonical_json_bytes():
            raise ModelFeatureSetIntegrityError("Model Feature Set is not reproducible")
        inventory = selected_feature_inventory(feature_set)
        inventory_json = [value.as_dict() for value in inventory]
        inventory_checksum = selected_feature_inventory_checksum(inventory)
        warnings = tuple(
            {"source_game_id": game.source_game_id, "issue_code": code}
            for game in feature_set.games
            for code in game.quality_issue_codes
        )
        now = self._now()
        manifest = self._manifest(
            run_id=run_id, attempt=phase_attempt, feature_set=feature_set,
            phase_input_checksum=phase_input_checksum,
            identities=(quality_identity, packet_identity),
            outcome=ModelFeatureSetAttemptOutcome.ASSEMBLED,
            snapshot_checksum=feature_set.checksum, warnings=warnings, now=now,
        )
        manifest_artifact: PreModelArtifactV1 | None = None
        feature_artifact: ModelFeatureSetArtifactV1 | None = None
        try:
            with self.database.connect() as connection:
                self._active(connection, run_id, phase_attempt, feature_set.requested_date, feature_set.as_of_time)
            manifest_artifact = publish_model_feature_set_attempt_manifest(manifest, self.artifact_root)
            feature_artifact = write_model_feature_set_artifact(feature_set, self.artifact_root, secret_values=self.secret_values)
            snapshot_id = f"model-feature-set:{feature_set.checksum}"
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt, feature_set.requested_date, feature_set.as_of_time)
                self._insert_attempt(connection, manifest, manifest_artifact, inventory_json, inventory_checksum)
                connection.execute(
                    """
                    INSERT INTO model_feature_set_snapshots(
                      snapshot_id,run_id,phase_attempt,requested_date,as_of_time,observed_at,
                      contract_version,schema_version,schema_checksum,transformation_policy_version,
                      missing_value_policy_version,encoding_policy_version,feature_version,phase_input_checksum,
                      upstream_data_quality_snapshot_id,upstream_data_quality_checksum,
                      upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum,
                      selected_feature_inventory_json,selected_feature_inventory_checksum,
                      feature_set_checksum,warnings_json,warning_count,canonical_json,game_count,
                      artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        snapshot_id, run_id, phase_attempt, feature_set.requested_date,
                        feature_set.as_of_time.isoformat(), feature_set.observed_at.isoformat(),
                        feature_set.contract_version, feature_set.schema_version, feature_set.schema_checksum,
                        MODEL_FEATURE_TRANSFORMATION_POLICY_VERSION,
                        MODEL_FEATURE_MISSING_VALUE_POLICY_VERSION,
                        MODEL_FEATURE_ENCODING_POLICY_VERSION, FEATURE_VERSION_V3,
                        phase_input_checksum, quality_identity.snapshot_id, quality_identity.checksum,
                        packet.snapshot_id, packet.packet.checksum, canonical_text(inventory_json),
                        inventory_checksum, feature_set.checksum, canonical_text(list(warnings)),
                        len(warnings), feature_set.canonical_json_bytes().decode("utf-8"),
                        len(feature_set.games), feature_artifact.relpath, feature_artifact.checksum,
                        feature_artifact.byte_count, now.isoformat(),
                    ),
                )
                self._insert_children(connection, snapshot_id, run_id, phase_attempt, feature_set)
                self._verify_children(connection, snapshot_id, feature_set)
                connection.execute("UPDATE model_feature_set_snapshots SET sealed_at=? WHERE snapshot_id=?", (self._now().isoformat(), snapshot_id))
        except sqlite3.IntegrityError as exc:
            existing = self._existing(run_id, phase_attempt, phase_input_checksum, feature_set)
            if existing is not None:
                return existing
            self._cleanup(feature_artifact, manifest_artifact)
            raise ModelFeatureSetPersistenceConflict("conflicting immutable Model Feature Set") from exc
        except Exception:
            self._cleanup(feature_artifact, manifest_artifact)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(self, feature: ModelFeatureSetArtifactV1 | None, manifest: PreModelArtifactV1 | None) -> None:
        for artifact in (feature, manifest):
            if artifact is not None:
                cleanup_owned_artifact(self.artifact_root, PreModelArtifactV1(artifact.relpath, artifact.checksum, artifact.byte_count, artifact.created))

    @staticmethod
    def _insert_attempt(connection: sqlite3.Connection, manifest: ModelFeatureSetAttemptManifestV1, artifact: PreModelArtifactV1, inventory: list[dict[str, str]], inventory_checksum: str) -> None:
        upstream = {value.phase_key: value for value in manifest.upstream}
        connection.execute(
            """
            INSERT INTO model_feature_set_attempt_evidence(
              run_id,phase_attempt,requested_date,as_of_time,observed_at,phase_input_checksum,
              upstream_data_quality_snapshot_id,upstream_data_quality_checksum,
              upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum,
              selected_feature_inventory_json,selected_feature_inventory_checksum,
              outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,
              evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                manifest.run_id, manifest.phase_attempt, manifest.requested_date,
                manifest.as_of_time.isoformat(), manifest.observed_at.isoformat(),
                manifest.phase_input_checksum, upstream["data_quality"].snapshot_id,
                upstream["data_quality"].checksum, upstream["matchup_packet"].snapshot_id,
                upstream["matchup_packet"].checksum, canonical_text(inventory), inventory_checksum,
                manifest.outcome, manifest.snapshot_checksum, artifact.relpath, artifact.checksum,
                artifact.byte_count, canonical_text(list(manifest.warnings)), len(manifest.warnings),
                manifest.created_at.isoformat(), manifest.completed_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_children(connection: sqlite3.Connection, snapshot_id: str, run_id: str, attempt: int, feature_set: ModelFeatureSetV1) -> None:
        for ordinal, game in enumerate(feature_set.games, 1):
            connection.execute(
                """
                INSERT INTO model_feature_set_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id, run_id, attempt, ordinal, game.edge_event_id,
                    game.daily_mlb_game_id, game.source_game_id, game.away_team_id,
                    game.home_team_id, game.upstream_matchup_packet_game_checksum,
                    game.quality_disposition.value, canonical_text(list(game.quality_issue_codes)),
                    canonical_text(list(game.feature_values)), canonical_text(list(game.missing_feature_names)),
                    game.available_feature_count, game.predictive_feature_checksum,
                    canonical_text(game.as_dict()), game.checksum,
                ),
            )
            connection.execute(
                "INSERT INTO model_feature_set_market_contexts VALUES (?,?,?,?)",
                (
                    snapshot_id,
                    game.source_game_id,
                    canonical_text(
                        thaw_mapping(game.market_context)
                        if game.market_context is not None
                        else {}
                    ),
                    game.market_context_checksum,
                ),
            )
            for source_ordinal, source in enumerate(game.source_features, 1):
                connection.execute(
                    "INSERT INTO model_feature_set_source_features VALUES (?,?,?,?,?)",
                    (snapshot_id, game.source_game_id, source_ordinal, source.feature_snapshot_id, source.feature_checksum),
                )

    @staticmethod
    def _verify_children(connection: sqlite3.Connection, snapshot_id: str, feature_set: ModelFeatureSetV1) -> None:
        rows = connection.execute("SELECT * FROM model_feature_set_games WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)).fetchall()
        if len(rows) != len(feature_set.games):
            raise ModelFeatureSetIntegrityError("Model Feature Set game count mismatch")
        for ordinal, (row, game) in enumerate(zip(rows, feature_set.games, strict=True), 1):
            if int(row["ordinal"]) != ordinal or str(row["row_checksum"]) != game.checksum or str(row["predictive_feature_checksum"]) != game.predictive_feature_checksum or str(row["canonical_json"]) != canonical_text(game.as_dict()):
                raise ModelFeatureSetIntegrityError("Model Feature Set relational game mismatch")
            market = connection.execute("SELECT * FROM model_feature_set_market_contexts WHERE snapshot_id=? AND source_game_id=?", (snapshot_id, game.source_game_id)).fetchone()
            if (
                market is None
                or str(market["market_context_checksum"])
                != game.market_context_checksum
                or str(market["market_context_json"])
                != canonical_text(
                    thaw_mapping(game.market_context)
                    if game.market_context is not None
                    else {}
                )
            ):
                raise ModelFeatureSetIntegrityError("market context is not independently preserved")
            sources = connection.execute("SELECT feature_snapshot_id,feature_checksum FROM model_feature_set_source_features WHERE snapshot_id=? AND source_game_id=? ORDER BY ordinal", (snapshot_id, game.source_game_id)).fetchall()
            if tuple((str(value[0]), str(value[1])) for value in sources) != tuple((value.feature_snapshot_id, value.feature_checksum) for value in game.source_features):
                raise ModelFeatureSetIntegrityError("selected feature lineage mismatch")

    def _existing(self, run_id: str, attempt: int, checksum: str, feature_set: ModelFeatureSetV1) -> PersistedModelFeatureSetV1 | None:
        try:
            existing = self.get_for_run_attempt(run_id, attempt)
        except ModelFeatureSetNotFoundError:
            return None
        return existing if existing.phase_input_checksum == checksum and existing.feature_set.canonical_json_bytes() == feature_set.canonical_json_bytes() else None

    def persist_failed_attempt(self, *, run_id: str, phase_attempt: int, phase_input_checksum: str, observed_at: datetime, outcome: ModelFeatureSetAttemptOutcome | str, warnings: Iterable[Mapping[str, object]]) -> ModelFeatureSetAttemptEvidenceV1:
        selected = ModelFeatureSetAttemptOutcome(outcome)
        if selected is ModelFeatureSetAttemptOutcome.ASSEMBLED:
            raise ValueError("failed outcome cannot be assembled")
        safe_warnings = tuple(dict(value) for value in warnings)
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except ModelFeatureSetNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.outcome is selected
                and existing.warnings == safe_warnings
                and existing.observed_at == aware_utc(observed_at, "observed_at")
            ):
                return existing
            raise ModelFeatureSetPersistenceConflict(
                "conflicting failed Model Feature Set"
            )
        feature_set = self.build_for_run(run_id, observed_at=observed_at)
        _, quality, packet = self._upstream(run_id)
        inventory = selected_feature_inventory(feature_set)
        inventory_json = [value.as_dict() for value in inventory]
        inventory_checksum = selected_feature_inventory_checksum(inventory)
        now = self._now()
        manifest = self._manifest(run_id=run_id, attempt=phase_attempt, feature_set=feature_set, phase_input_checksum=phase_input_checksum, identities=(quality, packet), outcome=selected, snapshot_checksum=None, warnings=safe_warnings, now=now)
        artifact = publish_model_feature_set_attempt_manifest(manifest, self.artifact_root)
        try:
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt, feature_set.requested_date, feature_set.as_of_time)
                self._insert_attempt(connection, manifest, artifact, inventory_json, inventory_checksum)
        except sqlite3.IntegrityError as exc:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
            if existing.phase_input_checksum == phase_input_checksum and existing.outcome is selected and existing.warnings == safe_warnings:
                return existing
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise ModelFeatureSetPersistenceConflict("conflicting failed Model Feature Set") from exc
        except Exception:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _manifest_from_row(self, row: sqlite3.Row) -> ModelFeatureSetAttemptManifestV1:
        upstream = (
            PreModelUpstreamIdentityV1("data_quality", str(row["upstream_data_quality_snapshot_id"]), str(row["upstream_data_quality_checksum"])),
            PreModelUpstreamIdentityV1("matchup_packet", str(row["upstream_matchup_packet_snapshot_id"]), str(row["upstream_matchup_packet_checksum"])),
        )
        return create_model_feature_set_attempt_manifest(
            run_id=str(row["run_id"]), phase_attempt=int(row["phase_attempt"]), requested_date=str(row["requested_date"]),
            as_of_time=_time(row["as_of_time"], "as_of_time"), observed_at=_time(row["observed_at"], "observed_at"),
            phase_input_checksum=str(row["phase_input_checksum"]), upstream=upstream, outcome=str(row["outcome"]),
            snapshot_checksum=None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            warnings=tuple(dict(value) for value in json_array(str(row["warnings_json"]), "warnings")),
            created_at=_time(row["created_at"], "created_at"), completed_at=_time(row["completed_at"], "completed_at"), secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> ModelFeatureSetAttemptManifestV1:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM model_feature_set_attempt_evidence WHERE run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)).fetchone()
        if row is None:
            raise ModelFeatureSetNotFoundError("Model Feature Set attempt not found")
        manifest = self._manifest_from_row(row)
        verify_model_feature_set_attempt_manifest(manifest, PreModelArtifactV1(str(row["evidence_manifest_relpath"]), str(row["evidence_manifest_checksum"]), int(row["evidence_manifest_byte_count"])), self.artifact_root)
        return manifest

    def get_attempt_evidence(self, run_id: str, attempt: int) -> ModelFeatureSetAttemptEvidenceV1:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM model_feature_set_attempt_evidence WHERE run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)).fetchone()
        if row is None:
            raise ModelFeatureSetNotFoundError("Model Feature Set attempt not found")
        manifest = self.get_attempt_manifest(run_id, attempt)
        raw_inventory = json_array(str(row["selected_feature_inventory_json"]), "selected feature inventory")
        inventory = tuple(ModelFeatureSourceV1(str(value["feature_snapshot_id"]), str(value["feature_checksum"])) for value in raw_inventory if isinstance(value, Mapping))
        if selected_feature_inventory_checksum(inventory) != str(row["selected_feature_inventory_checksum"]):
            raise ModelFeatureSetIntegrityError("selected feature inventory checksum mismatch")
        return ModelFeatureSetAttemptEvidenceV1(
            manifest.run_id, manifest.phase_attempt, manifest.requested_date, manifest.as_of_time,
            manifest.observed_at, manifest.phase_input_checksum, ModelFeatureSetAttemptOutcome(manifest.outcome),
            manifest.snapshot_checksum, inventory, str(row["selected_feature_inventory_checksum"]),
            PreModelArtifactV1(str(row["evidence_manifest_relpath"]), str(row["evidence_manifest_checksum"]), int(row["evidence_manifest_byte_count"])),
            manifest.warnings, manifest.created_at, manifest.completed_at,
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[ModelFeatureSetAttemptEvidenceV1, ...]:
        with self.database.connect() as connection:
            attempts = [int(row[0]) for row in connection.execute("SELECT phase_attempt FROM model_feature_set_attempt_evidence WHERE run_id=? ORDER BY phase_attempt", (validate_run_id(run_id),)).fetchall()]
        return tuple(self.get_attempt_evidence(run_id, value) for value in attempts)

    def _row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT * FROM model_feature_set_snapshots WHERE {where}", values).fetchone()
        if row is None or row["sealed_at"] is None:
            raise ModelFeatureSetNotFoundError("sealed Model Feature Set not found")
        return row

    def _verify(self, row: sqlite3.Row) -> PersistedModelFeatureSetV1:
        feature_set = self.build_for_run(str(row["run_id"]), observed_at=_time(row["observed_at"], "observed_at"))
        if str(row["snapshot_id"]) != f"model-feature-set:{feature_set.checksum}" or str(row["canonical_json"]) != feature_set.canonical_json_bytes().decode("utf-8"):
            raise ModelFeatureSetIntegrityError("Model Feature Set canonical evidence mismatch")
        with self.database.connect() as connection:
            self._verify_children(connection, str(row["snapshot_id"]), feature_set)
        artifact = ModelFeatureSetArtifactV1(str(row["artifact_relpath"]), str(row["artifact_checksum"]), int(row["artifact_byte_count"]))
        try:
            verify_model_feature_set_artifact(
                feature_set, artifact, self.artifact_root
            )
        except PreModelEvidenceError as exc:
            raise ModelFeatureSetIntegrityError(
                "Model Feature Set artifact verification failed"
            ) from exc
        attempt = self.get_attempt_evidence(str(row["run_id"]), int(row["phase_attempt"]))
        if attempt.snapshot_checksum != feature_set.checksum:
            raise ModelFeatureSetIntegrityError("Model Feature Set attempt mismatch")
        return PersistedModelFeatureSetV1(
            str(row["snapshot_id"]), str(row["run_id"]), int(row["phase_attempt"]), feature_set,
            artifact, str(row["phase_input_checksum"]), str(row["selected_feature_inventory_checksum"]),
            _time(row["created_at"], "created_at"), _time(row["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedModelFeatureSetV1:
        return self._verify(self._row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedModelFeatureSetV1:
        return self._verify(self._row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedModelFeatureSetV1 | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM model_feature_set_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1", (validate_run_id(run_id),)).fetchone()
        return None if row is None else self._verify(row)
