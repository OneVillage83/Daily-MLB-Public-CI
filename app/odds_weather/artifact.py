from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
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


def odds_weather_artifact_relpath(snapshot: OddsWeatherV1) -> str:
    return ODDS_WEATHER_ARTIFACT_RELPATH.format(snapshot_checksum=snapshot.checksum)


def write_odds_weather_artifact(
    snapshot: OddsWeatherV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> OddsWeatherArtifactV1:
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
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return OddsWeatherArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
