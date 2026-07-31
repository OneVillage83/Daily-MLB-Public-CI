"""Durable, offline-verifiable Baseball Intelligence Assembly V1 evidence."""

from __future__ import annotations

import json
import hashlib
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.artifacts import UnsafeArtifactPath, resolve_contained_path
from app.baseball_intelligence.artifact import (
    BaseballIntelligenceArtifactV1,
    BaseballIntelligenceArtifactIntegrityError,
    publish_baseball_intelligence_artifact,
    verify_baseball_intelligence_artifact,
)
from app.baseball_intelligence.assembly import (
    BaseballIntelligenceAssemblyResultV1,
    BaseballIntelligenceWarningV1,
    assemble_baseball_intelligence,
)
from app.baseball_intelligence.attempt_manifest import (
    BaseballIntelligenceAttemptManifestArtifactV1,
    BaseballIntelligenceAttemptManifestError,
    BaseballIntelligenceAttemptManifestV1,
    BaseballIntelligenceAttemptOutcome,
    publish_baseball_intelligence_attempt_manifest,
    verify_baseball_intelligence_attempt_manifest,
)
from app.baseball_intelligence.contracts import (
    BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION,
    BaseballFeatureSnapshotV1,
    BaseballIntelligenceAssemblyV1,
    BaseballIntelligenceContractError,
    BaseballIntelligenceGameV1,
    BaseballIntelligenceRole,
    FeatureCompletenessState,
    IntelligenceAvailability,
    PlayerIntelligenceV1,
    TeamBaseballIntelligenceV1,
    TeamIntelligenceCoverageV1,
)
from app.baseball_intelligence.selector import (
    BaseballFeatureCandidateInventoryV1,
    BaseballIntelligenceFeatureSelector,
)
from app.daily_slate.artifact import DailySlateArtifactV1, verify_daily_slate_artifact
from app.daily_slate.contracts import DailySlateGameStatus, canonical_sha256
from app.daily_slate.repository import DailySlateRepository, PersistedDailySlateV1
from app.database import Database
from app.game_state.contracts import (
    GameStatePlayerV1,
    TeamGameStateV1,
)
from app.game_state.repository import GameStateRepository, PersistedGameStateV1
from app.identifiers import validate_run_id
from app.redaction import redact_value
from app.stats.features import FEATURE_VERSION_V3


class BaseballIntelligenceRepositoryError(RuntimeError):
    """Base error for retained Baseball Intelligence Assembly evidence."""


class BaseballIntelligenceNotFoundError(BaseballIntelligenceRepositoryError):
    pass


class BaseballIntelligencePersistenceConflict(BaseballIntelligenceRepositoryError):
    pass


class BaseballIntelligenceIntegrityError(BaseballIntelligenceRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class BaseballIntelligenceAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    upstream_daily_slate_snapshot_id: str
    upstream_daily_slate_checksum: str
    upstream_game_state_snapshot_id: str
    upstream_game_state_checksum: str
    selection_observed_at: datetime
    outcome: BaseballIntelligenceAttemptOutcome
    assembly_checksum: str | None
    manifest: BaseballIntelligenceAttemptManifestArtifactV1
    warnings: tuple[Mapping[str, object], ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedBaseballIntelligenceV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    upstream_daily_slate_snapshot_id: str
    upstream_game_state_snapshot_id: str
    assembly: BaseballIntelligenceAssemblyV1
    artifact: BaseballIntelligenceArtifactV1
    created_at: datetime
    sealed_at: datetime


@dataclass(frozen=True, slots=True)
class _ExpectedGameStatePlayerV1:
    edge_event_id: str
    team_side: str
    team_id: str
    source_team_id: str
    player: GameStatePlayerV1
    roles: tuple[BaseballIntelligenceRole, ...]


def _canonical_text(value: Mapping[str, object]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _aware(value: object, field: str) -> datetime:
    if not isinstance(value, str):
        raise BaseballIntelligenceIntegrityError(f"persisted {field} is not ISO-8601")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise BaseballIntelligenceIntegrityError(f"persisted {field} is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BaseballIntelligenceIntegrityError(f"persisted {field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BaseballIntelligenceIntegrityError(f"persisted {field} must be an object")
    return value


def _string_list(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise BaseballIntelligenceIntegrityError(f"persisted {field} must be a string array")
    return tuple(value)


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise BaseballIntelligenceIntegrityError(f"persisted {field} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise BaseballIntelligenceIntegrityError(f"persisted {field} must be an integer") from exc


def _feature(payload: Mapping[str, object]) -> BaseballFeatureSnapshotV1:
    return BaseballFeatureSnapshotV1(
        feature_snapshot_id=str(payload["feature_snapshot_id"]),
        stats_run_id=str(payload["stats_run_id"]),
        feature_version=str(payload["feature_version"]),
        entity_kind=str(payload["entity_kind"]),
        entity_id=str(payload["entity_id"]),
        feature_as_of=str(payload["feature_as_of"]),
        completeness_state=str(payload["completeness_state"]),
        input_checksum=str(payload["input_checksum"]),
        feature_checksum=str(payload["feature_checksum"]),
        features=_mapping(payload["features"], "feature payload"),
        created_at=_aware(payload["created_at"], "feature created_at"),
    )


def _player(payload: Mapping[str, object]) -> PlayerIntelligenceV1:
    raw_feature = payload.get("feature")
    if raw_feature is not None and not isinstance(raw_feature, Mapping):
        raise BaseballIntelligenceIntegrityError("persisted BIA player feature is invalid")
    raw_roles = payload.get("roles")
    raw_snapshot_ids = payload.get("equivalent_feature_snapshot_ids")
    raw_run_ids = payload.get("equivalent_stats_run_ids")
    if not isinstance(raw_roles, list) or not isinstance(raw_snapshot_ids, list) or not isinstance(raw_run_ids, list):
        raise BaseballIntelligenceIntegrityError("persisted BIA player arrays are invalid")
    return PlayerIntelligenceV1(
        source_player_id=str(payload["source_player_id"]),
        full_name=str(payload["full_name"]),
        player_identity_id=None if payload.get("player_identity_id") is None else str(payload["player_identity_id"]),
        canonical_player_id=None if payload.get("canonical_player_id") is None else str(payload["canonical_player_id"]),
        roles=tuple(BaseballIntelligenceRole(str(value)) for value in raw_roles),
        game_state_player_checksum=str(payload["game_state_player_checksum"]),
        availability=IntelligenceAvailability(str(payload["availability"])),
        feature=None if raw_feature is None else _feature(raw_feature),
        equivalent_feature_snapshot_ids=tuple(str(value) for value in raw_snapshot_ids),
        equivalent_stats_run_ids=tuple(str(value) for value in raw_run_ids),
    )


def _team(payload: Mapping[str, object]) -> TeamBaseballIntelligenceV1:
    coverage = _mapping(payload["coverage"], "team coverage")
    raw_players = payload.get("players")
    if not isinstance(raw_players, list):
        raise BaseballIntelligenceIntegrityError("persisted BIA team players are invalid")
    return TeamBaseballIntelligenceV1(
        team_id=str(payload["team_id"]),
        source_team_id=str(payload["source_team_id"]),
        starter_source_player_id=None if payload.get("starter_source_player_id") is None else str(payload["starter_source_player_id"]),
        lineup_source_player_ids=_string_list(payload["lineup_source_player_ids"], "lineup_source_player_ids"),
        bullpen_source_player_ids=_string_list(payload["bullpen_source_player_ids"], "bullpen_source_player_ids"),
        bench_source_player_ids=_string_list(payload["bench_source_player_ids"], "bench_source_player_ids"),
        batter_source_player_ids=_string_list(payload["batter_source_player_ids"], "batter_source_player_ids"),
        pitcher_source_player_ids=_string_list(payload["pitcher_source_player_ids"], "pitcher_source_player_ids"),
        players=tuple(_player(_mapping(item, "BIA player")) for item in raw_players),
        coverage=TeamIntelligenceCoverageV1(
            gameday_player_count=_integer(coverage["gameday_player_count"], "gameday_player_count"),
            resolved_player_count=_integer(coverage["resolved_player_count"], "resolved_player_count"),
            player_feature_count=_integer(coverage["player_feature_count"], "player_feature_count"),
            lineup_player_count=_integer(coverage["lineup_player_count"], "lineup_player_count"),
            lineup_feature_count=_integer(coverage["lineup_feature_count"], "lineup_feature_count"),
            bullpen_player_count=_integer(coverage["bullpen_player_count"], "bullpen_player_count"),
            bullpen_feature_count=_integer(coverage["bullpen_feature_count"], "bullpen_feature_count"),
            bench_player_count=_integer(coverage["bench_player_count"], "bench_player_count"),
            bench_feature_count=_integer(coverage["bench_feature_count"], "bench_feature_count"),
            starter_feature_available=bool(coverage["starter_feature_available"]),
        ),
    )


def _game(payload: Mapping[str, object]) -> BaseballIntelligenceGameV1:
    return BaseballIntelligenceGameV1(
        edge_event_id=str(payload["edge_event_id"]),
        daily_mlb_game_id=str(payload["daily_mlb_game_id"]),
        source_game_id=str(payload["source_game_id"]),
        away_team_id=str(payload["away_team_id"]),
        home_team_id=str(payload["home_team_id"]),
        venue_id=None if payload.get("venue_id") is None else str(payload["venue_id"]),
        game_status=DailySlateGameStatus(str(payload["game_status"])),
        away=_team(_mapping(payload["away"], "away BIA team")),
        home=_team(_mapping(payload["home"], "home BIA team")),
        upstream_daily_slate_game_checksum=str(payload["upstream_daily_slate_game_checksum"]),
        upstream_game_state_game_checksum=str(payload["upstream_game_state_game_checksum"]),
    )


def _assembly(payload: Mapping[str, object]) -> BaseballIntelligenceAssemblyV1:
    raw_games = payload.get("games")
    raw_runs = payload.get("source_stats_run_ids")
    raw_checksums = payload.get("source_feature_checksums")
    if not isinstance(raw_games, list) or not isinstance(raw_runs, list) or not isinstance(raw_checksums, list):
        raise BaseballIntelligenceIntegrityError("persisted BIA snapshot arrays are invalid")
    return BaseballIntelligenceAssemblyV1(
        requested_date=str(payload["requested_date"]),
        as_of_time=_aware(payload["as_of_time"], "as_of_time"),
        observed_at=_aware(payload["observed_at"], "observed_at"),
        upstream_daily_slate_checksum=str(payload["upstream_daily_slate_checksum"]),
        upstream_game_state_checksum=str(payload["upstream_game_state_checksum"]),
        source_stats_run_ids=tuple(str(value) for value in raw_runs),
        source_feature_checksums=tuple(str(value) for value in raw_checksums),
        games=tuple(_game(_mapping(item, "BIA game")) for item in raw_games),
        feature_version=str(payload["feature_version"]),
        contract_version=str(payload["contract_version"]),
        sport=str(payload["sport"]),
        league=str(payload["league"]),
    )


class BaseballIntelligenceRepository:
    """Persist and reconstruct only sealed, internally retained Phase 3 evidence."""

    def __init__(
        self,
        database: Database,
        *,
        artifact_root: Path,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.artifact_root = Path(artifact_root)
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.daily_slate = DailySlateRepository(database, secret_values=self.secret_values, clock=self._clock)
        self.game_state = GameStateRepository(database, artifact_root=self.artifact_root, secret_values=self.secret_values, clock=self._clock)
        self.selector = BaseballIntelligenceFeatureSelector(database, secret_values=self.secret_values)

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BaseballIntelligenceRepositoryError("repository clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _snapshot_id(checksum: str) -> str:
        return f"bia:{checksum}"

    @staticmethod
    def _warning_payload(warnings: Iterable[BaseballIntelligenceWarningV1 | Mapping[str, object]]) -> tuple[dict[str, object], ...]:
        result: list[dict[str, object]] = []
        for warning in warnings:
            value = warning.as_dict() if isinstance(warning, BaseballIntelligenceWarningV1) else dict(warning)
            result.append(value)
        return tuple(sorted(result, key=lambda value: json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)))

    def _verify_upstream(self, run_id: str) -> tuple[PersistedDailySlateV1, PersistedGameStateV1]:
        slate = self.daily_slate.get_latest_daily_slate_for_run(run_id)
        if slate is None or slate.artifact_relpath is None or slate.artifact_checksum is None:
            raise BaseballIntelligenceNotFoundError("sealed DailySlate artifact evidence does not exist")
        verify_daily_slate_artifact(
            slate.slate,
            DailySlateArtifactV1(slate.artifact_relpath, slate.artifact_checksum, len(slate.slate.canonical_json_bytes())),
            self.artifact_root,
        )
        state = self.game_state.get_latest_game_state_for_run(run_id)
        if state is None:
            raise BaseballIntelligenceNotFoundError("sealed GameState evidence does not exist")
        if (
            state.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or state.state.upstream_daily_slate_checksum != slate.slate.checksum
            or state.state.requested_date != slate.slate.requested_date
            or state.state.as_of_time != slate.slate.as_of_time
            or len(state.state.games) != len(slate.slate.games)
        ):
            raise BaseballIntelligenceIntegrityError("sealed DailySlate/GameState lineage does not reconcile")
        for slate_game, state_game in zip(slate.slate.games, state.state.games, strict=True):
            if (slate_game.edge_event_id, slate_game.daily_mlb_game_id, slate_game.source_game_id, slate_game.away_team_id, slate_game.home_team_id) != (state_game.edge_event_id, state_game.daily_mlb_game_id, state_game.source_game_id, state_game.away_team_id, state_game.home_team_id):
                raise BaseballIntelligenceIntegrityError("sealed DailySlate/GameState games do not reconcile")
        return slate, state

    @staticmethod
    def _expected_team_players(
        *,
        edge_event_id: str,
        team_side: str,
        team: TeamGameStateV1,
    ) -> tuple[_ExpectedGameStatePlayerV1, ...]:
        registry: dict[
            str,
            tuple[GameStatePlayerV1, set[BaseballIntelligenceRole]],
        ] = {}

        def merge(
            player: GameStatePlayerV1,
            role: BaseballIntelligenceRole,
        ) -> None:
            existing = registry.get(player.source_player_id)
            if existing is None:
                registry[player.source_player_id] = (player, {role})
                return
            existing_player, roles = existing
            if existing_player.as_dict() != player.as_dict():
                raise BaseballIntelligenceIntegrityError(
                    "sealed GameState repeats a player with contradictory evidence"
                )
            roles.add(role)

        if team.starter.player is not None:
            merge(team.starter.player, BaseballIntelligenceRole.STARTER)
        for entry in team.lineup.entries:
            merge(entry.player, BaseballIntelligenceRole.LINEUP)
        for player in team.personnel.bullpen:
            merge(player, BaseballIntelligenceRole.BULLPEN)
        for player in team.personnel.bench:
            merge(player, BaseballIntelligenceRole.BENCH)
        for player in team.personnel.batters:
            merge(player, BaseballIntelligenceRole.BATTER)
        for player in team.personnel.pitchers:
            merge(player, BaseballIntelligenceRole.PITCHER)

        role_order = tuple(BaseballIntelligenceRole)
        return tuple(
            _ExpectedGameStatePlayerV1(
                edge_event_id=edge_event_id,
                team_side=team_side,
                team_id=team.team_id,
                source_team_id=team.source_team_id,
                player=player,
                roles=tuple(
                    sorted(
                        roles,
                        key=role_order.index,
                    )
                ),
            )
            for _, (player, roles) in sorted(
                registry.items(),
                key=lambda item: int(item[0]),
            )
        )

    @classmethod
    def _expected_players(
        cls,
        state: PersistedGameStateV1,
    ) -> tuple[_ExpectedGameStatePlayerV1, ...]:
        return tuple(
            player
            for game in state.state.games
            for team_side, team in (("away", game.away), ("home", game.home))
            for player in cls._expected_team_players(
                edge_event_id=game.edge_event_id,
                team_side=team_side,
                team=team,
            )
        )

    @classmethod
    def relevant_canonical_player_ids(
        cls,
        state: PersistedGameStateV1,
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    player.player.canonical_player_id
                    for player in cls._expected_players(state)
                    if player.player.canonical_player_id is not None
                }
            )
        )

    def load_candidate_inventory(self, *, run_id: str, connection: sqlite3.Connection | None = None) -> BaseballFeatureCandidateInventoryV1:
        slate, state = self._verify_upstream(validate_run_id(run_id))
        return self.selector.load_candidates(
            requested_date=slate.slate.requested_date,
            canonical_player_ids=self.relevant_canonical_player_ids(state),
            connection=connection,
        )

    def assemble_for_run(self, *, run_id: str, observed_at: datetime | None = None) -> tuple[BaseballIntelligenceAssemblyResultV1, BaseballFeatureCandidateInventoryV1]:
        safe_run = validate_run_id(run_id)
        slate, state = self._verify_upstream(safe_run)
        inventory = self.selector.load_candidates(
            requested_date=slate.slate.requested_date,
            canonical_player_ids=self.relevant_canonical_player_ids(state),
        )
        return (
            assemble_baseball_intelligence(
                slate=slate.slate,
                game_state=state.state,
                feature_snapshots=inventory.candidates,
                observed_at=observed_at,
            ),
            inventory,
        )

    def _active_phase(self, connection: sqlite3.Connection, run_id: str, attempt: int, requested_date: str) -> None:
        row = connection.execute(
            """SELECT run.requested_date, phase.status, phase.attempt_count
               FROM pipeline_runs run JOIN pipeline_run_phases phase ON phase.run_id=run.run_id
               WHERE run.run_id=? AND phase.phase_key='baseball_intelligence_assembly'""",
            (run_id,),
        ).fetchone()
        if row is None:
            raise BaseballIntelligenceNotFoundError("pipeline run has no BIA phase")
        if str(row["requested_date"]) != requested_date or str(row["status"]) != "running" or int(row["attempt_count"]) != attempt:
            raise BaseballIntelligencePersistenceConflict("BIA persistence requires matching active phase attempt")

    def _bind_inventory(
        self,
        connection: sqlite3.Connection,
        *,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        supplied: BaseballFeatureCandidateInventoryV1,
    ) -> BaseballFeatureCandidateInventoryV1:
        verified = self.selector.load_candidates(
            requested_date=slate.slate.requested_date,
            canonical_player_ids=self.relevant_canonical_player_ids(state),
            connection=connection,
        )
        if (
            supplied.requested_date != verified.requested_date
            or supplied.feature_version != verified.feature_version
            or supplied.canonical_player_ids != verified.canonical_player_ids
            or supplied.candidates != verified.candidates
            or supplied.candidate_feature_snapshot_ids
            != verified.candidate_feature_snapshot_ids
            or supplied.candidate_stats_run_ids != verified.candidate_stats_run_ids
            or supplied.candidate_feature_checksums
            != verified.candidate_feature_checksums
            or supplied.checksum != verified.checksum
        ):
            raise BaseballIntelligencePersistenceConflict(
                "BIA candidate inventory does not match the retained selector result"
            )
        return verified

    def _verify_reassembly(
        self,
        *,
        supplied: BaseballIntelligenceAssemblyResultV1,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        inventory: BaseballFeatureCandidateInventoryV1,
    ) -> None:
        reproduced = assemble_baseball_intelligence(
            slate=slate.slate,
            game_state=state.state,
            feature_snapshots=inventory.candidates,
            observed_at=supplied.assembly.observed_at,
        )
        if (
            reproduced.assembly.canonical_json_bytes()
            != supplied.assembly.canonical_json_bytes()
            or self._warning_payload(reproduced.warnings)
            != self._warning_payload(supplied.warnings)
        ):
            raise BaseballIntelligenceIntegrityError(
                "BIA assembly is not reproducible from retained selector evidence"
            )

    def _manifest(self, *, run_id: str, phase_attempt: int, slate: PersistedDailySlateV1, state: PersistedGameStateV1, outcome: BaseballIntelligenceAttemptOutcome, assembly_checksum: str | None, inventory: BaseballFeatureCandidateInventoryV1, warnings: tuple[dict[str, object], ...], selection_observed_at: datetime | None = None) -> BaseballIntelligenceAttemptManifestV1:
        now = self._now()
        selected_observed_at = now if selection_observed_at is None else selection_observed_at
        return BaseballIntelligenceAttemptManifestV1(
            run_id=run_id,
            phase_attempt=phase_attempt,
            requested_date=slate.slate.requested_date,
            upstream_daily_slate_snapshot_id=slate.snapshot_id,
            upstream_daily_slate_checksum=slate.slate.checksum,
            upstream_game_state_snapshot_id=state.snapshot_id,
            upstream_game_state_checksum=state.state.checksum,
            selection_observed_at=selected_observed_at,
            outcome=outcome,
            assembly_checksum=assembly_checksum,
            candidate_canonical_player_ids=inventory.canonical_player_ids,
            candidate_feature_snapshot_ids=inventory.candidate_feature_snapshot_ids,
            candidate_stats_run_ids=inventory.candidate_stats_run_ids,
            candidate_feature_checksums=inventory.candidate_feature_checksums,
            candidate_inventory_checksum=inventory.checksum,
            warnings=warnings,
            created_at=now,
        )

    def _insert_attempt(self, connection: sqlite3.Connection, manifest: BaseballIntelligenceAttemptManifestV1, artifact: BaseballIntelligenceAttemptManifestArtifactV1) -> None:
        connection.execute(
            """INSERT INTO baseball_intelligence_attempt_evidence(
                 run_id, phase_key, phase_attempt, requested_date,
                 upstream_daily_slate_snapshot_id, upstream_daily_slate_checksum,
                 upstream_game_state_snapshot_id, upstream_game_state_checksum,
                 selection_observed_at, outcome, assembly_checksum,
                 evidence_manifest_relpath, evidence_manifest_checksum,
                 evidence_manifest_byte_count, warnings_json, warning_count, created_at)
               VALUES (?, 'baseball_intelligence_assembly', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                manifest.run_id, manifest.phase_attempt, manifest.requested_date,
                manifest.upstream_daily_slate_snapshot_id, manifest.upstream_daily_slate_checksum,
                manifest.upstream_game_state_snapshot_id, manifest.upstream_game_state_checksum,
                manifest.selection_observed_at.isoformat(), BaseballIntelligenceAttemptOutcome(manifest.outcome).value,
                manifest.assembly_checksum, artifact.relpath, artifact.checksum, artifact.byte_count,
                json.dumps([dict(item) for item in manifest.warnings], sort_keys=True, separators=(",", ":"), ensure_ascii=False),
                len(manifest.warnings), manifest.created_at.isoformat(),
            ),
        )

    def _cleanup_owned_file(
        self,
        *,
        relpath: str,
        checksum: str,
        byte_count: int,
    ) -> None:
        try:
            destination = resolve_contained_path(self.artifact_root, relpath)
            content = destination.read_bytes()
        except (FileNotFoundError, OSError, UnsafeArtifactPath):
            return
        if (
            len(content) == byte_count
            and hashlib.sha256(content).hexdigest() == checksum
        ):
            destination.unlink(missing_ok=True)

    def persist_failed_attempt(self, *, run_id: str, phase_attempt: int, outcome: BaseballIntelligenceAttemptOutcome | str, inventory: BaseballFeatureCandidateInventoryV1, warnings: Iterable[BaseballIntelligenceWarningV1 | Mapping[str, object]] = ()) -> BaseballIntelligenceAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        selected = BaseballIntelligenceAttemptOutcome(outcome)
        if selected is BaseballIntelligenceAttemptOutcome.ASSEMBLED:
            raise ValueError("failed attempt persistence requires a failed outcome")
        slate, state = self._verify_upstream(safe_run)
        warning_payload = self._warning_payload(warnings)
        created_manifest: BaseballIntelligenceAttemptManifestArtifactV1 | None = None
        try:
            with self.database.connect(write=True) as connection:
                verified_inventory = self._bind_inventory(
                    connection,
                    slate=slate,
                    state=state,
                    supplied=inventory,
                )
                existing = connection.execute("SELECT 1 FROM baseball_intelligence_attempt_evidence WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
                if existing is None:
                    self._active_phase(
                        connection,
                        safe_run,
                        phase_attempt,
                        slate.slate.requested_date,
                    )
                    manifest = self._manifest(
                        run_id=safe_run,
                        phase_attempt=phase_attempt,
                        slate=slate,
                        state=state,
                        outcome=selected,
                        assembly_checksum=None,
                        inventory=verified_inventory,
                        warnings=warning_payload,
                    )
                    manifest_artifact, created = (
                        publish_baseball_intelligence_attempt_manifest(
                            manifest,
                            self.artifact_root,
                        )
                    )
                    if created:
                        created_manifest = manifest_artifact
                    self._insert_attempt(connection, manifest, manifest_artifact)
        except BaseException as exc:
            if created_manifest is not None:
                self._cleanup_owned_file(
                    relpath=created_manifest.relpath,
                    checksum=created_manifest.checksum,
                    byte_count=created_manifest.byte_count,
                )
            if isinstance(exc, sqlite3.IntegrityError):
                raise BaseballIntelligencePersistenceConflict(
                    "BIA attempt evidence conflicts with immutable evidence"
                ) from exc
            raise
        evidence = self.get_attempt_evidence(safe_run, phase_attempt)
        manifest = self._manifest_from_artifact(evidence.manifest)
        if (
            evidence.outcome is not selected
            or evidence.assembly_checksum is not None
            or tuple(evidence.warnings) != tuple(warning_payload)
            or manifest.candidate_canonical_player_ids
            != inventory.canonical_player_ids
            or manifest.candidate_feature_snapshot_ids
            != inventory.candidate_feature_snapshot_ids
            or manifest.candidate_stats_run_ids != inventory.candidate_stats_run_ids
            or manifest.candidate_feature_checksums
            != inventory.candidate_feature_checksums
            or manifest.candidate_inventory_checksum != inventory.checksum
        ):
            raise BaseballIntelligencePersistenceConflict(
                "existing BIA failed attempt conflicts with requested evidence"
            )
        return evidence

    def persist_assembly(self, *, run_id: str, phase_attempt: int, result: BaseballIntelligenceAssemblyResultV1, inventory: BaseballFeatureCandidateInventoryV1) -> PersistedBaseballIntelligenceV1:
        safe_run = validate_run_id(run_id)
        assembly = result.assembly
        if assembly.feature_version != FEATURE_VERSION_V3 or assembly.contract_version != BASEBALL_INTELLIGENCE_ASSEMBLY_CONTRACT_VERSION:
            raise BaseballIntelligenceIntegrityError("BIA assembly contract version is invalid")
        if redact_value(assembly.as_dict(), self.secret_values) != assembly.as_dict():
            raise BaseballIntelligenceIntegrityError("BIA assembly contains credential-bearing material")
        slate, state = self._verify_upstream(safe_run)
        warnings = self._warning_payload(result.warnings)
        snapshot_id = self._snapshot_id(assembly.checksum)
        now = self._now()
        created_manifest: BaseballIntelligenceAttemptManifestArtifactV1 | None = None
        created_artifact: BaseballIntelligenceArtifactV1 | None = None
        try:
            with self.database.connect(write=True) as connection:
                verified_inventory = self._bind_inventory(
                    connection,
                    slate=slate,
                    state=state,
                    supplied=inventory,
                )
                self._verify_reassembly(
                    supplied=result,
                    slate=slate,
                    state=state,
                    inventory=verified_inventory,
                )
                self._verify_assembly_lineage(
                    assembly,
                    slate,
                    state,
                    verified_inventory,
                )
                existing = connection.execute("SELECT snapshot_id FROM baseball_intelligence_snapshots WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
                if existing is not None:
                    if str(existing["snapshot_id"]) != snapshot_id:
                        raise BaseballIntelligencePersistenceConflict("BIA run attempt already has conflicting immutable snapshot")
                else:
                    attempt = connection.execute("SELECT 1 FROM baseball_intelligence_attempt_evidence WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
                    if attempt is not None:
                        raise BaseballIntelligencePersistenceConflict(
                            "BIA assembled attempt evidence exists without its immutable snapshot"
                        )
                    self._active_phase(
                        connection,
                        safe_run,
                        phase_attempt,
                        assembly.requested_date,
                    )
                    manifest = self._manifest(
                        run_id=safe_run,
                        phase_attempt=phase_attempt,
                        slate=slate,
                        state=state,
                        outcome=BaseballIntelligenceAttemptOutcome.ASSEMBLED,
                        assembly_checksum=assembly.checksum,
                        inventory=verified_inventory,
                        warnings=warnings,
                        selection_observed_at=assembly.observed_at,
                    )
                    manifest_artifact, manifest_created = (
                        publish_baseball_intelligence_attempt_manifest(
                            manifest,
                            self.artifact_root,
                        )
                    )
                    if manifest_created:
                        created_manifest = manifest_artifact
                    artifact, artifact_created = publish_baseball_intelligence_artifact(
                        assembly,
                        self.artifact_root,
                        secret_values=self.secret_values,
                    )
                    if artifact_created:
                        created_artifact = artifact
                    self._insert_attempt(connection, manifest, manifest_artifact)
                    self._insert_snapshot(connection, snapshot_id, safe_run, phase_attempt, assembly, slate, state, artifact, warnings, now)
                    self._insert_children(
                        connection,
                        snapshot_id,
                        assembly,
                        verified_inventory,
                    )
                    self._verify_relational_snapshot(
                        connection,
                        snapshot_id,
                        assembly,
                        slate,
                        state,
                        verified_inventory,
                        require_sealed=False,
                    )
                    connection.execute("UPDATE baseball_intelligence_snapshots SET sealed_at=? WHERE snapshot_id=?", (now.isoformat(), snapshot_id))
                    self._verify_snapshot(connection, snapshot_id)
        except BaseException as exc:
            for owned in (created_artifact, created_manifest):
                if owned is not None:
                    self._cleanup_owned_file(
                        relpath=owned.relpath,
                        checksum=owned.checksum,
                        byte_count=owned.byte_count,
                    )
            if isinstance(exc, sqlite3.IntegrityError):
                raise BaseballIntelligencePersistenceConflict(
                    "BIA snapshot conflicts with immutable evidence"
                ) from exc
            raise
        persisted = self.get_by_snapshot_id(snapshot_id)
        evidence = self.get_attempt_evidence(safe_run, phase_attempt)
        persisted_manifest = self._manifest_from_artifact(evidence.manifest)
        if (
            persisted.assembly.canonical_json_bytes()
            != assembly.canonical_json_bytes()
            or tuple(evidence.warnings) != tuple(warnings)
            or persisted_manifest.candidate_inventory_checksum != inventory.checksum
            or persisted_manifest.candidate_feature_snapshot_ids
            != inventory.candidate_feature_snapshot_ids
        ):
            raise BaseballIntelligencePersistenceConflict(
                "existing BIA snapshot conflicts with requested immutable evidence"
            )
        return persisted

    def _insert_snapshot(self, connection: sqlite3.Connection, snapshot_id: str, run_id: str, phase_attempt: int, assembly: BaseballIntelligenceAssemblyV1, slate: PersistedDailySlateV1, state: PersistedGameStateV1, artifact: BaseballIntelligenceArtifactV1, warnings: tuple[dict[str, object], ...], now: datetime) -> None:
        connection.execute(
            """INSERT INTO baseball_intelligence_snapshots(
                snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,sport,league,contract_version,feature_version,
                upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,upstream_game_state_snapshot_id,upstream_game_state_checksum,
                assembly_checksum,artifact_relpath,artifact_checksum,artifact_byte_count,source_stats_run_ids_json,source_feature_checksums_json,
                warnings_json,warning_count,canonical_json,game_count,player_count,available_feature_count,equivalent_feature_row_count,sealed_at,created_at
            ) VALUES (
                :snapshot_id,:run_id,'baseball_intelligence_assembly',:phase_attempt,:requested_date,:as_of_time,:observed_at,:sport,:league,:contract_version,:feature_version,
                :slate_id,:slate_checksum,:state_id,:state_checksum,:assembly_checksum,:artifact_relpath,:artifact_checksum,:artifact_byte_count,:source_stats_run_ids_json,:source_feature_checksums_json,
                :warnings_json,:warning_count,:canonical_json,:game_count,:player_count,:available_feature_count,:equivalent_feature_row_count,NULL,:created_at
            )""",
            {
                "snapshot_id": snapshot_id, "run_id": run_id, "phase_attempt": phase_attempt,
                "requested_date": assembly.requested_date, "as_of_time": assembly.as_of_time.isoformat(), "observed_at": assembly.observed_at.isoformat(),
                "sport": assembly.sport, "league": assembly.league, "contract_version": assembly.contract_version, "feature_version": assembly.feature_version,
                "slate_id": slate.snapshot_id, "slate_checksum": assembly.upstream_daily_slate_checksum, "state_id": state.snapshot_id, "state_checksum": assembly.upstream_game_state_checksum,
                "assembly_checksum": assembly.checksum, "artifact_relpath": artifact.relpath, "artifact_checksum": artifact.checksum, "artifact_byte_count": artifact.byte_count,
                "source_stats_run_ids_json": json.dumps(list(assembly.source_stats_run_ids), separators=(",", ":")),
                "source_feature_checksums_json": json.dumps(list(assembly.source_feature_checksums), separators=(",", ":")),
                "warnings_json": json.dumps(list(warnings), sort_keys=True, separators=(",", ":"), ensure_ascii=False), "warning_count": len(warnings),
                "canonical_json": assembly.canonical_json_bytes().decode("utf-8"), "game_count": len(assembly.games),
                "player_count": sum(len(team.players) for game in assembly.games for team in (game.away, game.home)),
                "available_feature_count": sum(player.availability is IntelligenceAvailability.AVAILABLE for game in assembly.games for team in (game.away, game.home) for player in team.players),
                "equivalent_feature_row_count": sum(len(player.equivalent_feature_snapshot_ids) for game in assembly.games for team in (game.away, game.home) for player in team.players),
                "created_at": now.isoformat(),
            },
        )

    def _insert_children(self, connection: sqlite3.Connection, snapshot_id: str, assembly: BaseballIntelligenceAssemblyV1, inventory: BaseballFeatureCandidateInventoryV1) -> None:
        features = {feature.feature_snapshot_id: feature for feature in inventory.candidates}
        for ordinal, game in enumerate(assembly.games, start=1):
            players = tuple((*game.away.players, *game.home.players))
            connection.execute(
                """INSERT INTO baseball_intelligence_games(snapshot_id,ordinal,edge_event_id,daily_mlb_game_id,source_game_id,away_team_id,home_team_id,venue_id,game_status,upstream_daily_slate_game_checksum,upstream_game_state_game_checksum,player_count,available_feature_count,canonical_json,row_checksum) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (snapshot_id,ordinal,game.edge_event_id,game.daily_mlb_game_id,game.source_game_id,game.away_team_id,game.home_team_id,game.venue_id,game.game_status.value,game.upstream_daily_slate_game_checksum,game.upstream_game_state_game_checksum,len(players),sum(player.availability is IntelligenceAvailability.AVAILABLE for player in players),json.dumps(game.as_dict(),sort_keys=True,separators=(",",":"),ensure_ascii=False),game.checksum),
            )
            for team_side, team in (("away", game.away), ("home", game.home)):
                for player_ordinal, player in enumerate(team.players, start=1):
                    connection.execute(
                        """INSERT INTO baseball_intelligence_players(snapshot_id,edge_event_id,team_side,team_id,source_team_id,source_player_id,player_identity_id,canonical_player_id,availability,representative_feature_snapshot_id,representative_stats_run_id,representative_feature_checksum,representative_completeness_state,game_state_player_checksum,roles_json,canonical_json,row_checksum,ordinal) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (snapshot_id,game.edge_event_id,team_side,team.team_id,team.source_team_id,player.source_player_id,player.player_identity_id,player.canonical_player_id,player.availability.value,None if player.feature is None else player.feature.feature_snapshot_id,None if player.feature is None else player.feature.stats_run_id,None if player.feature is None else player.feature.feature_checksum,None if player.feature is None else FeatureCompletenessState(player.feature.completeness_state).value,player.game_state_player_checksum,json.dumps([role.value for role in player.roles],separators=(",",":")),json.dumps(player.as_dict(),sort_keys=True,separators=(",",":"),ensure_ascii=False),canonical_sha256(player.as_dict()),player_ordinal),
                    )
                    if player.feature is None:
                        continue
                    equivalent_sources: list[BaseballFeatureSnapshotV1] = []
                    for equivalent_ordinal, feature_id in enumerate(player.equivalent_feature_snapshot_ids, start=1):
                        feature = features.get(feature_id)
                        if (
                            feature is None
                            or feature.entity_id != player.canonical_player_id
                            or feature.feature_checksum
                            != player.feature.feature_checksum
                        ):
                            raise BaseballIntelligenceIntegrityError("selected BIA equivalent source feature is absent from candidate inventory")
                        equivalent_sources.append(feature)
                        connection.execute(
                            """INSERT INTO baseball_intelligence_feature_equivalents(snapshot_id,edge_event_id,team_id,source_player_id,ordinal,feature_snapshot_id,stats_run_id,canonical_player_id,feature_checksum,completeness_state,is_representative) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (snapshot_id,game.edge_event_id,team.team_id,player.source_player_id,equivalent_ordinal,feature.feature_snapshot_id,feature.stats_run_id,feature.entity_id,feature.feature_checksum,FeatureCompletenessState(feature.completeness_state).value,int(feature.feature_snapshot_id == player.feature.feature_snapshot_id)),
                        )
                    if (
                        tuple(
                            sorted(
                                {
                                    source.stats_run_id
                                    for source in equivalent_sources
                                }
                            )
                        )
                        != player.equivalent_stats_run_ids
                    ):
                        raise BaseballIntelligenceIntegrityError(
                            "selected BIA equivalent snapshot/run pairs do not reconcile"
                        )

    def _verify_assembly_lineage(self, assembly: BaseballIntelligenceAssemblyV1, slate: PersistedDailySlateV1, state: PersistedGameStateV1, inventory: BaseballFeatureCandidateInventoryV1) -> None:
        if assembly.requested_date != slate.slate.requested_date or assembly.as_of_time != slate.slate.as_of_time or assembly.upstream_daily_slate_checksum != slate.slate.checksum or assembly.upstream_game_state_checksum != state.state.checksum:
            raise BaseballIntelligenceIntegrityError("assembly does not match sealed upstream lineage")
        if assembly.observed_at < max(slate.slate.observed_at, state.state.observed_at):
            raise BaseballIntelligenceIntegrityError("assembly observed_at precedes sealed upstream evidence")
        if len(assembly.games) != len(slate.slate.games) or len(assembly.games) != len(
            state.state.games
        ):
            raise BaseballIntelligenceIntegrityError("assembly game inventory does not match sealed GameState")
        expected_players = {
            (
                player.edge_event_id,
                player.team_side,
                player.player.source_player_id,
            ): player
            for player in self._expected_players(state)
        }
        actual_player_keys: set[tuple[str, str, str]] = set()
        selected_features: list[BaseballFeatureSnapshotV1] = []
        by_id = {item.feature_snapshot_id: item for item in inventory.candidates}
        for ordinal, (game, slate_game, state_game) in enumerate(
            zip(
                assembly.games,
                slate.slate.games,
                state.state.games,
                strict=True,
            ),
            start=1,
        ):
            expected_game = (
                slate_game.edge_event_id,
                slate_game.daily_mlb_game_id,
                slate_game.source_game_id,
                slate_game.away_team_id,
                slate_game.home_team_id,
                slate_game.venue_id,
                state_game.game_status,
                slate_game.checksum,
                state_game.checksum,
            )
            actual_game = (
                game.edge_event_id,
                game.daily_mlb_game_id,
                game.source_game_id,
                game.away_team_id,
                game.home_team_id,
                game.venue_id,
                game.game_status,
                game.upstream_daily_slate_game_checksum,
                game.upstream_game_state_game_checksum,
            )
            if actual_game != expected_game:
                raise BaseballIntelligenceIntegrityError(
                    f"BIA game ordinal {ordinal} does not match sealed upstream evidence"
                )
            for team_side, team, state_team in (
                ("away", game.away, state_game.away),
                ("home", game.home, state_game.home),
            ):
                expected_indexes = (
                    state_team.team_id,
                    state_team.source_team_id,
                    (
                        None
                        if state_team.starter.player is None
                        else state_team.starter.player.source_player_id
                    ),
                    tuple(
                        entry.player.source_player_id
                        for entry in state_team.lineup.entries
                    ),
                    tuple(
                        player.source_player_id
                        for player in state_team.personnel.bullpen
                    ),
                    tuple(
                        player.source_player_id
                        for player in state_team.personnel.bench
                    ),
                    tuple(
                        player.source_player_id
                        for player in state_team.personnel.batters
                    ),
                    tuple(
                        player.source_player_id
                        for player in state_team.personnel.pitchers
                    ),
                )
                actual_indexes = (
                    team.team_id,
                    team.source_team_id,
                    team.starter_source_player_id,
                    team.lineup_source_player_ids,
                    team.bullpen_source_player_ids,
                    team.bench_source_player_ids,
                    team.batter_source_player_ids,
                    team.pitcher_source_player_ids,
                )
                if actual_indexes != expected_indexes:
                    raise BaseballIntelligenceIntegrityError(
                        "BIA team structure does not match sealed GameState"
                    )
                for player in team.players:
                    key = (game.edge_event_id, team_side, player.source_player_id)
                    expected_player = expected_players.get(key)
                    if expected_player is None or key in actual_player_keys:
                        raise BaseballIntelligenceIntegrityError(
                            "BIA player membership does not match sealed GameState"
                        )
                    actual_player_keys.add(key)
                    source = expected_player.player
                    if (
                        player.full_name != source.full_name
                        or player.player_identity_id != source.player_identity_id
                        or player.canonical_player_id != source.canonical_player_id
                        or player.roles != expected_player.roles
                        or player.game_state_player_checksum
                        != canonical_sha256(source.as_dict())
                    ):
                        raise BaseballIntelligenceIntegrityError(
                            "BIA player evidence does not match sealed GameState"
                        )
                    if player.feature is None:
                        continue
                    selected_features.append(player.feature)
                    selected = by_id.get(player.feature.feature_snapshot_id)
                    if selected is None or selected != player.feature:
                        raise BaseballIntelligenceIntegrityError("selected feature is absent from retained candidate inventory")
                    for feature_id in player.equivalent_feature_snapshot_ids:
                        equivalent = by_id.get(feature_id)
                        if (
                            equivalent is None
                            or equivalent.entity_id != player.canonical_player_id
                            or equivalent.feature_checksum
                            != player.feature.feature_checksum
                            or FeatureCompletenessState(equivalent.completeness_state)
                            is FeatureCompletenessState.BLOCKED
                            or equivalent.created_at > assembly.observed_at
                        ):
                            raise BaseballIntelligenceIntegrityError("selected equivalent feature does not reconcile")
                    expected_runs = tuple(
                        sorted(
                            {
                                by_id[feature_id].stats_run_id
                                for feature_id in player.equivalent_feature_snapshot_ids
                            }
                        )
                    )
                    if player.equivalent_stats_run_ids != expected_runs:
                        raise BaseballIntelligenceIntegrityError(
                            "selected equivalent snapshot/run lineage does not reconcile"
                        )
        if actual_player_keys != set(expected_players):
            raise BaseballIntelligenceIntegrityError(
                "BIA omits sealed GameState player evidence"
            )
        expected_runs = tuple(sorted({run_id for game in assembly.games for team in (game.away, game.home) for player in team.players for run_id in player.equivalent_stats_run_ids}))
        expected_checksums = tuple(sorted({feature.feature_checksum for feature in selected_features}))
        if assembly.source_stats_run_ids != expected_runs or assembly.source_feature_checksums != expected_checksums:
            raise BaseballIntelligenceIntegrityError("assembly selected inventories do not reconcile")

    def _verify_relational_snapshot(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
        assembly: BaseballIntelligenceAssemblyV1,
        slate: PersistedDailySlateV1,
        state: PersistedGameStateV1,
        inventory: BaseballFeatureCandidateInventoryV1,
        *,
        require_sealed: bool,
    ) -> None:
        self._verify_assembly_lineage(assembly, slate, state, inventory)
        row = connection.execute(
            "SELECT * FROM baseball_intelligence_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            raise BaseballIntelligenceIntegrityError("BIA snapshot row is missing")
        if (row["sealed_at"] is not None) != require_sealed:
            raise BaseballIntelligenceIntegrityError(
                "BIA snapshot sealing state does not match verification boundary"
            )
        expected_player_count = sum(
            len(team.players)
            for game in assembly.games
            for team in (game.away, game.home)
        )
        expected_available_count = sum(
            player.availability is IntelligenceAvailability.AVAILABLE
            for game in assembly.games
            for team in (game.away, game.home)
            for player in team.players
        )
        expected_equivalent_count = sum(
            len(player.equivalent_feature_snapshot_ids)
            for game in assembly.games
            for team in (game.away, game.home)
            for player in team.players
        )
        snapshot_values = (
            str(row["snapshot_id"]),
            str(row["requested_date"]),
            _aware(row["as_of_time"], "snapshot as_of_time"),
            _aware(row["observed_at"], "snapshot observed_at"),
            str(row["assembly_checksum"]),
            str(row["upstream_daily_slate_snapshot_id"]),
            str(row["upstream_daily_slate_checksum"]),
            str(row["upstream_game_state_snapshot_id"]),
            str(row["upstream_game_state_checksum"]),
            str(row["canonical_json"]),
            int(row["game_count"]),
            int(row["player_count"]),
            int(row["available_feature_count"]),
            int(row["equivalent_feature_row_count"]),
        )
        expected_snapshot_values = (
            self._snapshot_id(assembly.checksum),
            assembly.requested_date,
            assembly.as_of_time,
            assembly.observed_at,
            assembly.checksum,
            slate.snapshot_id,
            slate.slate.checksum,
            state.snapshot_id,
            state.state.checksum,
            assembly.canonical_json_bytes().decode("utf-8"),
            len(assembly.games),
            expected_player_count,
            expected_available_count,
            expected_equivalent_count,
        )
        if snapshot_values != expected_snapshot_values:
            raise BaseballIntelligenceIntegrityError(
                "BIA snapshot relational evidence does not reconcile"
            )
        try:
            stored_runs = _string_list(
                json.loads(str(row["source_stats_run_ids_json"])),
                "source_stats_run_ids",
            )
            stored_checksums = _string_list(
                json.loads(str(row["source_feature_checksums_json"])),
                "source_feature_checksums",
            )
        except json.JSONDecodeError as exc:
            raise BaseballIntelligenceIntegrityError(
                "BIA snapshot selected inventories are invalid"
            ) from exc
        if (
            stored_runs != assembly.source_stats_run_ids
            or stored_checksums != assembly.source_feature_checksums
        ):
            raise BaseballIntelligenceIntegrityError(
                "BIA snapshot selected inventories do not reconcile"
            )
        artifact = BaseballIntelligenceArtifactV1(
            str(row["artifact_relpath"]),
            str(row["artifact_checksum"]),
            int(row["artifact_byte_count"]),
        )
        verify_baseball_intelligence_artifact(
            assembly,
            artifact,
            self.artifact_root,
            secret_values=self.secret_values,
        )
        attempt_row = connection.execute(
            """SELECT * FROM baseball_intelligence_attempt_evidence
               WHERE run_id=? AND phase_attempt=?""",
            (str(row["run_id"]), int(row["phase_attempt"])),
        ).fetchone()
        if attempt_row is None:
            raise BaseballIntelligenceIntegrityError(
                "BIA attempt evidence does not exist"
            )
        manifest = self._manifest_from_row(attempt_row)
        manifest_artifact = verify_baseball_intelligence_attempt_manifest(
            artifact_root=self.artifact_root,
            relpath=str(attempt_row["evidence_manifest_relpath"]),
            expected=manifest,
        )
        try:
            snapshot_warnings = json.loads(str(row["warnings_json"]))
        except json.JSONDecodeError as exc:
            raise BaseballIntelligenceIntegrityError(
                "BIA snapshot warnings are invalid"
            ) from exc
        if (
            manifest_artifact.checksum
            != str(attempt_row["evidence_manifest_checksum"])
            or manifest_artifact.byte_count
            != int(attempt_row["evidence_manifest_byte_count"])
            or manifest.outcome is not BaseballIntelligenceAttemptOutcome.ASSEMBLED
            or manifest.assembly_checksum != assembly.checksum
            or manifest.run_id != str(row["run_id"])
            or manifest.phase_attempt != int(row["phase_attempt"])
            or manifest.requested_date != assembly.requested_date
            or manifest.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or manifest.upstream_daily_slate_checksum != slate.slate.checksum
            or manifest.upstream_game_state_snapshot_id != state.snapshot_id
            or manifest.upstream_game_state_checksum != state.state.checksum
            or manifest.selection_observed_at != assembly.observed_at
            or manifest.candidate_canonical_player_ids
            != inventory.canonical_player_ids
            or manifest.candidate_feature_snapshot_ids
            != inventory.candidate_feature_snapshot_ids
            or manifest.candidate_stats_run_ids != inventory.candidate_stats_run_ids
            or manifest.candidate_feature_checksums
            != inventory.candidate_feature_checksums
            or manifest.candidate_inventory_checksum != inventory.checksum
            or snapshot_warnings != [dict(value) for value in manifest.warnings]
            or int(row["warning_count"]) != len(manifest.warnings)
        ):
            raise BaseballIntelligenceIntegrityError(
                "BIA attempt manifest and snapshot evidence do not reconcile"
            )

        game_rows = connection.execute(
            """SELECT * FROM baseball_intelligence_games
               WHERE snapshot_id=? ORDER BY ordinal""",
            (snapshot_id,),
        ).fetchall()
        if len(game_rows) != len(assembly.games):
            raise BaseballIntelligenceIntegrityError(
                "BIA relational game count is incomplete"
            )
        game_by_edge = {
            game.edge_event_id: game for game in assembly.games
        }
        for ordinal, (stored, game) in enumerate(
            zip(game_rows, assembly.games, strict=True),
            start=1,
        ):
            players = (*game.away.players, *game.home.players)
            expected_game_row = (
                ordinal,
                game.edge_event_id,
                game.daily_mlb_game_id,
                game.source_game_id,
                game.away_team_id,
                game.home_team_id,
                game.venue_id,
                game.game_status.value,
                game.upstream_daily_slate_game_checksum,
                game.upstream_game_state_game_checksum,
                len(players),
                sum(
                    player.availability is IntelligenceAvailability.AVAILABLE
                    for player in players
                ),
                _canonical_text(game.as_dict()),
                game.checksum,
            )
            actual_game_row = (
                int(stored["ordinal"]),
                str(stored["edge_event_id"]),
                str(stored["daily_mlb_game_id"]),
                str(stored["source_game_id"]),
                str(stored["away_team_id"]),
                str(stored["home_team_id"]),
                None if stored["venue_id"] is None else str(stored["venue_id"]),
                str(stored["game_status"]),
                str(stored["upstream_daily_slate_game_checksum"]),
                str(stored["upstream_game_state_game_checksum"]),
                int(stored["player_count"]),
                int(stored["available_feature_count"]),
                str(stored["canonical_json"]),
                str(stored["row_checksum"]),
            )
            if actual_game_row != expected_game_row:
                raise BaseballIntelligenceIntegrityError(
                    "BIA relational game evidence does not reconcile"
                )

        player_rows = connection.execute(
            """SELECT * FROM baseball_intelligence_players
               WHERE snapshot_id=?
               ORDER BY edge_event_id,
                        CASE team_side WHEN 'away' THEN 0 ELSE 1 END,
                        ordinal""",
            (snapshot_id,),
        ).fetchall()
        expected_players = [
            (game, side, team, ordinal, player)
            for game in assembly.games
            for side, team in (("away", game.away), ("home", game.home))
            for ordinal, player in enumerate(team.players, start=1)
        ]
        if len(player_rows) != len(expected_players):
            raise BaseballIntelligenceIntegrityError(
                "BIA relational player count does not reconcile"
            )
        feature_by_id = {
            feature.feature_snapshot_id: feature for feature in inventory.candidates
        }
        equivalent_rows = connection.execute(
            """SELECT * FROM baseball_intelligence_feature_equivalents
               WHERE snapshot_id=?
               ORDER BY edge_event_id,team_id,source_player_id,ordinal""",
            (snapshot_id,),
        ).fetchall()
        equivalents_by_player: dict[
            tuple[str, str, str],
            list[sqlite3.Row],
        ] = {}
        for equivalent_row in equivalent_rows:
            key = (
                str(equivalent_row["edge_event_id"]),
                str(equivalent_row["team_id"]),
                str(equivalent_row["source_player_id"]),
            )
            equivalents_by_player.setdefault(key, []).append(equivalent_row)

        selected_runs: set[str] = set()
        selected_checksums: set[str] = set()
        for stored, (game, side, team, ordinal, player) in zip(
            player_rows,
            expected_players,
            strict=True,
        ):
            feature = player.feature
            expected_player_row = (
                game.edge_event_id,
                side,
                team.team_id,
                team.source_team_id,
                player.source_player_id,
                player.player_identity_id,
                player.canonical_player_id,
                player.availability.value,
                None if feature is None else feature.feature_snapshot_id,
                None if feature is None else feature.stats_run_id,
                None if feature is None else feature.feature_checksum,
                (
                    None
                    if feature is None
                    else FeatureCompletenessState(
                        feature.completeness_state
                    ).value
                ),
                player.game_state_player_checksum,
                json.dumps(
                    [role.value for role in player.roles],
                    separators=(",", ":"),
                ),
                _canonical_text(player.as_dict()),
                canonical_sha256(player.as_dict()),
                ordinal,
            )
            actual_player_row = (
                str(stored["edge_event_id"]),
                str(stored["team_side"]),
                str(stored["team_id"]),
                str(stored["source_team_id"]),
                str(stored["source_player_id"]),
                (
                    None
                    if stored["player_identity_id"] is None
                    else str(stored["player_identity_id"])
                ),
                (
                    None
                    if stored["canonical_player_id"] is None
                    else str(stored["canonical_player_id"])
                ),
                str(stored["availability"]),
                (
                    None
                    if stored["representative_feature_snapshot_id"] is None
                    else str(stored["representative_feature_snapshot_id"])
                ),
                (
                    None
                    if stored["representative_stats_run_id"] is None
                    else str(stored["representative_stats_run_id"])
                ),
                (
                    None
                    if stored["representative_feature_checksum"] is None
                    else str(stored["representative_feature_checksum"])
                ),
                (
                    None
                    if stored["representative_completeness_state"] is None
                    else str(stored["representative_completeness_state"])
                ),
                str(stored["game_state_player_checksum"]),
                str(stored["roles_json"]),
                str(stored["canonical_json"]),
                str(stored["row_checksum"]),
                int(stored["ordinal"]),
            )
            if actual_player_row != expected_player_row:
                raise BaseballIntelligenceIntegrityError(
                    "BIA relational player evidence does not reconcile"
                )
            key = (game.edge_event_id, team.team_id, player.source_player_id)
            retained_rows = equivalents_by_player.pop(key, [])
            if feature is None:
                if retained_rows:
                    raise BaseballIntelligenceIntegrityError(
                        "unavailable BIA player has selected equivalent evidence"
                    )
                continue
            if len(retained_rows) != len(player.equivalent_feature_snapshot_ids):
                raise BaseballIntelligenceIntegrityError(
                    "BIA equivalent row count does not match its player"
                )
            retained_run_ids: list[str] = []
            representatives = 0
            for equivalent_ordinal, (equivalent_row, feature_id) in enumerate(
                zip(
                    retained_rows,
                    player.equivalent_feature_snapshot_ids,
                    strict=True,
                ),
                start=1,
            ):
                source = feature_by_id.get(feature_id)
                if source is None:
                    raise BaseballIntelligenceIntegrityError(
                        "BIA equivalent source row is absent"
                    )
                is_representative = int(
                    source.feature_snapshot_id == feature.feature_snapshot_id
                    and source.stats_run_id == feature.stats_run_id
                    and source.feature_checksum == feature.feature_checksum
                )
                expected_equivalent = (
                    equivalent_ordinal,
                    source.feature_snapshot_id,
                    source.stats_run_id,
                    source.entity_id,
                    source.feature_checksum,
                    FeatureCompletenessState(source.completeness_state).value,
                    is_representative,
                )
                actual_equivalent = (
                    int(equivalent_row["ordinal"]),
                    str(equivalent_row["feature_snapshot_id"]),
                    str(equivalent_row["stats_run_id"]),
                    str(equivalent_row["canonical_player_id"]),
                    str(equivalent_row["feature_checksum"]),
                    str(equivalent_row["completeness_state"]),
                    int(equivalent_row["is_representative"]),
                )
                if (
                    actual_equivalent != expected_equivalent
                    or source.entity_kind != "player"
                    or source.feature_version != FEATURE_VERSION_V3
                    or source.feature_as_of != assembly.requested_date
                    or FeatureCompletenessState(source.completeness_state)
                    is FeatureCompletenessState.BLOCKED
                    or source.created_at > assembly.observed_at
                ):
                    raise BaseballIntelligenceIntegrityError(
                        "BIA equivalent relational evidence does not reconcile"
                    )
                retained_run_ids.append(source.stats_run_id)
                selected_runs.add(source.stats_run_id)
                selected_checksums.add(source.feature_checksum)
                representatives += is_representative
            if (
                tuple(sorted(set(retained_run_ids)))
                != player.equivalent_stats_run_ids
                or representatives != 1
            ):
                raise BaseballIntelligenceIntegrityError(
                    "BIA equivalent representative lineage does not reconcile"
                )
        if equivalents_by_player:
            raise BaseballIntelligenceIntegrityError(
                "BIA contains extra equivalent relational evidence"
            )
        if (
            len(equivalent_rows) != expected_equivalent_count
            or tuple(sorted(selected_runs)) != assembly.source_stats_run_ids
            or tuple(sorted(selected_checksums))
            != assembly.source_feature_checksums
        ):
            raise BaseballIntelligenceIntegrityError(
                "BIA relational selected inventories do not reconcile"
            )
        if set(game_by_edge) != {
            str(row_value["edge_event_id"]) for row_value in game_rows
        }:
            raise BaseballIntelligenceIntegrityError(
                "BIA relational game membership does not reconcile"
            )

    def _manifest_from_row(self, row: sqlite3.Row) -> BaseballIntelligenceAttemptManifestV1:
        try:
            warnings = json.loads(str(row["warnings_json"]))
        except json.JSONDecodeError as exc:
            raise BaseballIntelligenceIntegrityError("BIA attempt warnings are invalid") from exc
        if not isinstance(warnings, list) or len(warnings) != int(row["warning_count"]):
            raise BaseballIntelligenceIntegrityError("BIA attempt warning count does not reconcile")
        try:
            content = self._read_manifest_json(str(row["evidence_manifest_relpath"]))
            return BaseballIntelligenceAttemptManifestV1(
                run_id=str(row["run_id"]), phase_attempt=int(row["phase_attempt"]), requested_date=str(row["requested_date"]),
                upstream_daily_slate_snapshot_id=str(row["upstream_daily_slate_snapshot_id"]), upstream_daily_slate_checksum=str(row["upstream_daily_slate_checksum"]),
                upstream_game_state_snapshot_id=str(row["upstream_game_state_snapshot_id"]), upstream_game_state_checksum=str(row["upstream_game_state_checksum"]),
                selection_observed_at=_aware(row["selection_observed_at"], "selection_observed_at"), outcome=BaseballIntelligenceAttemptOutcome(str(row["outcome"])), assembly_checksum=None if row["assembly_checksum"] is None else str(row["assembly_checksum"]),
                candidate_canonical_player_ids=_string_list(content["candidate_canonical_player_ids"], "candidate_canonical_player_ids"),
                candidate_feature_snapshot_ids=_string_list(content["candidate_feature_snapshot_ids"], "candidate_feature_snapshot_ids"),
                candidate_stats_run_ids=_string_list(content["candidate_stats_run_ids"], "candidate_stats_run_ids"),
                candidate_feature_checksums=_string_list(content["candidate_feature_checksums"], "candidate_feature_checksums"),
                candidate_inventory_checksum=str(content["candidate_inventory_checksum"]),
                warnings=tuple(_mapping(value, "attempt warning") for value in warnings), created_at=_aware(row["created_at"], "attempt created_at"),
            )
        except (KeyError, TypeError, ValueError, BaseballIntelligenceAttemptManifestError) as exc:
            raise BaseballIntelligenceIntegrityError("BIA attempt manifest does not reconcile") from exc

    def _manifest_from_artifact(
        self,
        artifact: BaseballIntelligenceAttemptManifestArtifactV1,
    ) -> BaseballIntelligenceAttemptManifestV1:
        content = self._read_manifest_json(artifact.relpath)
        try:
            raw_warnings = content["warnings"]
            if not isinstance(raw_warnings, list):
                raise TypeError("warnings")
            return BaseballIntelligenceAttemptManifestV1(
                run_id=str(content["run_id"]),
                phase_attempt=_integer(content["phase_attempt"], "phase_attempt"),
                requested_date=str(content["requested_date"]),
                upstream_daily_slate_snapshot_id=str(
                    content["upstream_daily_slate_snapshot_id"]
                ),
                upstream_daily_slate_checksum=str(
                    content["upstream_daily_slate_checksum"]
                ),
                upstream_game_state_snapshot_id=str(
                    content["upstream_game_state_snapshot_id"]
                ),
                upstream_game_state_checksum=str(
                    content["upstream_game_state_checksum"]
                ),
                selection_observed_at=_aware(
                    content["selection_observed_at"],
                    "selection_observed_at",
                ),
                outcome=BaseballIntelligenceAttemptOutcome(str(content["outcome"])),
                assembly_checksum=(
                    None
                    if content["assembly_checksum"] is None
                    else str(content["assembly_checksum"])
                ),
                candidate_canonical_player_ids=_string_list(
                    content["candidate_canonical_player_ids"],
                    "candidate_canonical_player_ids",
                ),
                candidate_feature_snapshot_ids=_string_list(
                    content["candidate_feature_snapshot_ids"],
                    "candidate_feature_snapshot_ids",
                ),
                candidate_stats_run_ids=_string_list(
                    content["candidate_stats_run_ids"],
                    "candidate_stats_run_ids",
                ),
                candidate_feature_checksums=_string_list(
                    content["candidate_feature_checksums"],
                    "candidate_feature_checksums",
                ),
                candidate_inventory_checksum=str(
                    content["candidate_inventory_checksum"]
                ),
                warnings=tuple(
                    _mapping(value, "attempt warning") for value in raw_warnings
                ),
                created_at=_aware(content["created_at"], "attempt created_at"),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            BaseballIntelligenceAttemptManifestError,
        ) as exc:
            raise BaseballIntelligenceIntegrityError(
                "BIA attempt manifest does not reconcile"
            ) from exc

    def _read_manifest_json(self, relpath: str) -> Mapping[str, object]:
        from app.artifacts import resolve_contained_path
        try:
            value = json.loads(resolve_contained_path(self.artifact_root, relpath).read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError) as exc:
            raise BaseballIntelligenceAttemptManifestError("BIA attempt manifest is unreadable") from exc
        return _mapping(value, "attempt manifest")

    def get_attempt_evidence(self, run_id: str, phase_attempt: int) -> BaseballIntelligenceAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM baseball_intelligence_attempt_evidence WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
        if row is None:
            raise BaseballIntelligenceNotFoundError("BIA attempt evidence does not exist")
        manifest = self._manifest_from_row(row)
        artifact = verify_baseball_intelligence_attempt_manifest(artifact_root=self.artifact_root, relpath=str(row["evidence_manifest_relpath"]), expected=manifest)
        if artifact.checksum != str(row["evidence_manifest_checksum"]) or artifact.byte_count != int(row["evidence_manifest_byte_count"]):
            raise BaseballIntelligenceIntegrityError("BIA attempt manifest metadata does not match retained bytes")
        return BaseballIntelligenceAttemptEvidenceV1(safe_run,int(row["phase_attempt"]),str(row["requested_date"]),str(row["upstream_daily_slate_snapshot_id"]),str(row["upstream_daily_slate_checksum"]),str(row["upstream_game_state_snapshot_id"]),str(row["upstream_game_state_checksum"]),_aware(row["selection_observed_at"], "selection_observed_at"),BaseballIntelligenceAttemptOutcome(str(row["outcome"])),None if row["assembly_checksum"] is None else str(row["assembly_checksum"]),artifact,tuple(_mapping(value,"attempt warning") for value in json.loads(str(row["warnings_json"]))),_aware(row["created_at"],"attempt created_at"))

    def list_attempt_evidence(self, run_id: str) -> tuple[BaseballIntelligenceAttemptEvidenceV1, ...]:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute("SELECT phase_attempt FROM baseball_intelligence_attempt_evidence WHERE run_id=? ORDER BY phase_attempt", (safe_run,)).fetchall()
        return tuple(self.get_attempt_evidence(safe_run, int(row["phase_attempt"])) for row in rows)

    def _verify_snapshot(self, connection: sqlite3.Connection, snapshot_id: str) -> PersistedBaseballIntelligenceV1:
        row = connection.execute("SELECT * FROM baseball_intelligence_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise BaseballIntelligenceNotFoundError("BIA snapshot does not exist")
        if row["sealed_at"] is None:
            raise BaseballIntelligenceIntegrityError("BIA snapshot is not sealed")
        try:
            assembly = _assembly(_mapping(json.loads(str(row["canonical_json"])), "BIA snapshot"))
        except (json.JSONDecodeError, BaseballIntelligenceContractError, KeyError, TypeError, ValueError) as exc:
            raise BaseballIntelligenceIntegrityError("BIA snapshot canonical JSON is invalid") from exc
        if assembly.checksum != str(row["assembly_checksum"]) or assembly.canonical_json_bytes().decode("utf-8") != str(row["canonical_json"]):
            raise BaseballIntelligenceIntegrityError("BIA snapshot checksum does not reconcile")
        try:
            slate = self.daily_slate.get_daily_slate_snapshot(
                str(row["upstream_daily_slate_snapshot_id"])
            )
            state = self.game_state.get_game_state_snapshot(
                str(row["upstream_game_state_snapshot_id"])
            )
        except (BaseballIntelligenceRepositoryError, RuntimeError, ValueError) as exc:
            raise BaseballIntelligenceIntegrityError(
                "BIA sealed upstream evidence cannot be reconstructed"
            ) from exc
        if (
            slate.run_id != str(row["run_id"])
            or state.run_id != str(row["run_id"])
            or state.upstream_daily_slate_snapshot_id != slate.snapshot_id
            or state.state.upstream_daily_slate_checksum != slate.slate.checksum
            or str(row["upstream_daily_slate_checksum"]) != slate.slate.checksum
            or str(row["upstream_game_state_checksum"]) != state.state.checksum
        ):
            raise BaseballIntelligenceIntegrityError(
                "BIA persisted upstream snapshot chain does not reconcile"
            )
        inventory = self.selector.load_candidates(
            requested_date=assembly.requested_date,
            canonical_player_ids=self.relevant_canonical_player_ids(state),
            connection=connection,
        )
        try:
            self._verify_relational_snapshot(
                connection,
                snapshot_id,
                assembly,
                slate,
                state,
                inventory,
                require_sealed=True,
            )
        except (
            BaseballIntelligenceArtifactIntegrityError,
            BaseballIntelligenceAttemptManifestError,
        ) as exc:
            raise BaseballIntelligenceIntegrityError(
                "BIA retained file evidence does not reconcile"
            ) from exc
        artifact = BaseballIntelligenceArtifactV1(
            str(row["artifact_relpath"]),
            str(row["artifact_checksum"]),
            int(row["artifact_byte_count"]),
        )
        return PersistedBaseballIntelligenceV1(snapshot_id,str(row["run_id"]),int(row["phase_attempt"]),str(row["upstream_daily_slate_snapshot_id"]),str(row["upstream_game_state_snapshot_id"]),assembly,artifact,_aware(row["created_at"],"created_at"),_aware(row["sealed_at"],"sealed_at"))

    def get_by_snapshot_id(self, snapshot_id: str) -> PersistedBaseballIntelligenceV1:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("snapshot_id must be non-empty")
        with self.database.connect() as connection:
            return self._verify_snapshot(connection, snapshot_id)

    def get_for_run_attempt(self, run_id: str, phase_attempt: int) -> PersistedBaseballIntelligenceV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT snapshot_id FROM baseball_intelligence_snapshots WHERE run_id=? AND phase_attempt=?", (safe_run,phase_attempt)).fetchone()
            if row is None:
                raise BaseballIntelligenceNotFoundError("BIA snapshot does not exist for run attempt")
            return self._verify_snapshot(connection, str(row["snapshot_id"]))

    def get_latest_for_run(self, run_id: str) -> PersistedBaseballIntelligenceV1 | None:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT snapshot_id FROM baseball_intelligence_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC,created_at DESC,snapshot_id DESC LIMIT 1", (safe_run,)).fetchone()
            return None if row is None else self._verify_snapshot(connection, str(row["snapshot_id"]))
