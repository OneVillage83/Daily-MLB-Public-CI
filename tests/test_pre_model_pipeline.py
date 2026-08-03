from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.data_quality import DataQualityIntegrityError, DataQualityRepository
from app.data_quality.engine import DataQualityAssessmentError
from app.database import Database
from app.matchup_packet import MatchupPacketIntegrityError, MatchupPacketRepository
from app.model_feature_set import (
    MODEL_FEATURE_NAMES_V1,
    ModelFeatureSetIntegrityError,
    ModelFeatureSetRepository,
    ModelFeatureSourceV1,
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
    quality_phase = controller.show(run_id).phases[
        list(PipelinePhaseKey).index(PipelinePhaseKey.DATA_QUALITY)
    ]
    assert quality.snapshot.degraded_game_count + quality.snapshot.insufficient_game_count > 0
    assert quality_phase.status is PipelinePhaseStatus.DEGRADED
    with reopened_database.connect() as connection:
        assert connection.execute(
            "SELECT quality_state FROM data_quality_snapshots WHERE snapshot_id=?",
            (quality.snapshot_id,),
        ).fetchone()[0] == "degraded"
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

    def latest_must_not_run(*args, **kwargs):
        raise AssertionError("historical reconstruction used latest evidence")

    monkeypatch.setattr(
        quality_repository.daily_slate,
        "get_latest_daily_slate_for_run",
        latest_must_not_run,
    )
    monkeypatch.setattr(
        quality_repository.game_state,
        "get_latest_game_state_for_run",
        latest_must_not_run,
    )
    monkeypatch.setattr(
        quality_repository.baseball_intelligence,
        "get_latest_for_run",
        latest_must_not_run,
    )
    monkeypatch.setattr(
        quality_repository.odds_weather,
        "get_latest_for_run",
        latest_must_not_run,
    )
    monkeypatch.setattr(
        packet_repository.data_quality,
        "get_latest_for_run",
        latest_must_not_run,
    )
    monkeypatch.setattr(
        feature_repository.matchup_packet,
        "get_latest_for_run",
        latest_must_not_run,
    )
    assert quality_repository.get_by_snapshot_id(quality.snapshot_id) == quality
    assert packet_repository.get_by_snapshot_id(packet.snapshot_id) == packet
    assert feature_repository.get_by_snapshot_id(feature_set.snapshot_id) == feature_set

    inventory = feature_repository.resolve_selected_inventory(packet)
    assert len(inventory) >= 2
    player_a, player_b = inventory[:2]
    wrong_player = ModelFeatureSourceV1(
        canonical_player_id=player_a.canonical_player_id,
        feature_snapshot_id=player_b.feature_snapshot_id,
        feature_checksum=player_b.feature_checksum,
    )
    corrupted = tuple(
        wrong_player if value == player_a else value for value in inventory
    )
    with pytest.raises(ModelFeatureSetIntegrityError, match="disagrees"):
        feature_repository._verify_selected_features(
            replace(feature_set.feature_set, games=(replace(feature_set.feature_set.games[0], source_features=corrupted),)),
            packet,
        )


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


@pytest.mark.parametrize(
    "phase_key,method_name,error_type,expected_outcome",
    (
        (
            PipelinePhaseKey.DATA_QUALITY,
            "assess_for_run",
            DataQualityAssessmentError,
            "assessment_failed",
        ),
        (
            PipelinePhaseKey.MATCHUP_PACKET,
            "assemble_for_run",
            RuntimeError,
            "assembly_failed",
        ),
        (
            PipelinePhaseKey.MODEL_FEATURE_SET,
            "build_from_exact_packet",
            RuntimeError,
            "transformation_failed",
        ),
    ),
)
def test_true_transform_failure_retains_manifest_without_rerunning_failed_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase_key: PipelinePhaseKey,
    method_name: str,
    error_type: type[Exception],
    expected_outcome: str,
) -> None:
    controller, _, _, run_id, _ = _phase4_pending_controller(tmp_path, monkeypatch)
    repository = controller.handlers[phase_key].repository
    original = getattr(repository, method_name)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise error_type("deterministic true transform failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(repository, method_name, fail_once)
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is phase_key
    first = repository.get_attempt_evidence(run_id, 1)
    assert first.outcome.value == expected_outcome
    assert first.snapshot_checksum is None
    assert repository.get_attempt_manifest(run_id, 1).outcome == expected_outcome
    assert calls == 1

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS
    assert repository.get_attempt_evidence(run_id, 1) == first
    assert repository.get_attempt_evidence(run_id, 2).outcome.value == "assembled"


def test_first_seal_rejects_semantic_mutation_and_incomplete_feature_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, configured, _, run_id, _ = _phase4_pending_controller(
        tmp_path, monkeypatch
    )
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(run_id)

    cases: dict[str, tuple[str, tuple[tuple[str, object], ...]]] = {
        "data_quality_snapshots": (
            "data_quality",
            (
                ("canonical_json", "{}"),
                ("snapshot_checksum", "b" * 64),
                ("upstream_odds_weather_snapshot_id", "odds-weather:other"),
                ("upstream_odds_weather_checksum", "b" * 64),
                ("phase_input_checksum", "b" * 64),
                ("artifact_relpath", "data_quality/snapshots/other/data_quality_v1.json"),
                ("artifact_checksum", "b" * 64),
                ("artifact_byte_count", 1),
                ("game_count", 2),
                ("issue_count", 999),
                ("quality_state", "clear"),
                ("policy_version", "DSE_DATA_QUALITY_POLICY_MUTATED"),
            ),
        ),
        "matchup_packet_snapshots": (
            "matchup_packet",
            (
                ("canonical_json", "{}"),
                ("packet_checksum", "b" * 64),
                ("upstream_data_quality_snapshot_id", "data-quality:other"),
                ("upstream_data_quality_checksum", "b" * 64),
                ("phase_input_checksum", "b" * 64),
                ("artifact_relpath", "matchup_packet/snapshots/other/matchup_packet_v1.json"),
                ("artifact_checksum", "b" * 64),
                ("artifact_byte_count", 1),
                ("game_count", 2),
                ("warning_count", 999),
                ("assembly_policy_version", "DSE_MATCHUP_PACKET_POLICY_MUTATED"),
            ),
        ),
        "model_feature_set_snapshots": (
            "model_feature_set",
            (
                ("canonical_json", "{}"),
                ("feature_set_checksum", "b" * 64),
                ("upstream_matchup_packet_snapshot_id", "matchup-packet:other"),
                ("upstream_matchup_packet_checksum", "b" * 64),
                ("phase_input_checksum", "b" * 64),
                ("artifact_relpath", "model_feature_set/snapshots/other/model_feature_set_v1.json"),
                ("artifact_checksum", "b" * 64),
                ("artifact_byte_count", 1),
                ("game_count", 2),
                ("warning_count", 999),
                ("selected_feature_inventory_checksum", "b" * 64),
            ),
        ),
    }

    database = Database(configured.database_path)
    with database.connect(write=True) as connection:
        for table, (prefix, mutations) in cases.items():
            snapshot_id = str(
                connection.execute(
                    f"SELECT snapshot_id FROM {table} WHERE run_id=?", (run_id,)
                ).fetchone()[0]
            )
            trigger_names = (
                f"{prefix}_snapshot_validate_seal",
                f"{table}_reject_semantic_update",
            )
            trigger_sql = {
                name: str(
                    connection.execute(
                        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                        (name,),
                    ).fetchone()[0]
                )
                for name in trigger_names
            }
            for name in trigger_names:
                connection.execute(f"DROP TRIGGER {name}")
            connection.execute(
                f"UPDATE {table} SET sealed_at=NULL WHERE snapshot_id=?", (snapshot_id,)
            )
            for sql in trigger_sql.values():
                connection.execute(sql)

            for column, value in mutations:
                with pytest.raises(sqlite3.IntegrityError):
                    connection.execute(
                        f"UPDATE {table} SET {column}=?, sealed_at=? WHERE snapshot_id=?",
                        (value, "2026-08-02T00:00:00+00:00", snapshot_id),
                    )
                assert connection.execute(
                    f"SELECT sealed_at FROM {table} WHERE snapshot_id=?",
                    (snapshot_id,),
                ).fetchone()[0] is None

        feature_snapshot_id = str(
            connection.execute(
                "SELECT snapshot_id FROM model_feature_set_snapshots WHERE run_id=?",
                (run_id,),
            ).fetchone()[0]
        )
        source_count = int(
            connection.execute(
                "SELECT count(*) FROM model_feature_set_source_features WHERE snapshot_id=?",
                (feature_snapshot_id,),
            ).fetchone()[0]
        )
        assert source_count > 0
        delete_trigger = "model_feature_set_source_features_reject_delete"
        delete_sql = str(
            connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                (delete_trigger,),
            ).fetchone()[0]
        )
        connection.execute(f"DROP TRIGGER {delete_trigger}")
        connection.execute(
            "DELETE FROM model_feature_set_source_features WHERE rowid=(SELECT min(rowid) FROM model_feature_set_source_features WHERE snapshot_id=?)",
            (feature_snapshot_id,),
        )
        connection.execute(delete_sql)
        with pytest.raises(sqlite3.IntegrityError, match="children are incomplete"):
            connection.execute(
                "UPDATE model_feature_set_snapshots SET sealed_at=? WHERE snapshot_id=?",
                ("2026-08-02T00:00:00+00:00", feature_snapshot_id),
            )
