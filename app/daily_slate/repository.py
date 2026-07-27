from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from app.artifacts import validate_artifact_relpath
from app.daily_slate.artifact import (
    DailySlateArtifactV1,
    daily_slate_artifact_relpath,
)
from app.daily_slate.contracts import (
    DailySlateContractError,
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    ProbableStarterClassification,
    ProbableStarterV1,
    VenueMappingStatus,
    canonical_json_bytes,
)
from app.database import Database
from app.identifiers import validate_run_id
from app.redaction import redact_value


class DailySlateRepositoryError(RuntimeError):
    """Base error for durable DailySlateV1 persistence."""


class DailySlateNotFoundError(DailySlateRepositoryError):
    pass


class DailySlatePersistenceConflict(DailySlateRepositoryError):
    pass


@dataclass(frozen=True, slots=True)
class PersistedDailySlateV1:
    snapshot_id: str
    run_id: str
    phase_attempt: int
    slate: DailySlateV1
    artifact_relpath: str | None
    artifact_checksum: str | None
    created_at: datetime


def _parse_datetime(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise DailySlateRepositoryError(f"persisted {name} is not an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise DailySlateRepositoryError(
            f"persisted {name} is not an ISO datetime"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise DailySlateRepositoryError(f"persisted {name} is not timezone-aware")
    return parsed


def _optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else _parse_datetime(value, name)


def _provenance_from_dict(payload: Mapping[str, object]) -> DailySlateProvenanceV1:
    return DailySlateProvenanceV1(
        source_provider=str(payload["source_provider"]),
        source_record_id=(
            None
            if payload.get("source_record_id") is None
            else str(payload["source_record_id"])
        ),
        observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
        source_updated_at=_optional_datetime(
            payload.get("source_updated_at"), "source_updated_at"
        ),
        source_version=(
            None if payload.get("source_version") is None else str(payload["source_version"])
        ),
        raw_status=(
            None if payload.get("raw_status") is None else str(payload["raw_status"])
        ),
        upstream_checksum=(
            None
            if payload.get("upstream_checksum") is None
            else str(payload["upstream_checksum"])
        ),
        normalization_version=str(payload["normalization_version"]),
    )


def _starter_from_dict(payload: object) -> ProbableStarterV1 | None:
    if payload is None:
        return None
    if not isinstance(payload, Mapping):
        raise DailySlateRepositoryError("persisted probable starter is not an object")
    raw_provenance = payload.get("provenance")
    provenance = (
        None
        if raw_provenance is None
        else _provenance_from_dict(_mapping(raw_provenance, "starter provenance"))
    )
    return ProbableStarterV1(
        source_player_id=str(payload["source_player_id"]),
        full_name=str(payload["full_name"]),
        source_provider=str(payload["source_provider"]),
        observed_at=_parse_datetime(payload["observed_at"], "starter observed_at"),
        player_identity_id=(
            None
            if payload.get("player_identity_id") is None
            else str(payload["player_identity_id"])
        ),
        canonical_player_id=(
            None
            if payload.get("canonical_player_id") is None
            else str(payload["canonical_player_id"])
        ),
        classification=ProbableStarterClassification(str(payload["classification"])),
        provenance=provenance,
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise DailySlateRepositoryError(f"persisted {name} is not an object")
    return value


def _game_from_dict(payload: Mapping[str, object]) -> DailySlateGameV1:
    raw_game_number = payload.get("game_number")
    if raw_game_number is not None and (
        isinstance(raw_game_number, bool) or not isinstance(raw_game_number, int)
    ):
        raise DailySlateRepositoryError("persisted game_number is not an integer")
    return DailySlateGameV1(
        edge_event_id=str(payload["edge_event_id"]),
        daily_mlb_game_id=str(payload["daily_mlb_game_id"]),
        official_date=str(payload["official_date"]),
        scheduled_start_time=_optional_datetime(
            payload.get("scheduled_start_time"), "scheduled_start_time"
        ),
        away_team_id=str(payload["away_team_id"]),
        home_team_id=str(payload["home_team_id"]),
        venue_id=(
            None if payload.get("venue_id") is None else str(payload["venue_id"])
        ),
        venue_mapping_status=VenueMappingStatus(str(payload["venue_mapping_status"])),
        game_number=raw_game_number,
        doubleheader_status=DailySlateDoubleheaderStatus(
            str(payload["doubleheader_status"])
        ),
        game_status=DailySlateGameStatus(str(payload["game_status"])),
        source_game_id=str(payload["source_game_id"]),
        source_provider=str(payload["source_provider"]),
        observed_at=_parse_datetime(payload["observed_at"], "game observed_at"),
        provenance=_provenance_from_dict(
            _mapping(payload["provenance"], "game provenance")
        ),
        source_home_team_id=(
            None
            if payload.get("source_home_team_id") is None
            else str(payload["source_home_team_id"])
        ),
        source_away_team_id=(
            None
            if payload.get("source_away_team_id") is None
            else str(payload["source_away_team_id"])
        ),
        source_venue_id=(
            None
            if payload.get("source_venue_id") is None
            else str(payload["source_venue_id"])
        ),
        source_venue_name=(
            None
            if payload.get("source_venue_name") is None
            else str(payload["source_venue_name"])
        ),
        source_updated_at=_optional_datetime(
            payload.get("source_updated_at"), "game source_updated_at"
        ),
        away_probable_starter=_starter_from_dict(
            payload.get("away_probable_starter")
        ),
        home_probable_starter=_starter_from_dict(
            payload.get("home_probable_starter")
        ),
    )


def _slate_from_dict(payload: Mapping[str, object]) -> DailySlateV1:
    raw_games = payload.get("games")
    if not isinstance(raw_games, list):
        raise DailySlateRepositoryError("persisted DailySlateV1 games are not a list")
    return DailySlateV1(
        requested_date=str(payload["requested_date"]),
        as_of_time=_parse_datetime(payload["as_of_time"], "as_of_time"),
        observed_at=_parse_datetime(payload["observed_at"], "observed_at"),
        source_authority=str(payload["source_authority"]),
        source_version=(
            None
            if payload.get("source_version") is None
            else str(payload["source_version"])
        ),
        games=tuple(
            _game_from_dict(_mapping(game, "DailySlateV1 game")) for game in raw_games
        ),
        provenance=_provenance_from_dict(
            _mapping(payload["provenance"], "slate provenance")
        ),
        contract_version=str(payload["contract_version"]),
        sport=str(payload["sport"]),
        league=str(payload["league"]),
    )


class DailySlateRepository:
    def __init__(
        self,
        database: Database,
        *,
        secret_values: Iterable[str] = (),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.database = database
        self.secret_values = tuple(str(value) for value in secret_values if str(value))
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise DailySlateRepositoryError("repository clock must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _safe_payload(self, slate: DailySlateV1) -> dict[str, Any]:
        payload = slate.as_dict()
        if redact_value(payload, self.secret_values) != payload:
            raise DailySlateContractError(
                "DailySlateV1 contains credential-bearing material"
            )
        return payload

    @staticmethod
    def _snapshot_id(
        run_id: str, phase_attempt: int, snapshot_checksum: str
    ) -> str:
        identity = canonical_json_bytes(
            {
                "phase_attempt": phase_attempt,
                "run_id": run_id,
                "snapshot_checksum": snapshot_checksum,
            }
        )
        return f"slate:{hashlib.sha256(identity).hexdigest()}"

    @staticmethod
    def _validate_resolved_starter(
        connection: sqlite3.Connection,
        starter: ProbableStarterV1 | None,
    ) -> None:
        if starter is None or not starter.resolved:
            return
        row = connection.execute(
            """
            SELECT 1
            FROM stats_player_identities AS identity
            JOIN stats_player_identifier_mappings AS mapping
              ON mapping.player_identity_id=identity.player_identity_id
            WHERE identity.player_identity_id=?
              AND identity.provider=?
              AND identity.provider_player_id=?
              AND mapping.canonical_player_id=?
              AND mapping.verification_status='verified'
            LIMIT 1
            """,
            (
                starter.player_identity_id,
                starter.source_provider,
                starter.source_player_id,
                starter.canonical_player_id,
            ),
        ).fetchone()
        if row is None:
            raise DailySlateContractError(
                "probable starter canonical identity lacks a verified existing mapping"
            )

    def persist_daily_slate(
        self,
        *,
        run_id: str,
        phase_attempt: int,
        slate: DailySlateV1,
        artifact: DailySlateArtifactV1 | None = None,
    ) -> PersistedDailySlateV1:
        safe_run_id = validate_run_id(run_id)
        if isinstance(phase_attempt, bool) or phase_attempt < 1:
            raise ValueError("phase_attempt must be a positive integer")
        payload = self._safe_payload(slate)
        snapshot_checksum = slate.checksum
        snapshot_id = self._snapshot_id(
            safe_run_id, phase_attempt, snapshot_checksum
        )
        artifact_relpath = (
            None
            if artifact is None
            else validate_artifact_relpath(artifact.relpath)
        )
        artifact_checksum = None if artifact is None else artifact.checksum
        if artifact is not None:
            if artifact_relpath != daily_slate_artifact_relpath(slate):
                raise DailySlateContractError(
                    "artifact path does not match the canonical content-addressed path"
                )
            canonical_artifact_bytes = slate.canonical_json_bytes()
            expected_artifact_checksum = hashlib.sha256(
                canonical_artifact_bytes
            ).hexdigest()
            if (
                artifact.checksum != expected_artifact_checksum
                or artifact.byte_count != len(canonical_artifact_bytes)
            ):
                raise DailySlateContractError(
                    "artifact checksum or byte count does not match canonical DailySlateV1 bytes"
                )
        created_at = self._now()

        try:
            with self.database.connect(write=True) as connection:
                run = connection.execute(
                    "SELECT requested_date FROM pipeline_runs WHERE run_id=?",
                    (safe_run_id,),
                ).fetchone()
                if run is None:
                    raise DailySlateNotFoundError("pipeline run does not exist")
                if str(run["requested_date"]) != slate.requested_date:
                    raise DailySlateContractError(
                        "DailySlateV1 requested_date does not match its pipeline run"
                    )
                phase = connection.execute(
                    """
                    SELECT status, attempt_count
                    FROM pipeline_run_phases
                    WHERE run_id=? AND phase_key='daily_slate'
                    """,
                    (safe_run_id,),
                ).fetchone()
                if phase is None:
                    raise DailySlateNotFoundError(
                        "pipeline run has no DAILY_SLATE phase"
                    )
                if (
                    str(phase["status"]) != "running"
                    or int(phase["attempt_count"]) != phase_attempt
                ):
                    raise DailySlatePersistenceConflict(
                        "DailySlateV1 persistence requires the matching active phase attempt"
                    )
                for game in slate.games:
                    self._validate_resolved_starter(
                        connection, game.away_probable_starter
                    )
                    self._validate_resolved_starter(
                        connection, game.home_probable_starter
                    )
                connection.execute(
                    """
                    INSERT INTO daily_slate_snapshots(
                        snapshot_id, run_id, phase_key, phase_attempt,
                        requested_date, as_of_time, observed_at, sport, league,
                        source_authority, source_version, contract_version,
                        snapshot_checksum, artifact_relpath, artifact_checksum,
                        provenance_json, canonical_json, game_count, sealed_at,
                        created_at
                    ) VALUES (
                        ?, ?, 'daily_slate', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, NULL, ?
                    )
                    """,
                    (
                        snapshot_id,
                        safe_run_id,
                        phase_attempt,
                        slate.requested_date,
                        slate.as_of_time.isoformat(),
                        slate.observed_at.isoformat(),
                        slate.sport,
                        slate.league,
                        slate.source_authority,
                        slate.source_version,
                        slate.contract_version,
                        snapshot_checksum,
                        artifact_relpath,
                        artifact_checksum,
                        json.dumps(
                            slate.provenance.as_dict(),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                        len(slate.games),
                        created_at.isoformat(),
                    ),
                )
                for ordinal, game in enumerate(slate.games, start=1):
                    away = game.away_probable_starter
                    home = game.home_probable_starter
                    connection.execute(
                        """
                        INSERT INTO daily_slate_games(
                            snapshot_id, ordinal, edge_event_id,
                            daily_mlb_game_id, official_date,
                            scheduled_start_time, away_team_id, home_team_id,
                            venue_id, venue_mapping_status, game_number,
                            doubleheader_status, game_status, source_game_id,
                            source_provider, source_home_team_id,
                            source_away_team_id, source_venue_id,
                            source_venue_name, observed_at, source_updated_at,
                            away_probable_player_identity_id,
                            away_probable_canonical_player_id,
                            home_probable_player_identity_id,
                            home_probable_canonical_player_id,
                            provenance_json, canonical_json, row_checksum
                        ) VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?, ?, ?, ?, ?, ?
                        )
                        """,
                        (
                            snapshot_id,
                            ordinal,
                            game.edge_event_id,
                            game.daily_mlb_game_id,
                            game.official_date,
                            (
                                None
                                if game.scheduled_start_time is None
                                else game.scheduled_start_time.isoformat()
                            ),
                            game.away_team_id,
                            game.home_team_id,
                            game.venue_id,
                            game.venue_mapping_status.value,
                            game.game_number,
                            game.doubleheader_status.value,
                            game.game_status.value,
                            game.source_game_id,
                            game.source_provider,
                            game.source_home_team_id,
                            game.source_away_team_id,
                            game.source_venue_id,
                            game.source_venue_name,
                            game.observed_at.isoformat(),
                            (
                                None
                                if game.source_updated_at is None
                                else game.source_updated_at.isoformat()
                            ),
                            None if away is None else away.player_identity_id,
                            None if away is None else away.canonical_player_id,
                            None if home is None else home.player_identity_id,
                            None if home is None else home.canonical_player_id,
                            json.dumps(
                                game.provenance.as_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            json.dumps(
                                game.as_dict(),
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            ),
                            game.checksum,
                        ),
                    )
                connection.execute(
                    """
                    UPDATE daily_slate_snapshots
                    SET sealed_at=?
                    WHERE snapshot_id=?
                    """,
                    (created_at.isoformat(), snapshot_id),
                )
        except sqlite3.IntegrityError as exc:
            raise DailySlatePersistenceConflict(
                "DailySlateV1 snapshot conflicts with retained immutable evidence"
            ) from exc
        return self.get_daily_slate_snapshot(snapshot_id)

    def get_daily_slate_snapshot(self, snapshot_id: str) -> PersistedDailySlateV1:
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise ValueError("snapshot_id must be a non-empty string")
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM daily_slate_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise DailySlateNotFoundError("DailySlateV1 snapshot does not exist")
            if row["sealed_at"] is None:
                raise DailySlateRepositoryError(
                    "persisted DailySlateV1 snapshot is not sealed"
                )
            game_rows = connection.execute(
                """
                SELECT ordinal, canonical_json, row_checksum
                FROM daily_slate_games
                WHERE snapshot_id=?
                ORDER BY ordinal
                """,
                (snapshot_id,),
            ).fetchall()
        payload = json.loads(str(row["canonical_json"]))
        slate_payload = _mapping(payload, "DailySlateV1")
        payload_games = slate_payload.get("games")
        if not isinstance(payload_games, list):
            raise DailySlateRepositoryError(
                "persisted DailySlateV1 games are not a list"
            )
        expected_count = int(row["game_count"])
        if len(payload_games) != expected_count or len(game_rows) != expected_count:
            raise DailySlateRepositoryError(
                "persisted DailySlateV1 game count does not match canonical evidence"
            )
        stored_games: list[DailySlateGameV1] = []
        for expected_ordinal, game_row in enumerate(game_rows, start=1):
            if int(game_row["ordinal"]) != expected_ordinal:
                raise DailySlateRepositoryError(
                    "persisted DailySlateV1 game ordinals are not contiguous"
                )
            game_payload = json.loads(str(game_row["canonical_json"]))
            game = _game_from_dict(_mapping(game_payload, "DailySlateV1 game"))
            if game.checksum != str(game_row["row_checksum"]):
                raise DailySlateRepositoryError(
                    "persisted DailySlateV1 game checksum does not match canonical evidence"
                )
            stored_games.append(game)
        slate = _slate_from_dict(slate_payload)
        if tuple(stored_games) != slate.games:
            raise DailySlateRepositoryError(
                "persisted DailySlateV1 child evidence does not match snapshot evidence"
            )
        if slate.checksum != str(row["snapshot_checksum"]):
            raise DailySlateRepositoryError(
                "persisted DailySlateV1 checksum does not match canonical evidence"
            )
        return PersistedDailySlateV1(
            snapshot_id=str(row["snapshot_id"]),
            run_id=str(row["run_id"]),
            phase_attempt=int(row["phase_attempt"]),
            slate=slate,
            artifact_relpath=(
                None
                if row["artifact_relpath"] is None
                else str(row["artifact_relpath"])
            ),
            artifact_checksum=(
                None
                if row["artifact_checksum"] is None
                else str(row["artifact_checksum"])
            ),
            created_at=_parse_datetime(row["created_at"], "created_at"),
        )

    def get_daily_slate_games(self, snapshot_id: str) -> tuple[DailySlateGameV1, ...]:
        return self.get_daily_slate_snapshot(snapshot_id).slate.games

    def list_daily_slate_snapshots_for_run(
        self, run_id: str
    ) -> tuple[PersistedDailySlateV1, ...]:
        safe_run_id = validate_run_id(run_id)
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot_id
                FROM daily_slate_snapshots
                WHERE run_id=?
                ORDER BY phase_attempt, created_at, snapshot_id
                """,
                (safe_run_id,),
            ).fetchall()
        return tuple(
            self.get_daily_slate_snapshot(str(row["snapshot_id"])) for row in rows
        )

    def get_latest_daily_slate_for_run(
        self, run_id: str
    ) -> PersistedDailySlateV1 | None:
        snapshots = self.list_daily_slate_snapshots_for_run(run_id)
        return snapshots[-1] if snapshots else None

    def resolve_probable_starter(
        self,
        *,
        source_provider: str,
        source_player_id: str,
        full_name: str,
        observed_at: datetime,
        provenance: DailySlateProvenanceV1 | None = None,
    ) -> ProbableStarterV1:
        with self.database.connect() as connection:
            rows = connection.execute(
                """
                SELECT identity.player_identity_id, mapping.canonical_player_id
                FROM stats_player_identities AS identity
                JOIN stats_player_identifier_mappings AS mapping
                  ON mapping.player_identity_id=identity.player_identity_id
                WHERE identity.provider=?
                  AND identity.provider_player_id=?
                  AND mapping.verification_status='verified'
                ORDER BY mapping.observed_at DESC, mapping.canonical_player_id
                """,
                (source_provider, source_player_id),
            ).fetchall()
        unique = {
            (str(row["player_identity_id"]), str(row["canonical_player_id"]))
            for row in rows
        }
        if len(unique) > 1:
            raise DailySlateContractError(
                "source probable starter identity maps to conflicting canonical players"
            )
        player_identity_id: str | None = None
        canonical_player_id: str | None = None
        if unique:
            player_identity_id, canonical_player_id = next(iter(unique))
        return ProbableStarterV1(
            source_player_id=source_player_id,
            full_name=full_name,
            source_provider=source_provider,
            observed_at=observed_at,
            player_identity_id=player_identity_id,
            canonical_player_id=canonical_player_id,
            provenance=provenance,
        )
