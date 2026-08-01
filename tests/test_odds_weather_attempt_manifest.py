from __future__ import annotations

import hashlib
import os
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.odds_weather import (
    OddsWeatherAttemptManifestError,
    OddsWeatherAttemptManifestV1,
    OddsWeatherAttemptOutcome,
    OddsWeatherRetainedEvidenceInventoryV1,
    OddsWeatherWarningDomain,
    OddsWeatherWarningV1,
    odds_weather_attempt_manifest_relpath,
    publish_odds_weather_attempt_manifest,
    verify_odds_weather_attempt_manifest,
    write_odds_weather_attempt_manifest,
)


NOW = datetime(2026, 7, 31, 16, 0, tzinfo=timezone.utc)
RUN_ID = "run_20260731_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _checksum(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _inventory() -> OddsWeatherRetainedEvidenceInventoryV1:
    return OddsWeatherRetainedEvidenceInventoryV1(
        run_id=RUN_ID,
        phase_attempt=1,
        requested_date="2026-07-31",
        as_of_time=NOW,
        observed_at=NOW,
        phase_input_checksum=_checksum("input"),
        upstream_daily_slate_snapshot_id="daily-slate:fixture",
        upstream_daily_slate_checksum=_checksum("slate"),
        upstream_game_state_snapshot_id="game-state:fixture",
        upstream_game_state_checksum=_checksum("state"),
        upstream_baseball_intelligence_snapshot_id="bia:fixture",
        upstream_baseball_intelligence_checksum=_checksum("bia"),
        raw_captures=(),
        provider_events=(),
        weather_revisions=(),
    )


def _manifest() -> OddsWeatherAttemptManifestV1:
    return OddsWeatherAttemptManifestV1.from_inventory(
        inventory=_inventory(),
        outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
        snapshot_checksum=None,
        created_at=NOW,
        completed_at=NOW,
    )


def test_retained_inventory_checksum_binds_as_of_time() -> None:
    original = _inventory()
    changed = replace(original, as_of_time=original.as_of_time + timedelta(hours=1))

    assert original != changed
    assert original.identity_dict() != changed.identity_dict()
    assert original.checksum != changed.checksum


def test_manifest_constructor_rejects_configured_secret_in_warning() -> None:
    secret = "configured-secret-value"
    inventory = replace(
        _inventory(),
        source_warnings=(
            OddsWeatherWarningV1(
                code="collector_warning",
                domain=OddsWeatherWarningDomain.ODDS,
                message=f"retained warning contains {secret}",
            ),
        ),
    )

    with pytest.raises(ValueError, match="credential"):
        OddsWeatherAttemptManifestV1.from_inventory(
            inventory=inventory,
            outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
            snapshot_checksum=None,
            created_at=NOW,
            completed_at=NOW,
            secret_values=(secret,),
        )


@pytest.mark.parametrize(
    "location",
    (
        "source_warning",
        "final_warning",
        "raw_capture",
        "provider_event_revision",
        "odds_revision",
        "weather_revision",
        "upstream_identity",
    ),
)
def test_manifest_contract_rejects_configured_secret_in_every_identity_inventory(
    location: str,
) -> None:
    secret = "configured-secret-value"
    manifest = _manifest()
    warning = OddsWeatherWarningV1(
        code="retained_warning",
        domain=OddsWeatherWarningDomain.ODDS,
        message=f"valid surrounding warning text {secret}",
    )
    with pytest.raises(ValueError, match="credential"):
        if location == "source_warning":
            replace(manifest, source_warnings=(warning,), secret_values=(secret,))
        elif location == "final_warning":
            replace(manifest, final_warnings=(warning,), secret_values=(secret,))
        elif location == "raw_capture":
            replace(
                manifest,
                raw_capture_inventory=({"provider": f"provider-{secret}"},),
                secret_values=(secret,),
            )
        elif location == "provider_event_revision":
            replace(
                manifest,
                provider_event_revision_inventory=(
                    {"provider_event_id": f"event-{secret}"},
                ),
                secret_values=(secret,),
            )
        elif location == "odds_revision":
            replace(
                manifest,
                odds_revision_inventory=({"outcome_name": f"team-{secret}"},),
                secret_values=(secret,),
            )
        elif location == "weather_revision":
            replace(
                manifest,
                weather_revision_inventory=({"provider": f"nws-{secret}"},),
                secret_values=(secret,),
            )
        else:
            replace(
                manifest,
                upstream_daily_slate_snapshot_id=f"daily-slate:{secret}",
                secret_values=(secret,),
            )


def test_manifest_publish_write_and_verify_reject_configured_secret(
    tmp_path: Path,
) -> None:
    secret = "configured-secret-value"
    secret_manifest = replace(
        _manifest(),
        raw_capture_inventory=({"provider": f"provider-{secret}"},),
    )

    with pytest.raises(OddsWeatherAttemptManifestError, match="credential"):
        publish_odds_weather_attempt_manifest(
            secret_manifest,
            tmp_path / "publish",
            secret_values=(secret,),
        )
    with pytest.raises(OddsWeatherAttemptManifestError, match="credential"):
        write_odds_weather_attempt_manifest(
            secret_manifest,
            tmp_path / "write",
            secret_values=(secret,),
        )

    artifact, _ = publish_odds_weather_attempt_manifest(
        secret_manifest,
        tmp_path / "verify",
    )
    with pytest.raises(OddsWeatherAttemptManifestError, match="credential"):
        verify_odds_weather_attempt_manifest(
            artifact_root=tmp_path / "verify",
            relpath=artifact.relpath,
            expected=secret_manifest,
            secret_values=(secret,),
        )


def test_clean_manifest_identity_is_independent_of_configured_secret_inventory(
    tmp_path: Path,
) -> None:
    inventory = _inventory()
    manifests = tuple(
        OddsWeatherAttemptManifestV1.from_inventory(
            inventory=inventory,
            outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
            snapshot_checksum=None,
            created_at=NOW,
            completed_at=NOW,
            secret_values=secrets,
        )
        for secrets in ((), ("absent-secret-one",), ("absent-secret-two",))
    )
    artifacts = []
    for index, (manifest, secrets) in enumerate(
        zip(
            manifests,
            ((), ("absent-secret-one",), ("absent-secret-two",)),
            strict=True,
        )
    ):
        root = tmp_path / str(index)
        artifact, _ = publish_odds_weather_attempt_manifest(
            manifest,
            root,
            secret_values=secrets,
        )
        artifacts.append(artifact)

    assert manifests[0] == manifests[1] == manifests[2]
    assert manifests[0].as_dict() == manifests[1].as_dict() == manifests[2].as_dict()
    assert (
        manifests[0].canonical_json_bytes()
        == manifests[1].canonical_json_bytes()
        == manifests[2].canonical_json_bytes()
    )
    assert artifacts[0] == artifacts[1] == artifacts[2]
    assert inventory.checksum == _inventory().checksum
    assert "secret_values" not in {field.name for field in fields(manifests[0])}
    assert b"absent-secret" not in manifests[0].canonical_json_bytes()


def test_manifest_preserves_legitimate_bookmaker_and_market_identity_keys(
    tmp_path: Path,
) -> None:
    manifest = replace(
        _manifest(),
        odds_revision_inventory=(
            {
                "bookmaker_key": "book-a",
                "market_key": "h2h",
                "outcome_name": "Los Angeles Dodgers",
            },
        ),
        secret_values=("unrelated-configured-secret",),
    )

    artifact, _ = publish_odds_weather_attempt_manifest(
        manifest,
        tmp_path,
        secret_values=("unrelated-configured-secret",),
    )

    assert manifest.as_dict()["odds_revision_inventory"] == [
        {
            "bookmaker_key": "book-a",
            "market_key": "h2h",
            "outcome_name": "Los Angeles Dodgers",
        }
    ]
    assert verify_odds_weather_attempt_manifest(
        artifact_root=tmp_path,
        relpath=artifact.relpath,
        expected=manifest,
        secret_values=("unrelated-configured-secret",),
    ) == artifact


def test_attempt_manifest_atomic_publish_verify_and_exact_replay(tmp_path: Path) -> None:
    manifest = _manifest()
    first, first_created = publish_odds_weather_attempt_manifest(manifest, tmp_path)
    replay, replay_created = publish_odds_weather_attempt_manifest(manifest, tmp_path)

    assert first_created is True
    assert replay_created is False
    assert replay == first
    assert first.relpath == f"odds_weather/attempts/{RUN_ID}/attempt_0001.json"
    assert first.byte_count == len(manifest.canonical_json_bytes())
    assert first.checksum == hashlib.sha256(manifest.canonical_json_bytes()).hexdigest()
    assert verify_odds_weather_attempt_manifest(
        artifact_root=tmp_path,
        relpath=first.relpath,
        expected=manifest,
    ) == first
    assert not tuple(first_path for first_path in tmp_path.rglob(".tmp-*.part"))


def test_attempt_manifest_conflicting_replay_preserves_original_bytes(
    tmp_path: Path,
) -> None:
    manifest = _manifest()
    artifact, _ = publish_odds_weather_attempt_manifest(manifest, tmp_path)
    path = tmp_path / artifact.relpath
    original = path.read_bytes()
    conflicting_inventory = replace(
        _inventory(),
        phase_input_checksum=_checksum("different input"),
    )
    conflicting = OddsWeatherAttemptManifestV1.from_inventory(
        inventory=conflicting_inventory,
        outcome=OddsWeatherAttemptOutcome.ACQUISITION_FAILED,
        snapshot_checksum=None,
        created_at=NOW,
        completed_at=NOW,
    )

    with pytest.raises(OddsWeatherAttemptManifestError, match="bytes"):
        publish_odds_weather_attempt_manifest(conflicting, tmp_path)

    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "relpath",
    (
        "../attempt_0001.json",
        "odds_weather/attempts/another-run/attempt_0001.json",
        f"odds_weather/attempts/{RUN_ID}/attempt_0002.json",
    ),
)
def test_attempt_manifest_rejects_unsafe_or_wrong_identity_path(
    tmp_path: Path,
    relpath: str,
) -> None:
    manifest = _manifest()
    publish_odds_weather_attempt_manifest(manifest, tmp_path)
    with pytest.raises(OddsWeatherAttemptManifestError):
        verify_odds_weather_attempt_manifest(
            artifact_root=tmp_path,
            relpath=relpath,
            expected=manifest,
        )


def test_attempt_manifest_tampering_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    artifact, _ = publish_odds_weather_attempt_manifest(manifest, tmp_path)
    path = tmp_path / artifact.relpath
    path.write_bytes(manifest.canonical_json_bytes() + b"\n")

    with pytest.raises(OddsWeatherAttemptManifestError, match="bytes"):
        verify_odds_weather_attempt_manifest(
            artifact_root=tmp_path,
            relpath=artifact.relpath,
            expected=manifest,
        )


def test_attempt_manifest_hard_link_is_rejected(tmp_path: Path) -> None:
    manifest = _manifest()
    artifact, _ = publish_odds_weather_attempt_manifest(manifest, tmp_path)
    path = tmp_path / artifact.relpath
    retained = path.with_name("retained-manifest.json")
    path.replace(retained)
    try:
        os.link(retained, path)
    except OSError as exc:
        pytest.skip(f"hard links are unavailable: {exc}")

    with pytest.raises(OddsWeatherAttemptManifestError, match="hard link"):
        verify_odds_weather_attempt_manifest(
            artifact_root=tmp_path,
            relpath=artifact.relpath,
            expected=manifest,
        )


def test_attempt_manifest_write_failure_leaves_no_final_or_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()

    def fail_write(path: Path, payload: bytes) -> bool:
        del path, payload
        raise OSError("injected write interruption")

    monkeypatch.setattr(
        "app.odds_weather.attempt_manifest.atomic_create_bytes",
        fail_write,
    )
    with pytest.raises(OddsWeatherAttemptManifestError, match="published"):
        publish_odds_weather_attempt_manifest(manifest, tmp_path)

    expected = tmp_path / odds_weather_attempt_manifest_relpath(RUN_ID, 1)
    assert not expected.exists()
    assert not tuple(tmp_path.rglob(".tmp-*.part"))


@pytest.mark.parametrize(
    ("outcome", "snapshot_checksum", "accepted"),
    (
        (OddsWeatherAttemptOutcome.ASSEMBLED, _checksum("snapshot"), True),
        (OddsWeatherAttemptOutcome.ASSEMBLED, None, False),
        (OddsWeatherAttemptOutcome.NORMALIZATION_FAILED, None, True),
        (OddsWeatherAttemptOutcome.ASSEMBLY_FAILED, _checksum("snapshot"), False),
    ),
)
def test_attempt_manifest_outcome_checksum_rules(
    outcome: OddsWeatherAttemptOutcome,
    snapshot_checksum: str | None,
    accepted: bool,
) -> None:
    if accepted:
        OddsWeatherAttemptManifestV1.from_inventory(
            inventory=_inventory(),
            outcome=outcome,
            snapshot_checksum=snapshot_checksum,
            created_at=NOW,
            completed_at=NOW,
        )
    else:
        with pytest.raises(ValueError):
            OddsWeatherAttemptManifestV1.from_inventory(
                inventory=_inventory(),
                outcome=outcome,
                snapshot_checksum=snapshot_checksum,
                created_at=NOW,
                completed_at=NOW,
            )
