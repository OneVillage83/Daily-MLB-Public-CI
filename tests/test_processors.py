from app.processors.weather_processor import wind_impact


def test_wind_out():
    # Wind from south blows north; outfield bearing north means blowing out.
    result = wind_impact(180, 10, 0, bearing_verification_state="VERIFIED")
    assert result["classification"] == "blowing_out"
    assert result["outfield_component_mph"] == 10.0


def test_unverified_bearing_never_produces_field_relative_wind() -> None:
    result = wind_impact(180, 10, None, bearing_verification_state="UNVERIFIED")

    assert result == {
        "classification": "unknown",
        "outfield_component_mph": None,
        "crosswind_component_mph": None,
        "wind_to_degrees": None,
        "reason_code": "outfield_bearing_unverified",
    }


def test_unknown_bearing_has_explicit_reason() -> None:
    result = wind_impact(180, 10, None, bearing_verification_state="UNKNOWN")

    assert result["outfield_component_mph"] is None
    assert result["crosswind_component_mph"] is None
    assert result["reason_code"] == "outfield_bearing_unknown"


def test_verified_but_invalid_bearing_has_explicit_reason() -> None:
    result = wind_impact(180, 10, 360, bearing_verification_state="VERIFIED")

    assert result["outfield_component_mph"] is None
    assert result["crosswind_component_mph"] is None
    assert result["reason_code"] == "outfield_bearing_invalid"
