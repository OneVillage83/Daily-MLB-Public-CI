from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sqlite3

import pytest

from app.predictions import (
    PredictionsPersistenceConflict,
    PredictionsPhaseHandler,
    PredictionsRepository,
    ReviewedPredictionInputV1,
)
from app.rankings import RankingsPersistenceConflict, RankingsPhaseHandler, RankingsRepository
from app.recommendation_gate import (
    RecommendationGatePersistenceConflict,
    RecommendationGatePhaseHandler,
    RecommendationGateRepository,
)
from app.value_engine import (
    ValueEnginePersistenceConflict,
    ValueEnginePhaseHandler,
    ValueEngineRepository,
)
from app.database import Database
from app.daily_slate.contracts import canonical_sha256
from app.run_controller.contracts import PipelinePhaseKey, PipelinePhaseStatus
from app.run_controller.service import ManualRunExecutionBlocked, ManualRunExecutionError
from tests.test_run_controller_odds_weather import _phase4_pending_controller


def _seal_reviewed_inputs(repository: PredictionsRepository, run_id: str) -> None:
    upstream = repository.resolve_upstream(run_id)
    packets = {game.source_game_id: game for game in upstream.matchup_packet.packet.games}
    for game in upstream.model_feature_set.feature_set.games:
        start = packets[game.source_game_id].schedule.scheduled_start_time
        assert start is not None
        sealed_at = min(upstream.model_feature_set.feature_set.observed_at, start - timedelta(seconds=1))
        repository.seal_authoring_input(
            ReviewedPredictionInputV1(
                run_id=run_id,
                source_game_id=game.source_game_id,
                upstream_model_feature_set_snapshot_id=upstream.model_feature_set.snapshot_id,
                upstream_model_feature_set_checksum=upstream.model_feature_set.feature_set.checksum,
                upstream_model_feature_game_checksum=game.checksum,
                predictive_feature_checksum=game.predictive_feature_checksum,
                provider_policy=repository.provider_policy,
                home_probability=0.55,
                home_lower=0.48,
                home_upper=0.62,
                generated_at=sealed_at - timedelta(seconds=1),
                sealed_at=sealed_at,
                authoring_evidence={"method": "market-blind reviewed baseball assessment"},
                market_independence_attested=True,
                secret_values=repository.secret_values,
            )
        )


def test_prediction_to_ranking_chain_retries_missing_input_and_blocks_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    controller, configured, _, run_id, calls = _phase4_pending_controller(
        tmp_path,
        monkeypatch,
        include_decision_phases=True,
    )
    assert tuple(controller.handlers) == tuple(PipelinePhaseKey)[:11]

    with pytest.raises(ManualRunExecutionError) as missing:
        controller.resume(run_id)
    assert missing.value.phase_key is PipelinePhaseKey.PREDICTIONS
    prediction_handler = controller.handlers[PipelinePhaseKey.PREDICTIONS]
    value_handler = controller.handlers[PipelinePhaseKey.VALUE_ENGINE]
    gate_handler = controller.handlers[PipelinePhaseKey.RECOMMENDATION_GATE]
    rankings_handler = controller.handlers[PipelinePhaseKey.RANKINGS]
    assert isinstance(prediction_handler, PredictionsPhaseHandler)
    assert isinstance(value_handler, ValueEnginePhaseHandler)
    assert isinstance(gate_handler, RecommendationGatePhaseHandler)
    assert isinstance(rankings_handler, RankingsPhaseHandler)
    attempt_one = prediction_handler.repository.get_attempt_evidence(run_id, 1)
    assert attempt_one.outcome.value == "input_failed"
    assert attempt_one.snapshot_checksum is None
    assert prediction_handler.repository.get_attempt_manifest(run_id, 1).outcome == "input_failed"
    from scripts.prediction_authoring import _templates

    templates = _templates(prediction_handler.repository, run_id)
    assert templates and templates[0]["market_independence_attested"] is False

    value_calculate = value_handler.repository.calculate
    gate_structural_proofs = gate_handler.repository._structural_proofs
    rankings_rank = rankings_handler.repository.rank
    failures = {"value": 0, "gate": 0, "rankings": 0}

    def fail_value_once(*args, **kwargs):
        failures["value"] += 1
        if failures["value"] == 1:
            raise RuntimeError("deterministic value calculation failure")
        return value_calculate(*args, **kwargs)

    def fail_gate_once(upstream, exact_prediction_upstream):
        failures["gate"] += 1
        proofs = gate_structural_proofs(upstream, exact_prediction_upstream)
        if failures["gate"] == 1:
            source_game_id = next(iter(proofs))
            proofs[source_game_id] = replace(
                proofs[source_game_id],
                prediction_checksum="f" * 64,
            )
        return proofs

    def fail_rankings_once(*args, **kwargs):
        failures["rankings"] += 1
        if failures["rankings"] == 1:
            raise RuntimeError("deterministic ranking failure")
        return rankings_rank(*args, **kwargs)

    monkeypatch.setattr(value_handler.repository, "calculate", fail_value_once)
    monkeypatch.setattr(gate_handler.repository, "_structural_proofs", fail_gate_once)
    monkeypatch.setattr(rankings_handler.repository, "rank", fail_rankings_once)

    _seal_reviewed_inputs(prediction_handler.repository, run_id)
    exact_upstream = prediction_handler.repository.resolve_upstream(run_id)
    source_game_id = exact_upstream.model_feature_set.feature_set.games[0].source_game_id
    snapshot_id = exact_upstream.model_feature_set.snapshot_id
    with prediction_handler.repository.database.connect(write=True) as connection:
        row = connection.execute(
            "SELECT input_checksum,canonical_json FROM prediction_authoring_inputs WHERE run_id=? AND source_game_id=? AND upstream_model_feature_set_snapshot_id=?",
            (run_id, source_game_id, snapshot_id),
        ).fetchone()
        assert row is not None
        original_canonical = str(row["canonical_json"])
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='prediction_authoring_inputs' AND sql IS NOT NULL"
        ).fetchall()
        for trigger in trigger_rows:
            connection.execute(f'DROP TRIGGER "{trigger["name"]}"')
        connection.execute(
            "UPDATE prediction_authoring_inputs SET canonical_json=? WHERE run_id=? AND source_game_id=? AND upstream_model_feature_set_snapshot_id=?",
            (json.dumps({"checksum": str(row["input_checksum"])}), run_id, source_game_id, snapshot_id),
        )
        for trigger in trigger_rows:
            connection.execute(str(trigger["sql"]))

    with pytest.raises(ManualRunExecutionError) as corrupt_input:
        controller.resume(run_id)
    assert corrupt_input.value.phase_key is PipelinePhaseKey.PREDICTIONS
    corrupt_attempt = prediction_handler.repository.get_attempt_evidence(run_id, 2)
    assert corrupt_attempt.outcome.value == "input_failed"
    assert corrupt_attempt.snapshot_checksum is None
    corrupt_manifest = prediction_handler.repository.get_attempt_manifest(run_id, 2)
    invalid_inputs = corrupt_manifest.phase_input_evidence["invalid_inputs"]
    assert isinstance(invalid_inputs, list) and invalid_inputs == [
        {
            "reason_code": "invalid_authoring_evidence",
            "retained_input_checksum": str(row["input_checksum"]),
            "source_game_id": source_game_id,
        }
    ]

    with prediction_handler.repository.database.connect(write=True) as connection:
        trigger_rows = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='prediction_authoring_inputs' AND sql IS NOT NULL"
        ).fetchall()
        for trigger in trigger_rows:
            connection.execute(f'DROP TRIGGER "{trigger["name"]}"')
        connection.execute(
            "UPDATE prediction_authoring_inputs SET canonical_json=? WHERE run_id=? AND source_game_id=? AND upstream_model_feature_set_snapshot_id=?",
            (original_canonical, run_id, source_game_id, snapshot_id),
        )
        for trigger in trigger_rows:
            connection.execute(str(trigger["sql"]))

    with pytest.raises(ManualRunExecutionError) as value_failure:
        controller.resume(run_id)
    assert value_failure.value.phase_key is PipelinePhaseKey.VALUE_ENGINE
    value_attempt_one = value_handler.repository.get_attempt_evidence(run_id, 1)
    assert value_attempt_one.outcome.value == "calculation_failed"
    assert value_attempt_one.snapshot_checksum is None
    assert value_handler.repository.get_attempt_manifest(run_id, 1).outcome == "calculation_failed"

    with pytest.raises(ManualRunExecutionError) as gate_failure:
        controller.resume(run_id)
    assert gate_failure.value.phase_key is PipelinePhaseKey.RECOMMENDATION_GATE
    gate_attempt_one = gate_handler.repository.get_attempt_evidence(run_id, 1)
    assert gate_attempt_one.outcome.value == "evaluation_failed"
    assert gate_attempt_one.snapshot_checksum is None
    assert gate_handler.repository.get_attempt_manifest(run_id, 1).outcome == "evaluation_failed"

    with pytest.raises(ManualRunExecutionError) as rankings_failure:
        controller.resume(run_id)
    assert rankings_failure.value.phase_key is PipelinePhaseKey.RANKINGS
    rankings_attempt_one = rankings_handler.repository.get_attempt_evidence(run_id, 1)
    assert rankings_attempt_one.outcome.value == "ranking_failed"
    assert rankings_attempt_one.snapshot_checksum is None
    assert rankings_handler.repository.get_attempt_manifest(run_id, 1).outcome == "ranking_failed"

    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.PDF_REPORT

    summary = controller.show(run_id)
    assert all(
        phase.status
        in {
            PipelinePhaseStatus.SUCCEEDED,
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
            PipelinePhaseStatus.DEGRADED,
        }
        for phase in summary.phases[:11]
    )
    assert summary.phases[11].status is PipelinePhaseStatus.PENDING
    assert summary.phases[7].attempt_count == 3
    assert [phase.attempt_count for phase in summary.phases[8:11]] == [2, 2, 2]
    assert prediction_handler.repository.get_attempt_evidence(run_id, 1) == attempt_one
    assert value_handler.repository.get_attempt_evidence(run_id, 1) == value_attempt_one
    assert gate_handler.repository.get_attempt_evidence(run_id, 1) == gate_attempt_one
    assert rankings_handler.repository.get_attempt_evidence(run_id, 1) == rankings_attempt_one
    persisted_predictions = prediction_handler.repository.get_latest_for_run(run_id)
    assert persisted_predictions is not None
    assert len(persisted_predictions.predictions.games) == 1

    persisted_value = value_handler.repository.get_latest_for_run(run_id)
    persisted_gate = gate_handler.repository.get_latest_for_run(run_id)
    persisted_rankings = rankings_handler.repository.get_latest_for_run(run_id)
    assert persisted_value is not None and persisted_gate is not None and persisted_rankings is not None
    assert tuple(outcome.side for outcome in persisted_value.value_engine.games[0].outcomes) == ("home", "away")
    assert all(outcome.availability in {"available", "unavailable"} for outcome in persisted_value.value_engine.games[0].outcomes)
    assert persisted_gate.gate.games[0].decision in {"recommend", "pass", "avoid"}
    assert persisted_rankings.rankings.entries[0].decision == persisted_gate.gate.games[0].decision
    assert persisted_rankings.rankings.entries[0].source_game_id == persisted_predictions.predictions.games[0].source_game_id

    reopened_database = Database(configured.database_path)
    reopened_predictions = PredictionsRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    reopened_value = ValueEngineRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    reopened_gate = RecommendationGateRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    reopened_rankings = RankingsRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    )
    assert reopened_predictions.get_by_snapshot_id(persisted_predictions.snapshot_id) == persisted_predictions
    assert reopened_value.get_by_snapshot_id(persisted_value.snapshot_id) == persisted_value
    assert reopened_gate.get_by_snapshot_id(persisted_gate.snapshot_id) == persisted_gate
    assert reopened_rankings.get_by_snapshot_id(persisted_rankings.snapshot_id) == persisted_rankings

    exact_predictions_upstream = reopened_predictions.resolve_upstream_by_snapshot_ids(
        model_feature_set_snapshot_id=persisted_predictions.predictions.upstream_model_feature_set_snapshot_id,
        data_quality_snapshot_id=persisted_predictions.predictions.upstream_data_quality_snapshot_id,
    )
    reviewed_inputs, missing_inputs = reopened_predictions.load_input_inventory(exact_predictions_upstream)
    assert missing_inputs == ()
    assert reopened_predictions.persist_assembly(
        run_id=run_id,
        phase_attempt=3,
        phase_input_checksum=persisted_predictions.phase_input_checksum,
        predictions=persisted_predictions.predictions,
        values=reviewed_inputs,
    ) == persisted_predictions
    assert reopened_value.persist_assembly(
        run_id=run_id,
        phase_attempt=2,
        phase_input_checksum=persisted_value.phase_input_checksum,
        snapshot=persisted_value.value_engine,
    ) == persisted_value
    assert reopened_gate.persist_assembly(
        run_id=run_id,
        phase_attempt=2,
        phase_input_checksum=persisted_gate.phase_input_checksum,
        snapshot=persisted_gate.gate,
    ) == persisted_gate
    assert reopened_rankings.persist_assembly(
        run_id=run_id,
        phase_attempt=2,
        phase_input_checksum=persisted_rankings.phase_input_checksum,
        snapshot=persisted_rankings.rankings,
    ) == persisted_rankings
    with pytest.raises(PredictionsPersistenceConflict):
        reopened_predictions.persist_assembly(
            run_id=run_id,
            phase_attempt=3,
            phase_input_checksum="f" * 64,
            predictions=persisted_predictions.predictions,
            values=reviewed_inputs,
        )
    with pytest.raises(ValueEnginePersistenceConflict):
        reopened_value.persist_assembly(
            run_id=run_id,
            phase_attempt=2,
            phase_input_checksum="f" * 64,
            snapshot=persisted_value.value_engine,
        )
    with pytest.raises(RecommendationGatePersistenceConflict):
        reopened_gate.persist_assembly(
            run_id=run_id,
            phase_attempt=2,
            phase_input_checksum="f" * 64,
            snapshot=persisted_gate.gate,
        )
    with pytest.raises(RankingsPersistenceConflict):
        reopened_rankings.persist_assembly(
            run_id=run_id,
            phase_attempt=2,
            phase_input_checksum="f" * 64,
            snapshot=persisted_rankings.rankings,
        )

    with reopened_database.connect(write=True) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="sealed child"):
            connection.execute(
                "UPDATE value_engine_outcomes SET row_checksum=? WHERE snapshot_id=?",
                ("f" * 64, persisted_value.snapshot_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="sealed child"):
            connection.execute(
                "DELETE FROM recommendation_gate_results WHERE snapshot_id=?",
                (persisted_gate.snapshot_id,),
            )

    attempts = tuple(phase.attempt_count for phase in summary.phases)
    first_calls = dict(calls)
    with pytest.raises(ManualRunExecutionBlocked) as repeated:
        controller.resume(run_id)
    assert repeated.value.phase_key is PipelinePhaseKey.PDF_REPORT
    assert tuple(phase.attempt_count for phase in controller.show(run_id).phases) == attempts
    assert calls == first_calls

    model_repository = prediction_handler.repository.model_feature_set
    model_a = model_repository.get_by_snapshot_id(
        persisted_predictions.predictions.upstream_model_feature_set_snapshot_id
    )
    with reopened_database.connect() as connection:
        phase_row = connection.execute(
            "SELECT * FROM pipeline_run_phases WHERE run_id=? AND phase_key='model_feature_set'",
            (run_id,),
        ).fetchone()
    assert phase_row is not None
    with reopened_database.connect(write=True) as connection:
        connection.execute(
            "UPDATE pipeline_run_phases SET status='running',attempt_count=2,completed_at=NULL,input_checksum=NULL,output_checksum=NULL,artifact_relpath=NULL,warnings_json=NULL,error_json=NULL WHERE run_id=? AND phase_key='model_feature_set'",
            (run_id,),
        )
    model_b_contract = model_repository.build_for_run(
        run_id,
        observed_at=model_a.feature_set.observed_at + timedelta(microseconds=1),
    )
    model_b = model_repository.persist_feature_set(
        run_id=run_id,
        phase_attempt=2,
        phase_input_checksum=canonical_sha256(
            {"controlled_historical_fixture": "model_feature_set_b", "run_id": run_id}
        ),
        feature_set=model_b_contract,
    )
    with reopened_database.connect(write=True) as connection:
        connection.execute(
            """UPDATE pipeline_run_phases SET status=?,attempt_count=?,started_at=?,completed_at=?,updated_at=?,input_checksum=?,output_checksum=?,artifact_relpath=?,warnings_json=?,error_json=?,reused_from_run_id=? WHERE run_id=? AND phase_key='model_feature_set'""",
            (
                phase_row["status"],
                phase_row["attempt_count"],
                phase_row["started_at"],
                phase_row["completed_at"],
                phase_row["updated_at"],
                phase_row["input_checksum"],
                phase_row["output_checksum"],
                phase_row["artifact_relpath"],
                phase_row["warnings_json"],
                phase_row["error_json"],
                phase_row["reused_from_run_id"],
                run_id,
            ),
        )
    packet_game = reopened_predictions.resolve_upstream_by_snapshot_ids(
        model_feature_set_snapshot_id=model_b.snapshot_id
    ).matchup_packet.packet.games[0]
    start = packet_game.schedule.scheduled_start_time
    assert start is not None
    model_b_game = model_b.feature_set.games[0]
    sealed_at = min(model_b.feature_set.observed_at, start - timedelta(seconds=1))
    input_b = reopened_predictions.seal_authoring_input(
        ReviewedPredictionInputV1(
            run_id=run_id,
            source_game_id=model_b_game.source_game_id,
            upstream_model_feature_set_snapshot_id=model_b.snapshot_id,
            upstream_model_feature_set_checksum=model_b.feature_set.checksum,
            upstream_model_feature_game_checksum=model_b_game.checksum,
            predictive_feature_checksum=model_b_game.predictive_feature_checksum,
            provider_policy=reopened_predictions.provider_policy,
            home_probability=0.65,
            home_lower=0.59,
            home_upper=0.71,
            generated_at=sealed_at - timedelta(seconds=1),
            sealed_at=sealed_at,
            authoring_evidence={"method": "market-blind controlled historical B"},
            market_independence_attested=True,
        )
    )
    historical_a = PredictionsRepository(
        reopened_database,
        artifact_root=configured.artifact_dir,
        secret_values=configured.credential_values(),
    ).get_by_snapshot_id(persisted_predictions.snapshot_id)
    exact_a = reopened_predictions.resolve_upstream_by_snapshot_ids(
        model_feature_set_snapshot_id=persisted_predictions.predictions.upstream_model_feature_set_snapshot_id
    )
    inputs_a, missing_a = reopened_predictions.load_input_inventory(exact_a)
    assert missing_a == ()
    assert inputs_a[0].upstream_model_feature_set_snapshot_id == model_a.snapshot_id
    assert inputs_a[0].checksum != input_b.checksum
    assert historical_a.predictions.canonical_json_bytes() == persisted_predictions.predictions.canonical_json_bytes()
