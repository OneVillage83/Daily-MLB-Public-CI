from __future__ import annotations

from app.config import Settings
from scripts.phase2_weather_validation_plan import PlanError, build_plan


def _event(event_id: str, home: str, away: str) -> dict[str, str]:
    return {
        "event_id": event_id,
        "home_team_key": home,
        "away_team_key": away,
        "commence_time": "2026-07-22T19:10:00-07:00",
    }


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "nws_user_agent": "Daily-MLB/1.0 (weather-ops@onevillage.org)",
        "openweather_enabled": False,
        "report_timezone": "America/Los_Angeles",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_plan_never_executes_network_and_counts_outdoor_nws_requests() -> None:
    plan = build_plan(
        [_event("ath-home", "ATH", "SF")],
        settings=_settings(),
        passes=2,
    )

    assert plan["status"] == "plan"
    assert plan["network_requests_executed"] == 0
    assert plan["request_budget"] == {
        "nws_exact": 4,
        "openweather_guaranteed": 0,
        "openweather_maximum": 0,
        "total_guaranteed": 4,
        "total_maximum": 4,
    }
    assert plan["events"][0]["weather_required"] is True
    assert plan["events"][0]["weather_relevance"] == "outdoor_material"
    assert plan["events"][0]["field_relative_wind_allowed"] is False


def test_fixed_roof_game_suppresses_weather_requests() -> None:
    plan = build_plan(
        [_event("rays-home", "TB", "NYY")],
        settings=_settings(),
        passes=2,
    )

    assert plan["status"] == "plan"
    assert plan["request_budget"]["total_maximum"] == 0
    assert plan["events"][0]["weather_required"] is False
    assert (
        plan["events"][0]["weather_relevance"]
        == "not_applicable_fixed_indoor"
    )


def test_comparison_mode_has_bounded_openweather_budget() -> None:
    plan = build_plan(
        [_event("ath-home", "ATH", "SF")],
        settings=_settings(
            openweather_enabled=True,
            openweather_api_key="configured-weather-key",
            weather_compare_enabled=True,
        ),
        passes=2,
    )

    assert plan["status"] == "plan"
    assert plan["request_budget"]["nws_exact"] == 4
    assert plan["request_budget"]["openweather_guaranteed"] == 2
    assert plan["request_budget"]["openweather_maximum"] == 2
    assert plan["events"][0]["openweather_mode"] == "comparison"


def test_fallback_mode_does_not_claim_guaranteed_openweather_calls() -> None:
    plan = build_plan(
        [_event("ath-home", "ATH", "SF")],
        settings=_settings(
            openweather_enabled=True,
            openweather_api_key="configured-weather-key",
            weather_compare_enabled=False,
        ),
        passes=2,
    )

    assert plan["request_budget"]["openweather_guaranteed"] == 0
    assert plan["request_budget"]["openweather_maximum"] == 2
    assert plan["events"][0]["openweather_mode"] == "fallback_only"


def test_missing_weather_configuration_blocks_without_network() -> None:
    plan = build_plan(
        [_event("ath-home", "ATH", "SF")],
        settings=Settings(
            nws_user_agent="replace-with-app-name-and-real-contact",
            openweather_enabled=True,
            openweather_api_key="",
        ),
        passes=2,
    )

    assert plan["status"] == "blocked"
    assert plan["network_requests_executed"] == 0
    assert plan["configuration_status"] == "blocked"
    assert any("NWS_USER_AGENT" in item for item in plan["configuration_errors"])
    assert any("OPENWEATHER_API_KEY" in item for item in plan["configuration_errors"])


def test_duplicate_event_identity_is_rejected() -> None:
    event = _event("duplicate", "ATH", "SF")

    try:
        build_plan([event, dict(event)], settings=_settings(), passes=2)
    except PlanError as exc:
        assert "duplicate event_id" in str(exc)
    else:
        raise AssertionError("duplicate event IDs must be rejected")
