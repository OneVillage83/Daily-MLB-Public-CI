from __future__ import annotations

import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import app.pre_model_evidence as evidence_module
from app.daily_slate.contracts import canonical_sha256

from app.pre_model_evidence import (
    PreModelAttemptManifestV1,
    PreModelEvidenceError,
    PreModelUpstreamIdentityV1,
    cleanup_owned_artifact,
    publish_canonical_bytes,
    publish_manifest,
    verify_canonical_bytes,
    verify_manifest,
)

RUN_ID = "run_20260730_22222222222222222222222222222222"
NOW = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
CHECKSUM = "a" * 64


def _policy_evidence() -> dict[str, object]:
    identity: dict[str, object] = {
        "network_enabled": False,
        "policy_version": "DSE_DATA_QUALITY_POLICY_V1",
        "supported_markets": ["h2h", "spreads", "totals"],
    }
    return {**identity, "checksum": canonical_sha256(identity)}


def _upstream() -> tuple[PreModelUpstreamIdentityV1, ...]:
    return tuple(
        PreModelUpstreamIdentityV1(phase, f"{phase}:snapshot", CHECKSUM)
        for phase in (
            "daily_slate",
            "game_state",
            "baseball_intelligence_assembly",
            "odds_weather",
        )
    )


def _manifest(**changes: object) -> PreModelAttemptManifestV1:
    values: dict[str, object] = {
        "contract_version": "DSE_DATA_QUALITY_ATTEMPT_MANIFEST_V1",
        "phase_key": "data_quality",
        "run_id": RUN_ID,
        "phase_attempt": 1,
        "requested_date": "2026-07-30",
        "as_of_time": NOW,
        "observed_at": NOW + timedelta(minutes=1),
        "phase_input_checksum": CHECKSUM,
        "upstream": _upstream(),
        "outcome": "assembled",
        "snapshot_checksum": CHECKSUM,
        "warnings": (),
        "created_at": NOW + timedelta(minutes=2),
        "completed_at": NOW + timedelta(minutes=2),
        "phase_input_evidence": {"policy": _policy_evidence()},
    }
    values.update(changes)
    return PreModelAttemptManifestV1(**values)  # type: ignore[arg-type]


def test_hard_linked_snapshot_artifact_and_cleanup_are_rejected(tmp_path: Path) -> None:
    content = b'{"canonical":true}'
    artifact = publish_canonical_bytes(
        tmp_path,
        f"data_quality/snapshots/{CHECKSUM}/data_quality_v1.json",
        content,
    )
    final_path = tmp_path / artifact.relpath
    linked_path = tmp_path / "retained-hard-link.json"
    try:
        os.link(final_path, linked_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(PreModelEvidenceError, match="hard link"):
        verify_canonical_bytes(tmp_path, artifact, content)
    cleanup_owned_artifact(tmp_path, artifact)
    assert final_path.exists()
    assert linked_path.exists()


def test_hard_linked_attempt_manifest_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    artifact = publish_manifest(manifest, tmp_path, "data_quality")
    linked_path = tmp_path / "manifest-hard-link.json"
    try:
        os.link(tmp_path / artifact.relpath, linked_path)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(PreModelEvidenceError, match="hard link"):
        verify_manifest(manifest, artifact, tmp_path)


def test_standalone_manifest_verifier_rejects_configured_secret_bytes(
    tmp_path: Path,
) -> None:
    secret = "configured-provider-secret"
    manifest = _manifest(
        warnings=({"code": "retained_warning", "message": f"prefix {secret} suffix"},)
    )
    artifact = publish_manifest(manifest, tmp_path, "data_quality")
    with pytest.raises(PreModelEvidenceError, match="credential-bearing"):
        verify_manifest(manifest, artifact, tmp_path, secret_values=(secret,))


@pytest.mark.parametrize(
    "changes",
    (
        {"phase_key": "odds_weather"},
        {"outcome": "assembly_failed", "snapshot_checksum": None},
        {"outcome": "assembled", "snapshot_checksum": None},
        {"outcome": "assessment_failed", "snapshot_checksum": CHECKSUM},
        {"phase_attempt": True},
        {"phase_attempt": 1.5},
        {"completed_at": NOW, "created_at": NOW + timedelta(seconds=1)},
        {"upstream": tuple(reversed(_upstream()))},
    ),
)
def test_manifest_profiles_fail_closed(changes: dict[str, object]) -> None:
    with pytest.raises(PreModelEvidenceError):
        _manifest(**changes)


def test_manifest_secret_inventory_is_validation_only() -> None:
    clean = _manifest(
        warnings=({"bookmaker_key": "draftkings", "market_key": "h2h"},)
    )
    with_secrets = _manifest(
        warnings=({"bookmaker_key": "draftkings", "market_key": "h2h"},),
        secret_values=("not-present-one", "not-present-two"),
    )
    assert clean == with_secrets
    assert clean.as_dict() == with_secrets.as_dict()
    assert clean.canonical_json_bytes() == with_secrets.canonical_json_bytes()


@pytest.mark.parametrize(
    "phase_key,contract,outcome,phases",
    (
        (
            "data_quality",
            "DSE_DATA_QUALITY_ATTEMPT_MANIFEST_V1",
            "assessment_failed",
            ("daily_slate", "game_state", "baseball_intelligence_assembly", "odds_weather"),
        ),
        (
            "matchup_packet",
            "DSE_MATCHUP_PACKET_ATTEMPT_MANIFEST_V1",
            "assembly_failed",
            ("daily_slate", "game_state", "baseball_intelligence_assembly", "odds_weather", "data_quality"),
        ),
        (
            "model_feature_set",
            "DSE_MODEL_FEATURE_SET_ATTEMPT_MANIFEST_V1",
            "transformation_failed",
            ("data_quality", "matchup_packet"),
        ),
    ),
)
def test_each_phase_manifest_profile_requires_exact_contract_outcome_and_order(
    phase_key: str,
    contract: str,
    outcome: str,
    phases: tuple[str, ...],
) -> None:
    upstream = tuple(
        PreModelUpstreamIdentityV1(value, f"{value}:snapshot", CHECKSUM)
        for value in phases
    )
    manifest = PreModelAttemptManifestV1(
        contract_version=contract,
        phase_key=phase_key,
        run_id=RUN_ID,
        phase_attempt=1,
        requested_date="2026-07-30",
        as_of_time=NOW,
        observed_at=NOW,
        phase_input_checksum=CHECKSUM,
        upstream=upstream,
        outcome=outcome,
        snapshot_checksum=None,
        warnings=(),
        created_at=NOW,
        completed_at=NOW,
        phase_input_evidence=(
            {"policy": _policy_evidence()}
            if phase_key == "data_quality"
            else {
                "assembly_policy_version": "DSE_MATCHUP_PACKET_ASSEMBLY_POLICY_V1"
            }
            if phase_key == "matchup_packet"
            else {
                "inventory_validation_state": "validated",
                "selected_feature_inventory": [],
                "selected_feature_inventory_checksum": canonical_sha256([]),
            }
        ),
    )
    assert manifest.outcome == outcome
    with pytest.raises(PreModelEvidenceError):
        replace(manifest, upstream=tuple(reversed(upstream)))
    with pytest.raises(PreModelEvidenceError):
        replace(manifest, outcome="not_a_phase_outcome")


def test_cleanup_never_removes_preexisting_exact_artifact(tmp_path: Path) -> None:
    content = b'{"canonical":true}'
    first = publish_canonical_bytes(tmp_path, "data_quality/exact.json", content)
    replay = publish_canonical_bytes(tmp_path, "data_quality/exact.json", content)
    assert first.created is True and replay.created is False
    cleanup_owned_artifact(tmp_path, replay)
    assert (tmp_path / replay.relpath).read_bytes() == content


def test_cleanup_never_removes_conflicting_artifact(tmp_path: Path) -> None:
    target = tmp_path / "data_quality/conflict.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"conflict")
    with pytest.raises(Exception):
        publish_canonical_bytes(tmp_path, "data_quality/conflict.json", b"expected")
    assert target.read_bytes() == b"conflict"


def test_cleanup_identity_race_quietly_preserves_original_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = b'{"canonical":true}'
    artifact = publish_canonical_bytes(
        tmp_path, "data_quality/cleanup-race.json", content
    )
    path = tmp_path / artifact.relpath
    monkeypatch.setattr(evidence_module, "_read_owned_bytes", lambda value: content)

    def changed_identity(value: Path) -> os.stat_result:
        raise PreModelEvidenceError("ownership changed before unlink")

    monkeypatch.setattr(evidence_module, "_safe_file_identity", changed_identity)
    original = RuntimeError("primary persistence failure")
    try:
        raise original
    except RuntimeError as caught:
        cleanup_owned_artifact(tmp_path, artifact)
        assert caught is original
    assert path.exists()
