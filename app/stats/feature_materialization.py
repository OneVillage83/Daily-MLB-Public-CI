from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from app.database import Database
from app.identifiers import generate_run_id
from app.run_state import FailureStage, RunStatus
from app.stats.completeness import CompletenessResult
from app.stats.features import (
    FEATURE_VERSION_V3,
    BattingAggregateLine,
    PitchingAggregateLine,
    StatcastBattedBall,
    StatcastPitchMetric,
    StatcastPitcherPitch,
    StatcastSwingMetric,
    build_aggregate_player_feature_payloads_v3,
    derive_statcast_plate_appearances,
    derive_statcast_pitcher_appearances,
)
from app.stats.identities import resolve_baseball_reference_aggregate_team
from app.stats.normalization import (
    optional_float,
    optional_int,
    statcast_plate_appearance_classification,
)
from app.stats.repository import StatsRepository


FEATURE_MATERIALIZATION_VERSION = "DSE_MLB_STATS_FEATURE_MATERIALIZATION_V1"
FEATURE_SOURCE_PROVIDER = "current_mlb_stats"
FEATURE_DERIVED_PROVIDER = "derived_mlb_features"
_SOURCE_TERMINAL_STATUSES = frozenset({"completed", "completed_with_warnings"})


class FeatureMaterializationError(RuntimeError):
    """Fail-closed feature-materialization error."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FeatureMaterializationError("feature timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _date_at_utc(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=timezone.utc)


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _checksum(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _inning_half(value: object) -> str | None:
    normalized = str(value or "").strip().casefold()
    if normalized == "top":
        return "top"
    if normalized in {"bot", "bottom"}:
        return "bottom"
    return None


def _count(values: Mapping[str, object], key: str) -> int:
    value = optional_int(values.get(key))
    if value is None or value < 0:
        raise FeatureMaterializationError(
            f"retained aggregate {key!r} is missing or invalid"
        )
    return value


@dataclass(frozen=True, slots=True)
class FeatureMaterializationRequest:
    feature_as_of: date
    source_stats_run_id: str
    dry_run: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.feature_as_of, date) or isinstance(
            self.feature_as_of, datetime
        ):
            raise TypeError("feature_as_of must be a calendar date")
        source = self.source_stats_run_id.strip()
        suffix = source.removeprefix("stats_")
        if (
            not source.startswith("stats_")
            or len(suffix) != 32
            or any(ch not in "0123456789abcdef" for ch in suffix)
        ):
            raise FeatureMaterializationError(
                "source_stats_run_id must identify a service-generated stats run"
            )


@dataclass(frozen=True, slots=True)
class FeatureMaterializationResult:
    feature_as_of: date
    source_stats_run_id: str
    source_observed_at: datetime
    completeness: CompletenessResult
    status: str
    counts: Mapping[str, int]
    run_id: str | None = None
    stats_run_id: str | None = None
    report_path: Path | None = None
    dry_run: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "materialization_version": FEATURE_MATERIALIZATION_VERSION,
            "feature_version": FEATURE_VERSION_V3,
            "feature_as_of": self.feature_as_of.isoformat(),
            "source_stats_run_id": self.source_stats_run_id,
            "source_observed_at": self.source_observed_at.isoformat(),
            "run_id": self.run_id,
            "stats_run_id": self.stats_run_id,
            "status": self.status,
            "counts": dict(sorted(self.counts.items())),
            "completeness": {
                key: value.isoformat() if isinstance(value, date) else value
                for key, value in asdict(self.completeness).items()
            },
            "network_requests": 0,
            "statcast_revision_writes": 0,
            "report_path": str(self.report_path) if self.report_path else None,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True, slots=True)
class _StatcastInputs:
    batted_balls: tuple[StatcastBattedBall, ...]
    pitch_metrics: tuple[StatcastPitchMetric, ...]
    pitcher_pitches: tuple[StatcastPitcherPitch, ...]
    swing_metrics: tuple[StatcastSwingMetric, ...]
    source_checksums: tuple[str, ...]
    revision_distribution: Mapping[int, int]
    v3_observations: Mapping[str, int]


class FeatureMaterializationService:
    """Backfill V3 player features from already-retained database evidence only.

    The service deliberately performs no provider requests, raw capture writes,
    schedule replay, Statcast revision writes, or completeness-watermark updates.
    """

    def __init__(
        self,
        *,
        database: Database,
        report_path: Path,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.database = database
        self.repository = StatsRepository(database)
        self.report_path = Path(report_path)
        self.clock = clock

    def materialize(
        self, request: FeatureMaterializationRequest
    ) -> FeatureMaterializationResult:
        source = self._source_run(request)
        observed_at = _aware(
            datetime.fromisoformat(str(source["source_observed_at"]))
        )
        completeness = self._completeness(source, request.feature_as_of)
        self._validate_reconciliation(request.source_stats_run_id)
        self._validate_aggregate_checkpoint(request)

        if request.dry_run:
            return FeatureMaterializationResult(
                feature_as_of=request.feature_as_of,
                source_stats_run_id=request.source_stats_run_id,
                source_observed_at=observed_at,
                completeness=completeness,
                status="dry_run",
                counts={"network_requests": 0, "persistent_writes": 0},
                dry_run=True,
            )

        run_id, stats_run_id = self._begin(request, observed_at)
        try:
            batting, pitching = self._aggregate_inputs(request)
            statcast = self._statcast_inputs(request.feature_as_of, observed_at)
            pitcher_appearances = derive_statcast_pitcher_appearances(
                statcast.pitcher_pitches
            )
            plate_appearances = derive_statcast_plate_appearances(
                statcast.pitcher_pitches
            )
            source_checksums = tuple(
                sorted(
                    set(self._raw_checksums(request.source_stats_run_id))
                    | set(statcast.source_checksums)
                )
            )
            players = build_aggregate_player_feature_payloads_v3(
                feature_as_of=request.feature_as_of,
                knowledge_cutoff=observed_at,
                batting_aggregates=batting,
                pitching_aggregates=pitching,
                batted_balls=statcast.batted_balls,
                pitch_metrics=statcast.pitch_metrics,
                swing_metrics=statcast.swing_metrics,
                pitcher_appearances=pitcher_appearances,
                statcast_plate_appearances=plate_appearances,
            )
            if not players or any(
                str(payload.get("contract_version")) != FEATURE_VERSION_V3
                for payload in players.values()
            ):
                raise FeatureMaterializationError(
                    "V3 feature builder did not produce a valid player inventory"
                )

            state = "complete" if completeness.partial_date is None else "degraded"
            records: list[dict[str, object]] = []
            for player_id, payload in sorted(players.items()):
                records.append(
                    {
                        "feature_snapshot_id": f"feature_{uuid4().hex}",
                        "stats_run_id": stats_run_id,
                        "feature_version": FEATURE_VERSION_V3,
                        "entity_kind": "player",
                        "canonical_player_id": player_id,
                        "feature_as_of": _date_at_utc(request.feature_as_of),
                        "completeness_state": state,
                        "input_checksum": _checksum(
                            {
                                "entity": player_id,
                                "sources": list(source_checksums),
                            }
                        ),
                        "feature_checksum": str(payload["feature_checksum"]),
                        "features": payload,
                        "created_at": observed_at,
                    }
                )

            revisions_before = self._scalar(
                "SELECT COUNT(*) FROM statcast_pitch_revisions"
            )
            v3_before = self._v3_count(request.feature_as_of)
            results = self.repository.record_feature_snapshots(records)
            revisions_after = self._scalar(
                "SELECT COUNT(*) FROM statcast_pitch_revisions"
            )
            if revisions_after != revisions_before:
                raise FeatureMaterializationError(
                    "feature-only materialization changed Statcast revisions"
                )
            v3_after = self._v3_count(request.feature_as_of)
            inserted = sum(
                str(row["stats_run_id"]) == stats_run_id for row in results
            )

            counts = Counter(
                {
                    "network_requests": 0,
                    "statcast_revision_writes": 0,
                    "statcast_revisions_before": revisions_before,
                    "statcast_revisions_after": revisions_after,
                    "batting_aggregate_rows": len(batting),
                    "pitching_aggregate_rows": len(pitching),
                    "statcast_batted_balls": len(statcast.batted_balls),
                    "statcast_pitch_metrics": len(statcast.pitch_metrics),
                    "statcast_pitcher_pitches": len(statcast.pitcher_pitches),
                    "statcast_swing_metrics": len(statcast.swing_metrics),
                    "player_v3_payloads": len(players),
                    "player_v3_inserted_for_run": inserted,
                    "player_v3_reused_existing": len(results) - inserted,
                    "player_v3_global_before": v3_before,
                    "player_v3_global_after": v3_after,
                }
            )
            for revision, count in statcast.revision_distribution.items():
                counts[f"selected_statcast_revision_{revision}"] = count
            for field, count in statcast.v3_observations.items():
                counts[f"v3_observations_{field}"] = count

            self._record_reconciliation(
                stats_run_id,
                request,
                completeness,
                observed_at,
                counts,
            )
            status = (
                "completed_with_warnings"
                if completeness.partial_date is not None
                else "completed"
            )
            self.repository.transition_ingestion_run(
                stats_run_id,
                status,
                transitioned_at=_aware(self.clock()),
                source_observed_at=observed_at,
                latest_ingested_completed_game_date=(
                    completeness.latest_ingested_completed_game_date
                ),
                contiguous_regular_season_complete_through_date=(
                    completeness.contiguous_regular_season_complete_through_date
                ),
                partial_date=completeness.partial_date,
                partial_date_reason=completeness.partial_date_reason,
            )
            self.database.transition_run(
                run_id,
                RunStatus.COMPLETED_WITH_WARNINGS
                if status == "completed_with_warnings"
                else RunStatus.COMPLETED,
                transitioned_at=_aware(self.clock()).isoformat(),
            )
            return FeatureMaterializationResult(
                feature_as_of=request.feature_as_of,
                source_stats_run_id=request.source_stats_run_id,
                source_observed_at=observed_at,
                completeness=completeness,
                status=status,
                counts=dict(counts),
                run_id=run_id,
                stats_run_id=stats_run_id,
                report_path=self.report_path,
            )
        except Exception as exc:
            self._fail(run_id, stats_run_id, exc)
            if isinstance(exc, FeatureMaterializationError):
                raise
            raise FeatureMaterializationError(str(exc)) from exc

    def _source_run(
        self, request: FeatureMaterializationRequest
    ) -> Mapping[str, Any]:
        source = self.repository.get_ingestion_run(request.source_stats_run_id)
        if (
            source is None
            or str(source["provider"]) != FEATURE_SOURCE_PROVIDER
            or str(source["status"]) not in _SOURCE_TERMINAL_STATUSES
        ):
            raise FeatureMaterializationError(
                "source stats run is not accepted terminal current-statistics evidence"
            )
        if (
            str(source["requested_through_date"]) != request.feature_as_of.isoformat()
            or not source.get("source_observed_at")
        ):
            raise FeatureMaterializationError(
                "source stats run cutoff does not match feature_as_of"
            )
        return source

    @staticmethod
    def _completeness(
        source: Mapping[str, Any], feature_as_of: date
    ) -> CompletenessResult:
        def parsed(field: str) -> date | None:
            value = source.get(field)
            result = date.fromisoformat(str(value)) if value else None
            if result is not None and result > feature_as_of:
                raise FeatureMaterializationError(
                    f"source {field} exceeds feature_as_of"
                )
            return result

        partial_date = parsed("partial_date")
        partial_reason = (
            str(source["partial_date_reason"])
            if source.get("partial_date_reason")
            else None
        )
        if (partial_date is None) != (partial_reason is None):
            raise FeatureMaterializationError(
                "source partial-date evidence is inconsistent"
            )
        return CompletenessResult(
            requested_through_date=feature_as_of,
            contiguous_regular_season_complete_through_date=parsed(
                "contiguous_regular_season_complete_through_date"
            ),
            latest_ingested_completed_game_date=parsed(
                "latest_ingested_completed_game_date"
            ),
            partial_date=partial_date,
            partial_date_reason=partial_reason,
        )

    def _validate_reconciliation(self, source_id: str) -> None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT status,details_json FROM stats_reconciliations "
                "WHERE stats_run_id=? AND dataset_key='regular_season_games' "
                "ORDER BY completed_at DESC,reconciliation_id DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        if row is None or str(row["status"]) not in {"passed", "warnings"}:
            raise FeatureMaterializationError(
                "source run lacks accepted regular-season reconciliation"
            )
        try:
            details = json.loads(str(row["details_json"]))
        except json.JSONDecodeError as exc:
            raise FeatureMaterializationError(
                "source reconciliation details are malformed"
            ) from exc
        if not bool(details.get("completeness_watermark_eligible", False)):
            raise FeatureMaterializationError(
                "source reconciliation is not completeness-eligible"
            )

    def _validate_aggregate_checkpoint(
        self, request: FeatureMaterializationRequest
    ) -> None:
        checkpoint = self.repository.get_checkpoint(
            request.source_stats_run_id,
            "baseball_reference_batting_pitching",
        )
        if (
            checkpoint is None
            or str(checkpoint["status"]) != "completed"
            or int(checkpoint["records_rejected"]) != 0
        ):
            raise FeatureMaterializationError(
                "source run lacks clean completed Baseball-Reference aggregates"
            )
        try:
            cursor = json.loads(str(checkpoint["cursor_after_json"] or "{}"))
            valid = (
                cursor["contract"] == "DSE_BREF_AGGREGATE_CHECKPOINT_V2"
                and date.fromisoformat(str(cursor["feature_as_of"]))
                == request.feature_as_of
                and date.fromisoformat(str(cursor["feature_through_date"]))
                == request.feature_as_of - timedelta(days=1)
                and int(cursor["actual_capture_count"])
                == int(cursor["expected_capture_count"])
                and int(cursor["actual_capture_count"]) > 0
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FeatureMaterializationError(
                "source aggregate checkpoint is malformed"
            ) from exc
        if not valid:
            raise FeatureMaterializationError(
                "source aggregate checkpoint changed its D-1 evidence contract"
            )

    def _begin(
        self,
        request: FeatureMaterializationRequest,
        observed_at: datetime,
    ) -> tuple[str, str]:
        now = _aware(self.clock())
        run_id = generate_run_id(request.feature_as_of)
        stats_run_id = f"stats_{uuid4().hex}"
        self.database.create_run(run_id, request.feature_as_of)
        self.database.transition_run(
            run_id,
            RunStatus.RUNNING,
            transitioned_at=now.isoformat(),
        )
        self.repository.create_ingestion_run(
            stats_run_id=stats_run_id,
            run_id=run_id,
            provider=FEATURE_DERIVED_PROVIDER,
            scope_key=f"feature-materialization:{request.feature_as_of.year}",
            requested_through_date=request.feature_as_of,
            configuration_checksum=_checksum(
                {
                    "version": FEATURE_MATERIALIZATION_VERSION,
                    "feature_as_of": request.feature_as_of,
                    "source": request.source_stats_run_id,
                    "observed_at": observed_at,
                }
            ),
            source_version=(
                f"{FEATURE_MATERIALIZATION_VERSION}:"
                f"source={request.source_stats_run_id}"
            ),
            adapter_version=FEATURE_MATERIALIZATION_VERSION,
            created_at=now,
        )
        self.repository.transition_ingestion_run(
            stats_run_id,
            "running",
            transitioned_at=now,
        )
        return run_id, stats_run_id

    def _aggregate_inputs(
        self, request: FeatureMaterializationRequest
    ) -> tuple[tuple[BattingAggregateLine, ...], tuple[PitchingAggregateLine, ...]]:
        through = request.feature_as_of - timedelta(days=1)
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT stats_json,retrieved_at FROM stats_season_snapshots "
                "WHERE stats_run_id=? AND provider='baseball_reference' "
                "AND entity_kind='player' ORDER BY split_key,player_identity_id",
                (request.source_stats_run_id,),
            ).fetchall()

        batting: list[BattingAggregateLine] = []
        pitching: list[PitchingAggregateLine] = []
        for row in rows:
            try:
                payload = json.loads(str(row["stats_json"]))
                if date.fromisoformat(str(payload["range_end"])) != through:
                    continue
                normalized = dict(payload["normalized_counts"])
                source_row = dict(payload.get("source_row") or {})
                team = resolve_baseball_reference_aggregate_team(
                    str(
                        payload.get("source_team_name")
                        or payload["source_team_id"]
                    ),
                    str(
                        payload.get("source_level")
                        or source_row.get("level")
                        or "MLB"
                    ),
                ).canonical_team_key
                source_team_id = str(payload["source_team_id"])
                source_stint_key = str(payload["source_stint_key"])
                available_at = _aware(
                    datetime.fromisoformat(str(row["retrieved_at"]))
                )
                player = str(payload["canonical_player_id"])
                window = str(payload["window_key"])
                stat_kind = str(payload["stat_kind"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise FeatureMaterializationError(
                    "retained Baseball-Reference aggregate evidence is malformed"
                ) from exc

            if stat_kind == "batting":
                batting.append(
                    BattingAggregateLine(
                        through,
                        player,
                        window,
                        **{
                            key: _count(normalized, key)
                            for key in (
                                "pa",
                                "ab",
                                "hits",
                                "doubles",
                                "triples",
                                "home_runs",
                                "walks",
                                "hit_by_pitch",
                                "strikeouts",
                                "sacrifice_flies",
                            )
                        },
                        source_team_id=source_team_id,
                        source_stint_key=source_stint_key,
                        team_key=team,
                        available_at=available_at,
                    )
                )
            elif stat_kind == "pitching":
                pitching.append(
                    PitchingAggregateLine(
                        through,
                        player,
                        window,
                        **{
                            key: _count(normalized, key)
                            for key in (
                                "batters_faced",
                                "outs_recorded",
                                "hits",
                                "earned_runs",
                                "home_runs",
                                "walks",
                                "hit_by_pitch",
                                "strikeouts",
                                "pitches",
                            )
                        },
                        source_team_id=source_team_id,
                        source_stint_key=source_stint_key,
                        team_key=team,
                        available_at=available_at,
                    )
                )
            else:
                raise FeatureMaterializationError(
                    "retained aggregate stat_kind is invalid"
                )
        if not batting or not pitching:
            raise FeatureMaterializationError(
                "source run lacks D-1 aggregate feature inputs"
            )
        return tuple(batting), tuple(pitching)

    def _player_mappings(self) -> dict[str, str]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT identity.provider_player_id,mapping.canonical_player_id "
                "FROM stats_player_identities AS identity "
                "JOIN stats_player_identifier_mappings AS mapping "
                "ON mapping.player_identity_id=identity.player_identity_id "
                "WHERE identity.provider='statcast' "
                "AND mapping.verification_status='verified' "
                "ORDER BY identity.provider_player_id,mapping.canonical_player_id"
            ).fetchall()
        result: dict[str, str] = {}
        for row in rows:
            source = str(row[0])
            canonical = str(row[1])
            if source in result and result[source] != canonical:
                raise FeatureMaterializationError(
                    "verified Statcast mapping is ambiguous"
                )
            result[source] = canonical
        if not result:
            raise FeatureMaterializationError(
                "no verified Statcast player mappings are available"
            )
        return result

    def _statcast_inputs(
        self,
        feature_as_of: date,
        observed_at: datetime,
    ) -> _StatcastInputs:
        mappings = self._player_mappings()
        balls: list[StatcastBattedBall] = []
        metrics_out: list[StatcastPitchMetric] = []
        pitcher_pitches: list[StatcastPitcherPitch] = []
        swings: list[StatcastSwingMetric] = []
        checksums: set[str] = set()
        revisions: Counter[int] = Counter()
        v3: Counter[str] = Counter()
        cutoff = observed_at.isoformat()
        sql = """
            SELECT revision.metrics_json,revision.retrieved_at,
                   revision.source_checksum,revision.revision_number,
                   identity.game_pk,identity.at_bat_number,
                   identity.pitch_number,game.official_date
            FROM statcast_pitch_identities AS identity
            JOIN stats_game_identities AS game
              ON game.game_identity_id=identity.game_identity_id
            JOIN statcast_pitch_revisions AS revision
              ON revision.pitch_identity_id=identity.pitch_identity_id
            WHERE game.provider='statcast'
              AND game.game_type='R'
              AND game.season=?
              AND game.official_date < ?
              AND revision.retrieved_at <= ?
              AND revision.revision_number=(
                  SELECT MAX(candidate.revision_number)
                  FROM statcast_pitch_revisions AS candidate
                  WHERE candidate.pitch_identity_id=identity.pitch_identity_id
                    AND candidate.retrieved_at <= ?
              )
            ORDER BY identity.pitch_identity_id
        """
        with self.database.connect() as connection:
            cursor = connection.execute(
                sql,
                (
                    feature_as_of.year,
                    feature_as_of.isoformat(),
                    cutoff,
                    cutoff,
                ),
            )
            for row in cursor:
                try:
                    payload = json.loads(str(row["metrics_json"]))
                    identity = payload.get("_dse_identity")
                    if not isinstance(identity, dict):
                        continue
                    game_date = date.fromisoformat(str(identity["game_date"]))
                    if (
                        game_date >= feature_as_of
                        or game_date.isoformat() != str(row["official_date"])
                    ):
                        raise FeatureMaterializationError(
                            "Statcast D-1 identity boundary changed"
                        )
                    batter = mappings.get(str(int(identity["batter_id"])))
                    pitcher = mappings.get(str(int(identity["pitcher_id"])))
                    home = str(identity["home_team_key"])
                    away = str(identity["away_team_key"])
                    available = _aware(
                        datetime.fromisoformat(str(row["retrieved_at"]))
                    )
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise FeatureMaterializationError(
                        "retained Statcast feature identity is malformed"
                    ) from exc

                half = _inning_half(payload.get("inning_topbot"))
                batter_home = (
                    False if half == "top" else True if half == "bottom" else None
                )
                for field in (
                    "bat_speed",
                    "swing_length",
                    "effective_speed",
                    "spin_axis",
                    "arm_angle",
                ):
                    if payload.get(field) is not None:
                        v3[field] += 1

                bat_speed = optional_float(payload.get("bat_speed"))
                swing_length = optional_float(payload.get("swing_length"))
                attack_angle = optional_float(payload.get("attack_angle"))
                attack_direction = optional_float(payload.get("attack_direction"))
                swing_path_tilt = optional_float(payload.get("swing_path_tilt"))
                miss_distance = optional_float(payload.get("miss_distance"))
                hyper_speed = optional_float(payload.get("hyper_speed"))
                if batter is not None and any(
                    value is not None
                    for value in (
                        bat_speed,
                        swing_length,
                        attack_angle,
                        attack_direction,
                        swing_path_tilt,
                        miss_distance,
                        hyper_speed,
                    )
                ):
                    swings.append(
                        StatcastSwingMetric(
                            game_date,
                            batter,
                            bat_speed=bat_speed,
                            swing_length=swing_length,
                            attack_angle=attack_angle,
                            attack_direction=attack_direction,
                            swing_path_tilt=swing_path_tilt,
                            miss_distance=miss_distance,
                            hyper_speed=hyper_speed,
                            pitch_type=(
                                str(payload.get("pitch_type") or "").strip()
                                or None
                            ),
                            is_home=batter_home,
                            batter_hand=(
                                str(payload.get("stand") or "").strip() or None
                            ),
                            opposing_pitcher_hand=(
                                str(payload.get("p_throws") or "").strip() or None
                            ),
                            available_at=available,
                        )
                    )

                used = False
                if batter is not None and (
                    optional_float(payload.get("launch_speed")) is not None
                    or optional_float(payload.get("launch_angle")) is not None
                ):
                    balls.append(
                        StatcastBattedBall(
                            game_date,
                            batter,
                            optional_float(payload.get("launch_speed")),
                            optional_float(payload.get("launch_angle")),
                            optional_int(payload.get("launch_speed_angle")),
                            optional_float(
                                payload.get("estimated_ba_using_speedangle")
                            ),
                            optional_float(
                                payload.get("estimated_slg_using_speedangle")
                            ),
                            optional_float(
                                payload.get("estimated_woba_using_speedangle")
                            ),
                            hit_distance_sc=optional_float(
                                payload.get("hit_distance_sc")
                            ),
                            hc_x=optional_float(payload.get("hc_x")),
                            hc_y=optional_float(payload.get("hc_y")),
                            hit_location=optional_int(payload.get("hit_location")),
                            is_home=batter_home,
                            batter_hand=(
                                str(payload.get("stand") or "").strip() or None
                            ),
                            opposing_pitcher_hand=(
                                str(payload.get("p_throws") or "").strip() or None
                            ),
                            available_at=available,
                        )
                    )
                    used = True

                pitching_team = (
                    home if half == "top" else away if half == "bottom" else None
                )
                if pitcher is not None and pitching_team is not None and half is not None:
                    metrics_out.append(
                        StatcastPitchMetric(
                            game_date,
                            pitcher,
                            pitching_team,
                            str(payload.get("pitch_type") or "").strip() or None,
                            optional_float(payload.get("release_speed")),
                            optional_float(payload.get("release_spin_rate")),
                            optional_float(payload.get("release_extension")),
                            optional_float(payload.get("pfx_x")),
                            optional_float(payload.get("pfx_z")),
                            optional_int(payload.get("zone")),
                            str(payload.get("description") or "").strip() or None,
                            effective_speed=optional_float(
                                payload.get("effective_speed")
                            ),
                            spin_axis=optional_int(payload.get("spin_axis")),
                            release_pos_x=optional_float(payload.get("release_pos_x")),
                            release_pos_y=optional_float(payload.get("release_pos_y")),
                            release_pos_z=optional_float(payload.get("release_pos_z")),
                            vx0=optional_float(payload.get("vx0")),
                            vy0=optional_float(payload.get("vy0")),
                            vz0=optional_float(payload.get("vz0")),
                            ax=optional_float(payload.get("ax")),
                            ay=optional_float(payload.get("ay")),
                            az=optional_float(payload.get("az")),
                            api_break_x_arm=optional_float(
                                payload.get("api_break_x_arm")
                            ),
                            api_break_x_batter_in=optional_float(
                                payload.get("api_break_x_batter_in")
                            ),
                            api_break_z_with_gravity=optional_float(
                                payload.get("api_break_z_with_gravity")
                            ),
                            arm_angle=optional_float(payload.get("arm_angle")),
                            sz_top=optional_float(payload.get("sz_top")),
                            sz_bot=optional_float(payload.get("sz_bot")),
                            n_thruorder_pitcher=optional_int(
                                payload.get("n_thruorder_pitcher")
                            ),
                            pitcher_days_since_prev_game=optional_int(
                                payload.get("pitcher_days_since_prev_game")
                            ),
                            opposing_batter_hand=(
                                str(payload.get("stand") or "").strip() or None
                            ),
                            available_at=available,
                        )
                    )
                    inning = optional_int(payload.get("inning"))
                    at_bat = optional_int(row["at_bat_number"])
                    pitch_no = optional_int(row["pitch_number"])
                    if inning is None or at_bat is None or pitch_no is None:
                        raise FeatureMaterializationError(
                            "retained Statcast pitcher sequence is malformed"
                        )
                    pitcher_pitches.append(
                        StatcastPitcherPitch(
                            game_pk=int(row["game_pk"]),
                            game_date=game_date,
                            pitcher_id=pitcher,
                            team_key=pitching_team,
                            at_bat_number=at_bat,
                            pitch_number=pitch_no,
                            inning=inning,
                            inning_half=half,
                            outs_when_up=optional_int(payload.get("outs_when_up")),
                            event=str(payload.get("events") or "").strip() or None,
                            description=(
                                str(payload.get("description") or "").strip()
                                or None
                            ),
                            plate_appearance_classification=(
                                statcast_plate_appearance_classification(payload)
                            ),
                            batter_id=batter,
                            batter_team_key=(away if half == "top" else home),
                            batter_is_home=batter_home,
                            batter_hand=(
                                str(payload.get("stand") or "").strip() or None
                            ),
                            pitcher_hand=(
                                str(payload.get("p_throws") or "").strip() or None
                            ),
                            game_is_final=True,
                            available_at=available,
                        )
                    )
                    used = True

                if used:
                    checksums.add(str(row["source_checksum"]))
                revisions[int(row["revision_number"])] += 1

        if not metrics_out or not pitcher_pitches or not swings or not any(v3.values()):
            raise FeatureMaterializationError(
                "retained Statcast V3 evidence is incomplete for feature materialization"
            )
        return _StatcastInputs(
            batted_balls=tuple(balls),
            pitch_metrics=tuple(metrics_out),
            pitcher_pitches=tuple(pitcher_pitches),
            swing_metrics=tuple(swings),
            source_checksums=tuple(sorted(checksums)),
            revision_distribution=dict(revisions),
            v3_observations=dict(v3),
        )

    def _raw_checksums(self, source_id: str) -> tuple[str, ...]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT checksum_sha256 FROM stats_raw_payload_metadata "
                "WHERE stats_run_id=?",
                (source_id,),
            ).fetchall()
        result = tuple(sorted({str(row[0]) for row in rows}))
        if not result:
            raise FeatureMaterializationError(
                "source run has no retained raw checksum inventory"
            )
        return result

    def _scalar(self, sql: str) -> int:
        with self.database.connect() as connection:
            return int(connection.execute(sql).fetchone()[0])

    def _v3_count(self, feature_as_of: date) -> int:
        with self.database.connect() as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_feature_snapshots "
                    "WHERE entity_kind='player' AND feature_version=? "
                    "AND substr(feature_as_of,1,10)=?",
                    (FEATURE_VERSION_V3, feature_as_of.isoformat()),
                ).fetchone()[0]
            )

    def _record_reconciliation(
        self,
        stats_run_id: str,
        request: FeatureMaterializationRequest,
        completeness: CompletenessResult,
        observed_at: datetime,
        counts: Mapping[str, int],
    ) -> None:
        details = {
            "contract_version": FEATURE_MATERIALIZATION_VERSION,
            "feature_version": FEATURE_VERSION_V3,
            "feature_as_of": request.feature_as_of,
            "source_stats_run_id": request.source_stats_run_id,
            "source_observed_at": observed_at,
            "d1_boundary": request.feature_as_of - timedelta(days=1),
            "network_requests": 0,
            "statcast_revision_writes": 0,
            "counts": dict(counts),
        }
        self.repository.record_reconciliation(
            {
                "reconciliation_id": f"reconciliation_{uuid4().hex}",
                "stats_run_id": stats_run_id,
                "dataset_key": "derived_player_features_v3",
                "scope_key": f"season:{request.feature_as_of.year}",
                "status": "warnings" if completeness.partial_date else "passed",
                "expected_count": counts["player_v3_payloads"],
                "observed_count": (
                    counts["player_v3_inserted_for_run"]
                    + counts["player_v3_reused_existing"]
                ),
                "conflict_count": 0,
                "started_at": observed_at,
                "completed_at": _aware(self.clock()),
                "details": details,
                "source_checksum": _checksum(details),
            }
        )

    def _fail(self, run_id: str, stats_run_id: str, exc: Exception) -> None:
        now = _aware(self.clock())
        stats = self.repository.get_ingestion_run(stats_run_id)
        if stats is not None and str(stats["status"]) in {"queued", "running"}:
            self.repository.transition_ingestion_run(
                stats_run_id,
                "failed",
                transitioned_at=now,
                failure_stage="collector",
                error={"type": type(exc).__name__, "message": str(exc)},
            )
        outer = self.database.get_run(run_id)
        if outer is not None and str(outer["status"]) in {"queued", "running"}:
            self.database.transition_run(
                run_id,
                RunStatus.FAILED,
                failure_stage=FailureStage.COLLECTOR,
                error_message=str(exc),
                transitioned_at=now.isoformat(),
            )
