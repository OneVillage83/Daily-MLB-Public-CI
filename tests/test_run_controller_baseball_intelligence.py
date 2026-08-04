from __future__ import annotations

import json
from pathlib import Path
from types import MappingProxyType

import pytest

from app.baseball_intelligence import (
    BaseballIntelligenceAssemblyError,
    BaseballIntelligenceAttemptOutcome,
    BaseballIntelligencePhaseHandler,
)
from app.config import Settings
from app.game_state import (
    GameStateRawLinkOutcome,
    write_game_state_artifact,
)
from app.run_controller.contracts import (
    PipelinePhaseKey,
    PipelinePhaseStatus,
    PipelineRunStatus,
)
from app.run_controller.service import (
    ManualRunExecutionBlocked,
    ManualRunExecutionError,
    PhaseExecutionResult,
)
from scripts import run_controller as run_controller_cli
from scripts.run_controller import (
    EXIT_BLOCKED,
    EXIT_SUCCESS,
    _safe_configuration_metadata,
    build_controller,
    main,
)
from tests import test_game_state_repository as game_state_repository_tests
from tests.test_baseball_intelligence_migration_v10_roundtrip import (
    SELECTION_OBSERVED,
    _fixture as _six_category_fixture,
)
from tests.test_baseball_intelligence_repository import (
    _persist_verified_game_state_mappings,
)
from tests.test_game_state_repository import (
    _raw_link,
    _setup,
    _slate,
    _state,
)


def _settings(database_path: Path, artifact_root: Path) -> Settings:
    return Settings(
        database_path=database_path,
        artifact_dir=artifact_root,
        report_timezone="America/Los_Angeles",
        service_auth_token="fixture-service-secret",
        odds_api_key="fixture-odds-secret",
        openweather_enabled=False,
        openweather_api_key="",
    )


def _persist_phase_two(
    tmp_path: Path,
    monkeypatch,
    *,
    six_category: bool,
):
    fixture = _six_category_fixture() if six_category else None
    slate = fixture.slate if fixture is not None else _slate()
    state = fixture.state if fixture is not None else _state(slate)
    run_id = (
        "run_20260730_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        if six_category
        else "run_20260729_cccccccccccccccccccccccccccccccc"
    )
    monkeypatch.setattr(game_state_repository_tests, "RUN_ID", run_id)
    monkeypatch.setattr(
        game_state_repository_tests,
        "NOW",
        slate.as_of_time,
    )
    game_state_repository, pipeline, _ = _setup(tmp_path, slate)
    if fixture is not None:
        _persist_verified_game_state_mappings(game_state_repository, fixture)
    raw_link = _raw_link(
        game_state_repository,
        state,
        GameStateRawLinkOutcome.NORMALIZED,
        normalized_checksum=state.checksum,
    )
    game_state_repository.persist_attempt_evidence(
        run_id=run_id,
        phase_attempt=1,
        requested_date=slate.requested_date,
        upstream_daily_slate_checksum=slate.checksum,
        outcome=GameStateRawLinkOutcome.NORMALIZED,
        normalized_snapshot_checksum=state.checksum,
        raw_link_relpath=raw_link,
    )
    game_state_repository.persist_game_state(
        run_id=run_id,
        phase_attempt=1,
        state=state,
        artifact=write_game_state_artifact(
            state,
            game_state_repository.artifact_root,
        ),
    )
    pipeline.transition_pipeline_phase(
        run_id,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseStatus.SUCCEEDED,
        input_checksum=slate.checksum,
        output_checksum=state.checksum,
        artifact_relpath=(
            f"game_state/snapshots/{state.checksum}/game_state_v1.json"
        ),
        transitioned_at=state.observed_at.isoformat(),
    )
    configured = _settings(
        game_state_repository.database.path,
        game_state_repository.artifact_root,
    )
    def phase_four_fixture(_context):
        return PhaseExecutionResult(
            status=PipelinePhaseStatus.SUCCEEDED,
            input_checksum="4" * 64,
            output_checksum="5" * 64,
            artifact_relpath=(
                "odds_weather/snapshots/" + "5" * 64 + "/odds_weather_v1.json"
            ),
        )

    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: SELECTION_OBSERVED,
        odds_weather_handler=phase_four_fixture,
    )
    controller.handlers = MappingProxyType(
        dict(tuple(controller.handlers.items())[:4])
    )
    return controller, configured, run_id, slate, state


def test_production_controller_registers_exactly_phases_one_through_seven(
    tmp_path,
) -> None:
    configured = _settings(tmp_path / "handlers.db", tmp_path / "artifacts")
    controller = build_controller(
        configured.database_path,
        configured_settings=configured,
        clock=lambda: SELECTION_OBSERVED,
    )
    assert tuple(controller.handlers) == (
        PipelinePhaseKey.DAILY_SLATE,
        PipelinePhaseKey.GAME_STATE,
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY,
        PipelinePhaseKey.ODDS_WEATHER,
        PipelinePhaseKey.DATA_QUALITY,
        PipelinePhaseKey.MATCHUP_PACKET,
        PipelinePhaseKey.MODEL_FEATURE_SET,
    )
    assert PipelinePhaseKey.PREDICTIONS not in controller.handlers
    metadata = _safe_configuration_metadata(configured)["baseball_intelligence"]
    assert metadata == {
        "attempt_manifest_version": (
            "DSE_BASEBALL_INTELLIGENCE_ATTEMPT_MANIFEST_V1"
        ),
        "contract_version": "DSE_BASEBALL_INTELLIGENCE_ASSEMBLY_V1",
        "feature_version": "DSE_MLB_STATS_FEATURES_V3",
        "network_enabled": False,
        "source_mode": "retained_sqlite",
    }


def test_six_category_resume_commits_phase_three_and_four_then_blocks_at_data_quality(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    controller, configured, run_id, _, _ = _persist_phase_two(
        tmp_path,
        monkeypatch,
        six_category=True,
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.DATA_QUALITY

    summary = controller.show(run_id)
    phase = summary.phases[2]
    odds = summary.phases[3]
    assert summary.run.status is PipelineRunStatus.RUNNING
    assert phase.status is PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    assert phase.attempt_count == 1
    assert phase.input_checksum is not None
    assert phase.output_checksum is not None
    assert phase.artifact_relpath == (
        f"baseball_intelligence/snapshots/{phase.output_checksum}/"
        "baseball_intelligence_assembly_v1.json"
    )
    assert odds.status is PipelinePhaseStatus.SUCCEEDED
    assert odds.attempt_count == 1
    assert all(
        item.status is PipelinePhaseStatus.PENDING
        for item in summary.phases[4:]
    )
    handler = controller.handlers[PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY]
    assert isinstance(handler, BaseballIntelligencePhaseHandler)
    persisted = handler.repository.get_for_run_attempt(run_id, 1)
    players = tuple(
        player
        for game in persisted.assembly.games
        for team in (game.away, game.home)
        for player in team.players
    )
    assert len(persisted.assembly.games) == 1
    assert len(players) == 6
    assert sum(player.feature is not None for player in players) == 2
    assert sum(len(player.equivalent_feature_snapshot_ids) for player in players) == 3

    first_attempt = handler.repository.get_attempt_evidence(run_id, 1)
    with pytest.raises(ManualRunExecutionBlocked) as second_block:
        controller.resume(run_id)
    assert second_block.value.phase_key is PipelinePhaseKey.DATA_QUALITY
    assert controller.show(run_id).phases[2].attempt_count == 1
    assert handler.repository.list_attempt_evidence(run_id) == (first_attempt,)

    monkeypatch.setattr(run_controller_cli, "build_controller", lambda *args, **kwargs: controller)
    exit_code = main(
        [
            "resume",
            "--database",
            str(configured.database_path),
            "--run-id",
            run_id,
        ],
        configured_settings=configured,
    )
    output = capsys.readouterr()
    assert exit_code == EXIT_BLOCKED
    assert "data_quality" in output.err
    assert configured.odds_api_key not in output.err
    assert configured.service_auth_token not in output.err

    assert (
        main(
            [
                "show",
                "--database",
                str(configured.database_path),
                "--run-id",
                run_id,
                "--json",
            ],
            configured_settings=configured,
        )
        == EXIT_SUCCESS
    )
    shown = json.loads(capsys.readouterr().out)
    shown_phase = shown["phases"][2]
    assert shown_phase["status"] == "succeeded_with_warnings"
    assert shown_phase["attempt_count"] == 1
    assert shown_phase["input_checksum"] == phase.input_checksum
    assert shown_phase["output_checksum"] == phase.output_checksum
    assert shown_phase["artifact_relpath"] == phase.artifact_relpath
    assert shown_phase["warnings"] == phase.warnings


def test_phase_three_failure_and_retry_preserve_attempt_one(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _, run_id, _, _ = _persist_phase_two(
        tmp_path,
        monkeypatch,
        six_category=True,
    )
    handler = controller.handlers[PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY]
    assert isinstance(handler, BaseballIntelligencePhaseHandler)
    original_assemble = handler.repository.assemble_for_run

    def fail_selection(**_kwargs):
        raise BaseballIntelligenceAssemblyError("safe conflicting feature fixture")

    monkeypatch.setattr(handler.repository, "assemble_for_run", fail_selection)
    with pytest.raises(ManualRunExecutionError) as failed:
        controller.resume(run_id)
    assert failed.value.phase_key is PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY
    failed_summary = controller.show(run_id)
    assert failed_summary.run.status is PipelineRunStatus.FAILED
    assert failed_summary.run.failure_phase is (
        PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY
    )
    assert failed_summary.phases[2].status is PipelinePhaseStatus.FAILED
    assert failed_summary.phases[3].status is PipelinePhaseStatus.PENDING
    first = handler.repository.get_attempt_evidence(run_id, 1)
    assert first.outcome is BaseballIntelligenceAttemptOutcome.SELECTION_FAILED

    monkeypatch.setattr(handler.repository, "assemble_for_run", original_assemble)
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.DATA_QUALITY
    succeeded = controller.show(run_id)
    assert succeeded.run.status is PipelineRunStatus.RUNNING
    assert succeeded.phases[2].status is (
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS
    )
    assert succeeded.phases[2].attempt_count == 2
    assert succeeded.phases[3].status is PipelinePhaseStatus.SUCCEEDED
    assert handler.repository.get_attempt_evidence(run_id, 1) == first
    second = handler.repository.get_attempt_evidence(run_id, 2)
    assert second.outcome is BaseballIntelligenceAttemptOutcome.ASSEMBLED
    latest = handler.repository.get_latest_for_run(run_id)
    assert latest is not None
    assert latest.phase_attempt == 2


def test_zero_game_controller_advances_through_odds_weather_without_network(
    tmp_path,
    monkeypatch,
) -> None:
    controller, _, run_id, _, _ = _persist_phase_two(
        tmp_path,
        monkeypatch,
        six_category=False,
    )
    with pytest.raises(ManualRunExecutionBlocked) as blocked:
        controller.resume(run_id)
    assert blocked.value.phase_key is PipelinePhaseKey.DATA_QUALITY
    summary = controller.show(run_id)
    phase = summary.phases[2]
    assert phase.status is PipelinePhaseStatus.SUCCEEDED
    assert phase.warnings is None
    handler = controller.handlers[PipelinePhaseKey.BASEBALL_INTELLIGENCE_ASSEMBLY]
    assert isinstance(handler, BaseballIntelligencePhaseHandler)
    persisted = handler.repository.get_for_run_attempt(run_id, 1)
    assert persisted.assembly.games == ()
    assert persisted.assembly.source_stats_run_ids == ()
    assert persisted.assembly.source_feature_checksums == ()
    with handler.repository.database.connect() as connection:
        counts = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "baseball_intelligence_games",
                "baseball_intelligence_players",
                "baseball_intelligence_feature_equivalents",
            )
        )
    assert counts == (0, 0, 0)
