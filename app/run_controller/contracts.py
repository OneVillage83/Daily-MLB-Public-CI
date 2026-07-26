from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


PIPELINE_SPORT = "MLB"
PIPELINE_RUN_TYPE = "manual_daily"


class PipelinePhaseKey(StrEnum):
    DAILY_SLATE = "daily_slate"
    GAME_STATE = "game_state"
    BASEBALL_INTELLIGENCE_ASSEMBLY = "baseball_intelligence_assembly"
    ODDS_WEATHER = "odds_weather"
    DATA_QUALITY = "data_quality"
    MATCHUP_PACKET = "matchup_packet"
    MODEL_FEATURE_SET = "model_feature_set"
    PREDICTIONS = "predictions"
    VALUE_ENGINE = "value_engine"
    RECOMMENDATION_GATE = "recommendation_gate"
    RANKINGS = "rankings"
    PDF_REPORT = "pdf_report"
    INFOGRAPHIC = "infographic"
    FINAL_QC = "final_qc"
    HUMAN_REVIEW = "human_review"


@dataclass(frozen=True, slots=True)
class PipelinePhaseDefinition:
    key: PipelinePhaseKey
    ordinal: int


CANONICAL_PIPELINE_PHASES: tuple[PipelinePhaseDefinition, ...] = tuple(
    PipelinePhaseDefinition(key=key, ordinal=ordinal)
    for ordinal, key in enumerate(PipelinePhaseKey, start=1)
)


class PipelinePhaseStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    DEGRADED = "degraded"
    FAILED = "failed"
    SKIPPED = "skipped"
    REUSED = "reused"


class PipelineRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    SUCCEEDED_WITH_WARNINGS = "succeeded_with_warnings"
    DEGRADED = "degraded"
    FAILED = "failed"


class PipelineTransitionType(StrEnum):
    CREATED = "created"
    STATE_TRANSITION = "state_transition"
    RESUME = "resume"
    RETRY = "retry"
    REUSE = "reuse"
    FORCED_REFRESH = "forced_refresh"


_RUN_TRANSITIONS: dict[PipelineRunStatus, frozenset[PipelineRunStatus]] = {
    PipelineRunStatus.PENDING: frozenset({PipelineRunStatus.RUNNING}),
    PipelineRunStatus.RUNNING: frozenset(
        {
            PipelineRunStatus.SUCCEEDED,
            PipelineRunStatus.SUCCEEDED_WITH_WARNINGS,
            PipelineRunStatus.DEGRADED,
            PipelineRunStatus.FAILED,
        }
    ),
    PipelineRunStatus.SUCCEEDED: frozenset(),
    PipelineRunStatus.SUCCEEDED_WITH_WARNINGS: frozenset(),
    PipelineRunStatus.DEGRADED: frozenset(),
    PipelineRunStatus.FAILED: frozenset(),
}

_PHASE_TRANSITIONS: dict[PipelinePhaseStatus, frozenset[PipelinePhaseStatus]] = {
    PipelinePhaseStatus.PENDING: frozenset(
        {
            PipelinePhaseStatus.RUNNING,
            PipelinePhaseStatus.SKIPPED,
        }
    ),
    PipelinePhaseStatus.RUNNING: frozenset(
        {
            PipelinePhaseStatus.SUCCEEDED,
            PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
            PipelinePhaseStatus.DEGRADED,
            PipelinePhaseStatus.FAILED,
        }
    ),
    PipelinePhaseStatus.SUCCEEDED: frozenset(),
    PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS: frozenset(),
    PipelinePhaseStatus.DEGRADED: frozenset(),
    PipelinePhaseStatus.FAILED: frozenset(),
    PipelinePhaseStatus.SKIPPED: frozenset(),
    PipelinePhaseStatus.REUSED: frozenset(),
}

SUCCESSFUL_PHASE_STATUSES = frozenset(
    {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
        PipelinePhaseStatus.SKIPPED,
        PipelinePhaseStatus.REUSED,
    }
)

REUSABLE_PHASE_STATUSES = frozenset(
    {
        PipelinePhaseStatus.SUCCEEDED,
        PipelinePhaseStatus.SUCCEEDED_WITH_WARNINGS,
        PipelinePhaseStatus.DEGRADED,
        PipelinePhaseStatus.REUSED,
    }
)


class InvalidPipelineTransition(ValueError):
    def __init__(
        self,
        *,
        lifecycle: str,
        current: StrEnum,
        target: StrEnum,
        transition_type: PipelineTransitionType,
    ) -> None:
        self.lifecycle = lifecycle
        self.current = current
        self.target = target
        self.transition_type = transition_type
        super().__init__(
            f"invalid pipeline {lifecycle} transition "
            f"{current.value} -> {target.value} via {transition_type.value}"
        )


def validate_pipeline_run_transition(
    current: PipelineRunStatus,
    target: PipelineRunStatus,
    *,
    transition_type: PipelineTransitionType = PipelineTransitionType.STATE_TRANSITION,
) -> None:
    if not isinstance(current, PipelineRunStatus):
        raise TypeError("current must be a PipelineRunStatus")
    if not isinstance(target, PipelineRunStatus):
        raise TypeError("target must be a PipelineRunStatus")
    if not isinstance(transition_type, PipelineTransitionType):
        raise TypeError("transition_type must be a PipelineTransitionType")

    allowed = target in _RUN_TRANSITIONS[current]
    if (
        transition_type is PipelineTransitionType.RESUME
        and current is PipelineRunStatus.FAILED
        and target is PipelineRunStatus.RUNNING
    ):
        allowed = True
    elif transition_type is not PipelineTransitionType.STATE_TRANSITION:
        allowed = False
    if not allowed:
        raise InvalidPipelineTransition(
            lifecycle="run",
            current=current,
            target=target,
            transition_type=transition_type,
        )


def validate_pipeline_phase_transition(
    current: PipelinePhaseStatus,
    target: PipelinePhaseStatus,
    *,
    transition_type: PipelineTransitionType = PipelineTransitionType.STATE_TRANSITION,
) -> None:
    if not isinstance(current, PipelinePhaseStatus):
        raise TypeError("current must be a PipelinePhaseStatus")
    if not isinstance(target, PipelinePhaseStatus):
        raise TypeError("target must be a PipelinePhaseStatus")
    if not isinstance(transition_type, PipelineTransitionType):
        raise TypeError("transition_type must be a PipelineTransitionType")

    allowed = target in _PHASE_TRANSITIONS[current]
    if transition_type is PipelineTransitionType.RETRY:
        allowed = (
            current is PipelinePhaseStatus.FAILED
            and target is PipelinePhaseStatus.RUNNING
        )
    elif transition_type is PipelineTransitionType.REUSE:
        allowed = (
            current is PipelinePhaseStatus.PENDING
            and target is PipelinePhaseStatus.REUSED
        )
    elif transition_type is PipelineTransitionType.FORCED_REFRESH:
        allowed = (
            current in SUCCESSFUL_PHASE_STATUSES
            and target is PipelinePhaseStatus.RUNNING
        )
    elif transition_type is not PipelineTransitionType.STATE_TRANSITION:
        allowed = False
    if not allowed:
        raise InvalidPipelineTransition(
            lifecycle="phase",
            current=current,
            target=target,
            transition_type=transition_type,
        )


@dataclass(frozen=True, slots=True)
class PipelineRunV1:
    run_id: str
    sport: str
    run_type: str
    requested_date: str
    as_of_time: str
    timezone: str
    pipeline_version: str
    configuration_version: str
    configuration_fingerprint: str
    configuration_metadata: Mapping[str, Any]
    code_revision: str
    database_schema_version: int
    force_refresh: bool
    status: PipelineRunStatus
    failure_phase: PipelinePhaseKey | None
    error_message: str | None
    final_summary: Mapping[str, Any] | None
    created_at: str
    started_at: str | None
    completed_at: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PipelinePhaseV1:
    run_id: str
    phase_key: PipelinePhaseKey
    ordinal: int
    status: PipelinePhaseStatus
    attempt_count: int
    started_at: str | None
    completed_at: str | None
    updated_at: str
    input_checksum: str | None
    output_checksum: str | None
    artifact_relpath: str | None
    warnings: Any | None
    error: Any | None
    reused_from_run_id: str | None


@dataclass(frozen=True, slots=True)
class PipelineTransitionV1:
    transition_id: int
    run_id: str
    phase_key: PipelinePhaseKey | None
    from_status: str | None
    to_status: str
    transition_type: PipelineTransitionType
    reason: str | None
    audit_metadata: Mapping[str, Any]
    transitioned_at: str


@dataclass(frozen=True, slots=True)
class PipelineRunSummaryV1:
    run: PipelineRunV1
    phases: tuple[PipelinePhaseV1, ...]
