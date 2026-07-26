from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from typing import Any, Callable, TypeVar
from zoneinfo import ZoneInfo

from app.artifacts import (
    COLLECTION_ERRORS_FILENAME,
    COLLECTION_MANIFEST_FILENAME,
    GAMES_FILENAME,
    ODDS_CONSENSUS_FILENAME,
    ODDS_RAW_ALL_UPCOMING_FILENAME,
    ODDS_RAW_FILENAME,
    WEATHER_FILENAME,
    ArtifactPaths,
)
from app.collectors.nws_weather_collector import NwsWeatherCollector
from app.collectors.odds_collector import COLLECTOR_VERSION, OddsCollector, OddsPayloadError
from app.collectors.openweather_collector import OpenWeatherCollector
from app.config import Settings
from app.database import Database, utc_now
from app.exporter import create_zip, write_bytes, write_json
from app.http import HttpClient
from app.jobs import RunExecutionError
from app.migrations import CURRENT_SCHEMA_VERSION
from app.processors.odds_processor import (
    CALCULATION_VERSION,
    ConsensusThresholds,
    FreshnessStatus,
    FreshnessThresholds,
    calculate_line_movement,
    process_game,
)
from app.processors.weather_processor import compare, wind_impact
from app.raw_payloads import RawPayloadCapture, sanitized_json_bytes
from app.redaction import redact_text, redact_value
from app.run_state import FailureStage, RunStatus
from app.stadiums import (
    field_verification,
    is_field_verified,
    material_weather_metadata_errors,
    roof_operational_status,
    stadium_for_team,
    verified_outfield_bearing,
)
from app.team_aliases import team_key

T = TypeVar("T")

_DATE_ASSOCIATION_FIELDS = frozenset(
    {"physical_venue_key", "active_club_association", "timezone"}
)
_WEATHER_COORDINATE_FIELDS = frozenset({"latitude", "longitude"})


@dataclass(frozen=True, slots=True)
class CollectionResult:
    status: RunStatus
    artifact_relpath: str
    completed_at: str
    warning_count: int


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _material_errors_for_fields(
    stadium: dict[str, Any], fields: frozenset[str]
) -> list[str]:
    return [
        error
        for error in material_weather_metadata_errors(stadium)
        if error.rsplit(":", 1)[-1] in fields
    ]


def _date_association_errors(
    stadium: dict[str, Any], *, home_team_key: str
) -> list[str]:
    errors = _material_errors_for_fields(stadium, _DATE_ASSOCIATION_FIELDS)
    if str(stadium.get("team_key") or "").strip() != home_team_key:
        errors.append("venue_metadata_invalid:active_club_team_key")
    return errors


def _weather_lookup_errors(
    stadium: dict[str, Any], *, home_team_key: str
) -> list[str]:
    return [
        *_date_association_errors(stadium, home_team_key=home_team_key),
        *_material_errors_for_fields(stadium, _WEATHER_COORDINATE_FIELDS),
    ]


def _fixed_indoor_suppression_allowed(
    stadium: dict[str, Any], *, home_team_key: str
) -> bool:
    roof_status_value = roof_operational_status(stadium)
    roof_status = str(getattr(roof_status_value, "value", roof_status_value))
    return (
        not _date_association_errors(stadium, home_team_key=home_team_key)
        and str(stadium.get("roof_type") or "").strip().lower() == "fixed"
        and is_field_verified(stadium, "roof_type")
        and roof_status == "closed"
    )


def run_collection(
    run_id: str,
    requested_date: date,
    settings: Settings,
    database: Database,
    artifacts: ArtifactPaths,
) -> CollectionResult:
    return _run_collection_impl(run_id, requested_date, settings, database, artifacts)


def _persistence(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except RunExecutionError:
        raise
    except Exception as exc:
        raise RunExecutionError(FailureStage.PERSISTENCE, str(exc)) from exc


def _artifact(operation: Callable[[], T]) -> T:
    try:
        return operation()
    except RunExecutionError:
        raise
    except Exception as exc:
        raise RunExecutionError(FailureStage.ARTIFACT_GENERATION, str(exc)) from exc


def _persist_raw_capture(
    capture: RawPayloadCapture,
    *,
    run_id: str,
    settings: Settings,
    database: Database,
    artifacts: ArtifactPaths,
) -> None:
    secrets = settings.credential_values()
    payload_bytes = _artifact(
        lambda: sanitized_json_bytes(capture, secret_values=secrets)
    )
    checksum = sha256(payload_bytes).hexdigest()
    raw_path = _artifact(
        lambda: artifacts.raw_json_path(
            capture.provider, capture.endpoint_category, checksum
        )
    )
    existed = raw_path.exists()
    _artifact(lambda: write_bytes(raw_path, payload_bytes))
    artifact_relpath = _artifact(
        lambda: raw_path.relative_to(artifacts.root).as_posix()
    )
    try:
        database.insert_raw_payload(
            run_id=run_id,
            event_id=capture.event_id,
            provider=capture.provider,
            endpoint_category=capture.endpoint_category,
            provider_timestamp=capture.provider_timestamp,
            retrieved_at=capture.retrieved_at,
            content_type=capture.content_type,
            checksum_sha256=checksum,
            artifact_relpath=artifact_relpath,
        )
    except Exception as exc:
        if not existed:
            try:
                raw_path.unlink(missing_ok=True)
            except OSError:
                pass
        raise RunExecutionError(FailureStage.PERSISTENCE, str(exc)) from exc


def _record_warning(
    errors: list[dict[str, Any]],
    *,
    database: Database,
    run_id: str,
    stage: str,
    provider: str | None,
    message: str,
    event_id: str | None,
) -> None:
    _persistence(
        lambda: database.add_error(
            run_id,
            stage=stage,
            provider=provider,
            message=message,
            event_id=event_id,
            details=None,
        )
    )
    errors.append(
        {
            "stage": stage,
            "provider": provider,
            "event_id": event_id,
            "message": message,
        }
    )


def _persist_odds_request_diagnostics(
    database: Database,
    run_id: str,
    diagnostics: Any,
    *,
    quota: dict[str, str | None] | None = None,
    warning_count: int = 0,
    credentials: tuple[str, ...] = (),
) -> None:
    if diagnostics is None:
        return
    details = {
        "status": "failed",
        "request_status": diagnostics.request_status,
        "status_code": diagnostics.status_code,
        "request_duration_seconds": diagnostics.duration_seconds,
        "attempts": diagnostics.attempts,
        "retries_performed": diagnostics.retries_performed,
        "response_date_utc": diagnostics.response_date_utc,
        "quota_headers": (
            quota
            if quota is not None
            else getattr(diagnostics, "quota_headers", {})
        ),
        "collector_version": COLLECTOR_VERSION,
        "warning_count": warning_count,
    }
    _persistence(
        lambda: database.add_error(
            run_id,
            stage="odds_request",
            provider="the_odds_api",
            message="The Odds API request or payload validation failed",
            details=json.dumps(redact_value(details, credentials), sort_keys=True),
        )
    )


def _run_collection_impl(
    run_id: str,
    requested_date: date,
    settings: Settings,
    database: Database,
    artifacts: ArtifactPaths,
) -> CollectionResult:
    _artifact(lambda: artifacts.run_dir.mkdir(parents=True, exist_ok=True))
    run_row = _persistence(lambda: database.get_run(run_id))
    if run_row is None:
        raise RunExecutionError(FailureStage.PERSISTENCE, "Collection run does not exist")

    errors: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "requested_date": requested_date.isoformat(),
        "started_at": run_row["started_at"],
        "completed_at": None,
        "status": RunStatus.RUNNING.value,
        "app_version": settings.app_version,
        "schema_version": CURRENT_SCHEMA_VERSION,
        "calculation_version": CALCULATION_VERSION,
        "sources": {},
        "warning_count": 0,
        "error_count": 0,
    }
    credentials = settings.credential_values()
    http = HttpClient(
        settings.request_timeout_seconds,
        max_attempts=settings.odds_max_attempts,
        retry_max_seconds=settings.odds_retry_max_seconds,
    )
    try:
        odds_result = OddsCollector(settings, http).collect()
    except OddsPayloadError as exc:
        _persist_odds_request_diagnostics(
            database,
            run_id,
            exc.request,
            quota=exc.quota,
            warning_count=len(exc.warnings),
            credentials=credentials,
        )
        _persist_raw_capture(
            exc.capture,
            run_id=run_id,
            settings=settings,
            database=database,
            artifacts=artifacts,
        )
        structural_warnings = [
            {
                "run_id": run_id,
                "event_id": warning.event_id,
                "bookmaker": warning.bookmaker,
                "market": warning.market,
                "code": warning.code,
                "message": warning.message,
                "created_at": warning.created_at,
            }
            for warning in exc.warnings
        ]
        if structural_warnings:
            _persistence(
                lambda: database.insert_odds_warnings(run_id, structural_warnings)
            )
        raise RunExecutionError(FailureStage.COLLECTOR, str(exc)) from exc
    except Exception as exc:
        _persist_odds_request_diagnostics(
            database,
            run_id,
            getattr(exc, "diagnostics", None),
            credentials=credentials,
        )
        raise RunExecutionError(FailureStage.COLLECTOR, str(exc)) from exc
    all_games = odds_result.games
    quota = odds_result.quota
    odds_capture = odds_result.capture
    _persist_raw_capture(
        odds_capture,
        run_id=run_id,
        settings=settings,
        database=database,
        artifacts=artifacts,
    )
    collector_warnings: list[dict[str, Any]] = [
        {
            "run_id": run_id,
            "event_id": warning.event_id,
            "bookmaker": warning.bookmaker,
            "market": warning.market,
            "code": warning.code,
            "message": warning.message,
            "created_at": warning.created_at,
        }
        for warning in odds_result.warnings
    ]
    odds_warnings = list(collector_warnings)
    if collector_warnings:
        _persistence(lambda: database.insert_odds_warnings(run_id, collector_warnings))
        errors.extend(
            {
                "stage": "odds_normalization",
                "provider": "the_odds_api",
                **warning,
            }
            for warning in collector_warnings
        )

    games: list[dict[str, Any]] = []
    filtering_warnings: list[dict[str, Any]] = []
    try:
        for candidate in all_games:
            home_key = team_key(candidate.get("home_team", ""))
            stadium = stadium_for_team(home_key)
            if not stadium:
                filtering_warnings.append(
                    {
                        "run_id": run_id,
                        "event_id": (
                            redact_text(str(candidate.get("id")), credentials)
                            if candidate.get("id")
                            else None
                        ),
                        "bookmaker": None,
                        "market": None,
                        "code": "unknown_team",
                        "message": (
                            "Event excluded because its home team has no exact canonical "
                            "stadium-timezone mapping"
                        ),
                        "created_at": odds_capture.retrieved_at,
                    }
                )
                continue
            stadium_errors = _date_association_errors(
                stadium, home_team_key=home_key or ""
            )
            if stadium_errors:
                filtering_warnings.append(
                    {
                        "run_id": run_id,
                        "event_id": (
                            redact_text(str(candidate.get("id")), credentials)
                            if candidate.get("id")
                            else None
                        ),
                        "bookmaker": None,
                        "market": None,
                        "code": "unknown_team",
                        "message": (
                            "Event excluded because material stadium metadata is "
                            "invalid or unverified: " + "; ".join(stadium_errors)
                        ),
                        "created_at": odds_capture.retrieved_at,
                    }
                )
                continue
            start = parse_dt(candidate["commence_time"]).astimezone(
                ZoneInfo(stadium["timezone"])
            )
            if start.date() == requested_date:
                games.append(candidate)
    except Exception as exc:
        raise RunExecutionError(FailureStage.COLLECTOR, str(exc)) from exc
    if filtering_warnings:
        _persistence(lambda: database.insert_odds_warnings(run_id, filtering_warnings))
        odds_warnings.extend(filtering_warnings)
        errors.extend(
            {
                "stage": "odds_normalization",
                "provider": "the_odds_api",
                **warning,
            }
            for warning in filtering_warnings
        )
    selected_event_ids = {str(game["id"]) for game in games}

    _artifact(
        lambda: write_json(
            artifacts.json_path(ODDS_RAW_FILENAME),
            redact_value(games, credentials, preserve_field_names=("key",)),
        )
    )
    _artifact(
        lambda: write_json(
            artifacts.json_path(ODDS_RAW_ALL_UPCOMING_FILENAME),
            redact_value(all_games, credentials, preserve_field_names=("key",)),
        )
    )

    normalized_games: list[dict[str, Any]] = []
    weather_packets: list[dict[str, Any]] = []
    nws = NwsWeatherCollector(http, settings.nws_user_agent)
    owm = (
        OpenWeatherCollector(
            http, settings.openweather_api_key, settings.openweather_api_version
        )
        if settings.openweather_enabled
        else None
    )
    nws_attempts = nws_successes = owm_attempts = owm_successes = 0
    raw_snapshot_count = 0
    normalized_market_count = 0
    freshness_counts = {status.value: 0 for status in FreshnessStatus}
    distinct_bookmakers: set[str] = set()
    freshness_thresholds = FreshnessThresholds(
        fresh_seconds=settings.odds_fresh_seconds,
        stale_seconds=settings.odds_stale_seconds,
        future_tolerance_seconds=settings.odds_future_tolerance_seconds,
    )
    consensus_thresholds = ConsensusThresholds(
        minimum_books=settings.odds_consensus_min_books,
        moderate_books=settings.odds_consensus_moderate_books,
        high_books=settings.odds_consensus_high_books,
    )

    for game in all_games:
        try:
            event_id = str(game["id"])
            home_key = team_key(game["home_team"])
            away_key = team_key(game["away_team"])
            raw_bookmakers = game.get("bookmakers", [])
            if not isinstance(raw_bookmakers, list):
                raise ValueError("bookmakers must be a list")

            processed = process_game(
                game,
                run_id=run_id,
                retrieved_at=odds_capture.retrieved_at,
                freshness_thresholds=freshness_thresholds,
                consensus_thresholds=consensus_thresholds,
            )
            annotated_bookmakers = processed.annotated_game.get("bookmakers", [])
            if not isinstance(annotated_bookmakers, list):
                raise ValueError("annotated bookmakers must be a list")

            def persist_game_and_odds() -> int:
                return database.persist_game_with_odds(
                    run_id,
                    game,
                    home_key,
                    away_key,
                    annotated_bookmakers,
                    odds_capture.retrieved_at,
                    warnings=list(processed.warnings),
                )

            _persistence(persist_game_and_odds)
            history_rows = _persistence(lambda: database.get_odds_history(event_id))
            game_summary = processed.summary
            game_summary["line_movement"] = calculate_line_movement(history_rows)
            structural_event_warnings = [
                warning
                for warning in collector_warnings
                if warning.get("event_id") == event_id
                and warning.get("code")
                in {
                    "malformed_event",
                    "malformed_bookmaker",
                    "malformed_market",
                    "malformed_outcome",
                }
            ]
            game_summary["warnings"] = [
                *structural_event_warnings,
                *game_summary["warnings"],
            ]
        except RunExecutionError:
            raise
        except Exception as exc:
            raise RunExecutionError(FailureStage.COLLECTOR, str(exc)) from exc

        raw_snapshot_count += processed.raw_snapshot_count
        normalized_market_count += processed.normalized_market_count
        for status, count in processed.freshness_counts.items():
            freshness_counts[status] += count
        for bookmaker in annotated_bookmakers:
            if isinstance(bookmaker, dict) and bookmaker.get("key"):
                distinct_bookmakers.add(str(bookmaker["key"]))
        normalized_warnings = list(processed.warnings)
        odds_warnings.extend(normalized_warnings)
        errors.extend(
            {
                "stage": "odds_normalization",
                "provider": "the_odds_api",
                **warning,
            }
            for warning in normalized_warnings
        )

        if event_id not in selected_event_ids:
            continue

        game_summary.update(
            {
                "sport_key": game.get("sport_key"),
                "commence_time": game["commence_time"],
                "home_team": game["home_team"],
                "away_team": game["away_team"],
                "home_team_key": home_key,
                "away_team_key": away_key,
            }
        )
        normalized_games.append(game_summary)
        stadium = stadium_for_team(home_key)
        packet: dict[str, Any] = {
            "event_id": event_id,
            "home_team": game["home_team"],
            "away_team": game["away_team"],
            "stadium": stadium,
        }
        if not stadium:
            message = f"No stadium metadata for home team {game['home_team']} ({home_key})"
            _record_warning(
                errors,
                database=database,
                run_id=run_id,
                stage="stadium",
                provider=None,
                message=message,
                event_id=event_id,
            )
            weather_packets.append(packet)
            continue
        association_errors = _date_association_errors(
            stadium, home_team_key=home_key or ""
        )
        roof_type = str(stadium.get("roof_type") or "unknown").strip().lower()
        roof_status_value = roof_operational_status(stadium)
        roof_status = str(getattr(roof_status_value, "value", roof_status_value))
        fixed_indoor = not association_errors and _fixed_indoor_suppression_allowed(
            stadium, home_team_key=home_key or ""
        )
        coordinate_errors = (
            []
            if association_errors or fixed_indoor
            else _material_errors_for_fields(stadium, _WEATHER_COORDINATE_FIELDS)
        )
        stadium_errors = association_errors or coordinate_errors
        if stadium_errors:
            packet["weather_status"] = "unavailable_invalid_stadium_metadata"
            packet["operational_roof_status"] = (
                "unknown" if association_errors else roof_status
            )
            _record_warning(
                errors,
                database=database,
                run_id=run_id,
                stage="stadium",
                provider=None,
                message=(
                    "Weather collection skipped because material stadium metadata "
                    "is invalid or unverified: " + "; ".join(stadium_errors)
                ),
                event_id=event_id,
            )
            weather_packets.append(packet)
            continue
        if fixed_indoor:
            packet["weather_status"] = "indoor_fixed_roof"
            packet["operational_roof_status"] = roof_status
            weather_packets.append(packet)
            continue
        packet["operational_roof_status"] = roof_status
        if roof_type == "retractable":
            packet["weather_relevance"] = "contextual_roof_status_unknown"
        elif not is_field_verified(stadium, "roof_type"):
            packet["weather_relevance"] = "contextual_roof_type_unverified"

        game_time = parse_dt(game["commence_time"])
        nws_row = owm_row = None
        nws_attempts += 1
        try:
            nws_row, point_capture, forecast_capture = nws.collect(
                stadium["latitude"],
                stadium["longitude"],
                game_time,
                event_id=event_id,
            )
        except Exception as exc:
            _record_warning(
                errors,
                database=database,
                run_id=run_id,
                stage="weather",
                provider="nws",
                message=redact_text(f"NWS collection failed: {exc}", credentials),
                event_id=event_id,
            )
        else:
            _persist_raw_capture(
                point_capture,
                run_id=run_id,
                settings=settings,
                database=database,
                artifacts=artifacts,
            )
            _persist_raw_capture(
                forecast_capture,
                run_id=run_id,
                settings=settings,
                database=database,
                artifacts=artifacts,
            )
            _persistence(
                lambda: database.insert_weather(
                    run_id,
                    event_id,
                    "nws",
                    nws_row,
                    retrieved_at=forecast_capture.retrieved_at,
                )
            )
            packet["nws"] = nws_row
            nws_successes += 1

        if owm is not None:
            owm_attempts += 1
            try:
                owm_row, owm_capture = owm.collect(
                    stadium["latitude"],
                    stadium["longitude"],
                    game_time,
                    event_id=event_id,
                )
            except Exception as exc:
                _record_warning(
                    errors,
                    database=database,
                    run_id=run_id,
                    stage="weather",
                    provider="openweather",
                    message=redact_text(
                        f"OpenWeather collection failed: {exc}", credentials
                    ),
                    event_id=event_id,
                )
            else:
                _persist_raw_capture(
                    owm_capture,
                    run_id=run_id,
                    settings=settings,
                    database=database,
                    artifacts=artifacts,
                )
                _persistence(
                    lambda: database.insert_weather(
                        run_id,
                        event_id,
                        "openweather",
                        owm_row,
                        retrieved_at=owm_capture.retrieved_at,
                    )
                )
                packet["openweather"] = owm_row
                owm_successes += 1

        source = nws_row or owm_row
        packet["comparison"] = compare(nws_row, owm_row)
        verified_bearing = verified_outfield_bearing(stadium)
        bearing_verification = field_verification(
            stadium, "outfield_bearing_degrees"
        )
        packet["baseball_wind_impact"] = wind_impact(
            source.get("wind_direction_deg") if source else None,
            source.get("wind_speed_mph") if source else None,
            verified_bearing,
            bearing_verification_state=str(
                bearing_verification.get("status", "UNKNOWN")
            ),
        )
        weather_packets.append(packet)

    manifest["sources"]["the_odds_api"] = {
        "status": "success",
        "request_status": odds_result.request.request_status,
        "status_code": odds_result.request.status_code,
        "provider_event_count": len(all_games),
        "event_count": len(games),
        "games_returned": len(games),
        "bookmaker_count": len(distinct_bookmakers),
        "raw_snapshot_count": raw_snapshot_count,
        "normalized_market_count": normalized_market_count,
        "warning_count": len(odds_warnings),
        "freshness_counts": freshness_counts,
        "retrieved_at": odds_capture.retrieved_at,
        "response_date_utc": odds_result.request.response_date_utc,
        "quota_headers": quota,
        "quota": quota,
        "request_duration_seconds": odds_result.request.duration_seconds,
        "attempts": odds_result.request.attempts,
        "retries_performed": odds_result.request.retries_performed,
        "collector_version": odds_result.collector_version,
    }

    manifest["sources"]["nws"] = {
        "status": (
            "completed"
            if nws_attempts == nws_successes
            else "completed_with_warnings"
        ),
        "provider": "api.weather.gov",
        "attempts": nws_attempts,
        "successes": nws_successes,
    }
    manifest["sources"]["openweather"] = {
        "status": (
            "disabled"
            if owm is None
            else "completed"
            if owm_attempts == owm_successes
            else "completed_with_warnings"
        ),
        "attempts": owm_attempts,
        "successes": owm_successes,
    }

    _artifact(lambda: write_json(artifacts.json_path(GAMES_FILENAME), normalized_games))
    _artifact(
        lambda: write_json(
            artifacts.json_path(ODDS_CONSENSUS_FILENAME), normalized_games
        )
    )
    _artifact(lambda: write_json(artifacts.json_path(WEATHER_FILENAME), weather_packets))
    _artifact(
        lambda: write_json(
            artifacts.json_path(COLLECTION_ERRORS_FILENAME),
            redact_value(errors, credentials),
        )
    )

    terminal_status = (
        RunStatus.COMPLETED_WITH_WARNINGS if errors else RunStatus.COMPLETED
    )
    completed_at = utc_now()
    manifest.update(
        {
            "completed_at": completed_at,
            "status": terminal_status.value,
            "warning_count": len(errors),
            "error_count": 0,
        }
    )
    _artifact(
        lambda: write_json(
            artifacts.json_path(COLLECTION_MANIFEST_FILENAME),
            redact_value(manifest, credentials),
        )
    )
    archive = _artifact(lambda: create_zip(artifacts.run_dir))
    return CollectionResult(
        status=terminal_status,
        artifact_relpath=archive.relative_to(artifacts.root).as_posix(),
        completed_at=completed_at,
        warning_count=len(errors),
    )


def write_failure_manifest(
    run_id: str,
    database: Database,
    artifacts: ArtifactPaths,
    settings: Settings,
) -> None:
    row = database.get_run(run_id)
    if row is None or row["status"] != RunStatus.FAILED.value:
        return
    artifacts.run_dir.mkdir(parents=True, exist_ok=True)
    safe_error = redact_text(
        row.get("error_message") or "Collection failed",
        settings.credential_values(),
    )
    sources: dict[str, Any] = {}
    odds_warning_count = len(database.list_odds_warnings(run_id))
    request_error = database.get_latest_error(run_id, stage="odds_request")
    if request_error and request_error.get("details"):
        try:
            request_details = json.loads(str(request_error["details"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            request_details = None
        if isinstance(request_details, dict):
            sources["the_odds_api"] = redact_value(
                request_details,
                settings.credential_values(),
            )
    write_json(
        artifacts.json_path(COLLECTION_ERRORS_FILENAME),
        [
            {
                "stage": "run",
                "failure_stage": row["failure_stage"],
                "message": safe_error,
            }
        ],
    )
    write_json(
        artifacts.json_path(COLLECTION_MANIFEST_FILENAME),
        {
            "run_id": run_id,
            "requested_date": row["requested_date"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "status": RunStatus.FAILED.value,
            "failure_stage": row["failure_stage"],
            "error": safe_error,
            "app_version": settings.app_version,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "calculation_version": CALCULATION_VERSION,
            "sources": sources,
            "warning_count": odds_warning_count,
            "error_count": 1,
        },
    )
