from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.data_quality import DataQualityIntegrityError, DataQualityRepository
from app.data_quality.contracts import DataQualityPolicyV1
from app.data_quality.engine import DataQualityAssessmentError
from app.daily_slate.contracts import canonical_json_bytes
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


def test_zero_game_chain_completes_outputs_and_waits_for_human_review(tmp_path: Path) -> None:
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
    result, completed_inventory = odds_repository.assemble_inventory(inventory)
    phase4 = odds_repository.persist_assembly(
        run_id=RUN_ID,
        phase_attempt=1,
        result=result,
        inventory=completed_inventory,
    )
    pipeline = PipelineRunRepository(odds_repository.database)
    pipeline.transition_pipeline_phase(
        RUN_ID,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseStatus.SUCCEEDED,
        input_checksum=completed_inventory.phase_input_checksum,
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
    from app.run_controller.service import ManualRunAwaitingHumanInput
    from app.pdf_report import PdfReportRepository
    from app.infographic import InfographicRepository
    from app.final_qc import FinalQcRepository
    from app.human_review import HumanReviewDecision, HumanReviewRepository

    with pytest.raises(ManualRunAwaitingHumanInput) as blocked:
        controller.resume(RUN_ID)
    assert blocked.value.phase_key is PipelinePhaseKey.HUMAN_REVIEW
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
    pdf = PdfReportRepository(odds_repository.database, artifact_root=odds_repository.artifact_root).get_latest_for_run(RUN_ID)
    infographic = InfographicRepository(odds_repository.database, artifact_root=odds_repository.artifact_root).get_latest_for_run(RUN_ID)
    qc = FinalQcRepository(odds_repository.database, artifact_root=odds_repository.artifact_root).get_latest_for_run(RUN_ID)
    assert pdf is not None and pdf.document.games == ()
    assert infographic is not None and infographic.document.full_report_game_count == 0
    assert qc is not None and qc.snapshot.overall_result == "pass"
    attempts = tuple(phase.attempt_count for phase in controller.show(RUN_ID).phases)
    with pytest.raises(ManualRunAwaitingHumanInput):
        controller.resume(RUN_ID)
    assert tuple(phase.attempt_count for phase in controller.show(RUN_ID).phases) == attempts
    review_repository = HumanReviewRepository(
        odds_repository.database,
        artifact_root=odds_repository.artifact_root,
        clock=lambda: phase4.sealed_at + timedelta(seconds=1),
    )
    review = review_repository.record_decision(
        run_id=RUN_ID,
        reviewer_id="zero-game-reviewer@example.test",
        decision=HumanReviewDecision.APPROVE,
        notes="approved exact zero-game fixture",
    )
    completed_summary = controller.resume(RUN_ID)
    assert completed_summary.phases[14].status is PipelinePhaseStatus.SUCCEEDED
    assert review_repository.get_attempt_evidence(RUN_ID, 1).review == review
    with odds_repository.database.connect(write=True) as connection:
        for table, checksum_column in (
            ("pdf_report_snapshots", "semantic_checksum"),
            ("infographic_snapshots", "infographic_checksum"),
            ("final_qc_snapshots", "qc_checksum"),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="immutable after seal"):
                connection.execute(
                    f"UPDATE {table} SET {checksum_column}=? WHERE run_id=?",
                    ("f" * 64, RUN_ID),
                )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute("DELETE FROM human_review_records WHERE review_id=?", (review.review_id,))
    assert odds_repository.database.integrity_check()["ok"] is True


def test_nondefault_data_quality_policy_reconstructs_from_retained_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, configured, _, run_id, _ = _phase4_pending_controller(
        tmp_path, monkeypatch
    )
    policy = DataQualityPolicyV1(
        policy_version="DSE_DATA_QUALITY_POLICY_V1",
        supported_markets=("h2h",),
    )
    handler = controller.handlers[PipelinePhaseKey.DATA_QUALITY]
    handler.policy = policy
    handler.repository.policy = policy
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(run_id)

    default_repository = DataQualityRepository(
        Database(configured.database_path),
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    retained = default_repository.get_latest_for_run(run_id)
    assert retained is not None
    assert retained.snapshot.policy == policy
    assert retained.snapshot.policy.checksum == policy.checksum
    attempt = default_repository.get_attempt_evidence(run_id, 1)
    assert attempt.policy == policy
    assert default_repository.policy != policy
    with default_repository.database.connect() as connection:
        row = connection.execute(
            "SELECT policy_version,policy_json,policy_checksum FROM data_quality_snapshots WHERE snapshot_id=?",
            (retained.snapshot_id,),
        ).fetchone()
    assert row[0] == policy.policy_version
    assert row[2] == policy.checksum
    assert policy.checksum in str(row[1])


def test_data_quality_retained_policy_tampering_and_attempt_disagreement_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, configured, _, run_id, _ = _phase4_pending_controller(
        tmp_path, monkeypatch
    )
    policy = DataQualityPolicyV1(
        policy_version="DSE_DATA_QUALITY_POLICY_V1",
        supported_markets=("h2h",),
    )
    handler = controller.handlers[PipelinePhaseKey.DATA_QUALITY]
    handler.policy = policy
    handler.repository.policy = policy
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(run_id)

    repository = DataQualityRepository(
        Database(configured.database_path),
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    retained = repository.get_latest_for_run(run_id)
    assert retained is not None
    altered = DataQualityPolicyV1(
        policy_version=policy.policy_version,
        supported_markets=("h2h", "totals"),
    )
    altered_json = canonical_json_bytes(altered.as_dict()).decode("utf-8")

    with sqlite3.connect(configured.database_path) as connection:
        connection.row_factory = sqlite3.Row
        snapshot_row = connection.execute(
            "SELECT policy_version,policy_json,policy_checksum FROM data_quality_snapshots WHERE snapshot_id=?",
            (retained.snapshot_id,),
        ).fetchone()
        attempt_row = connection.execute(
            "SELECT policy_version,policy_json,policy_checksum FROM data_quality_attempt_evidence WHERE run_id=? AND phase_attempt=1",
            (run_id,),
        ).fetchone()
        assert snapshot_row is not None and attempt_row is not None
        trigger_names = (
            "data_quality_snapshots_reject_semantic_update",
            "data_quality_attempt_evidence_reject_update",
        )
        trigger_sql = tuple(
            str(
                connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
                    (name,),
                ).fetchone()[0]
            )
            for name in trigger_names
        )
        for name in trigger_names:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.commit()

    def update_policy(table: str, values: tuple[object, object, object]) -> None:
        with sqlite3.connect(configured.database_path) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            where = (
                "snapshot_id=?"
                if table == "data_quality_snapshots"
                else "run_id=? AND phase_attempt=1"
            )
            keys: tuple[object, ...] = (
                (retained.snapshot_id,)
                if table == "data_quality_snapshots"
                else (run_id,)
            )
            connection.execute(
                f"UPDATE {table} SET policy_version=?,policy_json=?,policy_checksum=? WHERE {where}",
                (*values, *keys),
            )
            connection.commit()

    original_snapshot = tuple(snapshot_row)
    original_attempt = tuple(attempt_row)
    try:
        update_policy(
            "data_quality_snapshots",
            (policy.policy_version, altered_json, policy.checksum),
        )
        with pytest.raises(DataQualityIntegrityError):
            repository.get_by_snapshot_id(retained.snapshot_id)

        update_policy("data_quality_snapshots", original_snapshot)
        update_policy(
            "data_quality_snapshots",
            (policy.policy_version, original_snapshot[1], "f" * 64),
        )
        with pytest.raises(DataQualityIntegrityError):
            repository.get_by_snapshot_id(retained.snapshot_id)

        update_policy("data_quality_snapshots", original_snapshot)
        update_policy(
            "data_quality_attempt_evidence",
            (altered.policy_version, altered_json, altered.checksum),
        )
        with pytest.raises(DataQualityIntegrityError):
            repository.get_by_snapshot_id(retained.snapshot_id)
    finally:
        update_policy("data_quality_snapshots", original_snapshot)
        update_policy("data_quality_attempt_evidence", original_attempt)
        with sqlite3.connect(configured.database_path) as connection:
            for sql in trigger_sql:
                connection.execute(sql)
            connection.commit()

    assert repository.get_by_snapshot_id(retained.snapshot_id) == retained


@pytest.mark.parametrize(
    "phase_key,context_method",
    (
        (PipelinePhaseKey.DATA_QUALITY, "_validate_context"),
        (PipelinePhaseKey.MATCHUP_PACKET, "_context"),
    ),
)
def test_safe_context_input_failure_is_durable_idempotent_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase_key: PipelinePhaseKey,
    context_method: str,
) -> None:
    controller, _, _, run_id, _ = _phase4_pending_controller(tmp_path, monkeypatch)
    handler = controller.handlers[phase_key]
    repository = handler.repository
    original = getattr(handler, context_method)
    wrong_as_of = datetime(2026, 7, 31, 14, tzinfo=timezone.utc)
    monkeypatch.setattr(handler, context_method, lambda context: wrong_as_of)
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is phase_key
    first = repository.get_attempt_evidence(run_id, 1)
    assert first.outcome.value == "input_failed"
    assert first.snapshot_checksum is None
    assert first.as_of_time == wrong_as_of

    if phase_key is PipelinePhaseKey.DATA_QUALITY:
        upstream = repository.resolve_upstream(run_id)
        replay = repository.persist_failed_attempt(
            run_id=run_id,
            phase_attempt=1,
            phase_input_checksum=first.phase_input_checksum,
            observed_at=first.observed_at,
            outcome="input_failed",
            warnings=first.warnings,
            requested_date=first.requested_date,
            as_of_time=first.as_of_time,
            policy=first.policy,
            upstream=upstream,
        )
        conflict_kwargs = {
            "policy": first.policy,
            "upstream": upstream,
        }
    else:
        quality, identities = repository._upstream(run_id)
        replay = repository.persist_failed_attempt(
            run_id=run_id,
            phase_attempt=1,
            phase_input_checksum=first.phase_input_checksum,
            observed_at=first.observed_at,
            outcome="input_failed",
            warnings=first.warnings,
            requested_date=first.requested_date,
            as_of_time=first.as_of_time,
            quality=quality,
            upstream_identities=identities,
        )
        conflict_kwargs = {
            "quality": quality,
            "upstream_identities": identities,
        }
    assert replay == first
    with pytest.raises(Exception, match="conflicting"):
        repository.persist_failed_attempt(
            run_id=run_id,
            phase_attempt=1,
            phase_input_checksum=first.phase_input_checksum,
            observed_at=first.observed_at,
            outcome="input_failed",
            warnings=({"code": "conflict", "message": "different"},),
            requested_date=first.requested_date,
            as_of_time=first.as_of_time,
            **conflict_kwargs,
        )

    monkeypatch.setattr(handler, context_method, original)
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PREDICTIONS
    assert repository.get_attempt_evidence(run_id, 1) == first
    assert repository.get_attempt_evidence(run_id, 2).outcome.value == "assembled"


@pytest.mark.parametrize(
    "mutation,validation_message",
    (
        ("missing", "referenced feature snapshot is missing"),
        ("wrong_player", "referenced feature snapshot violates frozen PIT lineage"),
    ),
)
def test_model_feature_input_failure_retains_packet_inventory_before_transform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    validation_message: str,
) -> None:
    controller, _, _, run_id, _ = _phase4_pending_controller(tmp_path, monkeypatch)
    handler = controller.handlers[PipelinePhaseKey.MODEL_FEATURE_SET]
    repository = handler.repository
    original_validate = repository.validate_selected_inventory
    original_build = repository.build_from_exact_packet
    build_calls = 0
    retained_row: tuple[object, ...] | None = None
    retained_columns: tuple[str, ...] = ()
    retained_triggers: tuple[str, ...] = ()
    resolved_upstream = None

    def mutate_then_validate(inventory, packet):
        nonlocal retained_row, retained_columns, retained_triggers, resolved_upstream
        resolved_upstream = repository._upstream(run_id)
        source = inventory[0]
        with sqlite3.connect(repository.database.path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=OFF")
            retained_columns = tuple(
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(stats_feature_snapshots)"
                ).fetchall()
            )
            row = connection.execute(
                "SELECT * FROM stats_feature_snapshots WHERE feature_snapshot_id=?",
                (source.feature_snapshot_id,),
            ).fetchone()
            assert row is not None
            retained_row = tuple(row)
            trigger_rows = connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='stats_feature_snapshots' AND sql IS NOT NULL"
            ).fetchall()
            selected_triggers = tuple(
                (str(value[0]), str(value[1]))
                for value in trigger_rows
                if f"BEFORE {('DELETE' if mutation == 'missing' else 'UPDATE')}" in str(value[1]).upper()
            )
            retained_triggers = tuple(sql for _, sql in selected_triggers)
            for name, _ in selected_triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            if mutation == "missing":
                connection.execute(
                    "DELETE FROM stats_feature_snapshots WHERE feature_snapshot_id=?",
                    (source.feature_snapshot_id,),
                )
            else:
                other = next(
                    value
                    for value in inventory[1:]
                    if value.canonical_player_id != source.canonical_player_id
                )
                connection.execute(
                    "UPDATE stats_feature_snapshots SET canonical_player_id=? WHERE feature_snapshot_id=?",
                    (other.canonical_player_id, source.feature_snapshot_id),
                )
            connection.commit()
        return original_validate(inventory, packet)

    def count_build(*args, **kwargs):
        nonlocal build_calls
        build_calls += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(repository, "validate_selected_inventory", mutate_then_validate)
    monkeypatch.setattr(repository, "build_from_exact_packet", count_build)
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is PipelinePhaseKey.MODEL_FEATURE_SET
    causes: list[str] = []
    cause: BaseException | None = failed.value
    while cause is not None:
        causes.append(str(cause))
        cause = cause.__cause__
    assert any(validation_message in message for message in causes)
    assert build_calls == 0
    first = repository.get_attempt_evidence(run_id, 1)
    assert first.outcome.value == "input_failed"
    assert first.snapshot_checksum is None
    assert first.inventory_validation_state == "unverified"
    assert first.selected_feature_inventory

    assert resolved_upstream is not None
    packet, quality, packet_identity = resolved_upstream
    assert first.selected_feature_inventory == repository.derive_selected_inventory(packet)
    assert repository.persist_failed_attempt(
        run_id=run_id,
        phase_attempt=1,
        phase_input_checksum=first.phase_input_checksum,
        observed_at=first.observed_at,
        outcome="input_failed",
        warnings=first.warnings,
        requested_date=first.requested_date,
        as_of_time=first.as_of_time,
        packet_snapshot=packet,
        selected_inventory=first.selected_feature_inventory,
        inventory_validation_state="unverified",
        upstream_identities=(quality, packet_identity),
    ) == first
    with pytest.raises(Exception, match="conflicting"):
        repository.persist_failed_attempt(
            run_id=run_id,
            phase_attempt=1,
            phase_input_checksum=first.phase_input_checksum,
            observed_at=first.observed_at,
            outcome="input_failed",
            warnings=({"code": "conflict", "message": "different"},),
            requested_date=first.requested_date,
            as_of_time=first.as_of_time,
            packet_snapshot=packet,
            selected_inventory=first.selected_feature_inventory,
            inventory_validation_state="unverified",
            upstream_identities=(quality, packet_identity),
        )

    assert retained_row is not None
    with sqlite3.connect(repository.database.path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        if mutation == "missing":
            placeholders = ",".join("?" for _ in retained_columns)
            columns = ",".join(f'"{value}"' for value in retained_columns)
            connection.execute(
                f"INSERT INTO stats_feature_snapshots({columns}) VALUES ({placeholders})",
                retained_row,
            )
        else:
            player_index = retained_columns.index("canonical_player_id")
            snapshot_index = retained_columns.index("feature_snapshot_id")
            connection.execute(
                "UPDATE stats_feature_snapshots SET canonical_player_id=? WHERE feature_snapshot_id=?",
                (retained_row[player_index], retained_row[snapshot_index]),
            )
        for trigger_sql in retained_triggers:
            connection.execute(trigger_sql)
        connection.commit()

    monkeypatch.setattr(repository, "validate_selected_inventory", original_validate)
    with pytest.raises(ManualRunExecutionBlocked):
        controller.resume(run_id)
    assert repository.get_attempt_evidence(run_id, 1) == first
    assert repository.get_attempt_evidence(run_id, 2).outcome.value == "assembled"


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
                ("policy_json", "{}"),
                ("policy_checksum", "b" * 64),
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
