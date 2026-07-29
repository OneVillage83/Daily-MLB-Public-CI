from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.artifacts import UnsafeArtifactPath, resolve_contained_path, validate_artifact_relpath
from app.daily_slate.artifact import DailySlateArtifactV1, verify_daily_slate_artifact
from app.daily_slate.contracts import canonical_json_bytes
from app.daily_slate.repository import DailySlateRepository, PersistedDailySlateV1
from app.database import Database
from app.game_state.artifact import (
    GameStateArtifactV1,
    game_state_artifact_relpath,
    verify_game_state_artifact,
)
from app.game_state.contracts import (
    GamedayPersonnelV1,
    GameStateContractError,
    GameStateGameV1,
    GameStatePlayerV1,
    GameStateProvenanceV1,
    GameStateV1,
    LineupAvailability,
    LineupEntryV1,
    LineupStateV1,
    StarterCertainty,
    StarterStateV1,
    TeamGameStateV1,
)
from app.game_state.raw_link import GAME_STATE_RAW_LINK_CONTRACT, GameStateRawLinkOutcome
from app.identifiers import parse_requested_date, validate_run_id
from app.redaction import redact_text, redact_value


class GameStateRepositoryError(RuntimeError):
    """Base error for durable GameStateV1 evidence."""


class GameStateNotFoundError(GameStateRepositoryError):
    pass


class GameStatePersistenceConflict(GameStateRepositoryError):
    pass


class GameStateIntegrityError(GameStateRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class GameStateAttemptEvidenceV1:
    run_id: str
    phase_attempt: int
    requested_date: str
    upstream_daily_slate_checksum: str
    outcome: GameStateRawLinkOutcome
    normalized_snapshot_checksum: str | None
    raw_link_relpath: str
    raw_link_checksum: str
    raw_link_byte_count: int
    warnings: tuple[str, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PersistedGameStateV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    upstream_daily_slate_snapshot_id: str
    state: GameStateV1
    artifact: GameStateArtifactV1
    created_at: datetime
    sealed_at: datetime


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise GameStateIntegrityError(f"persisted {name} is not an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GameStateIntegrityError(f"persisted {name} is not an ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GameStateIntegrityError(f"persisted {name} is not timezone-aware")
    return parsed.astimezone(timezone.utc)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GameStateIntegrityError(f"persisted {name} is not an object")
    return value


def _player(payload: Mapping[str, object]) -> GameStatePlayerV1:
    return GameStatePlayerV1(
        source_player_id=str(payload["source_player_id"]),
        full_name=str(payload["full_name"]),
        player_identity_id=None if payload.get("player_identity_id") is None else str(payload["player_identity_id"]),
        canonical_player_id=None if payload.get("canonical_player_id") is None else str(payload["canonical_player_id"]),
        source_position_code=None if payload.get("source_position_code") is None else str(payload["source_position_code"]),
        source_position_name=None if payload.get("source_position_name") is None else str(payload["source_position_name"]),
        source_status_code=None if payload.get("source_status_code") is None else str(payload["source_status_code"]),
        source_status_description=None if payload.get("source_status_description") is None else str(payload["source_status_description"]),
    )


def _starter(payload: Mapping[str, object]) -> StarterStateV1:
    raw_player = payload.get("player")
    return StarterStateV1(
        certainty=StarterCertainty(str(payload["certainty"])),
        player=None if raw_player is None else _player(_mapping(raw_player, "starter player")),
        source_designation=None if payload.get("source_designation") is None else str(payload["source_designation"]),
    )


def _lineup(payload: Mapping[str, object]) -> LineupStateV1:
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise GameStateIntegrityError("persisted lineup entries are not a list")
    entries: list[LineupEntryV1] = []
    for value in raw_entries:
        entry = _mapping(value, "lineup entry")
        slot = entry["batting_order_slot"]
        if isinstance(slot, bool) or not isinstance(slot, int):
            raise GameStateIntegrityError("persisted batting-order slot is not an integer")
        entries.append(
            LineupEntryV1(
                player=_player(_mapping(entry["player"], "lineup player")),
                batting_order_slot=slot,
            )
        )
    return LineupStateV1(
        availability=LineupAvailability(str(payload["availability"])),
        entries=tuple(entries),
    )


def _personnel(payload: Mapping[str, object]) -> GamedayPersonnelV1:
    def bucket(name: str) -> tuple[GameStatePlayerV1, ...]:
        values = payload.get(name)
        if not isinstance(values, list):
            raise GameStateIntegrityError(f"persisted personnel {name} is not a list")
        return tuple(_player(_mapping(value, f"personnel {name}")) for value in values)

    return GamedayPersonnelV1(
        available=bool(payload["available"]),
        batters=bucket("batters"),
        pitchers=bucket("pitchers"),
        bench=bucket("bench"),
        bullpen=bucket("bullpen"),
    )


def _team(payload: Mapping[str, object]) -> TeamGameStateV1:
    return TeamGameStateV1(
        team_id=str(payload["team_id"]),
        source_team_id=str(payload["source_team_id"]),
        starter=_starter(_mapping(payload["starter"], "starter")),
        lineup=_lineup(_mapping(payload["lineup"], "lineup")),
        personnel=_personnel(_mapping(payload["personnel"], "personnel")),
    )


def _provenance(payload: Mapping[str, object]) -> GameStateProvenanceV1:
    return GameStateProvenanceV1(
        source_provider=str(payload["source_provider"]),
        source_record_id=None if payload.get("source_record_id") is None else str(payload["source_record_id"]),
        observed_at=_aware(payload["observed_at"], "provenance observed_at"),
        source_version=None if payload.get("source_version") is None else str(payload["source_version"]),
        raw_status=None if payload.get("raw_status") is None else str(payload["raw_status"]),
        upstream_checksum=None if payload.get("upstream_checksum") is None else str(payload["upstream_checksum"]),
        normalization_version=str(payload["normalization_version"]),
    )


def _game(payload: Mapping[str, object]) -> GameStateGameV1:
    from app.daily_slate.contracts import DailySlateGameStatus

    return GameStateGameV1(
        edge_event_id=str(payload["edge_event_id"]),
        daily_mlb_game_id=str(payload["daily_mlb_game_id"]),
        source_game_id=str(payload["source_game_id"]),
        away_team_id=str(payload["away_team_id"]),
        home_team_id=str(payload["home_team_id"]),
        game_status=DailySlateGameStatus(str(payload["game_status"])),
        away=_team(_mapping(payload["away"], "away team")),
        home=_team(_mapping(payload["home"], "home team")),
        observed_at=_aware(payload["observed_at"], "game observed_at"),
        provenance=_provenance(_mapping(payload["provenance"], "game provenance")),
    )


def _state(payload: Mapping[str, object]) -> GameStateV1:
    games = payload.get("games")
    if not isinstance(games, list):
        raise GameStateIntegrityError("persisted GameState games are not a list")
    return GameStateV1(
        requested_date=str(payload["requested_date"]),
        as_of_time=_aware(payload["as_of_time"], "as_of_time"),
        observed_at=_aware(payload["observed_at"], "observed_at"),
        source_authority=str(payload["source_authority"]),
        source_version=None if payload.get("source_version") is None else str(payload["source_version"]),
        upstream_daily_slate_checksum=str(payload["upstream_daily_slate_checksum"]),
        games=tuple(_game(_mapping(game, "GameState game")) for game in games),
        provenance=_provenance(_mapping(payload["provenance"], "snapshot provenance")),
        contract_version=str(payload["contract_version"]),
        sport=str(payload["sport"]),
        league=str(payload["league"]),
    )


class GameStateRepository:
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

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise GameStateRepositoryError("repository clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    @staticmethod
    def _snapshot_id(run_id: str, phase_attempt: int, checksum: str) -> str:
        payload = canonical_json_bytes({"phase_attempt": phase_attempt, "run_id": run_id, "snapshot_checksum": checksum})
        return f"state:{hashlib.sha256(payload).hexdigest()}"

    def _warnings(self, warnings: Iterable[object]) -> tuple[str, ...]:
        safe = tuple(sorted({redact_text(str(item), self.secret_values).strip() for item in warnings if str(item).strip()}))
        if any(not value for value in safe):
            raise GameStateRepositoryError("warnings must be non-empty after redaction")
        return safe

    def _active_phase(self, connection: sqlite3.Connection, run_id: str, attempt: int, requested_date: str) -> None:
        row = connection.execute(
            """SELECT run.requested_date, phase.status, phase.attempt_count
               FROM pipeline_runs AS run JOIN pipeline_run_phases AS phase
                 ON phase.run_id=run.run_id AND phase.phase_key='game_state'
               WHERE run.run_id=?""", (run_id,)
        ).fetchone()
        if row is None:
            raise GameStateNotFoundError("pipeline run or GAME_STATE phase does not exist")
        if str(row["requested_date"]) != requested_date or str(row["status"]) != "running" or int(row["attempt_count"]) != attempt:
            raise GameStatePersistenceConflict("GameState persistence requires matching active phase attempt")

    def _latest_slate(self, run_id: str) -> PersistedDailySlateV1:
        slate = self.daily_slate.get_latest_daily_slate_for_run(run_id)
        if slate is None:
            raise GameStateNotFoundError("sealed DailySlate snapshot does not exist")
        if slate.artifact_relpath is None or slate.artifact_checksum is None:
            raise GameStateIntegrityError("DailySlate artifact metadata is incomplete")
        verify_daily_slate_artifact(
            slate.slate,
            DailySlateArtifactV1(slate.artifact_relpath, slate.artifact_checksum, len(slate.slate.canonical_json_bytes())),
            self.artifact_root,
        )
        return slate

    def _raw_link(self, *, run_id: str, phase_attempt: int, requested_date: str, upstream_checksum: str, outcome: GameStateRawLinkOutcome, normalized_checksum: str | None, raw_link_relpath: str) -> tuple[str, int, str]:
        safe_relpath = validate_artifact_relpath(raw_link_relpath)
        try:
            path = resolve_contained_path(self.artifact_root, safe_relpath)
            content = path.read_bytes()
        except (FileNotFoundError, UnsafeArtifactPath) as exc:
            raise GameStateIntegrityError("GameState raw-link manifest is missing or unsafe") from exc
        checksum = hashlib.sha256(content).hexdigest()
        try:
            manifest = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GameStateIntegrityError("GameState raw-link manifest is not canonical JSON") from exc
        if not isinstance(manifest, Mapping) or manifest.get("contract_version") != GAME_STATE_RAW_LINK_CONTRACT:
            raise GameStateIntegrityError("GameState raw-link manifest contract is invalid")
        if json.dumps(manifest, ensure_ascii=True, allow_nan=False, separators=(",", ":"), sort_keys=True).encode("utf-8") != content:
            raise GameStateIntegrityError("GameState raw-link manifest bytes are not canonical")
        expected = {
            "run_id": run_id,
            "phase_attempt": phase_attempt,
            "requested_date": requested_date,
            "upstream_daily_slate_checksum": upstream_checksum,
            "outcome": outcome.value,
            "game_state_checksum": normalized_checksum,
        }
        if any(manifest.get(key) != value for key, value in expected.items()):
            raise GameStateIntegrityError("GameState raw-link manifest identity does not reconcile")
        if redact_value(manifest, self.secret_values) != manifest:
            raise GameStateIntegrityError("GameState raw-link manifest contains credential-bearing material")
        return checksum, len(content), safe_relpath

    def persist_attempt_evidence(self, *, run_id: str, phase_attempt: int, requested_date: str, upstream_daily_slate_checksum: str, outcome: GameStateRawLinkOutcome | str, raw_link_relpath: str, normalized_snapshot_checksum: str | None = None, warnings: Iterable[object] = ()) -> GameStateAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        date_text = parse_requested_date(requested_date).isoformat()
        if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
            raise ValueError("phase_attempt must be a positive integer")
        try:
            selected_outcome = GameStateRawLinkOutcome(outcome)
        except ValueError as exc:
            raise ValueError("unsupported GameState attempt outcome") from exc
        checksum = str(upstream_daily_slate_checksum)
        if len(checksum) != 64 or any(char not in "0123456789abcdef" for char in checksum):
            raise ValueError("upstream_daily_slate_checksum must be lowercase SHA-256")
        if selected_outcome is GameStateRawLinkOutcome.NORMALIZED:
            if normalized_snapshot_checksum is None or len(normalized_snapshot_checksum) != 64 or any(char not in "0123456789abcdef" for char in normalized_snapshot_checksum):
                raise ValueError("normalized attempt requires normalized_snapshot_checksum")
        elif normalized_snapshot_checksum is not None:
            raise ValueError("failed attempt must not include normalized_snapshot_checksum")
        raw_checksum, raw_size, safe_relpath = self._raw_link(run_id=safe_run, phase_attempt=phase_attempt, requested_date=date_text, upstream_checksum=checksum, outcome=selected_outcome, normalized_checksum=normalized_snapshot_checksum, raw_link_relpath=raw_link_relpath)
        safe_warnings = self._warnings(warnings)
        created_at = self._now()
        try:
            with self.database.connect(write=True) as connection:
                self._active_phase(connection, safe_run, phase_attempt, date_text)
                slate = self._latest_slate(safe_run)
                if slate.slate.requested_date != date_text or slate.slate.checksum != checksum:
                    raise GameStatePersistenceConflict("attempt evidence does not match sealed DailySlate")
                connection.execute("""INSERT INTO game_state_attempt_evidence(
                    run_id, phase_key, phase_attempt, requested_date, upstream_daily_slate_checksum,
                    outcome, normalized_snapshot_checksum, raw_link_relpath, raw_link_checksum,
                    raw_link_byte_count, warnings_json, warning_count, created_at)
                    VALUES (?, 'game_state', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (safe_run, phase_attempt, date_text, checksum, selected_outcome.value,
                     normalized_snapshot_checksum, safe_relpath, raw_checksum, raw_size,
                     json.dumps(safe_warnings, ensure_ascii=False, separators=(",", ":")), len(safe_warnings), created_at.isoformat()))
        except sqlite3.IntegrityError as exc:
            raise GameStatePersistenceConflict("GameState attempt evidence conflicts with immutable evidence") from exc
        return self.get_attempt_evidence(safe_run, phase_attempt)

    def get_attempt_evidence(self, run_id: str, phase_attempt: int) -> GameStateAttemptEvidenceV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT * FROM game_state_attempt_evidence WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
        if row is None:
            raise GameStateNotFoundError("GameState attempt evidence does not exist")
        raw_warnings = json.loads(str(row["warnings_json"]))
        if not isinstance(raw_warnings, list) or len(raw_warnings) != int(row["warning_count"]) or any(not isinstance(item, str) for item in raw_warnings):
            raise GameStateIntegrityError("GameState attempt warnings are invalid")
        outcome = GameStateRawLinkOutcome(str(row["outcome"]))
        checksum, size, relpath = self._raw_link(run_id=safe_run, phase_attempt=int(row["phase_attempt"]), requested_date=str(row["requested_date"]), upstream_checksum=str(row["upstream_daily_slate_checksum"]), outcome=outcome, normalized_checksum=None if row["normalized_snapshot_checksum"] is None else str(row["normalized_snapshot_checksum"]), raw_link_relpath=str(row["raw_link_relpath"]))
        if checksum != str(row["raw_link_checksum"]) or size != int(row["raw_link_byte_count"]):
            raise GameStateIntegrityError("GameState raw-link metadata does not match retained bytes")
        return GameStateAttemptEvidenceV1(safe_run, int(row["phase_attempt"]), str(row["requested_date"]), str(row["upstream_daily_slate_checksum"]), outcome, None if row["normalized_snapshot_checksum"] is None else str(row["normalized_snapshot_checksum"]), relpath, checksum, size, tuple(raw_warnings), _aware(row["created_at"], "attempt created_at"))

    def list_attempt_evidence(self, run_id: str) -> tuple[GameStateAttemptEvidenceV1, ...]:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute("SELECT phase_attempt FROM game_state_attempt_evidence WHERE run_id=? ORDER BY phase_attempt", (safe_run,)).fetchall()
        return tuple(self.get_attempt_evidence(safe_run, int(row["phase_attempt"])) for row in rows)

    def _validate_players(self, connection: sqlite3.Connection, state: GameStateV1) -> None:
        for game in state.games:
            players: list[GameStatePlayerV1] = []
            for team in (game.away, game.home):
                if team.starter.player is not None:
                    players.append(team.starter.player)
                players.extend(entry.player for entry in team.lineup.entries)
                for bucket in (team.personnel.batters, team.personnel.pitchers, team.personnel.bench, team.personnel.bullpen):
                    players.extend(bucket)
            for player in players:
                if not player.resolved:
                    continue
                row = connection.execute("""SELECT 1 FROM stats_player_identities AS identity
                    JOIN stats_player_identifier_mappings AS mapping ON mapping.player_identity_id=identity.player_identity_id
                    WHERE identity.player_identity_id=? AND identity.provider=? AND identity.provider_player_id=?
                      AND mapping.canonical_player_id=? AND mapping.verification_status='verified' LIMIT 1""",
                    (player.player_identity_id, state.source_authority, player.source_player_id, player.canonical_player_id)).fetchone()
                if row is None:
                    raise GameStateIntegrityError("resolved GameState player lacks verified existing mapping")

    def persist_game_state(self, *, run_id: str, phase_attempt: int, state: GameStateV1, artifact: GameStateArtifactV1) -> PersistedGameStateV1:
        safe_run = validate_run_id(run_id)
        if isinstance(phase_attempt, bool) or not isinstance(phase_attempt, int) or phase_attempt < 1:
            raise ValueError("phase_attempt must be a positive integer")
        if redact_value(state.as_dict(), self.secret_values) != state.as_dict():
            raise GameStateContractError("GameStateV1 contains credential-bearing material")
        slate = self._latest_slate(safe_run)
        if state.requested_date != slate.slate.requested_date or state.as_of_time != slate.slate.as_of_time or state.upstream_daily_slate_checksum != slate.slate.checksum:
            raise GameStatePersistenceConflict("GameState does not match its selected DailySlate lineage")
        expected_games = slate.slate.games
        if len(state.games) != len(expected_games):
            raise GameStatePersistenceConflict("GameState game set does not match DailySlate")
        for state_game, slate_game in zip(state.games, expected_games, strict=True):
            if (state_game.edge_event_id, state_game.daily_mlb_game_id, state_game.source_game_id, state_game.away_team_id, state_game.home_team_id) != (slate_game.edge_event_id, slate_game.daily_mlb_game_id, slate_game.source_game_id, slate_game.away_team_id, slate_game.home_team_id):
                raise GameStatePersistenceConflict("GameState game identities do not exactly match DailySlate ordering")
        expected_observed = slate.slate.observed_at if not state.games else max(game.observed_at for game in state.games)
        if state.observed_at != expected_observed:
            raise GameStatePersistenceConflict("GameState snapshot observed_at does not reconcile with game observations")
        if artifact.relpath != game_state_artifact_relpath(state):
            raise GameStateIntegrityError("GameState artifact path does not match semantic checksum")
        verify_game_state_artifact(state, artifact, self.artifact_root)
        snapshot_id = self._snapshot_id(safe_run, phase_attempt, state.checksum)
        now = self._now()
        try:
            with self.database.connect(write=True) as connection:
                self._active_phase(connection, safe_run, phase_attempt, state.requested_date)
                self._validate_players(connection, state)
                attempt = self.get_attempt_evidence(safe_run, phase_attempt)
                if attempt.outcome is not GameStateRawLinkOutcome.NORMALIZED or attempt.normalized_snapshot_checksum != state.checksum or attempt.upstream_daily_slate_checksum != slate.slate.checksum:
                    raise GameStatePersistenceConflict("normalized GameState attempt evidence does not reconcile")
                connection.execute("""INSERT INTO game_state_snapshots(
                    snapshot_id, run_id, phase_key, phase_attempt, requested_date, as_of_time, observed_at,
                    sport, league, source_authority, source_version, contract_version,
                    upstream_daily_slate_snapshot_id, upstream_daily_slate_checksum, snapshot_checksum,
                    artifact_relpath, artifact_checksum, artifact_byte_count, provenance_json, canonical_json,
                    game_count, sealed_at, created_at) VALUES (?, ?, 'game_state', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                    (snapshot_id, safe_run, phase_attempt, state.requested_date, state.as_of_time.isoformat(), state.observed_at.isoformat(), state.sport, state.league, state.source_authority, state.source_version, state.contract_version, slate.snapshot_id, state.upstream_daily_slate_checksum, state.checksum, artifact.relpath, artifact.checksum, artifact.byte_count, json.dumps(state.provenance.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True), state.canonical_json_bytes().decode("utf-8"), len(state.games), now.isoformat()))
                for ordinal, game in enumerate(state.games, start=1):
                    connection.execute("""INSERT INTO game_state_games(
                        snapshot_id, ordinal, edge_event_id, daily_mlb_game_id, source_game_id,
                        away_team_id, home_team_id, game_status, away_starter_certainty, home_starter_certainty,
                        away_lineup_availability, home_lineup_availability, observed_at, provenance_json,
                        canonical_json, row_checksum) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (snapshot_id, ordinal, game.edge_event_id, game.daily_mlb_game_id, game.source_game_id, game.away_team_id, game.home_team_id, game.game_status.value, game.away.starter.certainty.value, game.home.starter.certainty.value, game.away.lineup.availability.value, game.home.lineup.availability.value, game.observed_at.isoformat(), json.dumps(game.provenance.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True), json.dumps(game.as_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True), game.checksum))
                connection.execute("UPDATE game_state_snapshots SET sealed_at=? WHERE snapshot_id=?", (now.isoformat(), snapshot_id))
                self._verify_snapshot(connection, snapshot_id, verify_artifact=True)
        except sqlite3.IntegrityError as exc:
            raise GameStatePersistenceConflict("GameState snapshot conflicts with immutable evidence") from exc
        return self.get_game_state_snapshot(snapshot_id)

    def _verify_snapshot(self, connection: sqlite3.Connection, snapshot_id: str, *, verify_artifact: bool) -> PersistedGameStateV1:
        row = connection.execute("SELECT * FROM game_state_snapshots WHERE snapshot_id=?", (snapshot_id,)).fetchone()
        if row is None:
            raise GameStateNotFoundError("GameState snapshot does not exist")
        if row["sealed_at"] is None:
            raise GameStateIntegrityError("GameState snapshot is not sealed")
        payload = json.loads(str(row["canonical_json"]))
        state = _state(_mapping(payload, "GameState snapshot"))
        if state.checksum != str(row["snapshot_checksum"]) or state.canonical_json_bytes().decode("utf-8") != str(row["canonical_json"]):
            raise GameStateIntegrityError("GameState snapshot canonical evidence does not match checksum")
        if state.requested_date != str(row["requested_date"]) or state.as_of_time != _aware(row["as_of_time"], "as_of_time") or state.observed_at != _aware(row["observed_at"], "observed_at"):
            raise GameStateIntegrityError("GameState snapshot timestamp lineage does not reconcile")
        rows = connection.execute("SELECT ordinal, canonical_json, row_checksum FROM game_state_games WHERE snapshot_id=? ORDER BY ordinal", (snapshot_id,)).fetchall()
        if len(rows) != int(row["game_count"]) or len(state.games) != int(row["game_count"]):
            raise GameStateIntegrityError("GameState snapshot child count does not reconcile")
        for ordinal, child in enumerate(rows, start=1):
            if int(child["ordinal"]) != ordinal:
                raise GameStateIntegrityError("GameState game ordinals are not contiguous")
            game = _game(_mapping(json.loads(str(child["canonical_json"])), "GameState game"))
            if game.checksum != str(child["row_checksum"]) or game != state.games[ordinal - 1]:
                raise GameStateIntegrityError("GameState child canonical evidence does not reconcile")
        slate_row = connection.execute("SELECT snapshot_id, snapshot_checksum, game_count FROM daily_slate_snapshots WHERE snapshot_id=? AND sealed_at IS NOT NULL", (row["upstream_daily_slate_snapshot_id"],)).fetchone()
        if slate_row is None or str(slate_row["snapshot_checksum"]) != state.upstream_daily_slate_checksum or int(slate_row["game_count"]) != len(state.games):
            raise GameStateIntegrityError("GameState DailySlate lineage does not reconcile")
        attempt = connection.execute("SELECT outcome, normalized_snapshot_checksum FROM game_state_attempt_evidence WHERE run_id=? AND phase_attempt=?", (row["run_id"], row["phase_attempt"])).fetchone()
        if attempt is None or str(attempt["outcome"]) != "normalized" or str(attempt["normalized_snapshot_checksum"]) != state.checksum:
            raise GameStateIntegrityError("GameState attempt evidence does not reconcile")
        if row["artifact_relpath"] is None or row["artifact_checksum"] is None or row["artifact_byte_count"] is None:
            raise GameStateIntegrityError("GameState artifact metadata is incomplete")
        artifact = GameStateArtifactV1(str(row["artifact_relpath"]), str(row["artifact_checksum"]), int(row["artifact_byte_count"]))
        if verify_artifact:
            verify_game_state_artifact(state, artifact, self.artifact_root)
        return PersistedGameStateV1(str(row["snapshot_id"]), str(row["run_id"]), int(row["phase_attempt"]), str(row["upstream_daily_slate_snapshot_id"]), state, artifact, _aware(row["created_at"], "created_at"), _aware(row["sealed_at"], "sealed_at"))

    def get_game_state_snapshot(self, snapshot_id: str) -> PersistedGameStateV1:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")
        with self.database.connect() as connection:
            return self._verify_snapshot(connection, snapshot_id, verify_artifact=True)

    def get_game_state_for_run_attempt(self, run_id: str, phase_attempt: int) -> PersistedGameStateV1:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT snapshot_id FROM game_state_snapshots WHERE run_id=? AND phase_attempt=?", (safe_run, phase_attempt)).fetchone()
            if row is None:
                raise GameStateNotFoundError("GameState snapshot does not exist for run attempt")
            return self._verify_snapshot(connection, str(row["snapshot_id"]), verify_artifact=True)

    def get_latest_game_state_for_run(self, run_id: str) -> PersistedGameStateV1 | None:
        safe_run = validate_run_id(run_id)
        with self.database.connect() as connection:
            row = connection.execute("SELECT snapshot_id FROM game_state_snapshots WHERE run_id=? AND sealed_at IS NOT NULL ORDER BY phase_attempt DESC, created_at DESC, snapshot_id DESC LIMIT 1", (safe_run,)).fetchone()
            return None if row is None else self._verify_snapshot(connection, str(row["snapshot_id"]), verify_artifact=True)
