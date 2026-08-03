from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

from app.data_quality.repository import (
    DataQualityRepository,
    PersistedDataQualityV1,
    data_quality_warning_payload,
)
from app.database import Database
from app.identifiers import validate_run_id
from app.matchup_packet.artifact import (
    MatchupPacketArtifactV1,
    verify_matchup_packet_artifact,
    write_matchup_packet_artifact,
)
from app.matchup_packet.assembly import assemble_matchup_packet
from app.matchup_packet.attempt_manifest import (
    MatchupPacketAttemptManifestArtifactV1,
    MatchupPacketAttemptManifestV1,
    create_matchup_packet_attempt_manifest,
    publish_matchup_packet_attempt_manifest,
    verify_matchup_packet_attempt_manifest,
)
from app.matchup_packet.contracts import MatchupPacketV1
from app.pre_model_evidence import (
    PreModelArtifactV1,
    PreModelEvidenceError,
    PreModelUpstreamIdentityV1,
    aware_utc,
    canonical_text,
    cleanup_owned_artifact,
    json_array,
)

MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION = "DSE_MATCHUP_PACKET_ASSEMBLY_POLICY_V1"


class MatchupPacketAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    INPUT_FAILED = "input_failed"
    ASSEMBLY_FAILED = "assembly_failed"
    PERSISTENCE_FAILED = "persistence_failed"


class MatchupPacketRepositoryError(RuntimeError):
    pass


class MatchupPacketNotFoundError(MatchupPacketRepositoryError):
    pass


class MatchupPacketPersistenceConflict(MatchupPacketRepositoryError):
    pass


class MatchupPacketIntegrityError(MatchupPacketRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class MatchupPacketAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    outcome: MatchupPacketAttemptOutcome
    snapshot_checksum: str | None
    manifest: MatchupPacketAttemptManifestArtifactV1
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedMatchupPacketV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    packet: MatchupPacketV1
    artifact: MatchupPacketArtifactV1
    phase_input_checksum: str
    created_at: datetime
    sealed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _time(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise MatchupPacketIntegrityError(f"persisted {field} is not text")
    try:
        return aware_utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise MatchupPacketIntegrityError(f"persisted {field} is invalid") from exc


class MatchupPacketRepository:
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
        self.data_quality = DataQualityRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=clock,
        )

    def _now(self) -> datetime:
        return aware_utc(self.clock(), "repository clock")

    def _upstream(
        self, run_id: str
    ) -> tuple[PersistedDataQualityV1, tuple[PreModelUpstreamIdentityV1, ...]]:
        quality = self.data_quality.get_latest_for_run(run_id)
        if quality is None:
            raise MatchupPacketIntegrityError("Matchup Packet requires sealed Data Quality")
        chain = self.data_quality.resolve_upstream(run_id)
        identities = (*chain.identities(), PreModelUpstreamIdentityV1("data_quality", quality.snapshot_id, quality.snapshot.checksum))
        return quality, identities

    def assemble_for_run(self, run_id: str, *, observed_at: datetime) -> MatchupPacketV1:
        quality, _ = self._upstream(run_id)
        chain = self.data_quality.resolve_upstream(run_id)
        return assemble_matchup_packet(
            slate=chain.slate.slate,
            game_state=chain.state.state,
            baseball_intelligence=chain.baseball_intelligence.assembly,
            odds_weather=chain.odds_weather.snapshot,
            data_quality=quality.snapshot,
            observed_at=observed_at,
        )

    @staticmethod
    def _active(
        connection: sqlite3.Connection,
        run_id: str,
        attempt: int,
        requested_date: str,
        as_of_time: datetime,
    ) -> None:
        if connection.execute(
            """
            SELECT 1 FROM pipeline_runs run JOIN pipeline_run_phases phase ON phase.run_id=run.run_id
            WHERE run.run_id=? AND run.requested_date=? AND run.as_of_time=?
              AND phase.phase_key='matchup_packet' AND phase.status='running' AND phase.attempt_count=?
            """,
            (run_id, requested_date, as_of_time.isoformat(), attempt),
        ).fetchone() is None:
            raise MatchupPacketPersistenceConflict("Matchup Packet attempt is not active")

    def _manifest(
        self,
        *,
        run_id: str,
        attempt: int,
        packet: MatchupPacketV1,
        phase_input_checksum: str,
        identities: tuple[PreModelUpstreamIdentityV1, ...],
        outcome: MatchupPacketAttemptOutcome,
        snapshot_checksum: str | None,
        warnings: tuple[Mapping[str, object], ...],
        now: datetime,
    ) -> MatchupPacketAttemptManifestV1:
        return create_matchup_packet_attempt_manifest(
            run_id=run_id,
            phase_attempt=attempt,
            requested_date=packet.requested_date,
            as_of_time=packet.as_of_time,
            observed_at=packet.observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream=identities,
            outcome=outcome.value,
            snapshot_checksum=snapshot_checksum,
            warnings=warnings,
            created_at=now,
            completed_at=now,
            secret_values=self.secret_values,
        )

    def persist_packet(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        packet: MatchupPacketV1,
    ) -> PersistedMatchupPacketV1:
        try:
            retained = self.get_for_run_attempt(run_id, phase_attempt)
        except MatchupPacketNotFoundError:
            retained = None
        if retained is not None:
            if (
                retained.phase_input_checksum == phase_input_checksum
                and retained.packet.canonical_json_bytes() == packet.canonical_json_bytes()
            ):
                return retained
            raise MatchupPacketPersistenceConflict(
                "conflicting immutable Matchup Packet attempt"
            )
        quality, identities = self._upstream(run_id)
        replay = self.assemble_for_run(run_id, observed_at=packet.observed_at)
        if replay.canonical_json_bytes() != packet.canonical_json_bytes():
            raise MatchupPacketIntegrityError("Matchup Packet is not reproducible")
        warnings = data_quality_warning_payload(quality.snapshot)
        now = self._now()
        manifest = self._manifest(
            run_id=run_id,
            attempt=phase_attempt,
            packet=packet,
            phase_input_checksum=phase_input_checksum,
            identities=identities,
            outcome=MatchupPacketAttemptOutcome.ASSEMBLED,
            snapshot_checksum=packet.checksum,
            warnings=warnings,
            now=now,
        )
        manifest_artifact: PreModelArtifactV1 | None = None
        packet_artifact: MatchupPacketArtifactV1 | None = None
        try:
            with self.database.connect() as connection:
                self._active(connection, run_id, phase_attempt, packet.requested_date, packet.as_of_time)
            manifest_artifact = publish_matchup_packet_attempt_manifest(manifest, self.artifact_root)
            packet_artifact = write_matchup_packet_artifact(packet, self.artifact_root, secret_values=self.secret_values)
            snapshot_id = f"matchup-packet:{packet.checksum}"
            chain = self.data_quality.resolve_upstream(run_id)
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt, packet.requested_date, packet.as_of_time)
                self._insert_attempt(connection, manifest, manifest_artifact)
                connection.execute(
                    """
                    INSERT INTO matchup_packet_snapshots(
                      snapshot_id,run_id,phase_attempt,requested_date,as_of_time,observed_at,
                      contract_version,assembly_policy_version,phase_input_checksum,
                      upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
                      upstream_game_state_snapshot_id,upstream_game_state_checksum,
                      upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
                      upstream_odds_weather_snapshot_id,upstream_odds_weather_checksum,
                      upstream_data_quality_snapshot_id,upstream_data_quality_checksum,
                      packet_checksum,warnings_json,warning_count,canonical_json,game_count,
                      artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
                    """,
                    (
                        snapshot_id, run_id, phase_attempt, packet.requested_date,
                        packet.as_of_time.isoformat(), packet.observed_at.isoformat(),
                        packet.contract_version, MATCHUP_PACKET_ASSEMBLY_POLICY_VERSION,
                        phase_input_checksum, chain.slate.snapshot_id, chain.slate.slate.checksum,
                        chain.state.snapshot_id, chain.state.state.checksum,
                        chain.baseball_intelligence.snapshot_id, chain.baseball_intelligence.assembly.checksum,
                        chain.odds_weather.snapshot_id, chain.odds_weather.snapshot.checksum,
                        quality.snapshot_id, quality.snapshot.checksum, packet.checksum,
                        canonical_text(list(warnings)), len(warnings),
                        packet.canonical_json_bytes().decode("utf-8"), len(packet.games),
                        packet_artifact.relpath, packet_artifact.checksum,
                        packet_artifact.byte_count, now.isoformat(),
                    ),
                )
                self._insert_games(connection, snapshot_id, run_id, phase_attempt, packet)
                self._verify_games(connection, snapshot_id, packet)
                connection.execute(
                    "UPDATE matchup_packet_snapshots SET sealed_at=? WHERE snapshot_id=?",
                    (self._now().isoformat(), snapshot_id),
                )
        except sqlite3.IntegrityError as exc:
            existing = self._existing(run_id, phase_attempt, phase_input_checksum, packet)
            if existing is not None:
                return existing
            self._cleanup(packet_artifact, manifest_artifact)
            raise MatchupPacketPersistenceConflict("conflicting immutable Matchup Packet") from exc
        except Exception:
            self._cleanup(packet_artifact, manifest_artifact)
            raise
        return self.get_for_run_attempt(run_id, phase_attempt)

    def _cleanup(self, packet: MatchupPacketArtifactV1 | None, manifest: PreModelArtifactV1 | None) -> None:
        for artifact in (packet, manifest):
            if artifact is not None:
                cleanup_owned_artifact(
                    self.artifact_root,
                    PreModelArtifactV1(artifact.relpath, artifact.checksum, artifact.byte_count, artifact.created),
                )

    @staticmethod
    def _insert_attempt(connection: sqlite3.Connection, manifest: MatchupPacketAttemptManifestV1, artifact: PreModelArtifactV1) -> None:
        upstream = {item.phase_key: item for item in manifest.upstream}
        connection.execute(
            """
            INSERT INTO matchup_packet_attempt_evidence(
              run_id,phase_attempt,requested_date,as_of_time,observed_at,phase_input_checksum,
              upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
              upstream_game_state_snapshot_id,upstream_game_state_checksum,
              upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
              upstream_odds_weather_snapshot_id,upstream_odds_weather_checksum,
              upstream_data_quality_snapshot_id,upstream_data_quality_checksum,
              outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,
              evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                manifest.run_id, manifest.phase_attempt, manifest.requested_date,
                manifest.as_of_time.isoformat(), manifest.observed_at.isoformat(),
                manifest.phase_input_checksum,
                upstream["daily_slate"].snapshot_id, upstream["daily_slate"].checksum,
                upstream["game_state"].snapshot_id, upstream["game_state"].checksum,
                upstream["baseball_intelligence_assembly"].snapshot_id,
                upstream["baseball_intelligence_assembly"].checksum,
                upstream["odds_weather"].snapshot_id, upstream["odds_weather"].checksum,
                upstream["data_quality"].snapshot_id, upstream["data_quality"].checksum,
                manifest.outcome, manifest.snapshot_checksum, artifact.relpath,
                artifact.checksum, artifact.byte_count, canonical_text(list(manifest.warnings)),
                len(manifest.warnings), manifest.created_at.isoformat(), manifest.completed_at.isoformat(),
            ),
        )

    @staticmethod
    def _insert_games(connection: sqlite3.Connection, snapshot_id: str, run_id: str, attempt: int, packet: MatchupPacketV1) -> None:
        for ordinal, game in enumerate(packet.games, 1):
            sections = {
                "schedule": game.schedule.checksum,
                "game_state": game.game_state.checksum,
                "baseball_intelligence": game.baseball_intelligence.checksum,
                "odds_weather": game.odds_weather.checksum,
                "data_quality": game.data_quality.checksum,
            }
            connection.execute(
                """
                INSERT INTO matchup_packet_games VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id, run_id, attempt, ordinal, game.edge_event_id,
                    game.daily_mlb_game_id, game.source_game_id, game.away_team_id,
                    game.home_team_id, game.quality_disposition.value,
                    game.schedule.checksum, game.game_state.checksum,
                    game.baseball_intelligence.checksum, game.odds_weather.checksum,
                    game.data_quality.checksum, canonical_text(sections),
                    canonical_text(game.as_dict()), game.checksum,
                ),
            )

    @staticmethod
    def _verify_games(connection: sqlite3.Connection, snapshot_id: str, packet: MatchupPacketV1) -> None:
        rows = connection.execute(
            "SELECT * FROM matchup_packet_games WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)
        ).fetchall()
        if len(rows) != len(packet.games):
            raise MatchupPacketIntegrityError("Matchup Packet game count mismatch")
        for ordinal, (row, game) in enumerate(zip(rows, packet.games, strict=True), 1):
            if int(row["ordinal"]) != ordinal or str(row["row_checksum"]) != game.checksum or str(row["canonical_json"]) != canonical_text(game.as_dict()):
                raise MatchupPacketIntegrityError("Matchup Packet relational game mismatch")

    def _existing(self, run_id: str, attempt: int, checksum: str, packet: MatchupPacketV1) -> PersistedMatchupPacketV1 | None:
        try:
            existing = self.get_for_run_attempt(run_id, attempt)
        except MatchupPacketNotFoundError:
            return None
        return existing if existing.phase_input_checksum == checksum and existing.packet.canonical_json_bytes() == packet.canonical_json_bytes() else None

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        phase_input_checksum: str,
        observed_at: datetime,
        outcome: MatchupPacketAttemptOutcome | str,
        warnings: Iterable[Mapping[str, object]],
    ) -> MatchupPacketAttemptEvidenceV1:
        selected = MatchupPacketAttemptOutcome(outcome)
        if selected is MatchupPacketAttemptOutcome.ASSEMBLED:
            raise ValueError("failed outcome cannot be assembled")
        safe_warnings = tuple(dict(value) for value in warnings)
        try:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
        except MatchupPacketNotFoundError:
            existing = None
        if existing is not None:
            if (
                existing.phase_input_checksum == phase_input_checksum
                and existing.outcome is selected
                and existing.warnings == safe_warnings
                and existing.observed_at == aware_utc(observed_at, "observed_at")
            ):
                return existing
            raise MatchupPacketPersistenceConflict(
                "conflicting failed Matchup Packet"
            )
        packet = self.assemble_for_run(run_id, observed_at=observed_at)
        _, identities = self._upstream(run_id)
        now = self._now()
        manifest = self._manifest(
            run_id=run_id, attempt=phase_attempt, packet=packet,
            phase_input_checksum=phase_input_checksum, identities=identities,
            outcome=selected, snapshot_checksum=None, warnings=safe_warnings, now=now,
        )
        artifact = publish_matchup_packet_attempt_manifest(manifest, self.artifact_root)
        try:
            with self.database.connect(write=True) as connection:
                self._active(connection, run_id, phase_attempt, packet.requested_date, packet.as_of_time)
                self._insert_attempt(connection, manifest, artifact)
        except sqlite3.IntegrityError as exc:
            existing = self.get_attempt_evidence(run_id, phase_attempt)
            if existing.phase_input_checksum == phase_input_checksum and existing.outcome is selected and existing.warnings == safe_warnings:
                return existing
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise MatchupPacketPersistenceConflict("conflicting failed Matchup Packet") from exc
        except Exception:
            cleanup_owned_artifact(self.artifact_root, artifact)
            raise
        return self.get_attempt_evidence(run_id, phase_attempt)

    def _manifest_from_row(self, row: sqlite3.Row) -> MatchupPacketAttemptManifestV1:
        phases = ("daily_slate", "game_state", "baseball_intelligence", "odds_weather", "data_quality")
        keys = ("daily_slate", "game_state", "baseball_intelligence", "odds_weather", "data_quality")
        upstream = tuple(
            PreModelUpstreamIdentityV1(
                "baseball_intelligence_assembly" if phase == "baseball_intelligence" else phase,
                str(row[f"upstream_{key}_snapshot_id"]),
                str(row[f"upstream_{key}_checksum"]),
            )
            for phase, key in zip(phases, keys, strict=True)
        )
        return create_matchup_packet_attempt_manifest(
            run_id=str(row["run_id"]), phase_attempt=int(row["phase_attempt"]),
            requested_date=str(row["requested_date"]), as_of_time=_time(row["as_of_time"], "as_of_time"),
            observed_at=_time(row["observed_at"], "observed_at"), phase_input_checksum=str(row["phase_input_checksum"]),
            upstream=upstream, outcome=str(row["outcome"]),
            snapshot_checksum=None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            warnings=tuple(dict(value) for value in json_array(str(row["warnings_json"]), "warnings")),
            created_at=_time(row["created_at"], "created_at"), completed_at=_time(row["completed_at"], "completed_at"),
            secret_values=self.secret_values,
        )

    def get_attempt_manifest(self, run_id: str, attempt: int) -> MatchupPacketAttemptManifestV1:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM matchup_packet_attempt_evidence WHERE run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)).fetchone()
        if row is None:
            raise MatchupPacketNotFoundError("Matchup Packet attempt not found")
        manifest = self._manifest_from_row(row)
        verify_matchup_packet_attempt_manifest(
            manifest,
            PreModelArtifactV1(str(row["evidence_manifest_relpath"]), str(row["evidence_manifest_checksum"]), int(row["evidence_manifest_byte_count"])),
            self.artifact_root,
        )
        return manifest

    def get_attempt_evidence(self, run_id: str, attempt: int) -> MatchupPacketAttemptEvidenceV1:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM matchup_packet_attempt_evidence WHERE run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)).fetchone()
        if row is None:
            raise MatchupPacketNotFoundError("Matchup Packet attempt not found")
        manifest = self.get_attempt_manifest(run_id, attempt)
        return MatchupPacketAttemptEvidenceV1(
            manifest.run_id, manifest.phase_attempt, manifest.requested_date,
            manifest.as_of_time, manifest.observed_at, manifest.phase_input_checksum,
            MatchupPacketAttemptOutcome(manifest.outcome), manifest.snapshot_checksum,
            PreModelArtifactV1(str(row["evidence_manifest_relpath"]), str(row["evidence_manifest_checksum"]), int(row["evidence_manifest_byte_count"])),
            manifest.warnings, manifest.created_at, manifest.completed_at,
        )

    def list_attempt_evidence(self, run_id: str) -> tuple[MatchupPacketAttemptEvidenceV1, ...]:
        with self.database.connect() as connection:
            attempts = [int(row[0]) for row in connection.execute("SELECT phase_attempt FROM matchup_packet_attempt_evidence WHERE run_id=? ORDER BY phase_attempt", (validate_run_id(run_id),)).fetchall()]
        return tuple(self.get_attempt_evidence(run_id, attempt) for attempt in attempts)

    def _row(self, where: str, values: tuple[object, ...]) -> sqlite3.Row:
        with self.database.connect() as connection:
            row = connection.execute(f"SELECT * FROM matchup_packet_snapshots WHERE {where}", values).fetchone()
        if row is None or row["sealed_at"] is None:
            raise MatchupPacketNotFoundError("sealed Matchup Packet not found")
        return row

    def _verify(self, row: sqlite3.Row) -> PersistedMatchupPacketV1:
        packet = self.assemble_for_run(str(row["run_id"]), observed_at=_time(row["observed_at"], "observed_at"))
        if str(row["snapshot_id"]) != f"matchup-packet:{packet.checksum}" or str(row["canonical_json"]) != packet.canonical_json_bytes().decode("utf-8"):
            raise MatchupPacketIntegrityError("Matchup Packet canonical evidence mismatch")
        with self.database.connect() as connection:
            self._verify_games(connection, str(row["snapshot_id"]), packet)
        artifact = MatchupPacketArtifactV1(str(row["artifact_relpath"]), str(row["artifact_checksum"]), int(row["artifact_byte_count"]))
        try:
            verify_matchup_packet_artifact(packet, artifact, self.artifact_root)
        except PreModelEvidenceError as exc:
            raise MatchupPacketIntegrityError(
                "Matchup Packet artifact verification failed"
            ) from exc
        if self.get_attempt_evidence(str(row["run_id"]), int(row["phase_attempt"])).snapshot_checksum != packet.checksum:
            raise MatchupPacketIntegrityError("Matchup Packet attempt mismatch")
        return PersistedMatchupPacketV1(
            str(row["snapshot_id"]), str(row["run_id"]), int(row["phase_attempt"]),
            packet, artifact, str(row["phase_input_checksum"]),
            _time(row["created_at"], "created_at"), _time(row["sealed_at"], "sealed_at"),
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedMatchupPacketV1:
        return self._verify(self._row("snapshot_id=?", (snapshot_id,)))

    def get_for_run_attempt(self, run_id: str, attempt: int) -> PersistedMatchupPacketV1:
        return self._verify(self._row("run_id=? AND phase_attempt=?", (validate_run_id(run_id), attempt)))

    def get_latest_for_run(self, run_id: str) -> PersistedMatchupPacketV1 | None:
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM matchup_packet_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC LIMIT 1", (validate_run_id(run_id),)).fetchone()
        return None if row is None else self._verify(row)
