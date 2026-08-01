"""Immutable, credential-free Odds + Weather V1 attempt manifests."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from app.artifacts import (
    UnsafeArtifactPath,
    resolve_contained_path,
    validate_artifact_relpath,
)
from app.daily_slate.contracts import canonical_json_bytes
from app.exporter import atomic_create_bytes
from app.identifiers import parse_requested_date, validate_run_id
from app.odds_weather.contracts import OddsWeatherWarningV1
from app.odds_weather.selector import OddsWeatherRetainedEvidenceInventoryV1
from app.redaction import redact_value


ODDS_WEATHER_ATTEMPT_MANIFEST_CONTRACT = "DSE_ODDS_WEATHER_ATTEMPT_MANIFEST_V1"


class OddsWeatherAttemptOutcome(StrEnum):
    ASSEMBLED = "assembled"
    ACQUISITION_FAILED = "acquisition_failed"
    NORMALIZATION_FAILED = "normalization_failed"
    ASSEMBLY_FAILED = "assembly_failed"


class OddsWeatherAttemptManifestError(RuntimeError):
    """Raised when retained Phase 4 attempt evidence is unsafe or inconsistent."""


def _checksum(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError(f"{field} must be a lowercase SHA-256 checksum")
    return value


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def odds_weather_attempt_manifest_relpath(run_id: str, phase_attempt: int) -> str:
    safe_run = validate_run_id(run_id)
    if (
        isinstance(phase_attempt, bool)
        or not isinstance(phase_attempt, int)
        or phase_attempt < 1
    ):
        raise ValueError("phase_attempt must be a positive integer")
    return validate_artifact_relpath(
        f"odds_weather/attempts/{safe_run}/attempt_{phase_attempt:04d}.json"
    )


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ValueError("manifest inventory contains a non-JSON value")


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _inventory(values: Iterable[Mapping[str, object]], field: str) -> tuple[Mapping[str, object], ...]:
    original = tuple(values)
    frozen: list[Mapping[str, object]] = []
    for item in original:
        if not isinstance(item, Mapping):
            raise ValueError(f"{field} must contain mappings")
        value = _freeze(dict(item))
        assert isinstance(value, Mapping)
        frozen.append(value)
    return tuple(frozen)


def _warning_inventory(values: Iterable[OddsWeatherWarningV1]) -> tuple[OddsWeatherWarningV1, ...]:
    result = tuple(values)
    if any(not isinstance(item, OddsWeatherWarningV1) for item in result):
        raise ValueError("manifest warnings must contain OddsWeatherWarningV1 values")
    return result


@dataclass(frozen=True, slots=True)
class OddsWeatherAttemptManifestV1:
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
    outcome: OddsWeatherAttemptOutcome | str
    snapshot_checksum: str | None
    raw_capture_inventory: tuple[Mapping[str, object], ...]
    provider_event_revision_inventory: tuple[Mapping[str, object], ...]
    odds_revision_inventory: tuple[Mapping[str, object], ...]
    weather_revision_inventory: tuple[Mapping[str, object], ...]
    selected_raw_capture_checksums: tuple[str, ...]
    source_warnings: tuple[OddsWeatherWarningV1, ...]
    final_warnings: tuple[OddsWeatherWarningV1, ...]
    retained_inventory_checksum: str
    created_at: datetime
    completed_at: datetime
    contract_version: str = ODDS_WEATHER_ATTEMPT_MANIFEST_CONTRACT

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", validate_run_id(self.run_id))
        if (
            isinstance(self.phase_attempt, bool)
            or not isinstance(self.phase_attempt, int)
            or self.phase_attempt < 1
        ):
            raise ValueError("phase_attempt must be a positive integer")
        object.__setattr__(self, "requested_date", parse_requested_date(self.requested_date).isoformat())
        object.__setattr__(self, "as_of_time", _aware(self.as_of_time, "as_of_time"))
        object.__setattr__(self, "observed_at", _aware(self.observed_at, "observed_at"))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "completed_at", _aware(self.completed_at, "completed_at"))
        if self.completed_at < self.created_at:
            raise ValueError("completed_at cannot precede created_at")
        for field in (
            "phase_input_checksum",
            "upstream_daily_slate_checksum",
            "upstream_game_state_checksum",
            "upstream_baseball_intelligence_checksum",
            "retained_inventory_checksum",
        ):
            object.__setattr__(self, field, _checksum(getattr(self, field), field))
        for field in (
            "upstream_daily_slate_snapshot_id",
            "upstream_game_state_snapshot_id",
            "upstream_baseball_intelligence_snapshot_id",
        ):
            value = getattr(self, field)
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"{field} must be a non-empty trimmed string")
        try:
            selected_outcome = OddsWeatherAttemptOutcome(self.outcome)
        except ValueError as exc:
            raise ValueError("unsupported Odds Weather attempt outcome") from exc
        object.__setattr__(self, "outcome", selected_outcome)
        if selected_outcome is OddsWeatherAttemptOutcome.ASSEMBLED:
            if self.snapshot_checksum is None:
                raise ValueError("assembled attempt requires snapshot_checksum")
            object.__setattr__(self, "snapshot_checksum", _checksum(self.snapshot_checksum, "snapshot_checksum"))
        elif self.snapshot_checksum is not None:
            raise ValueError("failed attempt must not contain snapshot_checksum")
        for field in (
            "raw_capture_inventory",
            "provider_event_revision_inventory",
            "odds_revision_inventory",
            "weather_revision_inventory",
        ):
            object.__setattr__(self, field, _inventory(getattr(self, field), field))
        selected = tuple(sorted({_checksum(item, "selected raw checksum") for item in self.selected_raw_capture_checksums}))
        object.__setattr__(self, "selected_raw_capture_checksums", selected)
        object.__setattr__(self, "source_warnings", _warning_inventory(self.source_warnings))
        object.__setattr__(self, "final_warnings", _warning_inventory(self.final_warnings))
        if self.contract_version != ODDS_WEATHER_ATTEMPT_MANIFEST_CONTRACT:
            raise ValueError("invalid Odds Weather attempt manifest contract_version")
        if redact_value(self.as_dict()) != self.as_dict():
            raise ValueError("Odds Weather attempt manifest contains credential-bearing material")

    @classmethod
    def from_inventory(
        cls,
        *,
        inventory: OddsWeatherRetainedEvidenceInventoryV1,
        outcome: OddsWeatherAttemptOutcome | str,
        snapshot_checksum: str | None,
        created_at: datetime,
        completed_at: datetime,
    ) -> OddsWeatherAttemptManifestV1:
        return cls(
            run_id=inventory.run_id,
            phase_attempt=inventory.phase_attempt,
            requested_date=inventory.requested_date,
            as_of_time=inventory.as_of_time,
            observed_at=inventory.observed_at,
            phase_input_checksum=inventory.phase_input_checksum,
            upstream_daily_slate_snapshot_id=inventory.upstream_daily_slate_snapshot_id,
            upstream_daily_slate_checksum=inventory.upstream_daily_slate_checksum,
            upstream_game_state_snapshot_id=inventory.upstream_game_state_snapshot_id,
            upstream_game_state_checksum=inventory.upstream_game_state_checksum,
            upstream_baseball_intelligence_snapshot_id=inventory.upstream_baseball_intelligence_snapshot_id,
            upstream_baseball_intelligence_checksum=inventory.upstream_baseball_intelligence_checksum,
            outcome=outcome,
            snapshot_checksum=snapshot_checksum,
            raw_capture_inventory=tuple(item.as_dict() for item in inventory.raw_captures),
            provider_event_revision_inventory=tuple(item.identity_dict() for item in inventory.provider_events),
            odds_revision_inventory=inventory.odds_revision_inventory,
            weather_revision_inventory=tuple(item.identity_dict() for item in inventory.weather_revisions),
            selected_raw_capture_checksums=inventory.selected_raw_capture_checksums,
            source_warnings=inventory.source_warnings,
            final_warnings=inventory.final_warnings,
            retained_inventory_checksum=inventory.checksum,
            created_at=created_at,
            completed_at=completed_at,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "as_of_time": self.as_of_time.isoformat(),
            "completed_at": self.completed_at.isoformat(),
            "contract_version": self.contract_version,
            "created_at": self.created_at.isoformat(),
            "final_warnings": [item.as_dict() for item in self.final_warnings],
            "observed_at": self.observed_at.isoformat(),
            "odds_revision_inventory": [_thaw(item) for item in self.odds_revision_inventory],
            "outcome": OddsWeatherAttemptOutcome(self.outcome).value,
            "phase_attempt": self.phase_attempt,
            "phase_input_checksum": self.phase_input_checksum,
            "phase_key": "odds_weather",
            "provider_event_revision_inventory": [_thaw(item) for item in self.provider_event_revision_inventory],
            "raw_capture_inventory": [_thaw(item) for item in self.raw_capture_inventory],
            "requested_date": self.requested_date,
            "retained_inventory_checksum": self.retained_inventory_checksum,
            "run_id": self.run_id,
            "selected_raw_capture_checksums": list(self.selected_raw_capture_checksums),
            "snapshot_checksum": self.snapshot_checksum,
            "source_warnings": [item.as_dict() for item in self.source_warnings],
            "upstream_baseball_intelligence_checksum": self.upstream_baseball_intelligence_checksum,
            "upstream_baseball_intelligence_snapshot_id": self.upstream_baseball_intelligence_snapshot_id,
            "upstream_daily_slate_checksum": self.upstream_daily_slate_checksum,
            "upstream_daily_slate_snapshot_id": self.upstream_daily_slate_snapshot_id,
            "upstream_game_state_checksum": self.upstream_game_state_checksum,
            "upstream_game_state_snapshot_id": self.upstream_game_state_snapshot_id,
            "weather_revision_inventory": [_thaw(item) for item in self.weather_revision_inventory],
        }

    def canonical_json_bytes(self) -> bytes:
        return canonical_json_bytes(self.as_dict())


@dataclass(frozen=True, slots=True)
class OddsWeatherAttemptManifestArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def publish_odds_weather_attempt_manifest(
    manifest: OddsWeatherAttemptManifestV1,
    artifact_root: Path,
) -> tuple[OddsWeatherAttemptManifestArtifactV1, bool]:
    """Atomically publish immutable attempt evidence and report ownership."""

    relpath = odds_weather_attempt_manifest_relpath(manifest.run_id, manifest.phase_attempt)
    content = manifest.canonical_json_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    try:
        destination = resolve_contained_path(artifact_root, relpath)
        created = atomic_create_bytes(destination, content)
    except (OSError, UnsafeArtifactPath) as exc:
        raise OddsWeatherAttemptManifestError(
            "Odds Weather attempt manifest could not be atomically published"
        ) from exc
    artifact = OddsWeatherAttemptManifestArtifactV1(relpath, checksum, len(content))
    if not created:
        retained = verify_odds_weather_attempt_manifest(
            artifact_root=artifact_root,
            relpath=relpath,
            expected=manifest,
        )
        if retained != artifact:
            raise OddsWeatherAttemptManifestError(
                "immutable Odds Weather attempt manifest conflicts"
            )
        return retained, False
    try:
        return (
            verify_odds_weather_attempt_manifest(
                artifact_root=artifact_root,
                relpath=relpath,
                expected=manifest,
            ),
            True,
        )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


def write_odds_weather_attempt_manifest(
    manifest: OddsWeatherAttemptManifestV1,
    artifact_root: Path,
) -> OddsWeatherAttemptManifestArtifactV1:
    return publish_odds_weather_attempt_manifest(manifest, artifact_root)[0]


def verify_odds_weather_attempt_manifest(
    *,
    artifact_root: Path,
    relpath: str,
    expected: OddsWeatherAttemptManifestV1,
) -> OddsWeatherAttemptManifestArtifactV1:
    try:
        safe_relpath = validate_artifact_relpath(relpath)
        if safe_relpath != odds_weather_attempt_manifest_relpath(
            expected.run_id, expected.phase_attempt
        ):
            raise OddsWeatherAttemptManifestError(
                "attempt manifest path does not match run and attempt identity"
            )
        path = resolve_contained_path(artifact_root, safe_relpath)
        metadata = path.stat()
        content = path.read_bytes()
    except (FileNotFoundError, OSError, UnsafeArtifactPath) as exc:
        raise OddsWeatherAttemptManifestError("attempt manifest is missing or unsafe") from exc
    if content != expected.canonical_json_bytes():
        raise OddsWeatherAttemptManifestError(
            "attempt manifest bytes do not match canonical attempt evidence"
        )
    if getattr(metadata, "st_nlink", 1) != 1:
        raise OddsWeatherAttemptManifestError(
            "attempt manifest uses an unsafe hard link"
        )
    try:
        parsed: Any = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OddsWeatherAttemptManifestError("attempt manifest is not valid UTF-8 JSON") from exc
    if (
        not isinstance(parsed, Mapping)
        or canonical_json_bytes(dict(parsed)) != content
        or redact_value(parsed) != parsed
    ):
        raise OddsWeatherAttemptManifestError(
            "attempt manifest is noncanonical or credential-bearing"
        )
    return OddsWeatherAttemptManifestArtifactV1(
        safe_relpath,
        hashlib.sha256(content).hexdigest(),
        len(content),
    )
