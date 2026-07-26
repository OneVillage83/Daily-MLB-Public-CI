from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

import app.stadiums as stadium_module
from app.stadiums import (
    CATALOG_VERSION,
    DATA_PATH,
    EVIDENCE_TEXT_NORMALIZATION_VERSION,
    EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION,
    EXPECTED_TEAM_KEYS,
    FIELD_EVIDENCE_CONTRACT_VERSION,
    MATERIAL_WEATHER_FIELDS,
    OWNER_ATTESTATION_CONTRACT_VERSION,
    POLICY_VERSION,
    ROOF_TAXONOMY_CONTRACT_VERSION,
    TEAM_ALIASES_PATH,
    RoofOperationalStatus,
    VenueResolutionStatus,
    VerificationState,
    canonical_evidence_sha256,
    evidence_text_sha256,
    field_verification,
    is_field_verified,
    load_stadium_catalog,
    load_stadiums,
    material_weather_metadata_errors,
    normalize_evidence_text,
    resolve_venue_alias,
    roof_operational_status,
    stadium_catalog_inventory,
    validate_stadium_catalog,
    validate_stadium_catalog_data,
    verified_outfield_bearing,
)

EXHAUSTIVE_SOURCE_ID = "MLB_OUTFIELD_EXHAUSTIVE_ROOF_CLASSIFICATION_2026_V1"
OWNER_SOURCE_ID = "OWNER_ATTESTATION_SUTTER_ROOF_2026_V1"
NEWLY_VERIFIED_EXHAUSTIVE = {"ATL", "CIN", "CWS", "LAA", "PHI", "PIT"}
PRIOR_UNRESOLVED = NEWLY_VERIFIED_EXHAUSTIVE | {"ATH"}


def _raw_catalog() -> dict[str, Any]:
    value = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _team_aliases() -> dict[str, Any]:
    value = json.loads(TEAM_ALIASES_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _errors_for(catalog: dict[str, Any], aliases: dict[str, Any] | None = None) -> list[str]:
    return validate_stadium_catalog_data(catalog, aliases or _team_aliases())


def _source(catalog: dict[str, Any], source_id: str) -> dict[str, Any]:
    return next(source for source in catalog["sources"] if source["id"] == source_id)


def _exhaustive_claim(catalog: dict[str, Any]) -> dict[str, Any]:
    return _source(catalog, EXHAUSTIVE_SOURCE_ID)["roof_evidence"][
        "exhaustive_classification"
    ]


def _owner_claim(catalog: dict[str, Any]) -> dict[str, Any]:
    return _source(catalog, OWNER_SOURCE_ID)["roof_evidence"]["owner_attestation"]


def _refresh_structured_checksum(value: dict[str, Any]) -> None:
    value["structured_evidence_sha256"] = canonical_evidence_sha256(
        {key: item for key, item in value.items() if key != "structured_evidence_sha256"}
    )


def test_catalog_is_versioned_structurally_valid_and_complete() -> None:
    catalog = load_stadium_catalog()
    stadiums = load_stadiums()

    assert validate_stadium_catalog() == []
    assert catalog["policy_version"] == POLICY_VERSION
    assert catalog["catalog_version"] == CATALOG_VERSION
    assert catalog["evidence_contract"]["version"] == FIELD_EVIDENCE_CONTRACT_VERSION
    assert catalog["evidence_contract"]["catalog_revision"] == CATALOG_VERSION
    assert len(stadiums) == 30
    assert set(stadiums) == EXPECTED_TEAM_KEYS
    assert len({record["physical_venue_key"] for record in stadiums.values()}) == 30


def test_all_team_alias_keys_have_exactly_one_active_stadium() -> None:
    aliases = _team_aliases()["teams"]
    owners: dict[str, str] = {}

    assert {record["key"] for record in aliases} == EXPECTED_TEAM_KEYS
    for record in aliases:
        assert record["key"] in load_stadiums()
        for alias in record["aliases"]:
            assert alias not in owners
            owners[alias] = record["key"]


def test_field_level_provenance_is_present_and_conflict_safe() -> None:
    source_ids = {source["id"] for source in load_stadium_catalog()["sources"]}

    for stadium in load_stadiums().values():
        for field, metadata in stadium["field_verification"].items():
            assert metadata["status"] in {state.value for state in VerificationState}
            assert set(metadata["source_ids"]) <= source_ids
            if metadata["status"] == VerificationState.VERIFIED.value:
                assert metadata["source_ids"], field
                assert metadata["conflicts"] == []


def test_verified_roof_evidence_is_minimal_reproducible_and_value_matched() -> None:
    catalog = load_stadium_catalog()
    sources = {source["id"]: source for source in catalog["sources"]}
    retrieval_zone = ZoneInfo(catalog["evidence_contract"]["retrieval_timezone"])

    for stadium in load_stadiums().values():
        metadata = field_verification(stadium, "roof_type")
        if metadata["status"] != VerificationState.VERIFIED.value:
            continue
        matching: list[dict[str, Any]] = []
        for source_id in metadata["source_ids"]:
            source = sources[source_id]
            evidence = source.get("roof_evidence")
            if not evidence:
                continue
            assert source["source_title"]
            retrieved_at = datetime.fromisoformat(source["retrieved_at"].replace("Z", "+00:00"))
            assert retrieved_at.utcoffset() is not None
            assert retrieved_at.astimezone(retrieval_zone).date().isoformat() == source[
                "retrieval_date"
            ]
            assert source["retrieval_date"] == source["accessed_at"]
            assert evidence["normalization_version"] == EVIDENCE_TEXT_NORMALIZATION_VERSION
            assert isinstance(evidence["revision"], int) and evidence["revision"] >= 1
            assert evidence["locator"]
            assert evidence["notes"]
            kind = evidence["kind"]
            if kind == "exhaustive_classification":
                claim = evidence["exhaustive_classification"]
                assert claim["contract_version"] == EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION
                assert claim["taxonomy_contract_version"] == ROOF_TAXONOMY_CONTRACT_VERSION
                for fragment in claim["evidence_fragments"]:
                    text = fragment["minimal_evidence_text"]
                    assert text == normalize_evidence_text(text)
                    assert len(text.split()) <= 25
                    assert fragment["normalized_text_sha256"] == evidence_text_sha256(text)
                assert claim["structured_evidence_sha256"] == canonical_evidence_sha256(
                    {
                        key: value
                        for key, value in claim.items()
                        if key != "structured_evidence_sha256"
                    }
                )
                matching.extend(
                    item
                    for item in claim["subject_derivations"]
                    if item["team_key"] == stadium["team_key"]
                    and item["physical_venue_key"] == stadium["physical_venue_key"]
                    and item["observed_derived_value"] == stadium["roof_type"]
                )
                continue
            text = evidence["minimal_evidence_text"]
            assert text == normalize_evidence_text(text)
            assert len(text.split()) <= 25
            assert evidence["normalized_text_sha256"] == evidence_text_sha256(text)
            if kind == "owner_attested_direct_observation":
                claim = evidence["owner_attestation"]
                assert claim["contract_version"] == OWNER_ATTESTATION_CONTRACT_VERSION
                assert claim["structured_evidence_sha256"] == canonical_evidence_sha256(
                    {
                        key: value
                        for key, value in claim.items()
                        if key != "structured_evidence_sha256"
                    }
                )
                if (
                    claim["team_key"] == stadium["team_key"]
                    and claim["physical_venue_key"] == stadium["physical_venue_key"]
                    and claim["observed_value"] == stadium["roof_type"]
                ):
                    matching.append(claim)
            elif evidence["observed_value"] == stadium["roof_type"]:
                matching.append(evidence)
        assert matching, stadium["team_key"]


def test_evidence_text_normalization_is_deterministic() -> None:
    first = "  Open-air\r\n  ballpark  "
    second = "Open-air ballpark"

    assert normalize_evidence_text(first) == second
    assert evidence_text_sha256(first) == evidence_text_sha256(second)


def test_coordinate_evidence_is_record_specific_reproducible_and_reconciled() -> None:
    object_urls: set[str] = set()

    for stadium in load_stadiums().values():
        for field in ("latitude", "longitude"):
            metadata = field_verification(stadium, field)
            evidence = metadata["evidence"]
            assert metadata["status"] == VerificationState.VERIFIED.value
            assert evidence["source_id"] == "OSM_NOMINATIM_2026"
            assert evidence["official_address_source_id"] == "MLB_TEAM_LOCATIONS_2026"
            assert evidence["accepted_feature_category"] == "leisure"
            assert evidence["accepted_feature_type"] == "stadium"
            assert evidence["accessed_at"] == "2026-07-12"
            assert evidence["osm_type"] in {"node", "way", "relation"}
            assert isinstance(evidence["osm_id"], int)
            assert evidence["observed_latitude"] == stadium["latitude"]
            assert evidence["observed_longitude"] == stadium["longitude"]
            assert evidence["matched_official_address"] is True
            assert evidence["legacy_difference_meters"] <= 250
            object_urls.add(evidence["object_url"])
    assert len(object_urls) == 30
    assert load_stadium_catalog()["coordinate_validation"]["request_count"] == 30
    assert "OpenStreetMap contributors" in load_stadium_catalog()["coordinate_validation"]["attribution"]


def test_raw_coordinate_observations_are_self_contained_not_loader_synthesized() -> None:
    for record in _raw_catalog()["stadiums"]:
        evidence = record["coordinate_evidence"]
        assert evidence["query_name"]
        assert evidence["observed_latitude"] == record["latitude"]
        assert evidence["observed_longitude"] == record["longitude"]
        assert evidence["legacy_latitude"] is not None
        assert evidence["legacy_longitude"] is not None
        assert 0 <= evidence["legacy_difference_meters"] <= 250


def test_dodger_branding_is_same_current_physical_venue() -> None:
    stadium = load_stadiums()["LAD"]

    assert stadium["physical_venue_key"] == "dodger-stadium-los-angeles"
    assert stadium["base_venue_name"] == "Dodger Stadium"
    assert stadium["current_display_name"] == "UNIQLO Field at Dodger Stadium"
    assert stadium["venue"] == "UNIQLO Field at Dodger Stadium"
    assert stadium["former_physical_venues"] == []
    assert resolve_venue_alias("Dodger Stadium", team_key="LAD")["status"] == VenueResolutionStatus.CURRENT.value
    branded = resolve_venue_alias("UNIQLO Field at Dodger Stadium", team_key="LAD")
    assert branded["status"] == VenueResolutionStatus.CURRENT.value
    assert branded["physical_venue_key"] == stadium["physical_venue_key"]


def test_exact_alias_resolution_distinguishes_same_venue_history_and_former_places() -> None:
    assert resolve_venue_alias("Daikin Park")["status"] == VenueResolutionStatus.CURRENT.value
    assert resolve_venue_alias("Minute Maid Park")["status"] == VenueResolutionStatus.HISTORICAL_SAME_VENUE.value
    assert resolve_venue_alias("Globe Life Park in Arlington")["status"] == VenueResolutionStatus.FORMER_PHYSICAL_VENUE.value
    assert resolve_venue_alias("George M. Steinbrenner Field", team_key="TB")["status"] == VenueResolutionStatus.FORMER_PHYSICAL_VENUE.value
    assert resolve_venue_alias(" dodger stadium ")["status"] == VenueResolutionStatus.UNKNOWN.value
    assert resolve_venue_alias("Dodgers Stadium")["status"] == VenueResolutionStatus.UNKNOWN.value
    assert resolve_venue_alias("Comiskey Park", team_key="CWS")["status"] == VenueResolutionStatus.UNKNOWN.value


def test_roof_inventory_and_operational_semantics() -> None:
    stadiums = load_stadiums()
    retractable = {key for key, value in stadiums.items() if value["roof_type"] == "retractable"}

    assert retractable == {"ARI", "HOU", "MIA", "MIL", "SEA", "TEX", "TOR"}
    assert all(is_field_verified(stadiums[key], "roof_type") for key in retractable)
    assert roof_operational_status(stadiums["ARI"]) == RoofOperationalStatus.UNKNOWN
    assert stadiums["TB"]["roof_type"] == "fixed"
    assert is_field_verified(stadiums["TB"], "roof_type")
    assert roof_operational_status(stadiums["TB"]) == RoofOperationalStatus.CLOSED

    verified_open = deepcopy(stadiums["LAD"])
    verified_open["field_verification"]["roof_type"] = {
        "status": VerificationState.VERIFIED.value,
        "source_ids": ["test"],
        "verified_at": "2026-07-12",
        "notes": None,
        "conflicts": [],
    }
    assert roof_operational_status(verified_open) == RoofOperationalStatus.NOT_APPLICABLE


def test_catalog_revision_preserves_all_roof_enums_and_prior_catalog() -> None:
    prior_path = DATA_PATH.with_name("mlb_stadiums.v3.json")
    prior = json.loads(prior_path.read_text(encoding="utf-8"))
    current = _raw_catalog()

    assert prior["catalog_version"] == 3
    assert current["catalog_version"] == 4
    assert {item["team_key"]: item["roof_type"] for item in prior["stadiums"]} == {
        item["team_key"]: item["roof_type"] for item in current["stadiums"]
    }
    prior_by_team = {item["team_key"]: item for item in prior["stadiums"]}
    current_by_team = {item["team_key"]: item for item in current["stadiums"]}
    for team_key in EXPECTED_TEAM_KEYS:
        for field in (
            "physical_venue_key",
            "base_venue_name",
            "current_display_name",
            "aliases",
            "latitude",
            "longitude",
            "timezone",
            "roof_type",
            "outfield_bearing_degrees",
        ):
            assert current_by_team[team_key][field] == prior_by_team[team_key][field]
        prior_status = prior_by_team[team_key]["field_verification"]["roof_type"]["status"]
        current_status = current_by_team[team_key]["field_verification"]["roof_type"]["status"]
        assert (prior_status, current_status) == (
            ("UNVERIFIED", "VERIFIED")
            if team_key in PRIOR_UNRESOLVED
            else ("VERIFIED", "VERIFIED")
        )


def test_only_complete_qualifying_evidence_promotes_open_roofs() -> None:
    catalog = load_stadium_catalog()
    sources = {source["id"]: source for source in catalog["sources"]}
    verified_open = {
        team_key
        for team_key, stadium in load_stadiums().items()
        if stadium["roof_type"] == "open" and is_field_verified(stadium, "roof_type")
    }
    unresolved = {
        team_key
        for team_key, stadium in load_stadiums().items()
        if field_verification(stadium, "roof_type")["status"]
        == VerificationState.UNVERIFIED.value
    }

    assert verified_open == {
        team_key
        for team_key, stadium in load_stadiums().items()
        if stadium["roof_type"] == "open"
    }
    assert unresolved == set()
    for team_key in PRIOR_UNRESOLVED:
        metadata = field_verification(load_stadiums()[team_key], "roof_type")
        reviewed_sources = [
            sources[source_id]
            for source_id in metadata["source_ids"]
            if source_id != "LEGACY_STARTER_CATALOG"
        ]
        assert reviewed_sources
        assert any(
            source["roof_evidence"]["kind"] == "inference_only"
            and source["roof_evidence"]["observed_value"] is None
            for source in reviewed_sources
        )
    assert _source(catalog, OWNER_SOURCE_ID)["roof_evidence"]["kind"] == (
        "owner_attested_direct_observation"
    )
    assert _source(catalog, EXHAUSTIVE_SOURCE_ID)["roof_evidence"]["kind"] == (
        "exhaustive_classification"
    )


def test_unknown_roof_is_never_interpreted_as_open_or_closed() -> None:
    stadium = deepcopy(load_stadiums()["ARI"])
    stadium["roof_type"] = "unknown"
    stadium["field_verification"]["roof_type"] = {
        "status": VerificationState.UNKNOWN.value,
        "source_ids": [],
        "verified_at": None,
        "notes": "unknown",
        "conflicts": [],
    }

    assert roof_operational_status(stadium) == RoofOperationalStatus.UNKNOWN
    assert material_weather_metadata_errors(stadium) == ["venue_metadata_unknown:roof_type"]

    raw = _raw_catalog()
    raw["stadiums"][0]["roof_type"] = "unknown"
    raw["stadiums"][0]["field_verification"]["roof_type"] = {
        "status": VerificationState.UNKNOWN.value,
        "source_ids": [],
        "verified_at": None,
        "conflicts": [],
    }
    assert not any("roof_type is UNKNOWN but has a value" in error for error in _errors_for(raw))


def test_unverified_bearings_are_never_exposed_as_verified_facts() -> None:
    for stadium in load_stadiums().values():
        assert field_verification(stadium, "outfield_bearing_degrees")["status"] == VerificationState.UNVERIFIED.value
        assert verified_outfield_bearing(stadium) is None

    verified = deepcopy(load_stadiums()["ARI"])
    verified["field_verification"]["outfield_bearing_degrees"] = {
        "status": VerificationState.VERIFIED.value,
        "source_ids": ["test"],
        "verified_at": "2026-07-12",
        "notes": None,
        "conflicts": [],
    }
    assert verified_outfield_bearing(verified) == 0.0


def test_material_metadata_errors_fail_closed_by_field() -> None:
    for stadium in load_stadiums().values():
        expected = [
            f"venue_metadata_{field_verification(stadium, field)['status'].lower()}:{field}"
            for field in MATERIAL_WEATHER_FIELDS
            if not is_field_verified(stadium, field)
        ]
        assert material_weather_metadata_errors(stadium) == expected


def test_inventory_reports_field_states_and_weather_blockers() -> None:
    inventory = stadium_catalog_inventory()

    assert inventory["record_count"] == 30
    assert inventory["roof_type_counts"] == {"fixed": 1, "open": 22, "retractable": 7}
    assert inventory["field_verification_counts"]["latitude"] == {"VERIFIED": 30}
    assert inventory["field_verification_counts"]["longitude"] == {"VERIFIED": 30}
    assert inventory["field_verification_counts"]["timezone"] == {"VERIFIED": 30}
    assert inventory["field_verification_counts"]["roof_type"] == {"VERIFIED": 30}
    assert inventory["field_verification_counts"]["outfield_bearing_degrees"] == {"UNVERIFIED": 30}
    assert inventory["teams_blocked_for_weather"] == []
    assert inventory["stadium_catalog_readiness"] == "READY"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("latitude", 91, "latitude is outside its valid range"),
        ("latitude", float("nan"), "latitude must be finite numeric"),
        ("longitude", -181, "longitude is outside its valid range"),
        ("outfield_bearing_degrees", 360, "outfield_bearing_degrees is outside its valid range"),
        ("outfield_bearing_degrees", float("inf"), "outfield_bearing_degrees must be finite numeric"),
        ("timezone", "US/Not-A-Zone", "timezone is not an installed IANA timezone"),
        ("roof_type", "dome-ish", "unsupported roof_type"),
    ],
)
def test_invalid_ranges_timezones_and_enums_are_rejected(field: str, value: Any, message: str) -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][0][field] = value

    assert any(message in error for error in _errors_for(catalog))


def test_bearing_zero_is_valid_but_not_factually_verified() -> None:
    assert _errors_for(_raw_catalog()) == []
    stadium = load_stadiums()["ARI"]
    assert stadium["outfield_bearing_degrees"] == 0
    assert not is_field_verified(stadium, "outfield_bearing_degrees")


def test_duplicate_records_are_detected_before_lookup_construction() -> None:
    catalog = _raw_catalog()
    duplicate = deepcopy(catalog["stadiums"][0])
    duplicate["physical_venue_key"] = "other-key"
    catalog["stadiums"].append(duplicate)

    assert "duplicate team key: ARI" in _errors_for(catalog)

    catalog = _raw_catalog()
    catalog["stadiums"][1]["physical_venue_key"] = catalog["stadiums"][0]["physical_venue_key"]
    assert any("duplicate physical venue key" in error for error in _errors_for(catalog))


def test_duplicate_raw_json_object_key_is_rejected(tmp_path: Path) -> None:
    catalog_path = tmp_path / "stadiums.json"
    catalog_path.write_text('{"policy_version":"a","policy_version":"b"}', encoding="utf-8")

    assert validate_stadium_catalog(catalog_path, TEAM_ALIASES_PATH) == [
        "duplicate JSON object key: policy_version"
    ]


@pytest.mark.parametrize("field", ["aliases", "historical_display_names"])
def test_duplicate_names_within_a_record_are_rejected(field: str) -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][0][field] = ["Repeated", "Repeated"]

    assert any(f"duplicate names in {field}" in error for error in _errors_for(catalog))


def test_duplicate_former_venue_names_are_rejected() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][1]["former_physical_venues"][0]["names"] = ["Old Park", "Old Park"]

    assert any("duplicate former physical venue names" in error for error in _errors_for(catalog))


def test_alias_collisions_and_current_former_overlap_are_rejected() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][0]["aliases"].append("Collision Park")
    catalog["stadiums"][1]["aliases"].append("Collision Park")
    assert "current venue alias collision: Collision Park" in _errors_for(catalog)

    catalog = _raw_catalog()
    catalog["stadiums"][1]["former_physical_venues"][0]["names"].append("Chase Field")
    assert "former venue alias overlaps current physical venue: Chase Field" in _errors_for(catalog)


def test_team_alias_collisions_are_rejected() -> None:
    aliases = _team_aliases()
    aliases["teams"][1]["aliases"].append(aliases["teams"][0]["aliases"][0])

    assert any("team alias collision" in error for error in _errors_for(_raw_catalog(), aliases))


def test_source_records_and_conflict_sources_are_validated() -> None:
    catalog = _raw_catalog()
    catalog["sources"][0]["publisher"] = ""
    assert "source 0: publisher must be a non-empty string" in _errors_for(catalog)

    catalog = _raw_catalog()
    catalog["stadiums"][0].setdefault("field_verification", {})["roof_type"] = {
        "status": VerificationState.UNVERIFIED.value,
        "source_ids": ["MLB_ARI_ROOF"],
        "conflicts": [{"source_id": "not-a-source", "observed_value": "fixed"}],
    }
    assert any("conflict references unknown source" in error for error in _errors_for(catalog))


def test_roof_evidence_contract_rejects_naive_timestamp_and_checksum_drift() -> None:
    catalog = _raw_catalog()
    source = next(source for source in catalog["sources"] if source.get("roof_evidence"))
    source["retrieved_at"] = "2026-07-13T12:00:00"
    assert any("retrieved_at must be a timezone-aware" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    source = next(source for source in catalog["sources"] if source.get("roof_evidence"))
    source["roof_evidence"]["minimal_evidence_text"] += " changed"
    assert any("roof evidence checksum mismatch" in error for error in _errors_for(catalog))


def test_roof_evidence_contract_accepts_winter_pacific_timestamp() -> None:
    catalog = _raw_catalog()
    source = next(source for source in catalog["sources"] if source.get("roof_evidence"))
    source["retrieved_at"] = "2026-01-13T12:00:00-08:00"
    source["retrieval_date"] = "2026-01-13"
    source["accessed_at"] = "2026-01-13"

    assert _errors_for(catalog) == []


def test_roof_evidence_contract_rejects_invalid_retrieval_timezone() -> None:
    catalog = _raw_catalog()
    catalog["evidence_contract"]["retrieval_timezone"] = "Not/A_Timezone"

    assert any(
        "retrieval_timezone must be an IANA timezone" in error
        for error in _errors_for(catalog)
    )


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing_title", "source_title is required"),
        ("missing_notes", "roof evidence notes are required"),
        ("blank_locator", "roof evidence locator must be null or non-empty"),
        ("retrieval_date", "retrieval_date must match"),
        ("non_normalized_text", "minimal roof evidence text is not normalized"),
        ("overlong_text", "minimal roof evidence text exceeds 25 words"),
        ("invalid_kind", "unsupported roof evidence kind"),
        ("invalid_observed_value", "qualifying roof evidence has invalid observed_value"),
    ],
)
def test_roof_evidence_contract_rejects_invalid_metadata(
    case: str, expected_error: str
) -> None:
    catalog = _raw_catalog()
    source = next(source for source in catalog["sources"] if source.get("roof_evidence"))
    evidence = source["roof_evidence"]

    if case == "missing_title":
        source["source_title"] = ""
    elif case == "missing_notes":
        evidence["notes"] = ""
    elif case == "blank_locator":
        evidence["locator"] = ""
    elif case == "retrieval_date":
        source["retrieval_date"] = "2026-07-12"
    elif case == "non_normalized_text":
        evidence["minimal_evidence_text"] = f"  {evidence['minimal_evidence_text']}"
    elif case == "overlong_text":
        evidence["minimal_evidence_text"] = " ".join(["word"] * 26)
        evidence["normalized_text_sha256"] = evidence_text_sha256(
            evidence["minimal_evidence_text"]
        )
    elif case == "invalid_kind":
        evidence["kind"] = "visual_inference"
    elif case == "invalid_observed_value":
        evidence["observed_value"] = "unknown"

    assert any(expected_error in error for error in _errors_for(catalog))


def test_exhaustive_classification_is_bound_to_the_six_exact_catalog_records() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    records = {item["team_key"]: item for item in catalog["stadiums"]}
    expected_groups = {
        "ATL": "group_2",
        "CIN": "group_2",
        "CWS": "group_3",
        "LAA": "group_2",
        "PHI": "group_5",
        "PIT": "group_3",
    }

    assert claim["contract_version"] == EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION
    assert claim["taxonomy_contract_version"] == ROOF_TAXONOMY_CONTRACT_VERSION
    assert claim["universe"]["declared_size"] == 29
    assert claim["universe"]["group_count"] == 5
    assert sum(len(group["members"]) for group in claim["universe"]["groups"]) == 29
    assert sum(
        len(fragment["minimal_evidence_text"].split())
        for fragment in claim["evidence_fragments"]
    ) <= 25
    assert claim["contrary_category"]["completion_semantics"] == "remaining"
    assert len(claim["contrary_category"]["members"]) == 8
    assert {
        item["team_key"]: item["universe_group_id"]
        for item in claim["subject_derivations"]
    } == expected_groups
    for derivation in claim["subject_derivations"]:
        team_key = derivation["team_key"]
        assert derivation["physical_venue_key"] == records[team_key]["physical_venue_key"]
        assert derivation["subject_membership"] == "explicit_group_member"
        assert derivation["contrary_category_membership"] == (
            "not_in_exhaustively_enumerated_category"
        )
        assert derivation["operation"] == "set_complement"
        assert derivation["observed_derived_value"] == "open"
        assert derivation["authoritative_conflict_found"] is False
        roof_metadata = records[team_key]["field_verification"]["roof_type"]
        assert roof_metadata["status"] == "VERIFIED"
        assert EXHAUSTIVE_SOURCE_ID in roof_metadata["source_ids"]


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("undefined_universe", "universe definition is required"),
        ("missing_subject_membership", "lacks explicit universe membership"),
        ("non_exhaustive_category", "lacks an explicit remaining/completion basis"),
        ("missing_completion_basis", "completion evidence does not establish remaining"),
        ("ambiguous_category_membership", "contrary-category membership is ambiguous"),
        ("authoritative_conflict", "has an authoritative conflict"),
    ],
)
def test_exhaustive_classification_fails_closed_when_derivation_is_incomplete(
    case: str, expected_error: str
) -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    if case == "undefined_universe":
        claim["universe"]["definition"] = ""
    elif case == "missing_subject_membership":
        claim["subject_derivations"][0]["subject_membership"] = "assumed_by_omission"
    elif case == "non_exhaustive_category":
        claim["contrary_category"]["completion_semantics"] = "partial"
    elif case == "missing_completion_basis":
        fragment = next(
            item
            for item in claim["evidence_fragments"]
            if item["role"] == "completion_basis"
        )
        fragment["minimal_evidence_text"] = "three parks"
        fragment["normalized_text_sha256"] = evidence_text_sha256("three parks")
    elif case == "ambiguous_category_membership":
        claim["subject_derivations"][0]["contrary_category_membership"] = "unknown"
    elif case == "authoritative_conflict":
        claim["subject_derivations"][0]["authoritative_conflict_found"] = True
    _refresh_structured_checksum(claim)

    assert any(expected_error in error for error in _errors_for(catalog))


def test_exhaustive_classification_rejects_source_text_and_structured_checksum_drift() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    claim["evidence_fragments"][0]["minimal_evidence_text"] = "30 parks"

    errors = _errors_for(catalog)
    assert any("evidence checksum mismatch" in error for error in errors)
    assert any("structured evidence checksum mismatch" in error for error in errors)


def test_exhaustive_classification_rejects_arbitrary_omission() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    claim["contrary_category"]["completion_semantics"] = "not_mentioned"
    claim["evidence_fragments"] = [
        fragment
        for fragment in claim["evidence_fragments"]
        if fragment["role"] != "completion_basis"
    ]
    _refresh_structured_checksum(claim)

    errors = _errors_for(catalog)
    assert any("required evidence-fragment role" in error for error in errors)
    assert any("remaining/completion basis" in error for error in errors)


def test_exhaustive_classification_rejects_a_coordinated_universe_shrink() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    universe = claim["universe"]
    group = universe["groups"][3]
    group["members"].pop()
    group["members_sha256"] = canonical_evidence_sha256(group["members"])
    population = [
        member["physical_venue_key"]
        for source_group in universe["groups"]
        for member in source_group["members"]
    ]
    universe["declared_size"] = 28
    universe["population_members_sha256"] = canonical_evidence_sha256(population)
    _refresh_structured_checksum(claim)

    errors = _errors_for(catalog)
    assert any("does not match the reviewed 29-park source" in error for error in errors)
    assert any("do not cover the active catalog" in error for error in errors)


def test_exhaustive_classification_rejects_a_rewritten_roofed_subset() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    category = claim["contrary_category"]
    category["remaining_members"][0] = "petco-park-san-diego"
    category["members"] = category["initial_members"] + category["remaining_members"]
    category["members_sha256"] = canonical_evidence_sha256(category["members"])
    _refresh_structured_checksum(claim)

    assert any(
        "does not match the reviewed roofed set" in error for error in _errors_for(catalog)
    )


def test_exhaustive_classification_rejects_semantic_fragment_rewrites() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    fragment = next(
        item for item in claim["evidence_fragments"] if item["id"] == "initial_covered"
    )
    fragment["minimal_evidence_text"] = "pleasant weather"
    fragment["normalized_text_sha256"] = evidence_text_sha256("pleasant weather")
    _refresh_structured_checksum(claim)

    assert any(
        "source fragments do not match the reviewed revision" in error
        for error in _errors_for(catalog)
    )


def test_exhaustive_classification_rejects_a_rewritten_sutter_exclusion() -> None:
    catalog = _raw_catalog()
    claim = _exhaustive_claim(catalog)
    claim["universe"]["explicit_exclusions"][0]["physical_venue_key"] = (
        "fictional-stadium"
    )
    _refresh_structured_checksum(claim)

    assert any(
        "explicit exclusion must be the reviewed Sutter record" in error
        for error in _errors_for(catalog)
    )


def test_owner_attestation_is_exact_venue_bound_and_commercially_limited() -> None:
    catalog = _raw_catalog()
    claim = _owner_claim(catalog)
    source = _source(catalog, OWNER_SOURCE_ID)
    athletics = next(item for item in catalog["stadiums"] if item["team_key"] == "ATH")
    exact_text = (
        "I live in Sacramento and have been to Sutter Health Park multiple times. "
        "There is no roof."
    )

    assert claim["contract_version"] == OWNER_ATTESTATION_CONTRACT_VERSION
    assert claim["attestor_identity"] == "OneVillage83"
    assert claim["attestor_role"] == "project_owner"
    assert claim["physical_venue_key"] == "sutter-health-park-west-sacramento"
    assert claim["physical_venue_key"] == athletics["physical_venue_key"]
    assert athletics["field_verification"]["roof_type"]["source_ids"] == [
        "LEGACY_STARTER_CATALOG",
        "MLB_ATH_OPEN_AREA_CONTEXT_2026",
        OWNER_SOURCE_ID,
    ]
    assert claim["exact_attestation_text"] == exact_text
    assert source["roof_evidence"]["minimal_evidence_text"] == exact_text
    assert source["url"].startswith("internal://project-owner-attestation/")
    assert claim["attestation_recorded_at"] == source["retrieved_at"]
    assert claim["normalized_text_sha256"] == evidence_text_sha256(exact_text)
    assert claim["attested_field"] == "roof_type"
    assert claim["fact_class"] == "static_physical_venue_fact"
    assert claim["observed_value"] == "open"
    assert "observed_at" not in claim
    assert claim["commercialization_boundary_acknowledged"] is True
    assert catalog["evidence_contract"]["owner_attestation_policy"][
        "commercial_revalidation_required"
    ] is True


def test_owner_attestation_authorization_cannot_be_rewritten_in_catalog_data() -> None:
    catalog = _raw_catalog()
    claim = _owner_claim(catalog)
    claim["attestor_identity"] = "OtherOwner"
    catalog["evidence_contract"]["owner_attestation_policy"][
        "authorized_owner_identities"
    ] = ["OtherOwner"]
    _refresh_structured_checksum(claim)

    errors = _errors_for(catalog)
    assert any("authorized_owner_identities is invalid" in error for error in errors)
    assert any("attestor identity is not authorized" in error for error in errors)


def test_derived_evidence_outer_values_must_match_the_bound_claims() -> None:
    catalog = _raw_catalog()
    exhaustive_source = _source(catalog, EXHAUSTIVE_SOURCE_ID)
    exhaustive_source["roof_evidence"]["observed_value"] = "fixed"
    assert any(
        "outer observed value does not match its derivations" in error
        for error in _errors_for(catalog)
    )

    catalog = _raw_catalog()
    owner_source = _source(catalog, OWNER_SOURCE_ID)
    owner_source["roof_evidence"]["observed_value"] = "fixed"
    assert any(
        "outer observed value must match the attested value" in error
        for error in _errors_for(catalog)
    )


def test_historical_catalog_v3_bytes_remain_immutable() -> None:
    catalog_v3 = DATA_PATH.with_name("mlb_stadiums.v3.json")
    assert hashlib.sha256(catalog_v3.read_bytes()).hexdigest() == (
        "ba01582b5c59afe6116f8ab3501e66408d3a4c7e937a6e955799b180d7407a28"
    )


@pytest.mark.parametrize(
    ("field", "fact_class", "expected_error"),
    [
        ("weather", "static_physical_venue_fact", "restricted to approved static"),
        (
            "roof_type",
            "game_specific_roof_operational_status",
            "fact class is not an approved static observation",
        ),
    ],
)
def test_owner_attestation_cannot_establish_dynamic_or_unsupported_facts(
    field: str, fact_class: str, expected_error: str
) -> None:
    catalog = _raw_catalog()
    claim = _owner_claim(catalog)
    claim["attested_field"] = field
    claim["fact_class"] = fact_class
    _refresh_structured_checksum(claim)

    assert any(expected_error in error for error in _errors_for(catalog))


@pytest.mark.parametrize("field", ["weather", "roof_operational_status"])
def test_owner_attestation_source_cannot_be_reused_for_dynamic_fields(field: str) -> None:
    catalog = _raw_catalog()
    athletics = next(item for item in catalog["stadiums"] if item["team_key"] == "ATH")
    athletics["field_verification"][field] = {
        "status": "VERIFIED",
        "source_ids": [OWNER_SOURCE_ID],
        "verified_at": "2026-07-13",
        "notes": "Invalid attempted reuse.",
        "conflicts": [],
    }

    assert any(f"{field} cannot use owner_attested_direct_observation" in error for error in _errors_for(catalog))


def test_sutter_owner_attestation_cannot_be_reused_for_another_venue() -> None:
    catalog = _raw_catalog()
    atlanta = next(item for item in catalog["stadiums"] if item["team_key"] == "ATL")
    atlanta["field_verification"]["roof_type"]["source_ids"].append(OWNER_SOURCE_ID)

    assert any(
        "ATL: roof_type source OWNER_ATTESTATION_SUTTER_ROOF_2026_V1 has no venue-bound"
        in error
        for error in _errors_for(catalog)
    )


def test_inference_only_roof_evidence_cannot_verify_a_field() -> None:
    catalog = _raw_catalog()
    source = next(source for source in catalog["sources"] if source.get("roof_evidence"))
    source["roof_evidence"]["kind"] = "inference_only"
    source["roof_evidence"]["observed_value"] = None
    source["roof_evidence"]["normalized_text_sha256"] = evidence_text_sha256(
        source["roof_evidence"]["minimal_evidence_text"]
    )
    stadium = next(
        stadium
        for stadium in catalog["stadiums"]
        if source["id"] in stadium.get("field_verification", {}).get("roof_type", {}).get("source_ids", [])
    )

    assert any(
        f"{stadium['team_key']}: roof_type is VERIFIED without matching qualifying evidence"
        in error
        for error in _errors_for(catalog)
    )


def test_direct_roof_evidence_mismatch_requires_preserved_conflict() -> None:
    catalog = _raw_catalog()
    stadium = catalog["stadiums"][0]
    stadium["roof_type"] = "fixed"
    stadium["field_verification"]["roof_type"] = {
        "status": VerificationState.UNVERIFIED.value,
        "source_ids": ["MLB_ARI_ROOF"],
        "verified_at": None,
        "notes": "conflicting direct evidence",
        "conflicts": [],
    }

    assert any("mismatch is missing a conflict record" in error for error in _errors_for(catalog))

    stadium["field_verification"]["roof_type"]["conflicts"] = [
        {"source_id": "MLB_ARI_ROOF", "observed_value": "retractable"}
    ]
    assert not any(
        "mismatch is missing a conflict record" in error for error in _errors_for(catalog)
    )


def test_authoritative_conflict_must_degrade_verified_state() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][0].setdefault("field_verification", {})["roof_type"] = {
        "status": VerificationState.UNVERIFIED.value,
        "source_ids": ["MLB_ARI_ROOF", "LEGACY_STARTER_CATALOG"],
        "verified_at": None,
        "conflicts": [
            {"source_id": "LEGACY_STARTER_CATALOG", "observed_value": "fixed"}
        ],
    }
    assert not any("has conflicts but is VERIFIED" in error for error in _errors_for(catalog))

    catalog["stadiums"][0]["field_verification"]["roof_type"]["status"] = VerificationState.VERIFIED.value
    assert any("has conflicts but is VERIFIED" in error for error in _errors_for(catalog))


def test_verified_field_requires_provenance_and_unknown_field_requires_null() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][0].setdefault("field_verification", {})["roof_type"] = {
        "status": VerificationState.VERIFIED.value,
        "source_ids": [],
        "conflicts": [],
    }
    assert any("is VERIFIED without provenance" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    catalog["stadiums"][0].setdefault("field_verification", {})["roof_type"] = {
        "status": VerificationState.UNKNOWN.value,
        "source_ids": [],
        "conflicts": [],
    }
    assert any("is UNKNOWN but has a value" in error for error in _errors_for(catalog))


def test_verified_field_requires_a_valid_verification_date() -> None:
    catalog = _raw_catalog()
    catalog["default_field_verification"]["team_name"]["verified_at"] = None

    assert any("team_name is VERIFIED without valid verified_at" in error for error in _errors_for(catalog))


def test_display_name_history_is_sourced_ordered_and_same_venue() -> None:
    tampa = load_stadiums()["TB"]
    assert tampa["display_name_history"][1]["effective_to"] == "1996-10"

    catalog = _raw_catalog()
    catalog["stadiums"][11]["display_name_history"][0]["effective_to"] = "not-a-date"
    assert any("display history dates must be supported ISO dates" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    catalog["stadiums"][11]["display_name_history"][0]["source_ids"] = []
    assert any("display history sources are required" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    catalog["stadiums"][11]["display_name_history"][0]["name"] = "Other Physical Park"
    assert any("display history name is not a same-venue name" in error for error in _errors_for(catalog))


def test_current_display_effective_date_is_valid_and_provenanced() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][14]["current_display_effective_from"] = "2026-02-30"
    assert any("current_display_effective_from must be an ISO date" in error for error in _errors_for(catalog))


def test_former_venue_cannot_reuse_current_key_or_lack_verified_provenance() -> None:
    catalog = _raw_catalog()
    former = catalog["stadiums"][1]["former_physical_venues"][0]
    former["physical_venue_key"] = catalog["stadiums"][1]["physical_venue_key"]
    assert any("former physical venue key equals current venue" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    catalog["stadiums"][1]["former_physical_venues"][0]["source_ids"] = []
    assert any("VERIFIED former physical venue lacks provenance" in error for error in _errors_for(catalog))


def test_former_venue_association_seasons_are_explicit_and_valid() -> None:
    catalog = _raw_catalog()
    former = catalog["stadiums"][27]["former_physical_venues"][0]
    assert former["association_started_season"] == 1994
    assert former["association_ended_after_season"] == 2019
    assert "association_seasons" not in former

    former["association_seasons"] = [1994, 2019]
    assert any("association_seasons is ambiguous" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    former = catalog["stadiums"][27]["former_physical_venues"][0]
    former["association_started_season"] = 2020
    assert any("association range is reversed" in error for error in _errors_for(catalog))

    catalog = _raw_catalog()
    former = catalog["stadiums"][27]["former_physical_venues"][0]
    former["association_ended_after_season"] = "2019"
    assert any("association_ended_after_season is invalid" in error for error in _errors_for(catalog))


def test_unverified_former_venue_is_not_a_definitive_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    record = deepcopy(load_stadiums()["ATH"])
    record["former_physical_venues"][0]["status"] = VerificationState.UNVERIFIED.value
    monkeypatch.setattr(stadium_module, "load_stadiums", lambda: {"ATH": record})

    resolution = stadium_module.resolve_venue_alias("Oakland Coliseum", team_key="ATH")
    assert resolution["status"] == VenueResolutionStatus.UNKNOWN.value
    assert resolution["reason"] == "unverified_former_physical_venue"


def test_parent_former_venue_verification_cannot_hide_unverified_child() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"][1]["former_physical_venues"][0]["status"] = VerificationState.UNVERIFIED.value

    assert any("field is VERIFIED with unverified entries" in error for error in _errors_for(catalog))


def test_missing_active_club_mapping_is_rejected() -> None:
    catalog = _raw_catalog()
    catalog["stadiums"] = catalog["stadiums"][1:]

    errors = _errors_for(catalog)
    assert "expected 30 MLB stadium records, found 29" in errors
    assert "missing active MLB team keys: ARI" in errors
