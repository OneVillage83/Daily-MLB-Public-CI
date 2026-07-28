from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.matchup_packet.contracts import MatchupPacketContractError, MatchupPacketV1
from app.redaction import redact_value

MATCHUP_PACKET_ARTIFACT_RELPATH = (
    "matchup_packet/snapshots/{packet_checksum}/matchup_packet_v1.json"
)


@dataclass(frozen=True, slots=True)
class MatchupPacketArtifactV1:
    relpath: str
    checksum: str
    byte_count: int


def matchup_packet_artifact_relpath(packet: MatchupPacketV1) -> str:
    return MATCHUP_PACKET_ARTIFACT_RELPATH.format(packet_checksum=packet.checksum)


def write_matchup_packet_artifact(
    packet: MatchupPacketV1,
    artifact_root: Path,
    *,
    relpath: str | None = None,
    secret_values: tuple[str, ...] = (),
) -> MatchupPacketArtifactV1:
    selected_relpath = matchup_packet_artifact_relpath(packet) if relpath is None else relpath
    safe_relpath = validate_artifact_relpath(selected_relpath)
    payload = packet.as_dict()
    if redact_value(
        payload,
        secret_values,
        preserve_field_names=("key",),
    ) != payload:
        raise MatchupPacketContractError(
            "MatchupPacket artifact contains credential-bearing material"
        )
    content = packet.canonical_json_bytes()
    destination = resolve_contained_path(artifact_root, safe_relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return MatchupPacketArtifactV1(
        relpath=safe_relpath,
        checksum=hashlib.sha256(content).hexdigest(),
        byte_count=len(content),
    )
