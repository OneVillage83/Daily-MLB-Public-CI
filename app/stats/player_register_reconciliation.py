from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from app.stats.contracts import RawArtifact, StatsProviderPayloadError
from app.stats.providers.player_register import PlayerRegisterDataset
from app.stats.raw_store import RawArtifactStore


PLAYER_REGISTER_PIN_RECONCILIATION_CONTRACT = (
    "DSE_PLAYER_REGISTER_PIN_RECONCILIATION_V1"
)
PLAYER_REGISTER_MEMBER_INVENTORY_CONTRACT = (
    "DSE_PLAYER_REGISTER_MEMBER_INVENTORY_V1"
)
PLAYER_REGISTER_SEMANTIC_INVENTORY_CONTRACT = (
    "DSE_PLAYER_REGISTER_SEMANTIC_INVENTORY_V1"
)
PLAYER_REGISTER_RECONCILIATION_PROVIDER = "player_register_reconciliation"
PLAYER_REGISTER_RECONCILIATION_DATASET_KEY = "player_register_pin_reconciliation"
LEGACY_PLAYER_REGISTER_ENDPOINT_CATEGORY = "player_identifier_register"


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_checksum(value: object) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalized_member_path(name: str) -> str:
    source = name.replace("\\", "/")
    path = PurePosixPath(source)
    if (
        path.is_absolute()
        or len(path.parts) < 2
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ValueError("player register archive contains an unsafe member path")
    normalized = PurePosixPath(*path.parts[1:]).as_posix()
    if not normalized:
        raise ValueError("player register archive member has no normalized path")
    return normalized


def build_member_inventory(
    artifact: RawArtifact,
    raw_store: RawArtifactStore,
) -> dict[str, Any]:
    payload = raw_store.read_verified(artifact)
    entries: list[dict[str, object]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            normalized_paths: set[str] = set()
            for member in sorted(archive.infolist(), key=lambda value: value.filename):
                if member.is_dir():
                    continue
                if member.flag_bits & 0x1:
                    raise ValueError("player register archive contains encrypted members")
                file_type = (member.external_attr >> 16) & 0o170000
                if file_type == stat.S_IFLNK:
                    raise ValueError("player register archive contains symbolic links")
                normalized_path = _normalized_member_path(member.filename)
                if normalized_path in normalized_paths:
                    raise ValueError(
                        "player register archive has duplicate normalized member paths"
                    )
                normalized_paths.add(normalized_path)
                member_bytes = archive.read(member)
                entries.append(
                    {
                        "normalized_path": normalized_path,
                        "size_bytes": len(member_bytes),
                        "uncompressed_sha256": hashlib.sha256(
                            member_bytes
                        ).hexdigest(),
                    }
                )
    except (OSError, RuntimeError, zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise StatsProviderPayloadError(
            "player register archive inventory validation failed",
            capture=artifact,
        ) from exc
    if not entries:
        raise StatsProviderPayloadError(
            "player register archive inventory is empty",
            capture=artifact,
        )
    root = {
        "contract": PLAYER_REGISTER_MEMBER_INVENTORY_CONTRACT,
        "entries": entries,
    }
    return {
        **root,
        "member_count": len(entries),
        "inventory_sha256": canonical_checksum(root),
    }


def build_semantic_inventory(dataset: PlayerRegisterDataset) -> dict[str, object]:
    mappings = [
        {
            "baseball_reference_id": mapping.baseball_reference_id,
            "fangraphs_id": mapping.fangraphs_id,
            "first_name": mapping.first_name,
            "last_name": mapping.last_name,
            "mlbam_id": mapping.mlbam_id,
            "retrosheet_id": mapping.retrosheet_id,
            "source_member": _normalized_member_path(mapping.source_member),
            "source_row_number": mapping.source_row_number,
        }
        for mapping in dataset.mappings
    ]
    mappings.sort(
        key=lambda value: (
            str(value["retrosheet_id"]),
            str(value["mlbam_id"]),
            str(value["source_member"]),
            str(value["source_row_number"]),
        )
    )
    root = {
        "contract": PLAYER_REGISTER_SEMANTIC_INVENTORY_CONTRACT,
        "mappings": mappings,
    }
    return {
        "contract": PLAYER_REGISTER_SEMANTIC_INVENTORY_CONTRACT,
        "mapping_count": len(mappings),
        "semantic_sha256": canonical_checksum(root),
        "source_member_count": dataset.source_member_count,
        "source_row_count": dataset.source_row_count,
    }


def build_register_inventory(
    dataset: PlayerRegisterDataset,
    raw_store: RawArtifactStore,
) -> dict[str, object]:
    return {
        "member_inventory": build_member_inventory(dataset.raw, raw_store),
        "semantic_inventory": build_semantic_inventory(dataset),
    }


def inventories_match(
    source: Mapping[str, object],
    pinned: Mapping[str, object],
) -> bool:
    return dict(source) == dict(pinned)
