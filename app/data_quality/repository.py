from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.baseball_intelligence.repository import (
    BaseballIntelligenceRepository,
    PersistedBaseballIntelligenceV1,
)
from app.daily_slate.repository import DailySlateRepository, PersistedDailySlateV1
from app.data_quality.artifact import (
    DataQualityArtifactV1,
    verify_data_quality_artifact,
    write_data_quality_artifact,
)
from app.data_quality.attempt_manifest import (
    DataQualityAttemptManifestArtifactV1,
    DataQualityAttemptManifestV1,
    create_data_quality_attempt_manifest,
    publish_data_quality_attempt_manifest,
    verify_data_quality_attempt_manifest,
)
from app.data_quality.contracts import (
    DataQualityContractError,
    DataQualityDisposition,
    DataQualityPolicyV1,
    DataQualityV1,
)
from app.data_quality.engine import DataQualityAssessmentResultV1, assess_data_quality
from app.database import Database
from app.game_state.repository import GameStateRepository, PersistedGameStateV1
from app.identifiers import validate_run_id
from app.odds_weather.repository import OddsWeatherRepository, PersistedOddsWeatherV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelEvidenceError,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
    json_array,
    row_checksum,
)


class DataQualityAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    ASSESSMENT_FAILED = "assessment_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class DataQualityRepositoryError(RuntimeError):
    pass


class DataQualityNotFoundError(DataQualityRepositoryError):
    pass


class DataQualityPersistenceConflict(DataQualityRepositoryError):
    pass


class DataQualityIntegrityError(DataQualityRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class DataQualityAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    outcome: DataQualityAttemptOutcome
    snapshot_checksum: str | None
    manifest: DataQualityAttemptManifestArtifactV1
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedDataQualityV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    snapshot: DataQualityV1
    artifact: DataQualityArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class DataQualityUpstreamV1:
    slate: PersistedDailySlateV1
    state: PersistedGameStateV1
    baseball_intelligence: PersistedBaseballIntelligenceV1
    odds_weather: PersistedOddsWeatherV1

    def identities(self) -> tuple[PreModelUpstreamIdentityV1, ...]:
        return (
            PreModelUpstreamIdentityV1(
                "daily_slate", self.slate.snapshot_id, self.slate.slate.checksum
            ),
            PreModelUpstreamIdentityV1(
                "game_state", self.state.snapshot_id, self.state.state.checksum
            ),
            PreModelUpstreamIdentityV1(
                "baseball_intelligence_assembly",
                self.baseball_intelligence.snapshot_id,
                self.baseball_intelligence.assembly.checksum,
            ),
            PreModelUpstreamIdentityV1(
                "odds_weather",
                self.odds_weather.snapshot_id,
                self.odds_weather.snapshot.checksum,
            ),
        )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise DataQualityIntegrityError(f"persisted {field} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise DataQualityIntegrityError(f"persisted {field} is invalid") from exc


def data_quality_warning_payload(snapshot: DataQualityV1) -> tuple[dict[str, object], ...]:
    return tuple(
        {"source_game_id": game.source_game_id, **issue.as_dict()}
        for game in snapshot.games
        for issue in game.issues
    )


def data_quality_state(snapshot: DataQualityV1) -> str:
    if snapshot.insufficient_game_count or snapshot.degraded_game_count:
        return "degraded"
    if data_quality_warning_payload(snapshot):
        return "warning"
    return "clear"


class DataQualityRepository:
    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] = _utc_now,
        policy: DataQualityPolicyV1 = DataQualityPolicyV1(),
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self.clock = clock
        self.policy = policy
        self.daily_slate = DailySlateRepository(
            database, secret_values=self.secret_values, clock=clock
        )
        self.game_state = GameStateRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )
        self.baseball_intelligence = BaseballIntelligenceRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )
        self.odds_weather = OddsWeatherRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def resolve_upstream(self, run_id: str) -> DataQualityUpstreamV1:
        safe_run_id = validate_run_id(run_id)
        slate = self.daily_slate.get_latest_daily_slate_for_run(safe_run_id)
        state = self.game_state.get_latest_game_state_for_run(safe_run_id)
        bia = self.baseball_intelligence.get_latest_for_run(safe_run_id)
        odds_weather = self.odds_weather.get_latest_for_run(safe_run_id)
        if slate is None or state is None or bia is None or odds_weather is None:
            raise DataQualityIntegrityError("Data Quality requires sealed phases 1 through 4")
        return self._validate_upstream(
            safe_run_id, DataQualityUpstreamV1(slate, state, bia, odds_weather)
        )

    def resolve_upstream_by_snapshot_ids(
        self,
        run_id: str,
        *,
        daily_slate_snapshot_id: str,
        game_state_snapshot_id: str,
        baseball_intelligence_snapshot_id: str,
        odds_weather_snapshot_id: str,
    ) -> DataQualityUpstreamV1:
        """Resolve immutable historical lineage by the IDs stored on its snapshot."""

        safe_run_id = validate_run_id(run_id)
        upstream = DataQualityUpstreamV1(
            self.daily_slate.get_daily_slate_snapshot(daily_slate_snapshot_id),
            self.game_state.get_game_state_snapshot(game_state_snapshot_id),
            self.baseball_intelligence.get_by_snapshot_id(
                baseball_intelligence_snapshot_id
            ),
            self.odds_weather.get_by_snapshot_id(odds_weather_snapshot_id),
        )
        return self._validate_upstream(safe_run_id, upstream)

    @staticmethod
    def _validate_upstream(
        run_id: str, upstream: DataQualityUpstreamV1
    ) -> DataQualityUpstreamV1:
        slate = upstream.slate
        state = upstream.state
        bia = upstream.baseball_intelligence
        odds_weather = upstream.odds_weather
        if any(value.run_id != run_id for value in (slate, state, bia, odds_weather)):
            raise DataQualityIntegrityError("upstream run identity is inconsistent")
        requested = {
            slate.slate.requested_date,
            state.state.requested_date,
            bia.assembly.requested_date,
            odds_weather.snapshot.requested_date,
        }
        as_of = {
            slate.slate.as_of_time,
            state.state.as_of_time,
            bia.assembly.as_of_time,
            odds_weather.snapshot.as_of_time,
        }
        if len(requested) != 1 or len(as_of) != 1:
            raise DataQualityIntegrityError("upstream date/as-of lineage is inconsistent")
        if (
            state.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or bia.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or bia.upstream_game_state_snapshot_id != state.snapshot_id
            or odds_weather.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or odds_weather.upstream_game_state_snapshot_id != state.snapshot_id
            or odds_weather.upstream_baseball_intelligence_snapshot_id != bia.snapshot_id
        ):
            raise DataQualityIntegrityError("upstream snapshot lineage is inconsistent")
        return upstream

    def assess_for_run(
        self, run_id: str, *, observed_at: datetime
    ) -> tuple[DataQualityAssessmentResultV1, DataQualityUpstreamV1]:
        upstream = self.resolve_upstream(run_id)
        result = assess_data_quality(
            slate=upstream.slate.slate,
            game_state=upstream.state.state,
            baseball_intelligence=upstream.baseball_intelligence.assembly,
            odds_weather=upstream.odds_weather.snapshot,
            observed_at=observed_at,
            policy=self.policy,
        )
        return result, upstream

    @staticmethod
    def _active_phase(
        connection: sqlite3.Connection,
        run_id: str,
        phase_attempt: int,
        requested_date: str,
        as_of_time: datetime,
    ) -> None:
        row = connection.execute(
            """
            SELECT 1 FROM pipeline_runs run
            JOIN pipeline_run_phases phase ON phase.run_id=run.run_id
            WHERE run.run_id=? AND run.requested_date=? AND run.as_of_time=?
              AND phase.phase_key='data_quality' AND phase.status='running'
              AND phase.attempt_count=?
            """,
            (run_id, requested_date, as_of_time.isoformat(), phase_attempt),
        ).fetchone()
        if row is None:
            raise DataQualityPersistenceConflict(
                "Data Quality persistence requires the active controller attempt"
            )

    def _manifest(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        snapshot: DataQualityV1,
        upstream: DataQualityUpstreamV1,
        phase_input_checksum: str,
        outcome: DataQualityAttemptOutcome,
        snapshot_checksum: str | None,
        warnings: tuple[Mapping[str, object], ...],
        created_at: datetime,
        completed_at: datetime,
    ) -> DataQualityAttemptManifestV1:
        return create_data_quality_attempt_manifest(
            run_id=run_id,
            phase_attempt=phase_attempt,
            requested_date=snapshot.requested_date,
            as_of_time=snapshot.as_of_time,
            observed_at=snapshot.observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream.identities(),
            outcome=outcome.value,
            snapshot_checksum=snapshot_checksum,
            warnings=warnings,
            created_at=created_at,
            completed_at=completed_at,
            secret_values=self.secret_values,
        )

    def persist_assessment(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        result: DataQualityAssessmentResultV1,
    ) -> PersistedDataQualityV1:
        snapshot = result.snapshot
        try:
            retained = self.get_for_run_attempt(run_id, phase_attempt)
        except DataQualityNotFoundError:
            retained = None
        if retained is not None:
            if (
                retained.phase_input_checksum == phase_input_checksum
                and retained.snapshot.canonical_json_bytes()
                == snapshot.canonical_json_bytes()
            ):
                return retained
            raise DataQualityPersistenceConflict(
                "conflicting immutable Data Quality attempt"
            )
        upstream = self.resolve_upstream(run_id)
        replay = assess_data_quality(
            slate=upstream.slate.slate,
            game_state=upstream.state.state,
            baseball_intelligence=upstream.baseball_intelligence.assembly,
            odds_weather=upstream.odds_weather.snapshot,
            observed_at=snapshot.observed_at,
            policy=self.policy,
        ).snapshot
        if replay.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise DataQualityIntegrityError("Data Quality result is not reproducible")
        warnings = data_quality_warning_payload(snapshot)
        created_at = self._now()
        manifest = self._manifest(
            run_id=run_id,
            phase_attempt=phase_attempt,
            snapshot=snapshot,
            upstream=upstream,
            phase_input_checksum=phase_input_checksum,
            outcome=DataQualityAttemptOutcome.ASSEMBLED,
            snapshot_checksum=snapshot.checksum,
            warnings=warnings,
            created_at=created_at,
            completed_at=created_at,
        )
        manifest_artifact: DataQualityAttemptManifestArtifactV1 | None = None
        snapshot_artifact: DataQualityArtifactV1 | None = None
        try:
            with self.database.connect() as connection:
                self._active_phase(
                    connection,
                    run_id,
                    phase_attempt,
                    snapshot.requested_date,
                    snapshot.as_of_time,
                )
            manifest_artifact = publish_data_quality_attempt_manifest(
                manifest, self.artifact_root, secret_values=self.secret_values
            )
            snapshot_artifact = write_data_quality_artifact(
                snapshot, self.artifact_root, secret_values=self.secret_values
            )
            with self.database.connect(write=True) as connection:
                self._active_phase(
                    connection,
                    run_id,
                    phase_attempt,
                    snapshot.requested_date,
                    snapshot.as_of_time,
                )
                self._insert_attempt(connection, manifest, manifest_artifact)
                snapshot_id = f"data-quality:{snapshot.checksum}"
                connection.execute(
                    """
                    INSERT INTO data_quality_snapshots(
                      snapshot_id,run_id,phase_attempt,requested_date,as_of_time,observed_at,
                      contract_version,policy_version,phase_input_checksum,
                      upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
                      upstream_game_state_snapshot_id,upstream_game_state_checksum,
                      upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
                      upstream_odds_weather_snapshot_id,upstream_odds_weather_checksum,
                      snapshot_checksum,quality_state,warnings_json,warning_count,canonical_json,
                      game_count,issue_count,artifact_relpath,artifact_checksum,artifact_byte_count,
                      sealed_at,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        phase_attempt,
                        snapshot.requested_date,
                        snapshot.as_of_time.isoformat(),
                        snapshot.observed_at.isoformat(),
                        snapshot.contract_version,
                        snapshot.policy_version,
                        phase_input_checksum,
                        upstream.slate.snapshot_id,
                        upstream.slate.slate.checksum,
                        upstream.state.snapshot_id,
                        upstream.state.state.checksum,
                        upstream.baseball_intelligence.snapshot_id,
                        upstream.baseball_intelligence.assembly.checksum,
                        upstream.odds_weather.snapshot_id,
                        upstream.odds_weather.snapshot.checksum,
                        snapshot.checksum,
                        data_quality_state(snapshot),
                        canonical_text(list(warnings)),
                        len(warnings),
                        snapshot.canonical_json_bytes().decode("utf-8"),
                        len(snapshot.games),
                        sum(len(game.issues) for game in snapshot.games),
                        snapshot_artifact.relpath,
                        snapshot_artifact.checksum,
                        snapshot_artifact.byte_count,
                        created_at.isoformat(),
                    ),
                )
                self._insert_children(connection, snapshot_id, run_id, phase_attempt, snapshot)
                self._verify_relational(connection, snapshot_id, snapshot)
                connection.execute(
                    "UPDATE data_quality_snapshots SET sealed_at=? WHERE snapshot_id=?",
                    (self._now().isoformat(), snapshot_id),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._idempotent_existing(
                run_id, phase_attempt, phase_input_checksum, snapshot
            )
            if existing is not None:
                return existing
            self._cleanup(snapshot_artifact, manifest_artifact)
            raise DataQualityPersistenceConflict(
                "conflicting immutable Data Quality attempt"
            ) from exc
        except Exception:
            self._cleanup(snapshot_artifact, manifest_artifact)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(
        self,
        snapshot_artifact: DataQualityArtifactV1 | None,
        manifest_artifact: DataQualityAttemptManifestArtifactV1 | None,
    ) -> None:
        for artifact in (snapshot_artifact, manifest_artifact):
            if artifact is not None:
                cleanup_owned_artifact(
                    self.artifact_root,
                    PreModelArtifactV1(
                        artifact.relpath,
                        artifact.checksum,
                        artifact.byte_count,
                        artifact.created,
                    ),
                )

    @staticmethod
    def _insert_attempt(
        connection: sqlite3.Connection,
        manifest: DataQualityAttemptManifestV1,
        artifact: DataQualityAttemptManifestArtifactV1,
    ) -> None:
        upstream = {value.phase_key: value for value in manifest.upstream}
        connection.execute(
            """
            INSERT INTO data_quality_attempt_evidence(
              run_id,phase_attempt,requested_date,as_of_time,observed_at,phase_input_checksum,
              upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
              upstream_game_state_snapshot_id,upstream_game_state_checksum,
              upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
              upstream_odds_weather_snapshot_id,upstream_odds_weather_checksum,
              outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,
              evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                manifest.run_id,
                manifest.phase_attempt,
                manifest.requested_date,
                manifest.as_of_time.isoformat(),
                manifest.observed_at.isoformat(),
                manifest.phase_input_checksum,
                upstream["daily_slate"].snapshot_id,
                upstream["daily_slate"].checksum,
                upstream["game_state"].snapshot_id,
                upstream["game_state"].checksum,
                upstream["baseball_intelligence_assembly"].snapshot_id,
                upstream["baseball_intelligence_assembly"].checksum,
                upstream["odds_weather"].snapshot_id,
                upstream["odds_weather"].checksum,
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

    @staticmethod
    def _insert_children(
        connection: sqlite3.Connection,
        snapshot_id: str,
        run_id: str,
        phase_attempt: int,
        snapshot: DataQualityV1,
    ) -> None:
        for ordinal, game in enumerate(snapshot.games, 1):
            connection.execute(
                """
                INSERT INTO data_quality_games(
                  snapshot_id,run_id,phase_attempt,ordinal,edge_event_id,daily_mlb_game_id,
                  source_game_id,away_team_id,home_team_id,disposition,model_ready,
                  upstream_daily_slate_game_checksum,upstream_game_state_game_checksum,
                  upstream_baseball_intelligence_game_checksum,upstream_odds_weather_game_checksum,
                  issue_count,canonical_json,row_checksum
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    run_id,
                    phase_attempt,
                    ordinal,
                    game.edge_event_id,
                    game.daily_mlb_game_id,
                    game.source_game_id,
                    game.away_team_id,
                    game.home_team_id,
                    game.disposition.value,
                    int(game.disposition is not DataQualityDisposition.INSUFFICIENT),
                    game.upstream_daily_slate_game_checksum,
                    game.upstream_game_state_game_checksum,
                    game.upstream_baseball_intelligence_game_checksum,
                    game.upstream_odds_weather_game_checksum,
                    len(game.issues),
                    canonical_text(game.as_dict()),
                    game.checksum,
                ),
            )
            for issue_ordinal, issue in enumerate(game.issues, 1):
                payload = issue.as_dict()
                connection.execute(
                    "INSERT INTO data_quality_issues VALUES (?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        game.source_game_id,
                        issue_ordinal,
                        issue.code,
                        issue.severity.value,
                        issue.domain.value,
                        canonical_text(payload),
                        row_checksum(payload),
                    ),
                )

    @staticmethod
    def _verify_relational(
        connection: sqlite3.Connection,
        snapshot_id: str,
        snapshot: DataQualityV1,
    ) -> None:
        rows = connection.execute(
            "SELECT * FROM data_quality_games WHERE snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        if len(rows) != len(snapshot.games):
            raise DataQualityIntegrityError("Data Quality game row count mismatch")
        for ordinal, (row, game) in enumerate(zip(rows, snapshot.games, strict=True), 1):
            if (
                int(row["ordinal"]) != ordinal
                or str(row["row_checksum"]) != game.checksum
                or str(row["canonical_json"]) != canonical_text(game.as_dict())
            ):
                raise DataQualityIntegrityError("Data Quality relational game mismatch")
            issues = connection.execute(
                "SELECT canonical_json,row_checksum FROM data_quality_issues WHERE snapshot_id=? AND source_game_id=? ORDER BY ordinal",
                (snapshot_id, game.source_game_id),
            ).fetchall()
            expected = tuple(game.issues)
            if len(issues) != len(expected):
                raise DataQualityIntegrityError("Data Quality issue row count mismatch")
            for issue_row, issue in zip(issues, expected, strict=True):
                if str(issue_row["canonical_json"]) != canonical_text(issue.as_dict()):
                    raise DataQualityIntegrityError("Data Quality issue evidence mismatch")

    def _idempotent_existing(
        self,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        snapshot: DataQualityV1,
    ) -> PersistedDataQualityV1 | None:
        try:
            existing = self.get_for_run_attempt(run_id, phase_attempt)
        except DataQualityNotFoundError:
            return None
        if (
            existing.phase_input_checksum == phase_input_checksum
            and existing.snapshot.canonical_json_bytes() == snapshot.canonical_json_bytes()
        ):
            return existing
        return None

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        observed_at: datetime,
        outcome: DataQualityAttemptOutcome | str,
        warnings: Iterable[Mapping[str, object]],
        upstream: DataQualityUpstreamV1 | None = None,
    ) -> DataQualityAttemptEvidenceV1:
        selected_outcome = DataQualityAttemptOutcome(outcome)
        if selected_outcome is DataQualityAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt outcome cannot be assembled")
        safe_warnings = tuple(dict(value) for value in warnings)
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except DataQualityNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.outcome is selected_outcome
                and existing.warnings == safe_warnings
                and existing.observed_at == aware_utc(observed_at, "observed_at")
            ):
                return existing
            raise DataQualityPersistenceConflict("conflicting failed attempt")
        upstream = self.resolve_upstream(run_id) if upstream is None else upstream
        self._validate_upstream(validate_run_id(run_id), upstream)
        observed = aware_utc(observed_at, "observed_at")
        now = self._now()
        manifest = create_data_quality_attempt_manifest(
            run_id=run_id,
            phase_attempt=phase_attempt,
            requested_date=upstream.slate.slate.requested_date,
            as_of_time=upstream.slate.slate.as_of_time,
            observed_at=observed,
            phase_input_checksum=phase_input_checksum,
            upstream=upstream.identities(),
            outcome=selected_outcome.value,
            snapshot_checksum=None,
            warnings=safe_warnings,
            created_at=now,
            completed_at=now,
            secret_values=self.secret_values,
        )
        artifact = publish_data_quality_attempt_manifest(
            manifest, self.artifact_root, secret_values=self.secret_values
        )
        try:
            with self.database.connect(write=True) as connection:
                self._active_phase(
                    connection,
                    run_id,
                    phase_attempt,
                    upstream.slate.slate.requested_date,
                    upstream.slate.slate.as_of_time,
                )
                self._insert_attempt(connection, manifest, artifact)
        except sqlite3.IntegrityError as exc:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.outcome is selected_outcome
                and existing.warnings == safe_warnings
            ):
                return existing
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise DataQualityPersistenceConflict("conflicting failed attempt") from exc
        except Exception:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _manifest_from_row(self, row: sqlite3.Row) -> DataQualityAttemptManifestV1:
        upstream = (
            PreModelUpstreamIdentityV1("daily_slate", str(row["upstream_daily_slate_snapshot_id"]), str(row["upstream_daily_slate_checksum"])),
            PreModelUpstreamIdentityV1("game_state", str(row["upstream_game_state_snapshot_id"]), str(row["upstream_game_state_checksum"])),
            PreModelUpstreamIdentityV1("baseball_intelligence_assembly", str(row["upstream_baseball_intelligence_snapshot_id"]), str(row["upstream_baseball_intelligence_checksum"])),
            PreModelUpstreamIdentityV1("odds_weather", str(row["upstream_odds_weather_snapshot_id"]), str(row["upstream_odds_weather_checksum"])),
        )
        warnings = tuple(dict(value) for value in json_array(str(row["warnings_json"]), "warnings"))
        return create_data_quality_attempt_manifest(
            run_id=str(row["run_id"]),
            phase_attempt=int(row["phase_attempt"]),
            requested_date=str(row["requested_date"]),
            as_of_time=_parse_time(row["as_of_time"], "as_of_time"),
            observed_at=_parse_time(row["observed_at"], "observed_at"),
            phase_input_checksum=str(row["phase_input_checksum"]),
            upstream=upstream,
            outcome=str(row["outcome"]),
            snapshot_checksum=None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            warnings=warnings,
            created_at=_parse_time(row["created_at"], "created_at"),
            completed_at=_parse_time(row["completed_at"], "completed_at"),
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, phase_attempt: int) -> DataQualityAttemptManifestV1:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_quality_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), phase_attempt),
            ).fetchone()
        if row is None:
            raise DataQualityNotFoundError("Data Quality attempt not found")
        manifest = self._manifest_from_row(row)
        verify_data_quality_attempt_manifest(
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

    def get_attempt_evidence(self, run_id: str, phase_attempt: int) -> DataQualityAttemptEvidenceV1:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_quality_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (validate_run_id(run_id), phase_attempt),
            ).fetchone()
        if row is None:
            raise DataQualityNotFoundError("Data Quality attempt not found")
        manifest = self.get_attempt_manifest(run_id, phase_attempt)
        return DataQualityAttemptEvidenceV1(
            run_id=manifest.run_id,
            phase_attempt=manifest.phase_attempt,
            requested_date=manifest.requested_date,
            as_of_time=manifest.as_of_time,
            observed_at=manifest.observed_at,
            phase_input_checksum=manifest.phase_input_checksum,
            outcome=DataQualityAttemptOutcome(manifest.outcome),
            snapshot_checksum=manifest.snapshot_checksum,
            manifest=PreModelArtifactV1(
                str(row["evidence_manifest_relpath"]),
                str(row["evidence_manifest_checksum"]),
                int(row["evidence_manifest_byte_count"]),
            ),
            warnings=manifest.warnings,
            created_at=manifest.created_at,
            completed_at=manifest.completed_at,
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[DataQualityAttemptEvidenceV1, ...]:
        with self.database.connect() as connection:
            attempts = [
                int(row[0])
                for row in connection.execute(
                    "SELECT phase_attempt FROM data_quality_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                    (validate_run_id(run_id),),
                ).fetchall()
            ]
        return tuple(self.get_attempt_evidence(run_id, value) for value in attempts)

    def _load_snapshot_row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(
                f"SELECT * FROM data_quality_snapshots WHERE {where}", values
            ).fetchone()
        if row is None or row["sealed_at"] is None:
            raise DataQualityNotFoundError("sealed Data Quality snapshot not found")
        return row

    def _verify_snapshot(self, row: sqlite3.Row) -> PersistedDataQualityV1:
        run_id = str(row["run_id"])
        upstream = self.resolve_upstream_by_snapshot_ids(
            run_id,
            daily_slate_snapshot_id=str(row["upstream_daily_slate_snapshot_id"]),
            game_state_snapshot_id=str(row["upstream_game_state_snapshot_id"]),
            baseball_intelligence_snapshot_id=str(row["upstream_baseball_intelligence_snapshot_id"]),
            odds_weather_snapshot_id=str(row["upstream_odds_weather_snapshot_id"]),
        )
        expected_checksums = (
            upstream.slate.slate.checksum,
            upstream.state.state.checksum,
            upstream.baseball_intelligence.assembly.checksum,
            upstream.odds_weather.snapshot.checksum,
        )
        stored_checksums = tuple(
            str(row[name])
            for name in (
                "upstream_daily_slate_checksum",
                "upstream_game_state_checksum",
                "upstream_baseball_intelligence_checksum",
                "upstream_odds_weather_checksum",
            )
        )
        if stored_checksums != expected_checksums:
            raise DataQualityIntegrityError("historical upstream checksum lineage mismatch")
        if str(row["policy_version"]) != self.policy.policy_version:
            raise DataQualityIntegrityError("historical Data Quality policy is unavailable")
        snapshot = assess_data_quality(
            slate=upstream.slate.slate,
            game_state=upstream.state.state,
            baseball_intelligence=upstream.baseball_intelligence.assembly,
            odds_weather=upstream.odds_weather.snapshot,
            observed_at=_parse_time(row["observed_at"], "observed_at"),
            policy=self.policy,
        ).snapshot
        if (
            str(row["snapshot_id"]) != f"data-quality:{snapshot.checksum}"
            or str(row["snapshot_checksum"]) != snapshot.checksum
            or str(row["canonical_json"]) != snapshot.canonical_json_bytes().decode("utf-8")
        ):
            raise DataQualityIntegrityError("Data Quality canonical snapshot mismatch")
        with self.database.connect() as connection:
            self._verify_relational(connection, str(row["snapshot_id"]), snapshot)
        artifact = DataQualityArtifactV1(
            str(row["artifact_relpath"]),
            str(row["artifact_checksum"]),
            int(row["artifact_byte_count"]),
        )
        try:
            verify_data_quality_artifact(
                snapshot,
                artifact,
                self.artifact_root,
                secret_values=self.secret_values,
            )
        except (PreModelEvidenceError, DataQualityContractError) as exc:
            raise DataQualityIntegrityError(
                "Data Quality artifact verification failed"
            ) from exc
        attempt = self.get_attempt_evidence(run_id, int(row["phase_attempt"]))
        if attempt.snapshot_checksum != snapshot.checksum:
            raise DataQualityIntegrityError("Data Quality attempt/snapshot mismatch")
        return PersistedDataQualityV1(
            snapshot_id=str(row["snapshot_id"]),
            run_id=run_id,
            phase_attempt=int(row["phase_attempt"]),
            snapshot=snapshot,
            artifact=artifact,
            phase_input_checksum=str(row["phase_input_checksum"]),
            created_at=_parse_time(row["created_at"], "created_at"),
            sealed_at=_parse_time(row["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedDataQualityV1:
        return self._verify_snapshot(self._load_snapshot_row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, phase_attempt: int) -> PersistedDataQualityV1:
        return self._verify_snapshot(
            self._load_snapshot_row(
                "run_id=? AND phase_attempt=?", (validate_run_id(run_id), phase_attempt)
            )
        )

    def get_latest_for_run(self, run_id: str) -> PersistedDataQualityV1 | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM data_quality_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1",
                (validate_run_id(run_id),),
            ).fetchone()
        return None if row is None else self._verify_snapshot(row)
