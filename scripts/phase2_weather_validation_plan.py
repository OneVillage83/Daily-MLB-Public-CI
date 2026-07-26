from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from app.config import Settings
from app.stadiums import (
    field_verification,
    is_field_verified,
    material_weather_metadata_errors,
    roof_operational_status,
    stadium_catalog_inventory,
    stadium_for_team,
)

PLAN_CONTRACT_VERSION = "DSE_PHASE2_WEATHER_VALIDATION_PLAN_V1"
MAX_EVENTS = 30
MAX_PASSES = 3


class PlanError(ValueError):
    pass


def _aware_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{field} must be a timezone-aware ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise PlanError(f"{field} must be a timezone-aware ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PlanError(f"{field} must be timezone-aware")
    return parsed


def _required_text(value: object, field: str) -> str:
    text = str(value).strip() if value is not None else ""
    if not text:
        raise PlanError(f"{field} must not be empty")
    return text


def _load_events(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        raise PlanError("events input must be a regular non-symlink file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlanError("events input must contain valid UTF-8 JSON") from exc
    if isinstance(payload, Mapping):
        payload = payload.get("events")
    if not isinstance(payload, list) or not payload:
        raise PlanError("events input must contain a non-empty event list")
    if len(payload) > MAX_EVENTS:
        raise PlanError(f"events input may contain at most {MAX_EVENTS} events")
    if not all(isinstance(item, Mapping) for item in payload):
        raise PlanError("every event must be a JSON object")
    return [dict(item) for item in payload]


def _output_path(path: Path) -> Path:
    resolved_parent = path.parent.resolve()
    if not resolved_parent.exists() or not resolved_parent.is_dir():
        raise PlanError("output parent directory must already exist")
    if path.exists() or path.is_symlink():
        raise PlanError("output path must not already exist")
    candidate = resolved_parent / path.name
    if candidate.parent != resolved_parent:
        raise PlanError("output path is not contained in its parent directory")
    return candidate


def _configuration_status(settings: Settings) -> tuple[str, list[str]]:
    try:
        settings.validate_for_weather()
    except RuntimeError as exc:
        message = str(exc)
        prefix = "Missing weather configuration: "
        details = message[len(prefix) :] if message.startswith(prefix) else message
        return "blocked", [item.strip() for item in details.split(", ") if item.strip()]
    return "ready", []


def _roof_value(stadium: Mapping[str, Any]) -> str:
    value = stadium.get("roof_type")
    return str(value).strip().lower() if value is not None else "unknown"


def build_plan(
    events: list[dict[str, Any]], *, settings: Settings, passes: int
) -> dict[str, Any]:
    if not 1 <= passes <= MAX_PASSES:
        raise PlanError(f"passes must be between 1 and {MAX_PASSES}")
    catalog = stadium_catalog_inventory()
    configuration_status, configuration_errors = _configuration_status(settings)
    seen_event_ids: set[str] = set()
    planned_events: list[dict[str, Any]] = []
    blocking_errors: list[str] = []
    nws_requests = 0
    openweather_guaranteed_requests = 0
    openweather_maximum_requests = 0

    for index, event in enumerate(events):
        event_id = _required_text(event.get("event_id"), f"events[{index}].event_id")
        if event_id in seen_event_ids:
            raise PlanError(f"duplicate event_id: {event_id}")
        seen_event_ids.add(event_id)
        home_team_key = _required_text(
            event.get("home_team_key"), f"events[{index}].home_team_key"
        ).upper()
        away_team_key = _required_text(
            event.get("away_team_key"), f"events[{index}].away_team_key"
        ).upper()
        if home_team_key == away_team_key:
            raise PlanError(f"event {event_id} has identical home and away teams")
        commence_time = _aware_datetime(
            event.get("commence_time"), f"events[{index}].commence_time"
        )
        stadium = stadium_for_team(home_team_key)
        event_errors: list[str] = []
        if stadium is None:
            event_errors.append("no canonical stadium for home team")
            planned_events.append(
                {
                    "event_id": event_id,
                    "home_team_key": home_team_key,
                    "away_team_key": away_team_key,
                    "commence_time": commence_time.isoformat(),
                    "status": "blocked",
                    "blocking_errors": event_errors,
                    "network_requests_per_pass": {"nws": 0, "openweather": 0},
                }
            )
            blocking_errors.append(f"{event_id}: {event_errors[0]}")
            continue

        if str(stadium.get("team_key") or "").strip() != home_team_key:
            event_errors.append("stadium active-club association does not match home team")
        event_errors.extend(material_weather_metadata_errors(stadium))
        roof_type = _roof_value(stadium)
        roof_status_value = roof_operational_status(stadium)
        roof_status = str(getattr(roof_status_value, "value", roof_status_value))
        fixed_indoor = (
            not event_errors
            and roof_type == "fixed"
            and is_field_verified(stadium, "roof_type")
            and roof_status == "closed"
        )
        weather_required = not fixed_indoor
        event_nws_requests = 2 if weather_required else 0
        if weather_required and settings.openweather_enabled:
            event_owm_maximum = 1
            event_owm_guaranteed = 1 if settings.weather_compare_enabled else 0
            openweather_mode = (
                "comparison" if settings.weather_compare_enabled else "fallback_only"
            )
        else:
            event_owm_maximum = 0
            event_owm_guaranteed = 0
            openweather_mode = "disabled_or_not_required"
        if not event_errors:
            nws_requests += event_nws_requests * passes
            openweather_guaranteed_requests += event_owm_guaranteed * passes
            openweather_maximum_requests += event_owm_maximum * passes
        else:
            blocking_errors.extend(f"{event_id}: {error}" for error in event_errors)

        bearing = field_verification(stadium, "outfield_bearing_degrees")
        planned_events.append(
            {
                "event_id": event_id,
                "home_team_key": home_team_key,
                "away_team_key": away_team_key,
                "commence_time": commence_time.isoformat(),
                "status": "blocked" if event_errors else "planned",
                "blocking_errors": event_errors,
                "venue": {
                    "team_key": stadium.get("team_key"),
                    "physical_venue_key": stadium.get("physical_venue_key"),
                    "display_name": stadium.get("current_display_name"),
                    "timezone": stadium.get("timezone"),
                    "latitude": stadium.get("latitude"),
                    "longitude": stadium.get("longitude"),
                    "roof_type": roof_type,
                    "operational_roof_status": roof_status,
                },
                "weather_required": weather_required,
                "weather_relevance": (
                    "not_applicable_fixed_indoor"
                    if fixed_indoor
                    else "contextual_roof_status_unknown"
                    if roof_type == "retractable"
                    else "outdoor_material"
                ),
                "openweather_mode": openweather_mode,
                "network_requests_per_pass": {
                    "nws": event_nws_requests,
                    "openweather_guaranteed": event_owm_guaranteed,
                    "openweather_maximum": event_owm_maximum,
                },
                "field_relative_wind_allowed": (
                    bearing.get("status") == "VERIFIED"
                ),
                "field_bearing_verification": bearing.get("status", "UNKNOWN"),
            }
        )

    catalog_errors = list(catalog.get("validation_errors") or [])
    blocked_teams = list(catalog.get("teams_blocked_for_weather") or [])
    if catalog_errors:
        blocking_errors.extend(f"catalog: {error}" for error in catalog_errors)
    if blocked_teams:
        blocking_errors.append(
            "catalog teams blocked for weather: " + ", ".join(blocked_teams)
        )
    if configuration_errors:
        blocking_errors.extend(
            f"configuration: {error}" for error in configuration_errors
        )

    status = "blocked" if blocking_errors else "plan"
    return {
        "contract_version": PLAN_CONTRACT_VERSION,
        "status": status,
        "network_requests_executed": 0,
        "live_execution_authorized": False,
        "passes": passes,
        "event_count": len(planned_events),
        "configuration_status": configuration_status,
        "configuration_errors": configuration_errors,
        "catalog": {
            "policy_version": catalog.get("policy_version"),
            "catalog_version": catalog.get("catalog_version"),
            "record_count": catalog.get("record_count"),
            "stadium_catalog_readiness": catalog.get("stadium_catalog_readiness"),
            "teams_blocked_for_weather": blocked_teams,
            "validation_errors": catalog_errors,
        },
        "request_budget": {
            "nws_exact": nws_requests,
            "openweather_guaranteed": openweather_guaranteed_requests,
            "openweather_maximum": openweather_maximum_requests,
            "total_guaranteed": nws_requests + openweather_guaranteed_requests,
            "total_maximum": nws_requests + openweather_maximum_requests,
        },
        "events": planned_events,
        "blocking_errors": blocking_errors,
        "safety_boundary": {
            "odds_requests": 0,
            "weather_requests": 0,
            "database_writes": 0,
            "prediction_generation": False,
            "approval": False,
            "publication": False,
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a zero-network Phase 2 stadium/weather validation plan"
    )
    parser.add_argument("--events", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--passes", type=int, default=2)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        events = _load_events(args.events)
        output = _output_path(args.output)
        plan = build_plan(events, settings=Settings(), passes=args.passes)
        payload = json.dumps(plan, indent=2, sort_keys=True) + "\n"
        descriptor = os.open(
            output,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            output.unlink(missing_ok=True)
            raise
    except (OSError, PlanError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "usage_error", "error": str(exc)}, sort_keys=True))
        return 64
    print(json.dumps(plan, sort_keys=True))
    return 0 if plan["status"] == "plan" else 2


if __name__ == "__main__":
    raise SystemExit(main())
