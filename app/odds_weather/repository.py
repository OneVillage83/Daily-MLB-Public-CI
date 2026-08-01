"""Durable, offline-verifiable Odds + Weather V1 retained evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.artifacts import UnsafeArtifactPath, resolve_contained_path
from app.baseball_intelligence.repository import (
    BaseballIntelligenceRepository,
    PersistedBaseballIntelligenceV1,
)
from app.daily_slate.contracts import canonical_json_bytes, canonical_sha256
from app.daily_slate.repository import DailySlateRepository, PersistedDailySlateV1
from app.database import Database
from app.game_state.repository import GameStateRepository, PersistedGameStateV1
from app.identifiers import validate_run_id
from app.odds_weather.artifact import (
    OddsWeatherArtifactIntegrityError,
    OddsWeatherArtifactV1,
    publish_odds_weather_artifact,
    verify_odds_weather_artifact,
)
from app.odds_weather.assembly import OddsWeatherAssemblyResultV1, assemble_odds_weather
from app.odds_weather.attempt_manifest import (
    OddsWeatherAttemptManifestArtifactV1,
    OddsWeatherAttemptManifestError,
    OddsWeatherAttemptManifestV1,
    OddsWeatherAttemptOutcome,
    publish_odds_weather_attempt_manifest,
    verify_odds_weather_attempt_manifest,
)
from app.odds_weather.contracts import (
    ODDS_WEATHER_CONTRACT_VERSION,
    OddsAvailability,
    OddsProviderEventV1,
    OddsSnapshotV1,
    OddsWeatherContractError,
    OddsWeatherGameV1,
    OddsWeatherV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    VenueWeatherContextV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
    WeatherRelevance,
    WeatherSnapshotV1,
    WeatherStatus,
)
from app.odds_weather.selector import (
    OddsWeatherRawCaptureV1,
    OddsWeatherRetainedEvidenceInventoryV1,
    OddsWeatherRetainedEvidenceSelector,
    OddsWeatherSelectorError,
    RetainedOddsProviderEventV1,
    RetainedWeatherRevisionV1,
)
from app.redaction import redact_value


class OddsWeatherRepositoryError(RuntimeError):
    """Base error for durable Odds + Weather evidence."""


class OddsWeatherNotFoundError(OddsWeatherRepositoryError):
    pass


class OddsWeatherPersistenceConflict(OddsWeatherRepositoryError):
    pass


class OddsWeatherIntegrityError(OddsWeatherRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class OddsWeatherAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    as_of_time: datetime
    observed_at: datetime
    phase_input_checksum: str
    upstream_daily_slate_snapshot_id: str
    upstream_daily_slate_checksum: str
    upstream_game_state_snapshot_id: str
    upstream_game_state_checksum: str
    upstream_baseball_intelligence_snapshot_id: str
    upstream_baseball_intelligence_checksum: str
    outcome: OddsWeatherAttemptOutcome
    snapshot_checksum: str | None
    manifest: OddsWeatherAttemptManifestArtifactV1
    warnings: tuple[OddsWeatherWarningV1, ...]
    retained_inventory_checksum: str
    created_at: datetime
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedOddsWeatherV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    upstream_daily_slate_snapshot_id: str
    upstream_game_state_snapshot_id: str
    upstream_baseball_intelligence_snapshot_id: str
    snapshot: OddsWeatherV1
    artifact: OddsWeatherArtifactV1
    retained_inventory_checksum: str
    created_at: datetime
    sealed_at: datetime


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise OddsWeatherIntegrityError(f"persisted {field} is not ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OddsWeatherIntegrityError(f"persisted {field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OddsWeatherIntegrityError(f"persisted {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _checksum(value: object, field: str) -> str:
    text = str(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise OddsWeatherIntegrityError(f"persisted {field} is not a SHA-256 checksum")
    return text


def _canonical_text(value: Mapping[str, object]) -> str:
    return canonical_json_bytes(dict(value)).decode("utf-8")


def _mapping_value(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OddsWeatherIntegrityError(f"{field} is not a canonical mapping")
    return value


def _json_object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, str):
        raise OddsWeatherIntegrityError(f"persisted {field} is not JSON")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OddsWeatherIntegrityError(f"persisted {field} is malformed JSON") from exc
    if not isinstance(result, dict):
        raise OddsWeatherIntegrityError(f"persisted {field} must be a JSON object")
    return result


def _json_array(value: object, field: str) -> list[Any]:
    if not isinstance(value, str):
        raise OddsWeatherIntegrityError(f"persisted {field} is not JSON")
    try:
        result = json.loads(value)
    except json.JSONDecodeError as exc:
        raise OddsWeatherIntegrityError(f"persisted {field} is malformed JSON") from exc
    if not isinstance(result, list):
        raise OddsWeatherIntegrityError(f"persisted {field} must be a JSON array")
    return result


def _warning(payload: Mapping[str, object]) -> OddsWeatherWarningV1:
    try:
        return OddsWeatherWarningV1(
            code=str(payload["code"]),
            domain=OddsWeatherWarningDomain(str(payload["domain"])),
            message=str(payload["message"]),
            source_game_id=(
                None if payload.get("source_game_id") is None else str(payload["source_game_id"])
            ),
            provider=None if payload.get("provider") is None else str(payload["provider"]),
            provider_event_id=(
                None
                if payload.get("provider_event_id") is None
                else str(payload["provider_event_id"])
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OddsWeatherIntegrityError("persisted Odds Weather warning is invalid") from exc


def _warning_rows(value: object, field: str) -> tuple[OddsWeatherWarningV1, ...]:
    rows = _json_array(value, field)
    result: list[OddsWeatherWarningV1] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise OddsWeatherIntegrityError(f"persisted {field} contains a non-object")
        result.append(_warning(row))
    return tuple(result)


def _venue(payload: Mapping[str, object]) -> VenueWeatherContextV1:
    association_errors = payload.get("association_errors", [])
    coordinate_errors = payload.get("coordinate_errors", [])
    if not isinstance(association_errors, list) or not isinstance(coordinate_errors, list):
        raise OddsWeatherIntegrityError("persisted venue error inventories are invalid")
    try:
        return VenueWeatherContextV1(
            team_id=str(payload["team_id"]),
            physical_venue_key=None if payload.get("physical_venue_key") is None else str(payload["physical_venue_key"]),
            venue_name=None if payload.get("venue_name") is None else str(payload["venue_name"]),
            latitude=None if payload.get("latitude") is None else float(str(payload["latitude"])),
            longitude=None if payload.get("longitude") is None else float(str(payload["longitude"])),
            timezone_name=None if payload.get("timezone_name") is None else str(payload["timezone_name"]),
            roof_type=str(payload["roof_type"]),
            operational_roof_status=str(payload["operational_roof_status"]),
            roof_verification_state=str(payload["roof_verification_state"]),
            outfield_bearing_degrees=None if payload.get("outfield_bearing_degrees") is None else float(str(payload["outfield_bearing_degrees"])),
            outfield_bearing_verification_state=str(payload["outfield_bearing_verification_state"]),
            metadata_policy_version=None if payload.get("metadata_policy_version") is None else str(payload["metadata_policy_version"]),
            catalog_version=None if payload.get("catalog_version") is None else int(str(payload["catalog_version"])),
            association_errors=tuple(str(item) for item in association_errors),
            coordinate_errors=tuple(str(item) for item in coordinate_errors),
            contract_version=str(payload["contract_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise OddsWeatherIntegrityError("persisted venue weather context is invalid") from exc


class OddsWeatherRepository:
    """Persist and reconstruct only sealed, independently verified Phase 4 evidence."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.daily_slate = DailySlateRepository(
            database,
            secret_values=self.secret_values,
            clock=self._clock,
        )
        self.game_state = GameStateRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=self._clock,
        )
        self.baseball_intelligence = BaseballIntelligenceRepository(
            database,
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
            clock=self._clock,
        )
        self.selector = OddsWeatherRetainedEvidenceSelector(
            artifact_root=self.artifact_root,
            secret_values=self.secret_values,
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise OddsWeatherRepositoryError("repository clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _snapshot_id(checksum: str) -> str:
        return f"odds-weather:{checksum}"

    def _verify_upstream(
        self,
        run_id: str,
    ) -> tuple[
        PersistedDailySlateV1,
        PersistedGameStateV1,
        PersistedBaseballIntelligenceV1,
    ]:
        slate = self.daily_slate.get_latest_daily_slate_for_run(run_id)
        state = self.game_state.get_latest_game_state_for_run(run_id)
        bia = self.baseball_intelligence.get_latest_for_run(run_id)
        if slate is None or state is None or bia is None:
            raise OddsWeatherNotFoundError("sealed Phase 1-3 upstream chain does not exist")
        if (
            state.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or state.state.upstream_daily_slate_checksum != slate.slate.checksum
            or bia.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or bia.upstream_game_state_snapshot_id != state.snapshot_id
            or bia.assembly.upstream_daily_slate_checksum != slate.slate.checksum
            or bia.assembly.upstream_game_state_checksum != state.state.checksum
            or slate.run_id != run_id
            or state.run_id != run_id
            or bia.run_id != run_id
            or slate.slate.requested_date != state.state.requested_date
            or slate.slate.requested_date != bia.assembly.requested_date
            or slate.slate.as_of_time != state.state.as_of_time
            or slate.slate.as_of_time != bia.assembly.as_of_time
            or len(slate.slate.games) != len(state.state.games)
            or len(slate.slate.games) != len(bia.assembly.games)
        ):
            raise OddsWeatherIntegrityError("sealed Phase 1-3 lineage does not reconcile")
        for ordinal, (slate_game, state_game, bia_game) in enumerate(
            zip(slate.slate.games, state.state.games, bia.assembly.games, strict=True),
            start=1,
        ):
            slate_identity = (
                slate_game.edge_event_id,
                slate_game.daily_mlb_game_id,
                slate_game.source_game_id,
                slate_game.away_team_id,
                slate_game.home_team_id,
                slate_game.venue_id,
            )
            state_identity = (
                state_game.edge_event_id,
                state_game.daily_mlb_game_id,
                state_game.source_game_id,
                state_game.away_team_id,
                state_game.home_team_id,
            )
            bia_identity = (
                bia_game.edge_event_id,
                bia_game.daily_mlb_game_id,
                bia_game.source_game_id,
                bia_game.away_team_id,
                bia_game.home_team_id,
                bia_game.venue_id,
            )
            if slate_identity[:-1] != state_identity or slate_identity != bia_identity:
                raise OddsWeatherIntegrityError(
                    f"sealed upstream game ordinal {ordinal} does not reconcile"
                )
            if (
                bia_game.upstream_daily_slate_game_checksum != slate_game.checksum
                or bia_game.upstream_game_state_game_checksum != state_game.checksum
                or bia_game.game_status != state_game.game_status
                or bia_game.away.source_team_id != state_game.away.source_team_id
                or bia_game.home.source_team_id != state_game.home.source_team_id
            ):
                raise OddsWeatherIntegrityError("sealed upstream game lineage disagrees")
        return slate, state, bia

    @staticmethod
    def _event_revisions(events: Iterable[OddsProviderEventV1]) -> tuple[RetainedOddsProviderEventV1, ...]:
        grouped: dict[str, list[OddsProviderEventV1]] = {}
        for event in events:
            grouped.setdefault(event.provider_event_id, []).append(event)
        return tuple(
            RetainedOddsProviderEventV1(ordinal, event)
            for event_id in sorted(grouped)
            for ordinal, event in enumerate(
                sorted(grouped[event_id], key=lambda item: item.retrieved_at),
                start=1,
            )
        )

    @staticmethod
    def _weather_revisions(values: Iterable[WeatherForecastEvidenceV1]) -> tuple[RetainedWeatherRevisionV1, ...]:
        grouped: dict[tuple[str, str], list[WeatherForecastEvidenceV1]] = {}
        for value in values:
            grouped.setdefault((value.source_game_id, value.provider.value), []).append(value)
        return tuple(
            RetainedWeatherRevisionV1(ordinal, value)
            for key in sorted(grouped)
            for ordinal, value in enumerate(
                sorted(grouped[key], key=lambda item: item.retrieved_at),
                start=1,
            )
        )

    def build_inventory(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        observed_at: datetime,
        phase_input_checksum: str,
        raw_captures: Iterable[OddsWeatherRawCaptureV1],
        odds_events: Iterable[OddsProviderEventV1] = (),
        weather_evidence: Iterable[WeatherForecastEvidenceV1] = (),
        source_warnings: Iterable[OddsWeatherWarningV1] = (),
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        safe_run = validate_run_id(run_id)
        slate, state, bia = self._verify_upstream(safe_run)
        inventory = OddsWeatherRetainedEvidenceInventoryV1(
            run_id=safe_run,
            phase_attempt=phase_attempt,
            requested_date=slate.slate.requested_date,
            as_of_time=slate.slate.as_of_time,
            observed_at=observed_at,
            phase_input_checksum=phase_input_checksum,
            upstream_daily_slate_snapshot_id=slate.snapshot_id,
            upstream_daily_slate_checksum=slate.slate.checksum,
            upstream_game_state_snapshot_id=state.snapshot_id,
            upstream_game_state_checksum=state.state.checksum,
            upstream_baseball_intelligence_snapshot_id=bia.snapshot_id,
            upstream_baseball_intelligence_checksum=bia.assembly.checksum,
            raw_captures=tuple(raw_captures),
            provider_events=self._event_revisions(odds_events),
            weather_revisions=self._weather_revisions(weather_evidence),
            source_warnings=tuple(source_warnings),
        )
        self._verify_inventory_files(inventory)
        return inventory

    def assemble_inventory(
        self,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> tuple[OddsWeatherAssemblyResultV1, OddsWeatherRetainedEvidenceInventoryV1]:
        slate, _, bia = self._verify_upstream(inventory.run_id)
        self._verify_inventory_identity(inventory, slate, _, bia)
        result = assemble_odds_weather(
            slate=slate.slate,
            baseball_intelligence=bia.assembly,
            odds_events=inventory.odds_events,
            weather_evidence=inventory.weather_evidence,
            source_warnings=inventory.source_warnings,
            observed_at=inventory.observed_at,
        )
        completed = replace(
            inventory,
            final_warnings=result.snapshot.warnings,
            selected_raw_capture_checksums=result.snapshot.source_raw_capture_checksums,
        )
        return result, completed

    def _verify_inventory_files(self, inventory: OddsWeatherRetainedEvidenceInventoryV1) -> None:
        try:
            for capture in inventory.raw_captures:
                self.selector.verify_raw_capture(
                    capture,
                    run_id=inventory.run_id,
                    requested_date=inventory.requested_date,
                )
        except OddsWeatherSelectorError as exc:
            raise OddsWeatherIntegrityError(
                "retained raw evidence does not verify"
            ) from exc

    def _verify_inventory_identity(
        self,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
    ) -> None:
        expected = (
            slate.run_id,
            slate.slate.requested_date,
            slate.slate.as_of_time,
            slate.snapshot_id,
            slate.slate.checksum,
            state.snapshot_id,
            state.state.checksum,
            bia.snapshot_id,
            bia.assembly.checksum,
        )
        actual = (
            inventory.run_id,
            inventory.requested_date,
            inventory.as_of_time,
            inventory.upstream_daily_slate_snapshot_id,
            inventory.upstream_daily_slate_checksum,
            inventory.upstream_game_state_snapshot_id,
            inventory.upstream_game_state_checksum,
            inventory.upstream_baseball_intelligence_snapshot_id,
            inventory.upstream_baseball_intelligence_checksum,
        )
        if actual != expected or inventory.observed_at < max(
            slate.slate.observed_at,
            state.state.observed_at,
            bia.assembly.observed_at,
        ):
            raise OddsWeatherPersistenceConflict("retained inventory does not match sealed upstream lineage")
        if redact_value(inventory.identity_dict(), self.secret_values) != inventory.identity_dict():
            raise OddsWeatherIntegrityError("retained inventory contains credential material")
        for retained in inventory.provider_events:
            try:
                OddsProviderEventV1(
                    provider_event_id=retained.event.provider_event_id,
                    retrieved_at=retained.event.retrieved_at,
                    raw_capture_checksum=retained.event.raw_capture_checksum,
                    event=retained.event.mutable_event(),
                    history_rows=tuple(retained.event.mutable_history_rows()),
                    contract_version=retained.event.contract_version,
                    secret_values=self.secret_values,
                )
            except OddsWeatherContractError as exc:
                raise OddsWeatherIntegrityError(
                    "retained provider event contains credential material"
                ) from exc
        for retained_weather in inventory.weather_revisions:
            try:
                WeatherForecastEvidenceV1(
                    source_game_id=retained_weather.evidence.source_game_id,
                    provider=retained_weather.evidence.provider,
                    retrieved_at=retained_weather.evidence.retrieved_at,
                    raw_capture_checksums=retained_weather.evidence.raw_capture_checksums,
                    forecast=dict(retained_weather.evidence.forecast),
                    contract_version=retained_weather.evidence.contract_version,
                    secret_values=self.secret_values,
                )
            except OddsWeatherContractError as exc:
                raise OddsWeatherIntegrityError(
                    "retained weather evidence contains credential material"
                ) from exc
        game_ids = {game.source_game_id for game in bia.assembly.games}
        if any(item.evidence.source_game_id not in game_ids for item in inventory.weather_revisions):
            raise OddsWeatherIntegrityError("weather inventory references an unrelated upstream game")
        self._verify_inventory_files(inventory)

    @staticmethod
    def _active_phase(
        connection: sqlite3.Connection,
        run_id: str,
        phase_attempt: int,
        requested_date: str,
        as_of_time: datetime,
    ) -> None:
        row = connection.execute(
            """SELECT run.requested_date,run.as_of_time,phase.status,phase.attempt_count
               FROM pipeline_runs run
               JOIN pipeline_run_phases phase ON phase.run_id=run.run_id
               WHERE run.run_id=? AND phase.phase_key='odds_weather'""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise OddsWeatherNotFoundError("pipeline run has no Odds Weather phase")
        if (
            str(row["requested_date"]) != requested_date
            or _aware(row["as_of_time"], "pipeline as_of_time") != as_of_time
            or str(row["status"]) != "running"
            or int(row["attempt_count"]) != phase_attempt
        ):
            raise OddsWeatherPersistenceConflict(
                "Odds Weather persistence requires the matching active phase attempt"
            )

    def _cleanup_owned_file(self, artifact: OddsWeatherArtifactV1 | OddsWeatherAttemptManifestArtifactV1) -> None:
        try:
            destination = resolve_contained_path(self.artifact_root, artifact.relpath)
            content = destination.read_bytes()
        except (FileNotFoundError, OSError, UnsafeArtifactPath):
            return
        if len(content) == artifact.byte_count and hashlib.sha256(content).hexdigest() == artifact.checksum:
            destination.unlink(missing_ok=True)

    def _insert_attempt(
        self,
        connection: sqlite3.Connection,
        manifest: OddsWeatherAttemptManifestV1,
        artifact: OddsWeatherAttemptManifestArtifactV1,
    ) -> None:
        connection.execute(
            """INSERT INTO odds_weather_attempt_evidence(
                 run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
                 phase_input_checksum,upstream_daily_slate_snapshot_id,
                 upstream_daily_slate_checksum,upstream_game_state_snapshot_id,
                 upstream_game_state_checksum,upstream_baseball_intelligence_snapshot_id,
                 upstream_baseball_intelligence_checksum,outcome,snapshot_checksum,
                 evidence_manifest_relpath,evidence_manifest_checksum,
                 evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                manifest.run_id,
                "odds_weather",
                manifest.phase_attempt,
                manifest.requested_date,
                manifest.as_of_time.isoformat(),
                manifest.observed_at.isoformat(),
                manifest.phase_input_checksum,
                manifest.upstream_daily_slate_snapshot_id,
                manifest.upstream_daily_slate_checksum,
                manifest.upstream_game_state_snapshot_id,
                manifest.upstream_game_state_checksum,
                manifest.upstream_baseball_intelligence_snapshot_id,
                manifest.upstream_baseball_intelligence_checksum,
                OddsWeatherAttemptOutcome(manifest.outcome).value,
                manifest.snapshot_checksum,
                artifact.relpath,
                artifact.checksum,
                artifact.byte_count,
                canonical_json_bytes(
                    [item.as_dict() for item in manifest.final_warnings]
                ).decode(),
                len(manifest.final_warnings),
                manifest.created_at.isoformat(),
                manifest.completed_at.isoformat(),
            ),
        )

    def _insert_raw_evidence(
        self,
        connection: sqlite3.Connection,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> None:
        for capture in inventory.raw_captures:
            connection.execute(
                """INSERT INTO odds_weather_raw_captures(
                     run_id,phase_attempt,ordinal,provider,endpoint_category,
                     source_game_id,provider_event_id,retrieved_at,provider_timestamp,
                     raw_relpath,raw_capture_checksum,raw_byte_count,
                     canonical_metadata_json,row_checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    inventory.run_id,
                    inventory.phase_attempt,
                    capture.ordinal,
                    capture.provider,
                    capture.endpoint_category,
                    capture.source_game_id,
                    capture.provider_event_id,
                    capture.retrieved_at.isoformat(),
                    None if capture.provider_timestamp is None else capture.provider_timestamp.isoformat(),
                    capture.raw_relpath,
                    capture.checksum,
                    capture.byte_count,
                    _canonical_text(capture.as_dict()),
                    capture.row_checksum,
                ),
            )
        for retained_event in inventory.provider_events:
            self._insert_provider_event(connection, inventory, retained_event)
        for retained_weather in inventory.weather_revisions:
            self._insert_weather_revision(connection, inventory, retained_weather)

    def _insert_provider_event(
        self,
        connection: sqlite3.Connection,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        retained: RetainedOddsProviderEventV1,
    ) -> None:
        event = retained.event
        payload = event.mutable_event()
        connection.execute(
            """INSERT INTO odds_weather_provider_events(
                 run_id,phase_attempt,provider_event_id,retrieved_at,revision_ordinal,
                 sport_key,commence_time,away_team_id,home_team_id,raw_capture_checksum,
                 contract_version,event_checksum,canonical_event_json,row_checksum)
               VALUES (?,?,?,?,?,'baseball_mlb',?,?,?,?,?,?,?,?)""",
            (
                inventory.run_id,
                inventory.phase_attempt,
                event.provider_event_id,
                event.retrieved_at.isoformat(),
                retained.revision_ordinal,
                event.commence_time.isoformat(),
                event.away_team_id,
                event.home_team_id,
                event.raw_capture_checksum,
                event.contract_version,
                retained.event_checksum,
                _canonical_text(payload),
                retained.row_checksum,
            ),
        )
        bookmakers = payload.get("bookmakers")
        assert isinstance(bookmakers, list)
        for book_ordinal, bookmaker in enumerate(bookmakers, start=1):
            assert isinstance(bookmaker, dict)
            connection.execute(
                """INSERT INTO odds_weather_bookmakers(
                     run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
                     bookmaker_key,title,bookmaker_last_update,canonical_json,row_checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    inventory.run_id,
                    inventory.phase_attempt,
                    event.provider_event_id,
                    event.retrieved_at.isoformat(),
                    book_ordinal,
                    bookmaker["key"],
                    bookmaker["title"],
                    bookmaker.get("last_update"),
                    _canonical_text(bookmaker),
                    canonical_sha256(bookmaker),
                ),
            )
            markets = bookmaker.get("markets")
            assert isinstance(markets, list)
            for market_ordinal, market in enumerate(markets, start=1):
                assert isinstance(market, dict)
                connection.execute(
                    """INSERT INTO odds_weather_markets(
                         run_id,phase_attempt,provider_event_id,event_retrieved_at,
                         bookmaker_key,ordinal,market_key,market_last_update,
                         canonical_json,row_checksum)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
                    (
                        inventory.run_id,
                        inventory.phase_attempt,
                        event.provider_event_id,
                        event.retrieved_at.isoformat(),
                        bookmaker["key"],
                        market_ordinal,
                        market["key"],
                        market.get("last_update"),
                        _canonical_text(market),
                        canonical_sha256(market),
                    ),
                )
                outcomes = market.get("outcomes")
                assert isinstance(outcomes, list)
                for outcome_ordinal, outcome in enumerate(outcomes, start=1):
                    assert isinstance(outcome, dict)
                    connection.execute(
                        """INSERT INTO odds_weather_outcomes(
                             run_id,phase_attempt,provider_event_id,event_retrieved_at,
                             bookmaker_key,market_key,ordinal,outcome_name,
                             price_american,point,canonical_json,row_checksum)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            inventory.run_id,
                            inventory.phase_attempt,
                            event.provider_event_id,
                            event.retrieved_at.isoformat(),
                            bookmaker["key"],
                            market["key"],
                            outcome_ordinal,
                            outcome["name"],
                            outcome["price"],
                            outcome.get("point"),
                            _canonical_text(outcome),
                            canonical_sha256(outcome),
                        ),
                    )
        for ordinal, history in enumerate(event.mutable_history_rows(), start=1):
            price = history.get("price_american", history.get("price"))
            connection.execute(
                """INSERT INTO odds_weather_odds_revisions(
                     run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
                     bookmaker_key,market_key,outcome_name,price_american,point,
                     provider_last_update,bookmaker_last_update,market_last_update,
                     retrieved_at,canonical_json,row_checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    inventory.run_id,
                    inventory.phase_attempt,
                    event.provider_event_id,
                    event.retrieved_at.isoformat(),
                    ordinal,
                    history["bookmaker_key"],
                    history["market_key"],
                    history["outcome_name"],
                    price,
                    history.get("point"),
                    history.get("provider_last_update"),
                    history.get("bookmaker_last_update"),
                    history.get("market_last_update"),
                    history["retrieved_at"],
                    _canonical_text(history),
                    canonical_sha256(history),
                ),
            )

    def _insert_weather_revision(
        self,
        connection: sqlite3.Connection,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        retained: RetainedWeatherRevisionV1,
    ) -> None:
        evidence = retained.evidence
        connection.execute(
            """INSERT INTO odds_weather_weather_revisions(
                 run_id,phase_attempt,source_game_id,provider,retrieved_at,
                 revision_ordinal,forecast_time,forecast_offset_minutes,
                 contract_version,forecast_checksum,canonical_json,row_checksum)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                inventory.run_id,
                inventory.phase_attempt,
                evidence.source_game_id,
                evidence.provider.value,
                evidence.retrieved_at.isoformat(),
                retained.revision_ordinal,
                evidence.forecast_time.isoformat(),
                evidence.forecast.get("forecast_offset_minutes"),
                evidence.contract_version,
                retained.forecast_checksum,
                _canonical_text(evidence.as_dict()),
                retained.row_checksum,
            ),
        )
        for ordinal, checksum in enumerate(evidence.raw_capture_checksums, start=1):
            connection.execute(
                """INSERT INTO odds_weather_weather_raw_captures(
                     run_id,phase_attempt,source_game_id,provider,weather_retrieved_at,
                     ordinal,raw_capture_checksum) VALUES (?,?,?,?,?,?,?)""",
                (
                    inventory.run_id,
                    inventory.phase_attempt,
                    evidence.source_game_id,
                    evidence.provider.value,
                    evidence.retrieved_at.isoformat(),
                    ordinal,
                    checksum,
                ),
            )

    def _bind_reloaded_inventory(
        self,
        connection: sqlite3.Connection,
        supplied: OddsWeatherRetainedEvidenceInventoryV1,
        manifest: OddsWeatherAttemptManifestV1,
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        try:
            retained = self.selector.load_inventory(
                connection=connection,
                run_id=supplied.run_id,
                phase_attempt=supplied.phase_attempt,
                source_warnings=manifest.source_warnings,
                final_warnings=manifest.final_warnings,
                selected_raw_capture_checksums=manifest.selected_raw_capture_checksums,
            )
        except OddsWeatherSelectorError as exc:
            raise OddsWeatherIntegrityError(
                "retained relational inventory does not verify"
            ) from exc
        if retained != supplied or retained.checksum != supplied.checksum:
            raise OddsWeatherPersistenceConflict(
                "supplied Odds Weather inventory does not match retained relational evidence"
            )
        return retained

    def _verify_reassembly(
        self,
        *,
        supplied: OddsWeatherAssemblyResultV1,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        slate: PersistedDailySlateV1,
        bia: PersistedBaseballIntelligenceV1,
    ) -> None:
        reproduced = assemble_odds_weather(
            slate=slate.slate,
            baseball_intelligence=bia.assembly,
            odds_events=inventory.odds_events,
            weather_evidence=inventory.weather_evidence,
            source_warnings=inventory.source_warnings,
            observed_at=inventory.observed_at,
        )
        if (
            reproduced.snapshot.canonical_json_bytes()
            != supplied.snapshot.canonical_json_bytes()
            or reproduced.snapshot.warnings != inventory.final_warnings
            or reproduced.snapshot.source_raw_capture_checksums
            != inventory.selected_raw_capture_checksums
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather assembly is not reproducible from retained evidence"
            )

    def persist_assembly(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        result: OddsWeatherAssemblyResultV1,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> PersistedOddsWeatherV1:
        safe_run = validate_run_id(run_id)
        snapshot = result.snapshot
        if snapshot.contract_version != ODDS_WEATHER_CONTRACT_VERSION:
            raise OddsWeatherIntegrityError("Odds Weather snapshot contract version is invalid")
        if redact_value(snapshot.as_dict(), self.secret_values) != snapshot.as_dict():
            raise OddsWeatherIntegrityError("Odds Weather snapshot contains credential material")
        slate, state, bia = self._verify_upstream(safe_run)
        self._verify_inventory_identity(inventory, slate, state, bia)
        if inventory.run_id != safe_run or inventory.phase_attempt != phase_attempt:
            raise OddsWeatherPersistenceConflict("inventory run/attempt identity disagrees")
        self._verify_reassembly(
            supplied=result,
            inventory=inventory,
            slate=slate,
            bia=bia,
        )
        snapshot_id = self._snapshot_id(snapshot.checksum)
        created_manifest: OddsWeatherAttemptManifestArtifactV1 | None = None
        created_artifact: OddsWeatherArtifactV1 | None = None
        try:
            with self.database.connect(write=True) as connection:
                existing = connection.execute(
                    "SELECT snapshot_id FROM odds_weather_snapshots WHERE run_id=? AND phase_attempt=?",
                    (safe_run, phase_attempt),
                ).fetchone()
                if existing is not None:
                    if str(existing["snapshot_id"]) != snapshot_id:
                        raise OddsWeatherPersistenceConflict(
                            "Odds Weather run attempt already has a conflicting snapshot"
                        )
                else:
                    if connection.execute(
                        "SELECT 1 FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                        (safe_run, phase_attempt),
                    ).fetchone() is not None:
                        raise OddsWeatherPersistenceConflict(
                            "assembled attempt evidence exists without its immutable snapshot"
                        )
                    self._active_phase(
                        connection,
                        safe_run,
                        phase_attempt,
                        inventory.requested_date,
                        inventory.as_of_time,
                    )
                    now = self._now()
                    manifest = OddsWeatherAttemptManifestV1.from_inventory(
                        inventory=inventory,
                        outcome=OddsWeatherAttemptOutcome.ASSEMBLED,
                        snapshot_checksum=snapshot.checksum,
                        created_at=now,
                        completed_at=now,
                    )
                    manifest_artifact, manifest_created = publish_odds_weather_attempt_manifest(
                        manifest,
                        self.artifact_root,
                    )
                    if manifest_created:
                        created_manifest = manifest_artifact
                    artifact, artifact_created = publish_odds_weather_artifact(
                        snapshot,
                        self.artifact_root,
                        secret_values=self.secret_values,
                    )
                    if artifact_created:
                        created_artifact = artifact
                    self._insert_attempt(connection, manifest, manifest_artifact)
                    self._insert_raw_evidence(connection, inventory)
                    self._insert_snapshot(
                        connection,
                        snapshot_id=snapshot_id,
                        inventory=inventory,
                        snapshot=snapshot,
                        slate=slate,
                        state=state,
                        bia=bia,
                        artifact=artifact,
                        created_at=now,
                    )
                    self._insert_snapshot_children(
                        connection,
                        snapshot_id=snapshot_id,
                        inventory=inventory,
                        snapshot=snapshot,
                        slate=slate,
                        state=state,
                        bia=bia,
                    )
                    retained = self._bind_reloaded_inventory(connection, inventory, manifest)
                    self._verify_reassembly(
                        supplied=result,
                        inventory=retained,
                        slate=slate,
                        bia=bia,
                    )
                    self._verify_snapshot(
                        connection,
                        snapshot_id,
                        require_sealed=False,
                        upstream=(slate, state, bia),
                    )
                    connection.execute(
                        "UPDATE odds_weather_snapshots SET sealed_at=? WHERE snapshot_id=?",
                        (now.isoformat(), snapshot_id),
                    )
                    self._verify_snapshot(
                        connection,
                        snapshot_id,
                        require_sealed=True,
                        upstream=(slate, state, bia),
                    )
        except BaseException as exc:
            for owned in (created_artifact, created_manifest):
                if owned is not None:
                    self._cleanup_owned_file(owned)
            if isinstance(exc, sqlite3.IntegrityError):
                raise OddsWeatherPersistenceConflict(
                    "Odds Weather snapshot conflicts with immutable evidence"
                ) from exc
            raise
        persisted = self.get_by_snapshot_id(snapshot_id)
        retained = self.load_retained_inventory(safe_run, phase_attempt)
        if (
            persisted.snapshot.canonical_json_bytes() != snapshot.canonical_json_bytes()
            or retained != inventory
            or persisted.retained_inventory_checksum != inventory.checksum
        ):
            raise OddsWeatherPersistenceConflict(
                "existing Odds Weather snapshot conflicts with requested evidence"
            )
        return persisted

    def persist_failed_attempt(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        outcome: OddsWeatherAttemptOutcome | str,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> OddsWeatherAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        selected = OddsWeatherAttemptOutcome(outcome)
        if selected is OddsWeatherAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt persistence requires a failed outcome")
        if inventory.selected_raw_capture_checksums:
            raise OddsWeatherPersistenceConflict(
                "failed attempt cannot claim selected snapshot raw evidence"
            )
        slate, state, bia = self._verify_upstream(safe_run)
        self._verify_inventory_identity(inventory, slate, state, bia)
        if inventory.run_id != safe_run or inventory.phase_attempt != phase_attempt:
            raise OddsWeatherPersistenceConflict("inventory run/attempt identity disagrees")
        created_manifest: OddsWeatherAttemptManifestArtifactV1 | None = None
        try:
            with self.database.connect(write=True) as connection:
                existing = connection.execute(
                    "SELECT 1 FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                    (safe_run, phase_attempt),
                ).fetchone()
                if existing is None:
                    self._active_phase(
                        connection,
                        safe_run,
                        phase_attempt,
                        inventory.requested_date,
                        inventory.as_of_time,
                    )
                    now = self._now()
                    manifest = OddsWeatherAttemptManifestV1.from_inventory(
                        inventory=inventory,
                        outcome=selected,
                        snapshot_checksum=None,
                        created_at=now,
                        completed_at=now,
                    )
                    artifact, created = publish_odds_weather_attempt_manifest(
                        manifest,
                        self.artifact_root,
                    )
                    if created:
                        created_manifest = artifact
                    self._insert_attempt(connection, manifest, artifact)
                    self._insert_raw_evidence(connection, inventory)
                    self._bind_reloaded_inventory(connection, inventory, manifest)
        except BaseException as exc:
            if created_manifest is not None:
                self._cleanup_owned_file(created_manifest)
            if isinstance(exc, sqlite3.IntegrityError):
                raise OddsWeatherPersistenceConflict(
                    "Odds Weather failed attempt conflicts with immutable evidence"
                ) from exc
            raise
        evidence = self.get_attempt_evidence(safe_run, phase_attempt)
        retained = self.load_retained_inventory(safe_run, phase_attempt)
        if (
            evidence.outcome is not selected
            or evidence.snapshot_checksum is not None
            or retained != inventory
        ):
            raise OddsWeatherPersistenceConflict(
                "existing failed attempt conflicts with requested evidence"
            )
        return evidence

    def _insert_snapshot(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        snapshot: OddsWeatherV1,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
        artifact: OddsWeatherArtifactV1,
        created_at: datetime,
    ) -> None:
        weather_count = sum(
            int(game.weather.nws is not None) + int(game.weather.openweather is not None)
            for game in snapshot.games
        )
        connection.execute(
            """INSERT INTO odds_weather_snapshots(
                 snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,
                 observed_at,sport,league,contract_version,phase_input_checksum,
                 upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
                 upstream_game_state_snapshot_id,upstream_game_state_checksum,
                 upstream_baseball_intelligence_snapshot_id,
                 upstream_baseball_intelligence_checksum,snapshot_checksum,
                 source_raw_capture_checksums_json,warnings_json,warning_count,
                 canonical_json,game_count,selected_raw_capture_count,
                 weather_selection_count,artifact_relpath,artifact_checksum,
                 artifact_byte_count,sealed_at,created_at)
               VALUES (?,?,'odds_weather',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)""",
            (
                snapshot_id,
                inventory.run_id,
                inventory.phase_attempt,
                snapshot.requested_date,
                snapshot.as_of_time.isoformat(),
                snapshot.observed_at.isoformat(),
                snapshot.sport,
                snapshot.league,
                snapshot.contract_version,
                inventory.phase_input_checksum,
                slate.snapshot_id,
                slate.slate.checksum,
                state.snapshot_id,
                state.state.checksum,
                bia.snapshot_id,
                bia.assembly.checksum,
                snapshot.checksum,
                canonical_json_bytes(list(snapshot.source_raw_capture_checksums)).decode(),
                canonical_json_bytes([item.as_dict() for item in snapshot.warnings]).decode(),
                len(snapshot.warnings),
                snapshot.canonical_json_bytes().decode(),
                len(snapshot.games),
                len(snapshot.source_raw_capture_checksums),
                weather_count,
                artifact.relpath,
                artifact.checksum,
                artifact.byte_count,
                created_at.isoformat(),
            ),
        )

    def _insert_snapshot_children(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        snapshot: OddsWeatherV1,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        bia: PersistedBaseballIntelligenceV1,
    ) -> None:
        for ordinal, checksum in enumerate(snapshot.source_raw_capture_checksums, start=1):
            connection.execute(
                """INSERT INTO odds_weather_snapshot_raw_captures(
                     snapshot_id,run_id,phase_attempt,ordinal,raw_capture_checksum)
                   VALUES (?,?,?,?,?)""",
                (snapshot_id, inventory.run_id, inventory.phase_attempt, ordinal, checksum),
            )
        for ordinal, (game, slate_game, state_game, bia_game) in enumerate(
            zip(snapshot.games, slate.slate.games, state.state.games, bia.assembly.games, strict=True),
            start=1,
        ):
            self._insert_game(
                connection,
                snapshot_id=snapshot_id,
                inventory=inventory,
                ordinal=ordinal,
                game=game,
                slate_game=slate_game,
                state_game=state_game,
                bia_game=bia_game,
            )
        for ordinal, warning in enumerate(snapshot.warnings, start=1):
            payload = warning.as_dict()
            connection.execute(
                """INSERT INTO odds_weather_warnings(
                     snapshot_id,ordinal,code,domain,message,source_game_id,
                     provider,provider_event_id,canonical_json,row_checksum)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    ordinal,
                    warning.code,
                    warning.domain.value,
                    warning.message,
                    warning.source_game_id,
                    warning.provider,
                    warning.provider_event_id,
                    _canonical_text(payload),
                    canonical_sha256(payload),
                ),
            )

    def _insert_game(
        self,
        connection: sqlite3.Connection,
        *,
        snapshot_id: str,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        ordinal: int,
        game: OddsWeatherGameV1,
        slate_game: Any,
        state_game: Any,
        bia_game: Any,
    ) -> None:
        odds_payload = game.odds.as_dict()
        weather_payload = game.weather.as_dict()
        venue = game.weather.venue_context
        weather_evidence = tuple(
            item for item in (game.weather.nws, game.weather.openweather) if item is not None
        )
        connection.execute(
            """INSERT INTO odds_weather_games(
                 snapshot_id,run_id,phase_attempt,ordinal,edge_event_id,
                 daily_mlb_game_id,source_game_id,official_date,away_team_id,
                 home_team_id,source_away_team_id,source_home_team_id,venue_id,
                 game_status,scheduled_start_time,upstream_daily_slate_game_checksum,
                 upstream_game_state_game_checksum,
                 upstream_baseball_intelligence_game_checksum,odds_availability,
                 selected_provider_event_id,odds_retrieved_at,odds_raw_capture_checksum,
                 event_match_offset_minutes,odds_summary_json,odds_summary_checksum,
                 normalized_market_count,raw_snapshot_count,freshness_counts_json,
                 odds_calculation_version,odds_consensus_contract_version,
                 weather_status,weather_relevance,weather_primary_source,
                 venue_context_json,venue_context_checksum,weather_comparison_json,
                 baseball_wind_impact_json,weather_revision_count,canonical_json,row_checksum)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                snapshot_id,
                inventory.run_id,
                inventory.phase_attempt,
                ordinal,
                game.edge_event_id,
                game.daily_mlb_game_id,
                game.source_game_id,
                slate_game.official_date,
                game.away_team_id,
                game.home_team_id,
                state_game.away.source_team_id,
                state_game.home.source_team_id,
                bia_game.venue_id,
                bia_game.game_status.value,
                None if game.scheduled_start_time is None else game.scheduled_start_time.isoformat(),
                game.upstream_daily_slate_game_checksum,
                state_game.checksum,
                game.upstream_baseball_intelligence_game_checksum,
                game.odds.availability.value,
                game.odds.provider_event_id,
                None if game.odds.retrieved_at is None else game.odds.retrieved_at.isoformat(),
                game.odds.raw_capture_checksum,
                game.odds.event_match_offset_minutes,
                None
                if odds_payload["summary"] is None
                else _canonical_text(
                    _mapping_value(odds_payload["summary"], "odds summary")
                ),
                game.odds.summary_checksum,
                game.odds.normalized_market_count,
                game.odds.raw_snapshot_count,
                _canonical_text(
                    _mapping_value(
                        odds_payload["freshness_counts"],
                        "odds freshness counts",
                    )
                ),
                game.odds.calculation_version,
                game.odds.odds_consensus_contract_version,
                game.weather.status.value,
                game.weather.relevance.value,
                None if game.weather.primary_source is None else game.weather.primary_source.value,
                None if venue is None else _canonical_text(venue.as_dict()),
                None if venue is None else venue.checksum,
                _canonical_text(
                    _mapping_value(weather_payload["comparison"], "weather comparison")
                ),
                _canonical_text(
                    _mapping_value(
                        weather_payload["baseball_wind_impact"],
                        "baseball wind impact",
                    )
                ),
                len(weather_evidence),
                _canonical_text(game.as_dict()),
                game.checksum,
            ),
        )
        for evidence in weather_evidence:
            retained = next(
                (
                    item
                    for item in inventory.weather_revisions
                    if item.evidence == evidence
                ),
                None,
            )
            if retained is None:
                raise OddsWeatherIntegrityError(
                    "selected weather evidence is absent from retained inventory"
                )
            connection.execute(
                """INSERT INTO odds_weather_game_weather_selections(
                     snapshot_id,run_id,phase_attempt,source_game_id,provider,
                     weather_retrieved_at,forecast_checksum,is_primary)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (
                    snapshot_id,
                    inventory.run_id,
                    inventory.phase_attempt,
                    game.source_game_id,
                    evidence.provider.value,
                    evidence.retrieved_at.isoformat(),
                    retained.forecast_checksum,
                    int(evidence.provider is game.weather.primary_source),
                ),
            )

    @staticmethod
    def _manifest_inventory(
        payload: Mapping[str, object],
        field: str,
    ) -> tuple[Mapping[str, object], ...]:
        raw = payload.get(field)
        if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
            raise OddsWeatherIntegrityError(f"attempt manifest {field} is invalid")
        return tuple(dict(item) for item in raw)

    @staticmethod
    def _manifest_warnings(
        payload: Mapping[str, object],
        field: str,
    ) -> tuple[OddsWeatherWarningV1, ...]:
        raw = payload.get(field)
        if not isinstance(raw, list):
            raise OddsWeatherIntegrityError(f"attempt manifest {field} is invalid")
        result: list[OddsWeatherWarningV1] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise OddsWeatherIntegrityError(f"attempt manifest {field} is invalid")
            result.append(_warning(item))
        return tuple(result)

    def _read_manifest_payload(self, relpath: str) -> Mapping[str, object]:
        try:
            content = resolve_contained_path(self.artifact_root, relpath).read_bytes()
            payload: Any = json.loads(content.decode("utf-8"))
        except (
            FileNotFoundError,
            OSError,
            UnsafeArtifactPath,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise OddsWeatherAttemptManifestError(
                "Odds Weather attempt manifest is unreadable"
            ) from exc
        if (
            not isinstance(payload, Mapping)
            or canonical_json_bytes(dict(payload)) != content
            or redact_value(payload, self.secret_values) != payload
        ):
            raise OddsWeatherAttemptManifestError(
                "Odds Weather attempt manifest is noncanonical or unsafe"
            )
        return payload

    def _manifest_from_row(self, row: sqlite3.Row) -> OddsWeatherAttemptManifestV1:
        payload = self._read_manifest_payload(str(row["evidence_manifest_relpath"]))
        selected_values = payload.get("selected_raw_capture_checksums", [])
        if not isinstance(selected_values, list):
            raise OddsWeatherIntegrityError(
                "Odds Weather attempt manifest selected raw captures are invalid"
            )
        try:
            manifest = OddsWeatherAttemptManifestV1(
                run_id=str(payload["run_id"]),
                phase_attempt=int(str(payload["phase_attempt"])),
                requested_date=str(payload["requested_date"]),
                as_of_time=_aware(payload["as_of_time"], "manifest as_of_time"),
                observed_at=_aware(payload["observed_at"], "manifest observed_at"),
                phase_input_checksum=str(payload["phase_input_checksum"]),
                upstream_daily_slate_snapshot_id=str(payload["upstream_daily_slate_snapshot_id"]),
                upstream_daily_slate_checksum=str(payload["upstream_daily_slate_checksum"]),
                upstream_game_state_snapshot_id=str(payload["upstream_game_state_snapshot_id"]),
                upstream_game_state_checksum=str(payload["upstream_game_state_checksum"]),
                upstream_baseball_intelligence_snapshot_id=str(payload["upstream_baseball_intelligence_snapshot_id"]),
                upstream_baseball_intelligence_checksum=str(payload["upstream_baseball_intelligence_checksum"]),
                outcome=str(payload["outcome"]),
                snapshot_checksum=None if payload.get("snapshot_checksum") is None else str(payload["snapshot_checksum"]),
                raw_capture_inventory=self._manifest_inventory(payload, "raw_capture_inventory"),
                provider_event_revision_inventory=self._manifest_inventory(payload, "provider_event_revision_inventory"),
                odds_revision_inventory=self._manifest_inventory(payload, "odds_revision_inventory"),
                weather_revision_inventory=self._manifest_inventory(payload, "weather_revision_inventory"),
                selected_raw_capture_checksums=tuple(
                    str(item) for item in selected_values
                ),
                source_warnings=self._manifest_warnings(payload, "source_warnings"),
                final_warnings=self._manifest_warnings(payload, "final_warnings"),
                retained_inventory_checksum=str(payload["retained_inventory_checksum"]),
                created_at=_aware(payload["created_at"], "manifest created_at"),
                completed_at=_aware(payload["completed_at"], "manifest completed_at"),
                contract_version=str(payload["contract_version"]),
            )
        except (KeyError, TypeError, ValueError, OddsWeatherIntegrityError) as exc:
            raise OddsWeatherIntegrityError(
                "Odds Weather attempt manifest violates its contract"
            ) from exc
        row_values = (
            str(row["run_id"]),
            int(row["phase_attempt"]),
            str(row["requested_date"]),
            _aware(row["as_of_time"], "attempt as_of_time"),
            _aware(row["observed_at"], "attempt observed_at"),
            str(row["phase_input_checksum"]),
            str(row["upstream_daily_slate_snapshot_id"]),
            str(row["upstream_daily_slate_checksum"]),
            str(row["upstream_game_state_snapshot_id"]),
            str(row["upstream_game_state_checksum"]),
            str(row["upstream_baseball_intelligence_snapshot_id"]),
            str(row["upstream_baseball_intelligence_checksum"]),
            str(row["outcome"]),
            None if row["snapshot_checksum"] is None else str(row["snapshot_checksum"]),
            _aware(row["created_at"], "attempt created_at"),
            _aware(row["completed_at"], "attempt completed_at"),
        )
        manifest_values = (
            manifest.run_id,
            manifest.phase_attempt,
            manifest.requested_date,
            manifest.as_of_time,
            manifest.observed_at,
            manifest.phase_input_checksum,
            manifest.upstream_daily_slate_snapshot_id,
            manifest.upstream_daily_slate_checksum,
            manifest.upstream_game_state_snapshot_id,
            manifest.upstream_game_state_checksum,
            manifest.upstream_baseball_intelligence_snapshot_id,
            manifest.upstream_baseball_intelligence_checksum,
            OddsWeatherAttemptOutcome(manifest.outcome).value,
            manifest.snapshot_checksum,
            manifest.created_at,
            manifest.completed_at,
        )
        warnings = _warning_rows(row["warnings_json"], "attempt warnings")
        if (
            row_values != manifest_values
            or warnings != manifest.final_warnings
            or int(row["warning_count"]) != len(warnings)
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather attempt row and manifest do not reconcile"
            )
        return manifest

    def get_attempt_evidence(
        self,
        run_id: str,
        phase_attempt: int,
    ) -> OddsWeatherAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (safe_run, phase_attempt),
            ).fetchone()
        if row is None:
            raise OddsWeatherNotFoundError("Odds Weather attempt evidence does not exist")
        try:
            manifest = self._manifest_from_row(row)
            artifact = verify_odds_weather_attempt_manifest(
                artifact_root=self.artifact_root,
                relpath=str(row["evidence_manifest_relpath"]),
                expected=manifest,
            )
        except OddsWeatherAttemptManifestError as exc:
            raise OddsWeatherIntegrityError(
                "Odds Weather attempt manifest does not verify"
            ) from exc
        if (
            artifact.checksum != str(row["evidence_manifest_checksum"])
            or artifact.byte_count != int(row["evidence_manifest_byte_count"])
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather manifest metadata does not match retained bytes"
            )
        retained = self.load_retained_inventory(safe_run, phase_attempt)
        if retained.checksum != manifest.retained_inventory_checksum:
            raise OddsWeatherIntegrityError(
                "Odds Weather attempt retained inventory does not verify"
            )
        return OddsWeatherAttemptEvidenceV1(
            run_id=safe_run,
            phase_attempt=phase_attempt,
            requested_date=manifest.requested_date,
            as_of_time=manifest.as_of_time,
            observed_at=manifest.observed_at,
            phase_input_checksum=manifest.phase_input_checksum,
            upstream_daily_slate_snapshot_id=manifest.upstream_daily_slate_snapshot_id,
            upstream_daily_slate_checksum=manifest.upstream_daily_slate_checksum,
            upstream_game_state_snapshot_id=manifest.upstream_game_state_snapshot_id,
            upstream_game_state_checksum=manifest.upstream_game_state_checksum,
            upstream_baseball_intelligence_snapshot_id=manifest.upstream_baseball_intelligence_snapshot_id,
            upstream_baseball_intelligence_checksum=manifest.upstream_baseball_intelligence_checksum,
            outcome=OddsWeatherAttemptOutcome(manifest.outcome),
            snapshot_checksum=manifest.snapshot_checksum,
            manifest=artifact,
            warnings=manifest.final_warnings,
            retained_inventory_checksum=manifest.retained_inventory_checksum,
            created_at=manifest.created_at,
            completed_at=manifest.completed_at,
        )

    def get_attempt_manifest(
        self,
        run_id: str,
        phase_attempt: int,
    ) -> OddsWeatherAttemptManifestV1:
        evidence = self.get_attempt_evidence(run_id, phase_attempt)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
                (evidence.run_id, evidence.phase_attempt),
            ).fetchone()
        assert row is not None
        return self._manifest_from_row(row)

    def list_attempt_evidence(self, run_id: str) -> tuple[OddsWeatherAttemptEvidenceV1, ...]:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT phase_attempt FROM odds_weather_attempt_evidence WHERE run_id=? ORDER BY phase_attempt",
                (safe_run,),
            ).fetchall()
        return tuple(
            self.get_attempt_evidence(safe_run, int(row["phase_attempt"])) for row in rows
        )

    def load_retained_inventory(
        self,
        run_id: str,
        phase_attempt: int,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        safe_run = validate_run_id(run_id)
        if connection is not None:
            return self._load_retained_inventory(connection, safe_run, phase_attempt)
        with self.database.connect() as selected_connection:
            return self._load_retained_inventory(
                selected_connection,
                safe_run,
                phase_attempt,
            )

    def _load_retained_inventory(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        phase_attempt: int,
    ) -> OddsWeatherRetainedEvidenceInventoryV1:
        row = connection.execute(
            "SELECT * FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
            (run_id, phase_attempt),
        ).fetchone()
        if row is None:
            raise OddsWeatherNotFoundError("Odds Weather attempt evidence does not exist")
        manifest = self._manifest_from_row(row)
        try:
            inventory = self.selector.load_inventory(
                connection=connection,
                run_id=run_id,
                phase_attempt=phase_attempt,
                source_warnings=manifest.source_warnings,
                final_warnings=manifest.final_warnings,
                selected_raw_capture_checksums=manifest.selected_raw_capture_checksums,
            )
        except OddsWeatherSelectorError as exc:
            raise OddsWeatherIntegrityError(
                "retained relational inventory does not verify"
            ) from exc
        if inventory.checksum != manifest.retained_inventory_checksum:
            raise OddsWeatherIntegrityError(
                "retained relational inventory checksum disagrees with manifest"
            )
        return inventory

    def _selected_weather(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        source_game_id: str,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
    ) -> tuple[WeatherForecastEvidenceV1 | None, WeatherForecastEvidenceV1 | None, WeatherProvider | None]:
        rows = connection.execute(
            """SELECT * FROM odds_weather_game_weather_selections
               WHERE snapshot_id=? AND source_game_id=? ORDER BY provider""",
            (snapshot_id, source_game_id),
        ).fetchall()
        nws: WeatherForecastEvidenceV1 | None = None
        openweather: WeatherForecastEvidenceV1 | None = None
        primary: WeatherProvider | None = None
        for row in rows:
            provider = WeatherProvider(str(row["provider"]))
            retained = next(
                (
                    item
                    for item in inventory.weather_revisions
                    if item.evidence.source_game_id == source_game_id
                    and item.evidence.provider is provider
                    and item.evidence.retrieved_at
                    == _aware(row["weather_retrieved_at"], "selected weather retrieved_at")
                ),
                None,
            )
            if retained is None or retained.forecast_checksum != str(row["forecast_checksum"]):
                raise OddsWeatherIntegrityError(
                    "selected weather row does not match retained revision evidence"
                )
            if int(row["is_primary"]) == 1:
                if primary is not None:
                    raise OddsWeatherIntegrityError("game has multiple primary weather revisions")
                primary = provider
            if provider is WeatherProvider.NWS:
                nws = retained.evidence
            else:
                openweather = retained.evidence
        return nws, openweather, primary

    def _reconstruct_game(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        slate_game: Any,
        state_game: Any,
        bia_game: Any,
    ) -> OddsWeatherGameV1:
        summary = None if row["odds_summary_json"] is None else _json_object(row["odds_summary_json"], "odds summary")
        freshness = _json_object(row["freshness_counts_json"], "freshness counts")
        odds = OddsSnapshotV1(
            availability=OddsAvailability(str(row["odds_availability"])),
            provider_event_id=None if row["selected_provider_event_id"] is None else str(row["selected_provider_event_id"]),
            retrieved_at=None if row["odds_retrieved_at"] is None else _aware(row["odds_retrieved_at"], "odds retrieved_at"),
            raw_capture_checksum=None if row["odds_raw_capture_checksum"] is None else str(row["odds_raw_capture_checksum"]),
            event_match_offset_minutes=None if row["event_match_offset_minutes"] is None else float(row["event_match_offset_minutes"]),
            summary=summary,
            normalized_market_count=int(row["normalized_market_count"]),
            raw_snapshot_count=int(row["raw_snapshot_count"]),
            freshness_counts={str(key): int(value) for key, value in freshness.items()},
            calculation_version=str(row["odds_calculation_version"]),
            odds_consensus_contract_version=str(row["odds_consensus_contract_version"]),
        )
        venue = None if row["venue_context_json"] is None else _venue(_json_object(row["venue_context_json"], "venue context"))
        nws, openweather, primary = self._selected_weather(
            connection,
            str(row["snapshot_id"]),
            str(row["source_game_id"]),
            inventory,
        )
        weather = WeatherSnapshotV1(
            status=WeatherStatus(str(row["weather_status"])),
            relevance=WeatherRelevance(str(row["weather_relevance"])),
            venue_context=venue,
            primary_source=primary,
            nws=nws,
            openweather=openweather,
            comparison=_json_object(row["weather_comparison_json"], "weather comparison"),
            baseball_wind_impact=_json_object(row["baseball_wind_impact_json"], "wind impact"),
        )
        try:
            game = OddsWeatherGameV1(
                edge_event_id=str(row["edge_event_id"]),
                daily_mlb_game_id=str(row["daily_mlb_game_id"]),
                source_game_id=str(row["source_game_id"]),
                away_team_id=str(row["away_team_id"]),
                home_team_id=str(row["home_team_id"]),
                scheduled_start_time=None if row["scheduled_start_time"] is None else _aware(row["scheduled_start_time"], "scheduled_start_time"),
                upstream_daily_slate_game_checksum=str(row["upstream_daily_slate_game_checksum"]),
                upstream_baseball_intelligence_game_checksum=str(row["upstream_baseball_intelligence_game_checksum"]),
                odds=odds,
                weather=weather,
            )
        except (OddsWeatherContractError, ValueError) as exc:
            raise OddsWeatherIntegrityError("relational Odds Weather game violates its contract") from exc
        upstream_values = (
            slate_game.edge_event_id,
            slate_game.daily_mlb_game_id,
            slate_game.source_game_id,
            slate_game.official_date,
            slate_game.away_team_id,
            slate_game.home_team_id,
            state_game.away.source_team_id,
            state_game.home.source_team_id,
            bia_game.venue_id,
            bia_game.game_status.value,
            slate_game.scheduled_start_time,
            slate_game.checksum,
            state_game.checksum,
            bia_game.checksum,
        )
        row_values = (
            str(row["edge_event_id"]),
            str(row["daily_mlb_game_id"]),
            str(row["source_game_id"]),
            str(row["official_date"]),
            str(row["away_team_id"]),
            str(row["home_team_id"]),
            str(row["source_away_team_id"]),
            str(row["source_home_team_id"]),
            None if row["venue_id"] is None else str(row["venue_id"]),
            str(row["game_status"]),
            None if row["scheduled_start_time"] is None else _aware(row["scheduled_start_time"], "scheduled_start_time"),
            str(row["upstream_daily_slate_game_checksum"]),
            str(row["upstream_game_state_game_checksum"]),
            str(row["upstream_baseball_intelligence_game_checksum"]),
        )
        expected_weather_count = int(nws is not None) + int(openweather is not None)
        if (
            row_values != upstream_values
            or int(row["weather_revision_count"]) != expected_weather_count
            or (None if venue is None else venue.checksum)
            != (None if row["venue_context_checksum"] is None else str(row["venue_context_checksum"]))
            or odds.summary_checksum
            != (None if row["odds_summary_checksum"] is None else str(row["odds_summary_checksum"]))
            or str(row["canonical_json"]) != _canonical_text(game.as_dict())
            or str(row["row_checksum"]) != game.checksum
        ):
            raise OddsWeatherIntegrityError(
                "relational Odds Weather game does not reconcile independently"
            )
        return game

    def _verify_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        *,
        require_sealed: bool,
        upstream: tuple[
            PersistedDailySlateV1,
            PersistedGameStateV1,
            PersistedBaseballIntelligenceV1,
        ]
        | None = None,
    ) -> PersistedOddsWeatherV1:
        row = connection.execute(
            "SELECT * FROM odds_weather_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise OddsWeatherNotFoundError("Odds Weather snapshot does not exist")
        if (row["sealed_at"] is not None) != require_sealed:
            raise OddsWeatherIntegrityError(
                "Odds Weather snapshot sealing state does not match verification boundary"
            )
        if upstream is None:
            try:
                slate = self.daily_slate.get_daily_slate_snapshot(
                    str(row["upstream_daily_slate_snapshot_id"])
                )
                state = self.game_state.get_game_state_snapshot(
                    str(row["upstream_game_state_snapshot_id"])
                )
                bia = self.baseball_intelligence.get_by_snapshot_id(
                    str(row["upstream_baseball_intelligence_snapshot_id"])
                )
            except RuntimeError as exc:
                raise OddsWeatherIntegrityError(
                    "sealed upstream evidence cannot be reconstructed"
                ) from exc
        else:
            slate, state, bia = upstream
        if (
            slate.run_id != str(row["run_id"])
            or state.run_id != str(row["run_id"])
            or bia.run_id != str(row["run_id"])
            or slate.snapshot_id != str(row["upstream_daily_slate_snapshot_id"])
            or state.snapshot_id != str(row["upstream_game_state_snapshot_id"])
            or bia.snapshot_id != str(row["upstream_baseball_intelligence_snapshot_id"])
            or slate.slate.checksum != str(row["upstream_daily_slate_checksum"])
            or state.state.checksum != str(row["upstream_game_state_checksum"])
            or bia.assembly.checksum != str(row["upstream_baseball_intelligence_checksum"])
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather snapshot upstream chain does not reconcile"
            )
        attempt_row = connection.execute(
            "SELECT * FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=?",
            (str(row["run_id"]), int(row["phase_attempt"])),
        ).fetchone()
        if attempt_row is None:
            raise OddsWeatherIntegrityError("Odds Weather attempt evidence is missing")
        manifest = self._manifest_from_row(attempt_row)
        try:
            manifest_artifact = verify_odds_weather_attempt_manifest(
                artifact_root=self.artifact_root,
                relpath=str(attempt_row["evidence_manifest_relpath"]),
                expected=manifest,
            )
        except OddsWeatherAttemptManifestError as exc:
            raise OddsWeatherIntegrityError("Odds Weather attempt manifest is invalid") from exc
        if (
            manifest_artifact.checksum != str(attempt_row["evidence_manifest_checksum"])
            or manifest_artifact.byte_count != int(attempt_row["evidence_manifest_byte_count"])
            or manifest.outcome is not OddsWeatherAttemptOutcome.ASSEMBLED
            or manifest.snapshot_checksum != str(row["snapshot_checksum"])
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather snapshot and attempt manifest do not reconcile"
            )
        try:
            inventory = self.selector.load_inventory(
                connection=connection,
                run_id=str(row["run_id"]),
                phase_attempt=int(row["phase_attempt"]),
                source_warnings=manifest.source_warnings,
                final_warnings=manifest.final_warnings,
                selected_raw_capture_checksums=manifest.selected_raw_capture_checksums,
            )
        except OddsWeatherSelectorError as exc:
            raise OddsWeatherIntegrityError(
                "retained relational inventory does not verify"
            ) from exc
        if inventory.checksum != manifest.retained_inventory_checksum:
            raise OddsWeatherIntegrityError("Odds Weather inventory checksum does not reconcile")
        game_rows = connection.execute(
            "SELECT * FROM odds_weather_games WHERE snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        if len(game_rows) != len(slate.slate.games) or len(game_rows) != int(row["game_count"]):
            raise OddsWeatherIntegrityError("Odds Weather game inventory is incomplete")
        games: list[OddsWeatherGameV1] = []
        for ordinal, (game_row, slate_game, state_game, bia_game) in enumerate(
            zip(game_rows, slate.slate.games, state.state.games, bia.assembly.games, strict=True),
            start=1,
        ):
            if int(game_row["ordinal"]) != ordinal:
                raise OddsWeatherIntegrityError("Odds Weather game ordinals are not contiguous")
            games.append(
                self._reconstruct_game(
                    connection,
                    game_row,
                    inventory,
                    slate_game,
                    state_game,
                    bia_game,
                )
            )
        warning_rows = connection.execute(
            "SELECT * FROM odds_weather_warnings WHERE snapshot_id=? ORDER BY ordinal",
            (snapshot_id,),
        ).fetchall()
        warnings: list[OddsWeatherWarningV1] = []
        for ordinal, warning_row in enumerate(warning_rows, start=1):
            payload = _json_object(warning_row["canonical_json"], "warning")
            warning = _warning(payload)
            expected = (
                ordinal,
                warning.code,
                warning.domain.value,
                warning.message,
                warning.source_game_id,
                warning.provider,
                warning.provider_event_id,
                _canonical_text(warning.as_dict()),
                canonical_sha256(warning.as_dict()),
            )
            actual = (
                int(warning_row["ordinal"]),
                str(warning_row["code"]),
                str(warning_row["domain"]),
                str(warning_row["message"]),
                None if warning_row["source_game_id"] is None else str(warning_row["source_game_id"]),
                None if warning_row["provider"] is None else str(warning_row["provider"]),
                None if warning_row["provider_event_id"] is None else str(warning_row["provider_event_id"]),
                str(warning_row["canonical_json"]),
                str(warning_row["row_checksum"]),
            )
            if actual != expected:
                raise OddsWeatherIntegrityError("Odds Weather warning row does not reconcile")
            warnings.append(warning)
        selected_rows = connection.execute(
            """SELECT ordinal,raw_capture_checksum
               FROM odds_weather_snapshot_raw_captures
               WHERE snapshot_id=? ORDER BY ordinal""",
            (snapshot_id,),
        ).fetchall()
        selected = tuple(str(item["raw_capture_checksum"]) for item in selected_rows)
        if tuple(int(item["ordinal"]) for item in selected_rows) != tuple(range(1, len(selected_rows) + 1)):
            raise OddsWeatherIntegrityError("selected raw-capture ordinals are not contiguous")
        try:
            snapshot = OddsWeatherV1(
                requested_date=str(row["requested_date"]),
                as_of_time=_aware(row["as_of_time"], "snapshot as_of_time"),
                observed_at=_aware(row["observed_at"], "snapshot observed_at"),
                upstream_daily_slate_checksum=str(row["upstream_daily_slate_checksum"]),
                upstream_baseball_intelligence_checksum=str(row["upstream_baseball_intelligence_checksum"]),
                source_raw_capture_checksums=selected,
                games=tuple(games),
                warnings=tuple(warnings),
                secret_values=self.secret_values,
                contract_version=str(row["contract_version"]),
                sport=str(row["sport"]),
                league=str(row["league"]),
            )
        except (OddsWeatherContractError, ValueError) as exc:
            raise OddsWeatherIntegrityError("relational Odds Weather snapshot is invalid") from exc
        stored_selected = tuple(
            str(item)
            for item in _json_array(
                row["source_raw_capture_checksums_json"],
                "source raw capture checksums",
            )
        )
        stored_warnings = _warning_rows(row["warnings_json"], "snapshot warnings")
        weather_count = sum(
            int(game.weather.nws is not None) + int(game.weather.openweather is not None)
            for game in snapshot.games
        )
        row_values = (
            str(row["snapshot_id"]),
            str(row["requested_date"]),
            _aware(row["as_of_time"], "snapshot as_of_time"),
            _aware(row["observed_at"], "snapshot observed_at"),
            str(row["phase_input_checksum"]),
            str(row["snapshot_checksum"]),
            stored_selected,
            stored_warnings,
            int(row["warning_count"]),
            str(row["canonical_json"]),
            int(row["game_count"]),
            int(row["selected_raw_capture_count"]),
            int(row["weather_selection_count"]),
        )
        expected_values = (
            self._snapshot_id(snapshot.checksum),
            snapshot.requested_date,
            snapshot.as_of_time,
            snapshot.observed_at,
            inventory.phase_input_checksum,
            snapshot.checksum,
            snapshot.source_raw_capture_checksums,
            snapshot.warnings,
            len(snapshot.warnings),
            snapshot.canonical_json_bytes().decode(),
            len(snapshot.games),
            len(snapshot.source_raw_capture_checksums),
            weather_count,
        )
        if (
            row_values != expected_values
            or inventory.final_warnings != snapshot.warnings
            or inventory.selected_raw_capture_checksums
            != snapshot.source_raw_capture_checksums
        ):
            raise OddsWeatherIntegrityError(
                "Odds Weather top-level relational evidence does not reconcile"
            )
        artifact = OddsWeatherArtifactV1(
            str(row["artifact_relpath"]),
            _checksum(row["artifact_checksum"], "artifact checksum"),
            int(row["artifact_byte_count"]),
        )
        try:
            verify_odds_weather_artifact(
                snapshot,
                artifact,
                self.artifact_root,
                secret_values=self.secret_values,
            )
        except OddsWeatherArtifactIntegrityError as exc:
            raise OddsWeatherIntegrityError("Odds Weather artifact does not verify") from exc
        reproduced = assemble_odds_weather(
            slate=slate.slate,
            baseball_intelligence=bia.assembly,
            odds_events=inventory.odds_events,
            weather_evidence=inventory.weather_evidence,
            source_warnings=inventory.source_warnings,
            observed_at=inventory.observed_at,
        ).snapshot
        if reproduced.canonical_json_bytes() != snapshot.canonical_json_bytes():
            raise OddsWeatherIntegrityError(
                "offline Odds Weather reassembly does not reproduce retained snapshot"
            )
        sealed_at = (
            _aware(row["sealed_at"], "sealed_at")
            if row["sealed_at"] is not None
            else _aware(row["created_at"], "created_at")
        )
        return PersistedOddsWeatherV1(
            snapshot_id=snapshot_id,
            run_id=str(row["run_id"]),
            phase_attempt=int(row["phase_attempt"]),
            upstream_daily_slate_snapshot_id=slate.snapshot_id,
            upstream_game_state_snapshot_id=state.snapshot_id,
            upstream_baseball_intelligence_snapshot_id=bia.snapshot_id,
            snapshot=snapshot,
            artifact=artifact,
            retained_inventory_checksum=inventory.checksum,
            created_at=_aware(row["created_at"], "created_at"),
            sealed_at=sealed_at,
        )

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedOddsWeatherV1:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("snapshot_id must be non-empty")
        with self.database.connect() as connection:
            return self._verify_snapshot(connection, snapshot_id, require_sealed=True)

    def get_for_run_attempt(
        self,
        run_id: str,
        phase_attempt: int,
    ) -> PersistedOddsWeatherV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT snapshot_id FROM odds_weather_snapshots WHERE run_id=? AND phase_attempt=?",
                (safe_run, phase_attempt),
            ).fetchone()
            if row is None:
                raise OddsWeatherNotFoundError(
                    "Odds Weather snapshot does not exist for run attempt"
                )
            return self._verify_snapshot(
                connection,
                str(row["snapshot_id"]),
                require_sealed=True,
            )

    def get_latest_for_run(self, run_id: str) -> PersistedOddsWeatherV1 | None:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute(
                """SELECT snapshot_id FROM odds_weather_snapshots
                   WHERE run_id=? AND sealed_at IS NOT NULL
                   ORDER BY phase_attempt DESC,created_at DESC,snapshot_id DESC LIMIT 1""",
                (safe_run,),
            ).fetchone()
            return (
                None
                if row is None
                else self._verify_snapshot(
                    connection,
                    str(row["snapshot_id"]),
                    require_sealed=True,
                )
            )
