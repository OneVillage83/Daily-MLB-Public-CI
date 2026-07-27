from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.artifacts import resolve_contained_path, validate_artifact_relpath
from app.game_state.acquisition import MlbGameFeedEvidenceV1, build_mlb_game_feed_request
from app.game_state.batch import GameStateBatchAcquisitionError, MlbGameStateEvidenceSetV1
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text, redact_value
from app.stats.contracts import RawArtifact, StatsResponse

GAME_STATE_RAW_LINK_CONTRACT = "DSE_GAME_STATE_RAW_LINK_V1"


class GameStateRawLinkOutcome(StrEnum):
    NORMALIZED = "normalized"
    ACQUISITION_FAILED = "acquisition_failed"
    NORMALIZATION_FAILED = "normalization_failed"


def _sha256(value: str, field_name: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{field_name} must be 64 lowercase hexadecimal characters")
    return value


def game_state_raw_link_relpath(run_id: str, phase_attempt: int) -> str:
    safe_run_id = validate_run_id(run_id)
    if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
        raise ValueError("phase_attempt must be a positive integer")
    return validate_artifact_relpath(
        f"game_state/raw_links/{safe_run_id}/attempt_{phase_attempt:04d}.json"
    )


def _relative_raw_path(path: Path, artifact_root: Path, field_name: str) -> str:
    resolved_root = artifact_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"{field_name} must remain inside the artifact root") from exc
    return validate_artifact_relpath(relative.as_posix())


def _capture_payload(
    capture: RawArtifact,
    *,
    artifact_root: Path,
    ordinal: int,
) -> dict[str, object]:
    return {
        "capture_id": capture.capture_id,
        "content_type": capture.content_type,
        "endpoint_category": capture.endpoint_category,
        "ordinal": ordinal,
        "parent_checksum_sha256": capture.parent_checksum_sha256,
        "provider": capture.provider.value,
        "raw_artifact_relpath": _relative_raw_path(
            capture.path,
            artifact_root,
            "raw artifact",
        ),
        "raw_checksum_sha256": capture.checksum_sha256,
        "raw_metadata_relpath": _relative_raw_path(
            capture.metadata_path,
            artifact_root,
            "raw artifact metadata",
        ),
        "raw_size_bytes": capture.size_bytes,
        "retrieved_at": capture.retrieved_at.isoformat(),
    }


def _response_captures(response: StatsResponse) -> tuple[RawArtifact, ...]:
    return response.attempt_captures or (response.capture,)


def _evidence_payload(
    evidence: MlbGameFeedEvidenceV1,
    *,
    artifact_root: Path,
) -> dict[str, object]:
    captures = _response_captures(evidence.response)
    return {
        "captures": [
            _capture_payload(capture, artifact_root=artifact_root, ordinal=index)
            for index, capture in enumerate(captures, start=1)
        ],
        "final_raw_checksum_sha256": evidence.raw_checksum,
        "http_attempts": evidence.response.attempts,
        "http_status": evidence.response.status_code,
        "observed_at": evidence.observed_at.isoformat(),
        "request": {
            "method": "GET",
            "params": dict(evidence.request.params),
            "url": evidence.request.url,
        },
        "response_headers": dict(evidence.response.headers),
        "source_game_id": evidence.source_game_id,
    }


def _ordered_evidence_payloads(
    evidence_by_game: Mapping[str, MlbGameFeedEvidenceV1],
    *,
    artifact_root: Path,
) -> list[dict[str, object]]:
    return [
        _evidence_payload(evidence, artifact_root=artifact_root)
        for evidence in evidence_by_game.values()
    ]


def _failure_payload(
    error: BaseException,
    *,
    source_game_id: str | None,
    attempts: int | None,
    status_code: int | None,
    captures: tuple[RawArtifact, ...],
    artifact_root: Path,
    secret_values: tuple[str, ...],
) -> dict[str, object]:
    request: dict[str, object] | None = None
    if source_game_id is not None and source_game_id.isascii() and source_game_id.isdecimal():
        failed_request = build_mlb_game_feed_request(source_game_id)
        request = {
            "method": "GET",
            "params": dict(failed_request.params),
            "url": failed_request.url,
        }
    return {
        "attempts": attempts,
        "captures": [
            _capture_payload(capture, artifact_root=artifact_root, ordinal=index)
            for index, capture in enumerate(captures, start=1)
        ],
        "error_type": type(error).__name__,
        "message": redact_text(str(error), secret_values),
        "request": request,
        "source_game_id": source_game_id,
        "status_code": status_code,
    }


def write_game_state_raw_link(
    *,
    artifact_root: Path,
    run_id: str,
    requested_date: str,
    phase_attempt: int,
    upstream_daily_slate_checksum: str,
    outcome: GameStateRawLinkOutcome,
    evidence_set: MlbGameStateEvidenceSetV1 | None = None,
    batch_error: GameStateBatchAcquisitionError | None = None,
    game_state_checksum: str | None = None,
    normalization_error: BaseException | None = None,
    secret_values: Iterable[str] = (),
) -> str:
    relpath = game_state_raw_link_relpath(run_id, phase_attempt)
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    canonical_requested_date = parse_requested_date(requested_date).isoformat()
    safe_slate_checksum = _sha256(
        upstream_daily_slate_checksum,
        "upstream_daily_slate_checksum",
    )
    configured_secrets = tuple(str(value) for value in secret_values if str(value))

    if outcome is GameStateRawLinkOutcome.NORMALIZED:
        if evidence_set is None or batch_error is not None or normalization_error is not None:
            raise ValueError("normalized raw link requires only a completed evidence set")
        if game_state_checksum is None:
            raise ValueError("normalized raw link requires game_state_checksum")
        safe_state_checksum = _sha256(game_state_checksum, "game_state_checksum")
        failure = None
        evidence_by_game = evidence_set.evidence_by_game
    elif outcome is GameStateRawLinkOutcome.ACQUISITION_FAILED:
        if batch_error is None or evidence_set is not None or game_state_checksum is not None:
            raise ValueError("acquisition_failed raw link requires only batch_error")
        safe_state_checksum = None
        evidence_by_game = batch_error.completed_evidence
        failure = _failure_payload(
            batch_error,
            source_game_id=batch_error.failed_source_game_id,
            attempts=batch_error.attempts,
            status_code=batch_error.status_code,
            captures=batch_error.failure_captures,
            artifact_root=root,
            secret_values=configured_secrets,
        )
    elif outcome is GameStateRawLinkOutcome.NORMALIZATION_FAILED:
        if evidence_set is None or normalization_error is None or batch_error is not None:
            raise ValueError(
                "normalization_failed raw link requires evidence_set and normalization_error"
            )
        if game_state_checksum is not None:
            raise ValueError("normalization_failed raw link must not contain game_state_checksum")
        safe_state_checksum = None
        evidence_by_game = evidence_set.evidence_by_game
        failure = _failure_payload(
            normalization_error,
            source_game_id=None,
            attempts=None,
            status_code=None,
            captures=(),
            artifact_root=root,
            secret_values=configured_secrets,
        )
    else:
        raise ValueError("unsupported GameState raw-link outcome")

    if evidence_set is not None and evidence_set.slate_checksum != safe_slate_checksum:
        raise ValueError("GameState evidence set does not match upstream DailySlate checksum")

    payload: dict[str, Any] = {
        "contract_version": GAME_STATE_RAW_LINK_CONTRACT,
        "evidence_game_count": len(evidence_by_game),
        "failure": failure,
        "game_state_checksum": safe_state_checksum,
        "games": _ordered_evidence_payloads(
            evidence_by_game,
            artifact_root=root,
        ),
        "outcome": outcome.value,
        "phase_attempt": phase_attempt,
        "requested_date": canonical_requested_date,
        "run_id": validate_run_id(run_id),
        "upstream_daily_slate_checksum": safe_slate_checksum,
    }
    if redact_value(payload, configured_secrets) != payload:
        raise ValueError("GameState raw-link manifest contains credential-bearing material")

    content = json.dumps(
        payload,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    destination = resolve_contained_path(root, relpath)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        created = True
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            destination.unlink(missing_ok=True)
        raise
    return relpath
