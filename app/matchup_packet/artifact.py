from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from app.matchup_packet.contracts import MatchupPacketContractError, MatchupPacketV1
from app.pre_model_evidence import PreModelArtifactV1, publish_canonical_bytes, verify_canonical_bytes
from app.redaction import redact_value

MATCHUP_PACKET_ARTIFACT_RELPATH = (
    "matchup_packet/snapshots/{packet_checksum}/matchup_packet_v1.json"
)


@dataclass(frozen=True, slots=True)
class MatchupPacketArtifactV1:
    relpath: str
    checksum: str
    byte_count: int
    created: bool = field(default=False, compare=False, repr=False)


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
    payload = packet.as_dict()
    if redact_value(
        payload,
        secret_values,
        preserve_field_names=("key",),
    ) != payload:
        raise MatchupPacketContractError(
            "MatchupPacket artifact contains credential-bearing material"
        )
    artifact = publish_canonical_bytes(
        artifact_root, selected_relpath, packet.canonical_json_bytes()
    )
    return MatchupPacketArtifactV1(
        artifact.relpath, artifact.checksum, artifact.byte_count, artifact.created
    )


def verify_matchup_packet_artifact(
    packet: MatchupPacketV1,
    artifact: MatchupPacketArtifactV1,
    artifact_root: Path,
) -> None:
    if artifact.relpath != matchup_packet_artifact_relpath(packet):
        raise MatchupPacketContractError("Matchup Packet artifact path identity mismatch")
    verify_canonical_bytes(
        artifact_root,
        PreModelArtifactV1(artifact.relpath, artifact.checksum, artifact.byte_count),
        packet.canonical_json_bytes(),
    )
