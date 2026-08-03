from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.data_quality.artifact import (
    data_quality_artifact_relpath,
    verify_data_quality_artifact,
    write_data_quality_artifact,
)
from app.data_quality.contracts import (
    DataQualityContractError,
    DataQualityDisposition,
    DataQualityGameV1,
    DataQualityV1,
    QualityDomain,
    QualityIssueSeverity,
    QualityIssueV1,
)
from app.pre_model_evidence import PreModelEvidenceError

AS_OF = datetime(2026, 7, 27, 14, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 7, 27, 14, 15, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)


def _issue(
    severity: QualityIssueSeverity = QualityIssueSeverity.INFO,
) -> QualityIssueV1:
    return QualityIssueV1(
        code=f"fixture_{severity.value}",
        domain=QualityDomain.GAME_STATE,
        severity=severity,
        message=f"Fixture {severity.value} issue.",
    )


def _game(
    *,
    disposition: DataQualityDisposition = DataQualityDisposition.READY,
    issues: tuple[QualityIssueV1, ...] = (),
) -> DataQualityGameV1:
    return DataQualityGameV1(
        edge_event_id="edge:mlb:900001",
        daily_mlb_game_id="game:mlb:900001",
        source_game_id="900001",
        away_team_id="SF",
        home_team_id="LAD",
        scheduled_start_time=START,
        upstream_daily_slate_game_checksum="a" * 64,
        upstream_game_state_game_checksum="b" * 64,
        upstream_baseball_intelligence_game_checksum="c" * 64,
        upstream_odds_weather_game_checksum="d" * 64,
        disposition=disposition,
        issues=issues,
    )


def _snapshot(*, games: tuple[DataQualityGameV1, ...] = ()) -> DataQualityV1:
    return DataQualityV1(
        requested_date="2026-07-27",
        as_of_time=AS_OF,
        observed_at=OBSERVED,
        upstream_daily_slate_checksum="a" * 64,
        upstream_game_state_checksum="b" * 64,
        upstream_baseball_intelligence_checksum="c" * 64,
        upstream_odds_weather_checksum="d" * 64,
        games=games,
    )


def test_disposition_is_derived_from_issue_severity() -> None:
    with pytest.raises(DataQualityContractError, match="disposition"):
        _game(
            disposition=DataQualityDisposition.READY,
            issues=(_issue(QualityIssueSeverity.WARNING),),
        )
    degraded = _game(
        disposition=DataQualityDisposition.DEGRADED,
        issues=(_issue(QualityIssueSeverity.WARNING),),
    )
    insufficient = _game(
        disposition=DataQualityDisposition.INSUFFICIENT,
        issues=(_issue(QualityIssueSeverity.CRITICAL),),
    )
    assert degraded.warning_issue_count == 1
    assert insufficient.critical_issue_count == 1


def test_info_only_game_remains_ready() -> None:
    game = _game(issues=(_issue(QualityIssueSeverity.INFO),))
    assert game.disposition is DataQualityDisposition.READY
    assert game.info_issue_count == 1


def test_snapshot_counts_are_deterministic() -> None:
    snapshot = _snapshot(
        games=(
            _game(),
            DataQualityGameV1(
                edge_event_id="edge:mlb:900002",
                daily_mlb_game_id="game:mlb:900002",
                source_game_id="900002",
                away_team_id="NYY",
                home_team_id="BOS",
                scheduled_start_time=START,
                upstream_daily_slate_game_checksum="1" * 64,
                upstream_game_state_game_checksum="2" * 64,
                upstream_baseball_intelligence_game_checksum="3" * 64,
                upstream_odds_weather_game_checksum="4" * 64,
                disposition=DataQualityDisposition.DEGRADED,
                issues=(_issue(QualityIssueSeverity.WARNING),),
            ),
        )
    )
    assert snapshot.ready_game_count == 1
    assert snapshot.degraded_game_count == 1
    assert snapshot.insufficient_game_count == 0
    assert snapshot.checksum == _snapshot(games=snapshot.games).checksum


def test_zero_game_snapshot_is_valid() -> None:
    snapshot = _snapshot()
    assert snapshot.games == ()
    assert snapshot.ready_game_count == 0
    assert snapshot.degraded_game_count == 0
    assert snapshot.insufficient_game_count == 0


def test_standalone_artifact_verifier_rejects_configured_secret_retained_string(
    tmp_path: Path,
) -> None:
    secret = "configured-retained-secret"
    issue = QualityIssueV1(
        code="fixture_secret_boundary",
        domain=QualityDomain.GAME_STATE,
        severity=QualityIssueSeverity.INFO,
        message=f"safe prefix {secret} safe suffix",
    )
    snapshot = _snapshot(games=(_game(issues=(issue,)),))
    artifact = write_data_quality_artifact(snapshot, tmp_path)
    with pytest.raises(DataQualityContractError, match="credential-bearing"):
        verify_data_quality_artifact(
            snapshot, artifact, tmp_path, secret_values=(secret,)
        )


def test_phase_snapshot_artifact_verifier_rejects_hard_link(tmp_path: Path) -> None:
    snapshot = _snapshot()
    artifact = write_data_quality_artifact(snapshot, tmp_path)
    try:
        os.link(tmp_path / artifact.relpath, tmp_path / "snapshot-hard-link.json")
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"hard links unavailable: {exc}")
    with pytest.raises(PreModelEvidenceError, match="hard link"):
        verify_data_quality_artifact(snapshot, artifact, tmp_path)


def test_duplicate_game_identity_is_rejected() -> None:
    game = _game()
    with pytest.raises(DataQualityContractError, match="duplicate"):
        _snapshot(games=(game, game))


def test_artifact_is_content_addressed_and_exact_canonical_bytes(tmp_path: Path) -> None:
    snapshot = _snapshot(games=(_game(),))
    artifact = write_data_quality_artifact(snapshot, tmp_path)
    assert artifact.relpath == data_quality_artifact_relpath(snapshot)
    assert artifact.relpath == (
        f"data_quality/snapshots/{snapshot.checksum}/data_quality_v1.json"
    )
    content = (tmp_path / artifact.relpath).read_bytes()
    assert content == snapshot.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(content).hexdigest()
    assert artifact.byte_count == len(content)


def test_artifact_rewrite_is_idempotent(tmp_path: Path) -> None:
    snapshot = _snapshot(games=(_game(),))
    first = write_data_quality_artifact(snapshot, tmp_path)
    second = write_data_quality_artifact(snapshot, tmp_path)
    assert first == second


def test_artifact_path_cannot_escape_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        write_data_quality_artifact(_snapshot(), tmp_path, relpath="../escape.json")


def test_artifact_rejects_configured_secret_material(tmp_path: Path) -> None:
    configured_token = "fixture-token-value-47381"
    snapshot = _snapshot(
        games=(
            _game(
                disposition=DataQualityDisposition.READY,
                issues=(
                    QualityIssueV1(
                        code="fixture_note",
                        domain=QualityDomain.GAME_STATE,
                        severity=QualityIssueSeverity.INFO,
                        message=f"Provider note {configured_token}",
                    ),
                ),
            ),
        )
    )
    with pytest.raises(DataQualityContractError, match="credential-bearing"):
        write_data_quality_artifact(
            snapshot,
            tmp_path,
            secret_values=(configured_token,),
        )
