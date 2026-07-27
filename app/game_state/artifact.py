from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.game_state.contracts import GameStateContractError, GameStateV1
from app.redaction import redact_value

GAME_STATE_ARTIFACT_RELPATH = (
    "game_state/snapshots/{snapshot_checksum}/game_state_v1.json"
)


@dataclass(frozen=True, slots=True)
class GameStateArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def game_state_artifact_relpath(state: GameStateV1) -> str:
    return GAME_STATE_ARTIFACT_RELPATH.format(snapshot_checksum=state.checksum)


def write_game_state_artifact(
    state: GameStateV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> GameStateArtifactV1:
    selected_relpath = (
        game_state_artifact_relpath(state) if relpath is None else relpath
    )
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = state.as_dict()
    if redact_value(payload, secret_values) != payload:
        raise GameStateContractError(
            "GameStateV1 artifact contains credential-bearing material"
        )
    content = state.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return GameStateArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
