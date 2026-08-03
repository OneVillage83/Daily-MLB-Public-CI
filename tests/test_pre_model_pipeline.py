from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.data_quality import DataQualityIntegrityError, DataQualityRepository
from app.database import Database
from app.matchup_packet import MatchupPacketIntegrityError, MatchupPacketRepository
from app.model_feature_set import (
    MODEL_FEATURE_NAMES_V1,
    ModelFeatureSetIntegrityError,
    ModelFeatureSetRepository,
)
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.repository import PipelineRunRepository
from app.run_controller.service import (
    ManualRunExecutionBlocked,
    ManualRunExecutionError,
    PhaseExecutionContext,
)
from scripts.run_controller import build_controller
from tests.test_game_state_repository import RUN_ID
from tests.test_odds_weather_repository import _zero_game_odds_weather_repository
from tests.test_run_controller_odds_weather import (
    _phase4_pending_controller,
    _settings,
)


def _context(phase_key: PipelinePhaseKey, attempt: int = 1) -> PhaseExecutionContext:
    return PhaseExecutionContext(
        run_id=RUN_ID,
        requested_date="2026-07-30",
        as_of_time="2026-07-30T14:00:00+00:00",
        timezone="America/Los_Angeles",
        phase_key=phase_key,
        attempt_number=attempt,
        retry=False,
        force_refresh=False,
        pipeline_version="DSE_MANUAL_RUN_CONTROLLER_V1",
        configuration_version="DSE_DAILY_MLB_CONFIG_V1",
        configuration_fingerprint="0" * 64,
        code_revision="test",
    )


def test_one_game_pre_model_chain_persists_reconstructs_and_blocks_predictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, configured, _, run_id, calls = _phase4_pending_controller(
        tmp_path, monkeypatch
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS

    reopened_database = Database(configured.database_path)
    quality_repository = DataQualityRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    packet_repository = MatchupPacketRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    feature_repository = ModelFeatureSetRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    quality = quality_repository.get_latest_for_run(run_id)
    packet = packet_repository.get_latest_for_run(run_id)
    feature_set = feature_repository.get_latest_for_run(run_id)
    assert quality is not None and packet is not None and feature_set is not None
    assert len(quality.snapshot.games) == len(packet.packet.games) == len(feature_set.feature_set.games) == 1
    assert packet.packet.upstream_data_quality_checksum == quality.snapshot.checksum
    assert feature_set.feature_set.upstream_data_quality_checksum == quality.snapshot.checksum
    assert feature_set.feature_set.upstream_matchup_packet_checksum == packet.packet.checksum
    assert packet.packet.games[0].source_game_id == feature_set.feature_set.games[0].source_game_id
    assert tuple(controller.handlers) == tuple(PipelinePhaseKey)[:7]
    assert calls == {"odds": 1, "nws": 1, "openweather": 1}

    game = feature_set.feature_set.games[0]
    forbidden_predictive_tokens = (
        "bookmaker",
        "american_odds",
        "decimal_odds",
        "implied_probability",
        "consensus_market_probability",
        "best_price",
        "line_movement",
    )
    assert not any(
        token in name
        for name in MODEL_FEATURE_NAMES_V1
        for token in forbidden_predictive_tokens
    )
    assert game.market_context
    assert game.market_context_checksum == game.market_reference_checksum
    assert game.predictive_feature_checksum != game.market_context_checksum
    assert all(value is None or isinstance(value, float) for value in game.feature_values)
    assert game.missing_feature_names

    assert quality_repository.get_attempt_manifest(run_id, 1).snapshot_checksum == quality.snapshot.checksum
    assert packet_repository.get_attempt_manifest(run_id, 1).snapshot_checksum == packet.packet.checksum
    assert feature_repository.get_attempt_manifest(run_id, 1).snapshot_checksum == feature_set.feature_set.checksum
    assert quality_repository.persist_assessment(
        run_id=run_id,
        phase_attempt=1,
        phase_input_checksum=quality.phase_input_checksum,
        result=quality_repository.assess_for_run(
            run_id, observed_at=quality.snapshot.observed_at
        )[0],
    ) == quality
    assert packet_repository.persist_packet(
        run_id=run_id,
        phase_attempt=1,
        phase_input_checksum=packet.phase_input_checksum,
        packet=packet.packet,
    ) == packet
    assert feature_repository.persist_feature_set(
        run_id=run_id,
        phase_attempt=1,
        phase_input_checksum=feature_set.phase_input_checksum,
        feature_set=feature_set.feature_set,
    ) == feature_set

    attempts_before = tuple(phase.attempt_count for phase in controller.show(run_id).phases)
    with pytest.raises(ManualRunExecutionBlocked) as second:
        controller.resume(run_id)
    assert second.value.phase_key is PipelinePhaseKey.PREDICTIONS
    assert tuple(phase.attempt_count for phase in controller.show(run_id).phases) == attempts_before

    with reopened_database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE model_feature_set_games SET row_checksum=? WHERE snapshot_id=?",
                ("f" * 64, feature_set.snapshot_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
            connection.execute(
                "DELETE FROM matchup_packet_games WHERE snapshot_id=?",
                (packet.snapshot_id,),
            )

    for repository, persisted, error_type in (
        (quality_repository, quality, DataQualityIntegrityError),
        (packet_repository, packet, MatchupPacketIntegrityError),
        (feature_repository, feature_set, ModelFeatureSetIntegrityError),
    ):
        artifact_path = configured.artifact_dir / persisted.artifact.relpath
        original = artifact_path.read_bytes()
        artifact_path.write_bytes(original + b"tamper")
        with pytest.raises(error_type):
            repository.get_by_snapshot_id(persisted.snapshot_id)
        artifact_path.write_bytes(original)
        assert repository.get_by_snapshot_id(persisted.snapshot_id) == persisted


def test_zero_game_chain_uses_no_provider_and_blocks_predictions(tmp_path: Path) -> None:
    odds_repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = odds_repository.baseball_intelligence.get_latest_for_run(RUN_ID)
    assert upstream is not None
    inventory = odds_repository.build_inventory(
        run_id=RUN_ID,
        phase_attempt=1,
        observed_at=upstream.sealed_at,
        phase_input_checksum=hashlib.sha256(b"zero-game-phase4").hexdigest(),
        raw_captures=(),
    )
    result, completed = odds_repository.assemble_inventory(inventory)
    phase4 = odds_repository.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=completed,
    )
    pipeline = PipelineRunRepository(odds_repository.database)
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.SUCCEEDED,
        input_checksum=completed.phase_input_checksum,
        output_checksum=phase4.snapshot.checksum,
        artifact_relpath=phase4.artifact.relpath,
        transitioned_at=phase4.sealed_at.isoformat(),
    )
    configured = _settings(odds_repository.database.path, odds_repository.artifact_root)
    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: phase4.sealed_at + timedelta(seconds=1),
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(RUN_ID)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS
    quality = DataQualityRepository(
        odds_repository.database, artifact_root=odds_repository.artifact_root
    ).get_latest_for_run(RUN_ID)
    packet = MatchupPacketRepository(
        odds_repository.database, artifact_root=odds_repository.artifact_root
    ).get_latest_for_run(RUN_ID)
    feature_set = ModelFeatureSetRepository(
        odds_repository.database, artifact_root=odds_repository.artifact_root
    ).get_latest_for_run(RUN_ID)
    assert quality is not None and quality.snapshot.games == ()
    assert packet is not None and packet.packet.games == ()
    assert feature_set is not None and feature_set.feature_set.games == ()


@pytest.mark.parametrize(
    "handler_attribute,phase_key",
    (
        ("data_quality_handler", PipelinePhaseKey.DATA_QUALITY),
        ("matchup_packet_handler", PipelinePhaseKey.MATCHUP_PACKET),
        ("model_feature_set_handler", PipelinePhaseKey.MODEL_FEATURE_SET),
    ),
)
def test_pre_model_handlers_reject_wrong_phase_before_repository_access(
    tmp_path: Path,
    handler_attribute: str,
    phase_key: PipelinePhaseKey,
) -> None:
    configured = _settings(tmp_path / "wrong-phase.db", tmp_path / "artifacts")
    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    handler = controller.handlers[phase_key]
    wrong = PipelinePhaseKey.DATA_QUALITY
    if wrong is phase_key:
        wrong = PipelinePhaseKey.MATCHUP_PACKET
    with pytest.raises(Exception, match="requires"):
        handler(_context(wrong))


def test_schema_v11_identity_remains_frozen_in_pre_model_sprint() -> None:
    from app.migrations import FORMAL_SCHEMA_V11_FINGERPRINT, MIGRATION_V11_CHECKSUM

    assert MIGRATION_V11_CHECKSUM == "a54865d8b5623e96c4f571d6c9d7f899e9ced0d1911128b5a874b41e29df75dd"
    assert FORMAL_SCHEMA_V11_FINGERPRINT == "5b9635e1aac05d98fd61dadaf2ac5d435e4aaae9214c79501b5dd642673c75b8"


@pytest.mark.parametrize(
    "phase_key,persist_method",
    (
        (PipelinePhaseKey.DATA_QUALITY, "persist_assessment"),
        (PipelinePhaseKey.MATCHUP_PACKET, "persist_packet"),
        (PipelinePhaseKey.MODEL_FEATURE_SET, "persist_feature_set"),
    ),
)
def test_each_pre_model_phase_retry_preserves_failed_attempt_and_upstream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase_key: PipelinePhaseKey,
    persist_method: str,
) -> None:
    controller, _, _, run_id, calls = _phase4_pending_controller(tmp_path, monkeypatch)
    handler = controller.handlers[phase_key]
    repository = handler.repository
    original = getattr(repository, persist_method)
    invocations = 0

    def fail_once(*args, **kwargs):
        nonlocal invocations
        invocations += 1
        if invocations == 1:
            raise RuntimeError("deterministic pre-model persistence failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, persist_method, fail_once)
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is phase_key
    failed_summary = controller.show(run_id)
    target = failed_summary.phases[list(PipelinePhaseKey).index(phase_key)]
    assert target.status is PipelinePhaseStatus.FAILED
    assert target.attempt_count == 1
    first = repository.get_attempt_evidence(run_id, 1)
    assert first.outcome.value == "persistence_failed"
    assert first.snapshot_checksum is None
    upstream_attempts = tuple(
        value.attempt_count
        for value in failed_summary.phases[: list(PipelinePhaseKey).index(phase_key)]
    )

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS
    succeeded = controller.show(run_id)
    target = succeeded.phases[list(PipelinePhaseKey).index(phase_key)]
    assert target.attempt_count == 2
    assert target.status in {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
    }
    assert repository.get_attempt_evidence(run_id, 1) == first
    assert repository.get_attempt_evidence(run_id, 2).outcome.value == "assembled"
    assert tuple(
        value.attempt_count
        for value in succeeded.phases[: list(PipelinePhaseKey).index(phase_key)]
    ) == upstream_attempts
    assert calls == {"odds": 1, "nws": 1, "openweather": 1}
