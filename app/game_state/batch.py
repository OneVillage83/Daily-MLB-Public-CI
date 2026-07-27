from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from app.daily_slate.contracts import DailySlateV1
from app.game_state.acquisition import (
    GameStateAcquisitionError,
    MlbGameFeedEvidenceV1,
    acquire_mlb_game_feed,
)
from app.stats.contracts import (
    RawArtifact,
    StatsRequest,
    StatsResponse,
    StatsTransport,
    StatsTransportError,
)
from app.stats.raw_store import RawArtifactStore


@dataclass(frozen=True, slots=True)
class MlbGameStateEvidenceSetV1:
    slate_checksum: str
    evidence_by_game: Mapping[str, MlbGameFeedEvidenceV1]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "evidence_by_game",
            MappingProxyType(dict(self.evidence_by_game)),
        )


class GameStateBatchAcquisitionError(GameStateAcquisitionError):
    def __init__(
        self,
        message: str,
        *,
        failed_source_game_id: str,
        completed_evidence: Mapping[str, MlbGameFeedEvidenceV1],
        failure_captures: tuple[RawArtifact, ...] = (),
        attempts: int = 0,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_source_game_id = failed_source_game_id
        self.completed_evidence = MappingProxyType(dict(completed_evidence))
        self.failure_captures = tuple(failure_captures)
        self.attempts = attempts
        self.status_code = status_code


class _RecordingTransport:
    """Retains the last successful transport response around parser failures."""

    def __init__(self, delegate: StatsTransport) -> None:
        self.delegate = delegate
        self.last_response: StatsResponse | None = None

    def fetch(self, request: StatsRequest) -> StatsResponse:
        self.last_response = None
        response = self.delegate.fetch(request)
        self.last_response = response
        return response


def _response_captures(response: StatsResponse) -> tuple[RawArtifact, ...]:
    if response.attempt_captures:
        return response.attempt_captures
    return (response.capture,)


def acquire_mlb_game_state_feeds(
    *,
    transport: StatsTransport,
    raw_store: RawArtifactStore,
    slate: DailySlateV1,
    timeout_seconds: float = 30.0,
    max_attempts: int = 3,
) -> MlbGameStateEvidenceSetV1:
    completed: dict[str, MlbGameFeedEvidenceV1] = {}
    recorder = _RecordingTransport(transport)
    for slate_game in slate.games:
        source_game_id = slate_game.source_game_id
        try:
            evidence = acquire_mlb_game_feed(
                transport=recorder,
                raw_store=raw_store,
                source_game_id=source_game_id,
                timeout_seconds=timeout_seconds,
                max_attempts=max_attempts,
            )
        except StatsTransportError as exc:
            raise GameStateBatchAcquisitionError(
                f"authoritative MLB game-state batch failed for game {source_game_id}",
                failed_source_game_id=source_game_id,
                completed_evidence=completed,
                failure_captures=exc.captures,
                attempts=exc.attempts,
                status_code=exc.status_code,
            ) from exc
        except GameStateAcquisitionError as exc:
            response = recorder.last_response
            captures = () if response is None else _response_captures(response)
            raise GameStateBatchAcquisitionError(
                f"authoritative MLB game-state batch failed for game {source_game_id}: {exc}",
                failed_source_game_id=source_game_id,
                completed_evidence=completed,
                failure_captures=captures,
                attempts=0 if response is None else response.attempts,
                status_code=None if response is None else response.status_code,
            ) from exc
        if evidence.source_game_id != source_game_id:
            raise GameStateBatchAcquisitionError(
                "authoritative MLB game-state response identity changed during acquisition",
                failed_source_game_id=source_game_id,
                completed_evidence=completed,
                failure_captures=_response_captures(evidence.response),
                attempts=evidence.response.attempts,
                status_code=evidence.response.status_code,
            )
        completed[source_game_id] = evidence

    if tuple(completed) != tuple(game.source_game_id for game in slate.games):
        raise GameStateBatchAcquisitionError(
            "authoritative MLB game-state acquisition did not preserve DailySlate ordering",
            failed_source_game_id=(
                slate.games[len(completed)].source_game_id
                if len(completed) < len(slate.games)
                else "unknown"
            ),
            completed_evidence=completed,
        )
    return MlbGameStateEvidenceSetV1(
        slate_checksum=slate.checksum,
        evidence_by_game=completed,
    )
