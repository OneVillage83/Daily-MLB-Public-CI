from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.exporter import atomic_create_bytes
from app.odds_weather.contracts import OddsWeatherContractError, OddsWeatherV1
from app.redaction import redact_value

ODDS_WEATHER_ARTIFACT_RELPATH = (
    "odds_weather/snapshots/{snapshot_checksum}/odds_weather_v1.json"
)


@dataclass(frozen=True, slots=True)
class OddsWeatherArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


class OddsWeatherArtifactIntegrityError(RuntimeError):
    """Raised when retained Odds + Weather artifact evidence is invalid."""


def odds_weather_artifact_relpath(snapshot: OddsWeatherV1) -> str:
    return ODDS_WEATHER_ARTIFACT_RELPATH.format(snapshot_checksum=snapshot.checksum)


def write_odds_weather_artifact(
    snapshot: OddsWeatherV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> OddsWeatherArtifactV1:
    artifact, _ = publish_odds_weather_artifact(
        snapshot,
        artifact_root,
        relpath=relpath,
        secret_values=secret_values,
    )
    return artifact


def publish_odds_weather_artifact(
    snapshot: OddsWeatherV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> tuple[OddsWeatherArtifactV1, bool]:
    """Atomically publish immutable content-addressed Phase 4 bytes."""

    selected_relpath = (
        odds_weather_artifact_relpath(snapshot) if relpath is None else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = snapshot.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise OddsWeatherContractError(
            "Odds + Weather artifact contains credential-bearing material"
        )
    content = snapshot.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    artifact = OddsWeatherArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
    try:
        created = atomic_create_bytes(destination, content)
    except (OSError, ValueError) as exc:
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact could not be atomically published"
        ) from exc
    if not created:
        verify_odds_weather_artifact(
            snapshot,
            artifact,
            artifact_root,
            secret_values=secret_values,
        )
        return artifact, False
    try:
        verify_odds_weather_artifact(
            snapshot,
            artifact,
            artifact_root,
            secret_values=secret_values,
        )
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return artifact, True


def verify_odds_weather_artifact(
    snapshot: OddsWeatherV1,
    artifact: OddsWeatherArtifactV1,
    artifact_root: Path,
    *,
    secret_values: tuple[str, ...] = (),
) -> Path:
    """Verify exact content-addressed Phase 4 bytes without network access."""

    safe_relpath = validate_artifact_relpath(artifact.relpath)
    if safe_relpath != odds_weather_artifact_relpath(snapshot):
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact path does not match its semantic checksum"
        )
    payload = snapshot.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact contains credential-bearing material"
        )
    try:
        destination = resolve_contained_path(artifact_root, safe_relpath)
        metadata = destination.stat()
        content = destination.read_bytes()
    except (FileNotFoundError, ValueError) as exc:
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact is missing or unsafe"
        ) from exc
    expected = snapshot.canonical_json_bytes()
    if getattr(metadata, "st_nlink", 1) != 1:
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact uses an unsafe hard link"
        )
    if (
        content != expected
        or hashlib.sha256(content).hexdigest() != artifact.checksum
        or len(content) != artifact.byte_count
    ):
        raise OddsWeatherArtifactIntegrityError(
            "Odds + Weather artifact bytes, checksum, or byte count do not match"
        )
    return destination
