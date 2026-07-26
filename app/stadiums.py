from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from copy import deepcopy
from datetime import date, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DATA_PATH = Path(__file__).parent / "data" / "mlb_stadiums.v4.json"
TEAM_ALIASES_PATH = Path(__file__).parent / "data" / "mlb_team_aliases.v1.json"
POLICY_VERSION = "DSE_MLB_STADIUM_METADATA_V1"
CATALOG_VERSION = 4
EVIDENCE_TEXT_NORMALIZATION_VERSION = "DSE_EVIDENCE_TEXT_NORM_V1"
FIELD_EVIDENCE_CONTRACT_VERSION = "DSE_STADIUM_FIELD_EVIDENCE_V2"
EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION = "DSE_EXHAUSTIVE_CLASSIFICATION_V1"
OWNER_ATTESTATION_CONTRACT_VERSION = "DSE_OWNER_ATTESTED_DIRECT_OBSERVATION_V1"
ROOF_TAXONOMY_CONTRACT_VERSION = "DSE_MLB_STADIUM_ROOF_TAXONOMY_V1"

_ROOF_EVIDENCE_KINDS = frozenset(
    {
        "direct_statement",
        "structured_fact",
        "exhaustive_classification",
        "owner_attested_direct_observation",
        "inference_only",
    }
)
_SIMPLE_QUALIFYING_ROOF_EVIDENCE_KINDS = frozenset(
    {"direct_statement", "structured_fact"}
)
_DERIVED_ROOF_EVIDENCE_KINDS = frozenset(
    {"exhaustive_classification", "owner_attested_direct_observation"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_OWNER_IDENTITY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MAX_MINIMAL_EVIDENCE_WORDS = 25
_EXHAUSTIVE_FRAGMENT_ROLES = frozenset(
    {
        "universe_definition",
        "universe_exclusion",
        "initial_contrary_category",
        "completion_members",
        "completion_basis",
        "completion_action",
    }
)
_OWNER_ATTESTATION_ALLOWED_FIELDS = frozenset({"roof_type"})
_OWNER_ATTESTATION_PROHIBITED_FACT_CLASSES = frozenset(
    {
        "game_specific_roof_operational_status",
        "weather",
        "lineup_status",
        "injuries",
        "probable_or_confirmed_pitchers",
        "statistics",
        "market_data",
    }
)
_AUTHORIZED_OWNER_IDENTITIES = frozenset({"OneVillage83"})
_EXHAUSTIVE_SOURCE_ID = "MLB_OUTFIELD_EXHAUSTIVE_ROOF_CLASSIFICATION_2026_V1"
_EXHAUSTIVE_SOURCE_URL = (
    "https://www.mlb.com/news/ranking-every-mlb-outfield-by-fielding-difficulty"
)
_EXHAUSTIVE_SOURCE_RECORD_SHA256 = (
    "e2fd466d017895d7b473d5febb481d2f42509dc14b7733b3f6bb09051d91a0bb"
)
_EXHAUSTIVE_SOURCE_GROUPS = {
    "group_1": (
        ("ARI", "chase-field-phoenix", "Chase Field"),
        ("TEX", "globe-life-field-arlington", "Globe Life Field"),
        ("TOR", "rogers-centre-toronto", "Rogers Centre"),
        ("HOU", "downtown-houston-ballpark-2000", "Daikin Park"),
        ("TB", "tropicana-field-st-petersburg", "Tropicana Field"),
        ("SD", "petco-park-san-diego", "Petco Park"),
    ),
    "group_2": (
        ("CIN", "great-american-ball-park-cincinnati", "Great American Ball Park"),
        ("MIA", "miami-ballpark-2012", "loanDepot park"),
        ("SEA", "t-mobile-park-seattle", "T-Mobile Park"),
        ("LAA", "angel-stadium-anaheim", "Angel Stadium"),
        ("MIN", "target-field-minneapolis", "Target Field"),
        ("MIL", "milwaukee-ballpark-2001", "American Family Field"),
        ("ATL", "truist-park-cobb", "Truist Park"),
        ("STL", "busch-stadium-st-louis-2006", "Busch Stadium"),
    ),
    "group_3": (
        ("CWS", "rate-field-chicago", "Rate Field"),
        ("DET", "comerica-park-detroit", "Comerica Park"),
        ("PIT", "pnc-park-pittsburgh", "PNC Park"),
        ("LAD", "dodger-stadium-los-angeles", "Dodger Stadium"),
    ),
    "group_4": (
        ("BAL", "oriole-park-camden-yards-baltimore", "Oriole Park at Camden Yards"),
        ("NYM", "citi-field-queens", "Citi Field"),
        ("CLE", "progressive-field-cleveland", "Progressive Field"),
        ("NYY", "yankee-stadium-bronx-2009", "Yankee Stadium"),
        ("WSH", "nationals-park-washington", "Nationals Park"),
    ),
    "group_5": (
        ("PHI", "citizens-bank-park-philadelphia", "Citizens Bank Park"),
        ("COL", "coors-field-denver", "Coors Field"),
        ("BOS", "fenway-park-boston", "Fenway Park"),
        ("KC", "kauffman-stadium-kansas-city", "Kauffman Stadium"),
        ("CHC", "wrigley-field-chicago", "Wrigley Field"),
        ("SF", "oracle-park-san-francisco", "Oracle Park"),
    ),
}
_EXHAUSTIVE_EXCLUSION = "sutter-health-park-west-sacramento"
_EXHAUSTIVE_INITIAL_ROOFED = (
    "chase-field-phoenix",
    "globe-life-field-arlington",
    "rogers-centre-toronto",
    "downtown-houston-ballpark-2000",
    "tropicana-field-st-petersburg",
)
_EXHAUSTIVE_REMAINING_ROOFED = (
    "miami-ballpark-2012",
    "t-mobile-park-seattle",
    "milwaukee-ballpark-2001",
)
_EXHAUSTIVE_REMAINING_EVIDENCE = (
    ("Miami", "miami-ballpark-2012"),
    ("Seattle", "t-mobile-park-seattle"),
    ("Milwaukee", "milwaukee-ballpark-2001"),
)
_EXHAUSTIVE_FRAGMENT_EXPECTATIONS = {
    "population_29": ("universe_definition", "29 parks"),
    "ath_excluded": ("universe_exclusion", "they’re not included"),
    "initial_covered": (
        "initial_contrary_category",
        "top five all have covered roofs",
    ),
    "remaining_labels": ("completion_members", "Miami, Seattle, and Milwaukee"),
    "three_remaining": (
        "completion_basis",
        "three remaining parks",
    ),
    "close_roof": ("completion_action", "close the roof"),
}
_OWNER_ATTESTATION_SOURCE_ID = "OWNER_ATTESTATION_SUTTER_ROOF_2026_V1"
_OWNER_ATTESTATION_URL = (
    "internal://project-owner-attestation/"
    "sutter-health-park-west-sacramento/revision-1"
)
_OWNER_ATTESTATION_SOURCE_RECORD_SHA256 = (
    "dad0e622f4a80cfe5b65520a18282b78b8ec0849564777cfeb50b0e37f12dbb4"
)
_OWNER_ATTESTATION_TEXT = (
    "I live in Sacramento and have been to Sutter Health Park multiple times. "
    "There is no roof."
)

EXPECTED_TEAM_KEYS = frozenset(
    {
        "ARI",
        "ATH",
        "ATL",
        "BAL",
        "BOS",
        "CHC",
        "CIN",
        "CLE",
        "COL",
        "CWS",
        "DET",
        "HOU",
        "KC",
        "LAA",
        "LAD",
        "MIA",
        "MIL",
        "MIN",
        "NYM",
        "NYY",
        "PHI",
        "PIT",
        "SD",
        "SEA",
        "SF",
        "STL",
        "TB",
        "TEX",
        "TOR",
        "WSH",
    }
)

MATERIAL_WEATHER_FIELDS = (
    "physical_venue_key",
    "active_club_association",
    "latitude",
    "longitude",
    "timezone",
    "roof_type",
)


class VerificationState(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"


class RoofType(StrEnum):
    OPEN = "open"
    FIXED = "fixed"
    RETRACTABLE = "retractable"
    UNKNOWN = "unknown"


class RoofOperationalStatus(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class VenueResolutionStatus(StrEnum):
    CURRENT = "current"
    HISTORICAL_SAME_VENUE = "historical_same_venue"
    FORMER_PHYSICAL_VENUE = "former_physical_venue"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


class DuplicateJsonKeyError(ValueError):
    """Raised before mapping construction when a JSON object repeats a key."""


def normalize_evidence_text(value: str) -> str:
    """Normalize a minimal factual excerpt for deterministic evidence hashing."""
    normalized = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(normalized.strip().split())


def evidence_text_sha256(value: str) -> str:
    normalized = normalize_evidence_text(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def canonical_evidence_sha256(value: Any) -> str:
    """Hash structured evidence with deterministic UTF-8 JSON serialization."""
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKeyError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _read_json(path: Path) -> Any:
    return json.loads(
        path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_json_keys
    )


def _field_metadata(
    defaults: Mapping[str, Any], overrides: Mapping[str, Any], field: str
) -> dict[str, Any]:
    default_value = defaults.get(field)
    override_value = overrides.get(field)
    metadata: dict[str, Any] = {}
    if isinstance(default_value, Mapping):
        metadata.update(deepcopy(dict(default_value)))
    if isinstance(override_value, Mapping):
        metadata.update(deepcopy(dict(override_value)))
    metadata.setdefault("status", VerificationState.UNKNOWN.value)
    metadata.setdefault("source_ids", [])
    metadata.setdefault("verified_at", None)
    metadata.setdefault("notes", None)
    metadata.setdefault("conflicts", [])
    return metadata


def _expanded_records(catalog: Mapping[str, Any]) -> list[dict[str, Any]]:
    defaults = catalog.get("default_field_verification", {})
    default_mapping = defaults if isinstance(defaults, Mapping) else {}
    raw_records = catalog.get("stadiums", [])
    if not isinstance(raw_records, list):
        return []
    records: list[dict[str, Any]] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            continue
        record = deepcopy(dict(raw))
        raw_overrides = record.get("field_verification", {})
        overrides = raw_overrides if isinstance(raw_overrides, Mapping) else {}
        fields = set(default_mapping) | set(overrides)
        record["field_verification"] = {
            field: _field_metadata(default_mapping, overrides, field)
            for field in sorted(fields)
        }
        record["metadata_policy_version"] = catalog.get("policy_version")
        record["catalog_version"] = catalog.get("catalog_version")
        evidence = record.get("coordinate_evidence")
        if isinstance(evidence, Mapping):
            global_evidence = catalog.get("coordinate_validation")
            expanded_evidence = (
                deepcopy(dict(global_evidence))
                if isinstance(global_evidence, Mapping)
                else {}
            )
            expanded_evidence.update(deepcopy(dict(evidence)))
            osm_type = expanded_evidence.get("osm_type")
            osm_id = expanded_evidence.get("osm_id")
            if isinstance(osm_type, str) and isinstance(osm_id, int):
                expanded_evidence["object_url"] = (
                    f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
                )
            record["coordinate_evidence"] = expanded_evidence
            for field in ("latitude", "longitude"):
                metadata = record["field_verification"].get(field)
                if isinstance(metadata, dict):
                    metadata["evidence"] = deepcopy(expanded_evidence)
        # Compatibility aliases used by the Phase 1 pipeline and export contract.
        record["venue"] = record.get("current_display_name")
        states = [
            field_verification(record, field).get("status")
            for field in MATERIAL_WEATHER_FIELDS
        ]
        record["metadata_status"] = (
            "verified"
            if all(state == VerificationState.VERIFIED.value for state in states)
            else "unverified"
        )
        records.append(record)
    return records


@lru_cache(maxsize=1)
def load_stadium_catalog() -> dict[str, Any]:
    raw = _read_json(DATA_PATH)
    if not isinstance(raw, dict):
        raise ValueError("stadium catalog must be a JSON object")
    errors = validate_stadium_catalog_data(raw, _read_json(TEAM_ALIASES_PATH))
    if errors:
        raise ValueError("invalid stadium catalog: " + "; ".join(errors))
    catalog = deepcopy(raw)
    catalog["stadiums"] = _expanded_records(catalog)
    return catalog


@lru_cache(maxsize=1)
def load_stadiums() -> dict[str, dict[str, Any]]:
    records = load_stadium_catalog()["stadiums"]
    return {str(record["team_key"]): record for record in records}


def stadium_for_team(team_key: str | None) -> dict[str, Any] | None:
    return load_stadiums().get(team_key or "")


def field_verification(record: Mapping[str, Any], field: str) -> dict[str, Any]:
    all_metadata = record.get("field_verification")
    if isinstance(all_metadata, Mapping):
        metadata = all_metadata.get(field)
        if isinstance(metadata, Mapping):
            return dict(metadata)
    return {
        "status": VerificationState.UNKNOWN.value,
        "source_ids": [],
        "verified_at": None,
        "notes": "missing field verification",
        "conflicts": [],
    }


def is_field_verified(record: Mapping[str, Any], field: str) -> bool:
    metadata = field_verification(record, field)
    return (
        metadata.get("status") == VerificationState.VERIFIED.value
        and not metadata.get("conflicts")
    )


def verified_outfield_bearing(record: Mapping[str, Any]) -> float | None:
    if not is_field_verified(record, "outfield_bearing_degrees"):
        return None
    value = record.get("outfield_bearing_degrees")
    if not _is_finite_number(value):
        return None
    bearing = float(cast(int | float, value))
    return bearing if 0.0 <= bearing < 360.0 else None


def roof_operational_status(record: Mapping[str, Any]) -> RoofOperationalStatus:
    if not is_field_verified(record, "roof_type"):
        return RoofOperationalStatus.UNKNOWN
    roof = record.get("roof_type")
    if roof == RoofType.OPEN.value:
        return RoofOperationalStatus.NOT_APPLICABLE
    if roof == RoofType.FIXED.value:
        return RoofOperationalStatus.CLOSED
    return RoofOperationalStatus.UNKNOWN


def material_weather_metadata_errors(record: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for field in MATERIAL_WEATHER_FIELDS:
        if not _material_value_valid(record, field):
            errors.append(f"venue_metadata_invalid:{field}")
            continue
        metadata = field_verification(record, field)
        if metadata.get("conflicts"):
            errors.append(f"venue_metadata_conflict:{field}")
            continue
        state = str(metadata.get("status", VerificationState.UNKNOWN.value)).lower()
        if state != VerificationState.VERIFIED.value.lower():
            errors.append(f"venue_metadata_{state}:{field}")
    return errors


def _material_value_valid(record: Mapping[str, Any], field: str) -> bool:
    value = record.get(field)
    if field == "physical_venue_key":
        return isinstance(value, str) and bool(value) and value == value.strip()
    if field == "active_club_association":
        return value is True
    if field == "latitude":
        return _is_finite_number(value) and -90.0 <= float(cast(int | float, value)) <= 90.0
    if field == "longitude":
        return _is_finite_number(value) and -180.0 <= float(cast(int | float, value)) <= 180.0
    if field == "timezone":
        if not isinstance(value, str) or not value:
            return False
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return False
        return True
    if field == "roof_type":
        return value in {roof.value for roof in RoofType}
    return False


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _names(record: Mapping[str, Any], field: str) -> Iterable[str]:
    value = record.get(field)
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, str))


def resolve_venue_alias(
    venue_name: str | None, *, team_key: str | None = None
) -> dict[str, Any]:
    """Resolve an explicitly listed venue name without fuzzy matching."""
    if not venue_name:
        return {"status": VenueResolutionStatus.UNKNOWN.value, "venue_name": venue_name}
    matches: list[dict[str, Any]] = []
    unverified_former_matches: list[dict[str, Any]] = []
    for record in load_stadiums().values():
        if team_key is not None and record.get("team_key") != team_key:
            continue
        current = {
            record.get("base_venue_name"),
            record.get("current_display_name"),
            *_names(record, "aliases"),
        }
        if venue_name in current:
            matches.append(
                {
                    "status": VenueResolutionStatus.CURRENT.value,
                    "team_key": record["team_key"],
                    "physical_venue_key": record["physical_venue_key"],
                    "venue_name": venue_name,
                }
            )
            continue
        if venue_name in set(_names(record, "historical_display_names")):
            matches.append(
                {
                    "status": VenueResolutionStatus.HISTORICAL_SAME_VENUE.value,
                    "team_key": record["team_key"],
                    "physical_venue_key": record["physical_venue_key"],
                    "venue_name": venue_name,
                }
            )
            continue
        former = record.get("former_physical_venues", [])
        if isinstance(former, list):
            for old_venue in former:
                if not isinstance(old_venue, Mapping):
                    continue
                old_names = old_venue.get("names", [])
                if isinstance(old_names, list) and venue_name in old_names:
                    resolved = {
                        "status": VenueResolutionStatus.FORMER_PHYSICAL_VENUE.value,
                        "team_key": record["team_key"],
                        "physical_venue_key": old_venue.get("physical_venue_key"),
                        "venue_name": venue_name,
                        "verification_status": old_venue.get("status"),
                    }
                    if old_venue.get("status") == VerificationState.VERIFIED.value:
                        matches.append(resolved)
                    else:
                        unverified_former_matches.append(resolved)
    if not matches:
        if unverified_former_matches:
            return {
                "status": VenueResolutionStatus.UNKNOWN.value,
                "venue_name": venue_name,
                "reason": "unverified_former_physical_venue",
                "matches": unverified_former_matches,
            }
        return {"status": VenueResolutionStatus.UNKNOWN.value, "venue_name": venue_name}
    identities = {
        (match.get("team_key"), match.get("physical_venue_key")) for match in matches
    }
    if len(identities) > 1:
        return {
            "status": VenueResolutionStatus.AMBIGUOUS.value,
            "venue_name": venue_name,
            "matches": matches,
        }
    return matches[0]


def _append_duplicate_errors(
    errors: list[str], values: Iterable[Any], label: str
) -> None:
    seen: set[Any] = set()
    duplicates: set[Any] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    for value in sorted(duplicates, key=str):
        errors.append(f"duplicate {label}: {value}")


def _validate_verification(
    errors: list[str],
    record: Mapping[str, Any],
    field: str,
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    source_ids = set(sources_by_id)
    metadata = field_verification(record, field)
    status = metadata.get("status")
    if status not in {state.value for state in VerificationState}:
        errors.append(f"{record.get('team_key')}: {field} has unsupported verification status")
        return
    sources = metadata.get("source_ids")
    if not isinstance(sources, list) or not all(isinstance(item, str) for item in sources):
        errors.append(f"{record.get('team_key')}: {field} source_ids must be strings")
        return
    unknown_sources = set(sources) - source_ids
    if unknown_sources:
        errors.append(
            f"{record.get('team_key')}: {field} references unknown sources: "
            + ", ".join(sorted(unknown_sources))
        )
    conflicts = metadata.get("conflicts")
    if not isinstance(conflicts, list):
        errors.append(f"{record.get('team_key')}: {field} conflicts must be a list")
        return
    if status == VerificationState.VERIFIED.value and not sources:
        errors.append(f"{record.get('team_key')}: {field} is VERIFIED without provenance")
    if status == VerificationState.VERIFIED.value and conflicts:
        errors.append(f"{record.get('team_key')}: {field} has conflicts but is VERIFIED")
    if status == VerificationState.VERIFIED.value and _iso_date_key(metadata.get("verified_at")) is None:
        errors.append(f"{record.get('team_key')}: {field} is VERIFIED without valid verified_at")
    _validate_derived_evidence_field_scope(
        errors,
        record,
        field,
        metadata,
        sources_by_id,
    )
    if field == "roof_type":
        _validate_roof_evidence_consistency(
            errors,
            record,
            metadata,
            sources_by_id,
        )
    unknown_sentinel_allowed = field == "roof_type" and record.get(field) == RoofType.UNKNOWN.value
    if (
        status == VerificationState.UNKNOWN.value
        and record.get(field) is not None
        and not unknown_sentinel_allowed
    ):
        errors.append(f"{record.get('team_key')}: {field} is UNKNOWN but has a value")
    for conflict in conflicts:
        if not isinstance(conflict, Mapping):
            errors.append(f"{record.get('team_key')}: {field} conflict must be an object")
            continue
        if not conflict.get("source_id") or "observed_value" not in conflict:
            errors.append(
                f"{record.get('team_key')}: {field} conflict lacks source_id or observed_value"
            )
        elif conflict.get("source_id") not in source_ids:
            errors.append(
                f"{record.get('team_key')}: {field} conflict references unknown source: "
                f"{conflict.get('source_id')}"
            )


def _derived_evidence_field(evidence: Mapping[str, Any]) -> Any:
    kind = evidence.get("kind")
    if kind == "exhaustive_classification":
        claim = evidence.get("exhaustive_classification")
        return claim.get("field") if isinstance(claim, Mapping) else None
    if kind == "owner_attested_direct_observation":
        claim = evidence.get("owner_attestation")
        return claim.get("attested_field") if isinstance(claim, Mapping) else None
    return None


def _validate_derived_evidence_field_scope(
    errors: list[str],
    record: Mapping[str, Any],
    field: str,
    metadata: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    for source_id in metadata.get("source_ids", []):
        source = sources_by_id.get(str(source_id), {})
        evidence = source.get("roof_evidence")
        if not isinstance(evidence, Mapping):
            continue
        kind = evidence.get("kind")
        if kind not in _DERIVED_ROOF_EVIDENCE_KINDS:
            continue
        evidence_field = _derived_evidence_field(evidence)
        if evidence_field != field:
            errors.append(
                f"{record.get('team_key')}: {field} cannot use {kind} evidence "
                f"scoped to {evidence_field}"
            )


def _derived_subject_value(
    evidence: Mapping[str, Any], record: Mapping[str, Any]
) -> Any:
    kind = evidence.get("kind")
    if kind in _SIMPLE_QUALIFYING_ROOF_EVIDENCE_KINDS:
        return evidence.get("observed_value")
    if kind == "exhaustive_classification":
        claim = evidence.get("exhaustive_classification")
        if not isinstance(claim, Mapping):
            return None
        derivations = claim.get("subject_derivations")
        if not isinstance(derivations, list):
            return None
        matching = [
            item
            for item in derivations
            if isinstance(item, Mapping)
            and item.get("physical_venue_key") == record.get("physical_venue_key")
            and item.get("team_key") == record.get("team_key")
        ]
        if len(matching) != 1:
            return None
        return matching[0].get("observed_derived_value")
    if kind == "owner_attested_direct_observation":
        claim = evidence.get("owner_attestation")
        if not isinstance(claim, Mapping):
            return None
        if (
            claim.get("physical_venue_key") != record.get("physical_venue_key")
            or claim.get("team_key") != record.get("team_key")
        ):
            return None
        return claim.get("observed_value")
    return None


def _validate_roof_evidence_consistency(
    errors: list[str],
    record: Mapping[str, Any],
    metadata: Mapping[str, Any],
    sources_by_id: Mapping[str, Mapping[str, Any]],
) -> None:
    is_verified = metadata.get("status") == VerificationState.VERIFIED.value
    roof_type = record.get("roof_type")
    qualifying = False
    for source_id in metadata.get("source_ids", []):
        source = sources_by_id.get(str(source_id), {})
        evidence = source.get("roof_evidence")
        if not isinstance(evidence, Mapping):
            continue
        observed = _derived_subject_value(evidence, record)
        if observed == roof_type:
            qualifying = True
        if (
            evidence.get("kind") != "inference_only"
            and observed in {item.value for item in RoofType}
            and observed != roof_type
        ):
            conflicts = metadata.get("conflicts", [])
            has_conflict_record = any(
                isinstance(conflict, Mapping)
                and conflict.get("source_id") == source_id
                and conflict.get("observed_value") == observed
                for conflict in conflicts
            )
            if not has_conflict_record:
                errors.append(
                    f"{record.get('team_key')}: roof_type source {source_id} mismatch "
                    "is missing a conflict record"
                )
            if is_verified:
                errors.append(
                    f"{record.get('team_key')}: roof_type source {source_id} conflicts "
                    "with stored value"
                )
        if (
            evidence.get("kind") in _DERIVED_ROOF_EVIDENCE_KINDS
            and observed is None
            and source_id in metadata.get("source_ids", [])
        ):
            errors.append(
                f"{record.get('team_key')}: roof_type source {source_id} has no "
                "venue-bound derived claim"
            )
    if is_verified and not qualifying:
        errors.append(
            f"{record.get('team_key')}: roof_type is VERIFIED without matching qualifying evidence"
        )


def _aware_iso_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _validate_minimal_evidence_text(
    errors: list[str], prefix: str, text: Any, checksum: Any
) -> None:
    if not isinstance(text, str) or not normalize_evidence_text(text):
        errors.append(f"{prefix}: minimal roof evidence text is required")
        return
    if text != normalize_evidence_text(text):
        errors.append(f"{prefix}: minimal roof evidence text is not normalized")
    if len(text.split()) > _MAX_MINIMAL_EVIDENCE_WORDS:
        errors.append(f"{prefix}: minimal roof evidence text exceeds 25 words")
    if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
        errors.append(f"{prefix}: roof evidence checksum must be lowercase SHA-256")
    elif checksum != evidence_text_sha256(text):
        errors.append(f"{prefix}: roof evidence checksum mismatch")


def _validate_structured_evidence_checksum(
    errors: list[str], prefix: str, value: Mapping[str, Any]
) -> None:
    checksum = value.get("structured_evidence_sha256")
    if not isinstance(checksum, str) or not _SHA256_RE.fullmatch(checksum):
        errors.append(f"{prefix}: structured evidence checksum must be lowercase SHA-256")
        return
    payload = {str(key): item for key, item in value.items() if key != "structured_evidence_sha256"}
    if checksum != canonical_evidence_sha256(payload):
        errors.append(f"{prefix}: structured evidence checksum mismatch")


def _require_exact_keys(
    errors: list[str], prefix: str, value: Mapping[str, Any], expected: set[str]
) -> None:
    actual = {str(key) for key in value}
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        errors.append(f"{prefix}: schema keys mismatch; missing={missing}, extra={extra}")


def _string_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return value


def _validate_exhaustive_classification(
    errors: list[str],
    index: int,
    source: Mapping[str, Any],
    evidence: Mapping[str, Any],
    stadium_records: list[dict[str, Any]],
) -> None:
    prefix = f"source {index}: exhaustive classification"
    claim = evidence.get("exhaustive_classification")
    if not isinstance(claim, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    _require_exact_keys(
        errors,
        prefix,
        claim,
        {
            "contract_version",
            "taxonomy_contract_version",
            "field",
            "authoritative_first_party",
            "universe",
            "contrary_category",
            "evidence_fragments",
            "subject_derivations",
            "structured_evidence_sha256",
        },
    )
    if claim.get("contract_version") != EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION:
        errors.append(f"{prefix} has unsupported derivation contract")
    if claim.get("taxonomy_contract_version") != ROOF_TAXONOMY_CONTRACT_VERSION:
        errors.append(f"{prefix} has unsupported roof taxonomy contract")
    if claim.get("field") != "roof_type":
        errors.append(f"{prefix} may only classify roof_type")
    if claim.get("authoritative_first_party") is not True:
        errors.append(f"{prefix} must be authoritative and first-party")
    host = (urlparse(str(source.get("url", ""))).hostname or "").lower()
    if source.get("publisher") != "Major League Baseball" or host not in {
        "mlb.com",
        "www.mlb.com",
    }:
        errors.append(f"{prefix} source must be first-party Major League Baseball")
    if (
        source.get("id") != _EXHAUSTIVE_SOURCE_ID
        or source.get("url") != _EXHAUSTIVE_SOURCE_URL
    ):
        errors.append(f"{prefix} source identity does not match the reviewed revision")
    if canonical_evidence_sha256(dict(source)) != _EXHAUSTIVE_SOURCE_RECORD_SHA256:
        errors.append(f"{prefix} source record does not match the reviewed revision")

    fragments = claim.get("evidence_fragments")
    fragments_by_id: dict[str, Mapping[str, Any]] = {}
    roles: set[str] = set()
    if not isinstance(fragments, list):
        errors.append(f"{prefix} evidence_fragments must be a list")
        fragments = []
    for fragment_index, fragment in enumerate(fragments):
        fragment_prefix = f"{prefix} fragment {fragment_index}"
        if not isinstance(fragment, Mapping):
            errors.append(f"{fragment_prefix} must be an object")
            continue
        _require_exact_keys(
            errors,
            fragment_prefix,
            fragment,
            {"id", "role", "locator", "minimal_evidence_text", "normalized_text_sha256"},
        )
        fragment_id = fragment.get("id")
        role = fragment.get("role")
        if not isinstance(fragment_id, str) or not fragment_id.strip():
            errors.append(f"{fragment_prefix} id is required")
        elif fragment_id in fragments_by_id:
            errors.append(f"{prefix} has duplicate evidence fragment id {fragment_id}")
        else:
            fragments_by_id[fragment_id] = fragment
        if role not in _EXHAUSTIVE_FRAGMENT_ROLES:
            errors.append(f"{fragment_prefix} has unsupported role")
        else:
            roles.add(str(role))
        if not isinstance(fragment.get("locator"), str) or not str(
            fragment.get("locator", "")
        ).strip():
            errors.append(f"{fragment_prefix} locator is required")
        _validate_minimal_evidence_text(
            errors,
            fragment_prefix,
            fragment.get("minimal_evidence_text"),
            fragment.get("normalized_text_sha256"),
        )
    if roles != _EXHAUSTIVE_FRAGMENT_ROLES:
        errors.append(f"{prefix} must preserve every required evidence-fragment role")
    observed_fragments = {
        fragment_id: (
            str(fragment.get("role")),
            str(fragment.get("minimal_evidence_text")),
        )
        for fragment_id, fragment in fragments_by_id.items()
    }
    if observed_fragments != _EXHAUSTIVE_FRAGMENT_EXPECTATIONS:
        errors.append(f"{prefix} source fragments do not match the reviewed revision")

    universe = claim.get("universe")
    population_keys: list[str] = []
    group_members: dict[str, list[Mapping[str, Any]]] = {}
    observed_source_groups: dict[str, tuple[tuple[str, str, str], ...]] = {}
    if not isinstance(universe, Mapping):
        errors.append(f"{prefix} universe must be an object")
    else:
        _require_exact_keys(
            errors,
            f"{prefix} universe",
            universe,
            {
                "source_id",
                "definition",
                "declared_size",
                "group_count",
                "groups",
                "population_members_sha256",
                "definition_fragment_id",
                "explicit_exclusions",
            },
        )
        if universe.get("source_id") != source.get("id"):
            errors.append(f"{prefix} universe source_id must identify this source")
        if not isinstance(universe.get("definition"), str) or not str(
            universe.get("definition", "")
        ).strip():
            errors.append(f"{prefix} universe definition is required")
        definition_fragment = fragments_by_id.get(str(universe.get("definition_fragment_id")))
        if not definition_fragment or definition_fragment.get("role") != "universe_definition":
            errors.append(f"{prefix} universe definition evidence is missing")
        groups = universe.get("groups")
        if not isinstance(groups, list) or not groups:
            errors.append(f"{prefix} universe groups must be a non-empty list")
            groups = []
        group_ids: set[str] = set()
        group_orders: set[int] = set()
        population_teams: set[str] = set()
        for group_index, group in enumerate(groups):
            group_prefix = f"{prefix} universe group {group_index}"
            if not isinstance(group, Mapping):
                errors.append(f"{group_prefix} must be an object")
                continue
            _require_exact_keys(
                errors,
                group_prefix,
                group,
                {"group_id", "order", "locator", "members", "members_sha256"},
            )
            group_id = group.get("group_id")
            order = group.get("order")
            if not isinstance(group_id, str) or not group_id.strip() or group_id in group_ids:
                errors.append(f"{group_prefix} group_id must be unique and non-empty")
                continue
            group_ids.add(group_id)
            if not isinstance(order, int) or isinstance(order, bool) or order < 1 or order in group_orders:
                errors.append(f"{group_prefix} order must be a unique positive integer")
            else:
                group_orders.add(order)
            if not isinstance(group.get("locator"), str) or not str(
                group.get("locator", "")
            ).strip():
                errors.append(f"{group_prefix} locator is required")
            members = group.get("members")
            if not isinstance(members, list) or not members:
                errors.append(f"{group_prefix} members must be a non-empty list")
                members = []
            valid_members: list[Mapping[str, Any]] = []
            for member_index, member in enumerate(members):
                member_prefix = f"{group_prefix} member {member_index}"
                if not isinstance(member, Mapping):
                    errors.append(f"{member_prefix} must be an object")
                    continue
                _require_exact_keys(
                    errors,
                    member_prefix,
                    member,
                    {"team_key", "physical_venue_key", "source_venue_name"},
                )
                team_key = member.get("team_key")
                physical_key = member.get("physical_venue_key")
                source_name = member.get("source_venue_name")
                if not all(
                    isinstance(item, str) and item.strip()
                    for item in (team_key, physical_key, source_name)
                ):
                    errors.append(f"{member_prefix} identity fields are required")
                    continue
                if str(team_key) in population_teams or str(physical_key) in population_keys:
                    errors.append(f"{prefix} universe membership is duplicated or ambiguous")
                population_teams.add(str(team_key))
                population_keys.append(str(physical_key))
                valid_members.append(member)
            group_members[group_id] = valid_members
            observed_source_groups[group_id] = tuple(
                (
                    str(item.get("team_key")),
                    str(item.get("physical_venue_key")),
                    str(item.get("source_venue_name")),
                )
                for item in valid_members
            )
            if group.get("members_sha256") != canonical_evidence_sha256(valid_members):
                errors.append(f"{group_prefix} membership checksum mismatch")
        declared_size = universe.get("declared_size")
        group_count = universe.get("group_count")
        if declared_size != len(population_keys):
            errors.append(f"{prefix} universe declared_size does not match its members")
        if group_count != len(group_members):
            errors.append(f"{prefix} universe group_count does not match its groups")
        if group_orders and group_orders != set(range(1, len(group_members) + 1)):
            errors.append(f"{prefix} universe group order is incomplete")
        if universe.get("population_members_sha256") != canonical_evidence_sha256(
            population_keys
        ):
            errors.append(f"{prefix} universe population checksum mismatch")
        if (
            universe.get("declared_size") != 29
            or universe.get("group_count") != 5
            or observed_source_groups != _EXHAUSTIVE_SOURCE_GROUPS
        ):
            errors.append(f"{prefix} universe does not match the reviewed 29-park source")
        exclusions = universe.get("explicit_exclusions")
        if not isinstance(exclusions, list) or not exclusions:
            errors.append(f"{prefix} universe must preserve explicit exclusions")
            exclusions = []
        exclusion_keys: list[str] = []
        for exclusion_index, exclusion in enumerate(exclusions):
            exclusion_prefix = f"{prefix} universe exclusion {exclusion_index}"
            if not isinstance(exclusion, Mapping):
                errors.append(f"{exclusion_prefix} must be an object")
                continue
            _require_exact_keys(
                errors,
                exclusion_prefix,
                exclusion,
                {"physical_venue_key", "reason", "evidence_fragment_id"},
            )
            if exclusion.get("physical_venue_key") in population_keys:
                errors.append(f"{exclusion_prefix} cannot also be a universe member")
            exclusion_keys.append(str(exclusion.get("physical_venue_key")))
            fragment = fragments_by_id.get(str(exclusion.get("evidence_fragment_id")))
            if not fragment or fragment.get("role") != "universe_exclusion":
                errors.append(f"{exclusion_prefix} evidence is missing")
            if not isinstance(exclusion.get("reason"), str) or not str(
                exclusion.get("reason", "")
            ).strip():
                errors.append(f"{exclusion_prefix} reason is required")
        if exclusion_keys != [_EXHAUSTIVE_EXCLUSION] or any(
            exclusion.get("evidence_fragment_id") != "ath_excluded"
            for exclusion in exclusions
            if isinstance(exclusion, Mapping)
        ):
            errors.append(f"{prefix} explicit exclusion must be the reviewed Sutter record")

        catalog_pairs = {
            (str(record.get("team_key")), str(record.get("physical_venue_key")))
            for record in stadium_records
        }
        catalog_physical_keys = {physical_key for _team_key, physical_key in catalog_pairs}
        observed_coverage = set(population_keys) | set(exclusion_keys)
        expected_pairs = {
            (team_key, physical_key)
            for members in _EXHAUSTIVE_SOURCE_GROUPS.values()
            for team_key, physical_key, _source_name in members
        } | {("ATH", _EXHAUSTIVE_EXCLUSION)}
        if catalog_pairs != expected_pairs or observed_coverage != catalog_physical_keys:
            errors.append(f"{prefix} universe and exclusion do not cover the active catalog")
        records_by_physical = {
            str(record.get("physical_venue_key")): record for record in stadium_records
        }
        for members in _EXHAUSTIVE_SOURCE_GROUPS.values():
            for team_key, physical_key, source_name in members:
                record = records_by_physical.get(physical_key, {})
                accepted_names = {
                    str(record.get("base_venue_name")),
                    str(record.get("current_display_name")),
                }
                accepted_names.update(
                    str(item) for item in record.get("aliases", []) if isinstance(item, str)
                )
                accepted_names.update(
                    str(item)
                    for item in record.get("historical_display_names", [])
                    if isinstance(item, str)
                )
                if record.get("team_key") != team_key or source_name not in accepted_names:
                    errors.append(
                        f"{prefix} source membership does not match active physical identity"
                    )

    category = claim.get("contrary_category")
    category_members: list[str] = []
    category_checksum: Any = None
    if not isinstance(category, Mapping):
        errors.append(f"{prefix} contrary_category must be an object")
    else:
        _require_exact_keys(
            errors,
            f"{prefix} contrary category",
            category,
            {
                "definition",
                "members",
                "members_sha256",
                "initial_group_id",
                "initial_member_count",
                "initial_members",
                "remaining_members",
                "remaining_group_id",
                "remaining_member_count",
                "remaining_member_evidence",
                "remaining_member_evidence_sha256",
                "fixed_roof_exception_member",
                "completion_semantics",
                "evidence_fragment_ids",
            },
        )
        if not isinstance(category.get("definition"), str) or not str(
            category.get("definition", "")
        ).strip():
            errors.append(f"{prefix} contrary category definition is required")
        parsed_members = _string_list(category.get("members"))
        initial_members = _string_list(category.get("initial_members"))
        remaining_members = _string_list(category.get("remaining_members"))
        if parsed_members is None or not parsed_members or len(set(parsed_members)) != len(
            parsed_members
        ):
            errors.append(f"{prefix} contrary category members must be unique strings")
        else:
            category_members = parsed_members
        if initial_members is None or remaining_members is None:
            errors.append(f"{prefix} contrary category partitions must be string lists")
            initial_members = []
            remaining_members = []
        if category_members != initial_members + remaining_members or set(initial_members) & set(
            remaining_members
        ):
            errors.append(f"{prefix} contrary category partitions are incomplete or ambiguous")
        expected_category_members = list(
            _EXHAUSTIVE_INITIAL_ROOFED + _EXHAUSTIVE_REMAINING_ROOFED
        )
        if (
            category_members != expected_category_members
            or initial_members != list(_EXHAUSTIVE_INITIAL_ROOFED)
            or remaining_members != list(_EXHAUSTIVE_REMAINING_ROOFED)
        ):
            errors.append(f"{prefix} contrary category does not match the reviewed roofed set")
        if not set(category_members) <= set(population_keys):
            errors.append(f"{prefix} contrary category must be contained in the universe")
        category_checksum = canonical_evidence_sha256(category_members)
        if category.get("members_sha256") != category_checksum:
            errors.append(f"{prefix} contrary category membership checksum mismatch")
        initial_group_id = category.get("initial_group_id")
        initial_count = category.get("initial_member_count")
        initial_group = group_members.get(str(initial_group_id), [])
        if not isinstance(initial_count, int) or isinstance(initial_count, bool) or initial_count < 1:
            errors.append(f"{prefix} contrary category initial_member_count is invalid")
        elif initial_members != [
            str(item.get("physical_venue_key")) for item in initial_group[:initial_count]
        ]:
            errors.append(f"{prefix} initial contrary members do not match the source group")
        if category.get("fixed_roof_exception_member") not in initial_members:
            errors.append(f"{prefix} fixed-roof exception must be an initial contrary member")
        if category.get("fixed_roof_exception_member") != "tropicana-field-st-petersburg":
            errors.append(f"{prefix} fixed-roof exception does not match the reviewed source")
        remaining_group_id = category.get("remaining_group_id")
        remaining_evidence = category.get("remaining_member_evidence")
        expected_remaining_evidence = [
            {"source_label": label, "physical_venue_key": physical_key}
            for label, physical_key in _EXHAUSTIVE_REMAINING_EVIDENCE
        ]
        if (
            remaining_group_id != "group_2"
            or category.get("remaining_member_count") != 3
            or remaining_evidence != expected_remaining_evidence
            or category.get("remaining_member_evidence_sha256")
            != canonical_evidence_sha256(expected_remaining_evidence)
        ):
            errors.append(f"{prefix} remaining roofed membership evidence is incomplete")
        remaining_group_keys = {
            str(item.get("physical_venue_key"))
            for item in group_members.get(str(remaining_group_id), [])
        }
        if not set(_EXHAUSTIVE_REMAINING_ROOFED) <= remaining_group_keys:
            errors.append(f"{prefix} remaining roofed members are not source-group members")
        if category.get("completion_semantics") != "remaining":
            errors.append(f"{prefix} lacks an explicit remaining/completion basis")
        fragment_ids = category.get("evidence_fragment_ids")
        expected_fragment_roles = {
            "initial_membership": "initial_contrary_category",
            "completion_members": "completion_members",
            "completion_basis": "completion_basis",
            "completion_action": "completion_action",
        }
        if not isinstance(fragment_ids, Mapping) or {
            str(key) for key in fragment_ids
        } != set(expected_fragment_roles):
            errors.append(f"{prefix} contrary category evidence references are incomplete")
        else:
            for key, role in expected_fragment_roles.items():
                fragment = fragments_by_id.get(str(fragment_ids.get(key)))
                if not fragment or fragment.get("role") != role:
                    errors.append(f"{prefix} contrary category {key} evidence is missing")
            completion_fragment = fragments_by_id.get(str(fragment_ids.get("completion_basis")))
            member_fragment = fragments_by_id.get(str(fragment_ids.get("completion_members")))
            completion_text = (
                str(completion_fragment.get("minimal_evidence_text", "")).lower()
                if completion_fragment
                else ""
            )
            member_text = (
                str(member_fragment.get("minimal_evidence_text", ""))
                if member_fragment
                else ""
            )
            if "remaining" not in completion_text:
                errors.append(f"{prefix} completion evidence does not establish remaining status")
            if any(
                label.casefold() not in member_text.casefold()
                for label, _physical_key in _EXHAUSTIVE_REMAINING_EVIDENCE
            ):
                errors.append(f"{prefix} completion evidence does not name every remaining member")

    derivations = claim.get("subject_derivations")
    if not isinstance(derivations, list) or not derivations:
        errors.append(f"{prefix} subject_derivations must be a non-empty list")
        derivations = []
    derived_subjects: set[str] = set()
    derived_values: set[str] = set()
    for derivation_index, derivation in enumerate(derivations):
        derivation_prefix = f"{prefix} subject derivation {derivation_index}"
        if not isinstance(derivation, Mapping):
            errors.append(f"{derivation_prefix} must be an object")
            continue
        _require_exact_keys(
            errors,
            derivation_prefix,
            derivation,
            {
                "team_key",
                "physical_venue_key",
                "universe_group_id",
                "universe_group_members_sha256",
                "subject_membership",
                "contrary_category_membership",
                "contrary_category_members_sha256",
                "operation",
                "set_expression",
                "observed_derived_value",
                "authoritative_conflict_found",
            },
        )
        physical_key = derivation.get("physical_venue_key")
        team_key = derivation.get("team_key")
        group_id = str(derivation.get("universe_group_id"))
        if not isinstance(physical_key, str) or physical_key in derived_subjects:
            errors.append(f"{derivation_prefix} physical venue must be unique")
            continue
        derived_subjects.add(physical_key)
        group = group_members.get(group_id, [])
        matching = [
            item
            for item in group
            if item.get("physical_venue_key") == physical_key
            and item.get("team_key") == team_key
        ]
        if len(matching) != 1 or derivation.get("subject_membership") != "explicit_group_member":
            errors.append(f"{derivation_prefix} lacks explicit universe membership")
        if derivation.get("universe_group_members_sha256") != canonical_evidence_sha256(group):
            errors.append(f"{derivation_prefix} universe membership checksum mismatch")
        if physical_key in category_members or derivation.get(
            "contrary_category_membership"
        ) != "not_in_exhaustively_enumerated_category":
            errors.append(f"{derivation_prefix} contrary-category membership is ambiguous")
        if derivation.get("contrary_category_members_sha256") != category_checksum:
            errors.append(f"{derivation_prefix} contrary-category checksum mismatch")
        if (
            derivation.get("operation") != "set_complement"
            or derivation.get("set_expression") != "U\\R"
            or derivation.get("observed_derived_value") != RoofType.OPEN.value
        ):
            errors.append(f"{derivation_prefix} exclusion derivation is invalid")
        else:
            derived_values.add(str(derivation.get("observed_derived_value")))
        if derivation.get("authoritative_conflict_found") is not False:
            errors.append(f"{derivation_prefix} has an authoritative conflict")

    expected_derived_subjects = {
        "truist-park-cobb",
        "great-american-ball-park-cincinnati",
        "rate-field-chicago",
        "angel-stadium-anaheim",
        "citizens-bank-park-philadelphia",
        "pnc-park-pittsburgh",
    }
    if derived_subjects != expected_derived_subjects:
        errors.append(f"{prefix} subject derivations do not match the reviewed venue set")
    if len(derived_values) != 1 or evidence.get("observed_value") not in derived_values:
        errors.append(f"{prefix} outer observed value does not match its derivations")

    _validate_structured_evidence_checksum(errors, prefix, claim)


def _validate_owner_attestation(
    errors: list[str],
    index: int,
    source: Mapping[str, Any],
    evidence: Mapping[str, Any],
    evidence_contract: Mapping[str, Any],
) -> None:
    prefix = f"source {index}: owner attestation"
    claim = evidence.get("owner_attestation")
    if not isinstance(claim, Mapping):
        errors.append(f"{prefix} must be an object")
        return
    _require_exact_keys(
        errors,
        prefix,
        claim,
        {
            "contract_version",
            "evidence_revision",
            "attestor_identity",
            "attestor_role",
            "attestation_recorded_at",
            "team_key",
            "physical_venue_key",
            "attested_field",
            "fact_class",
            "exact_attestation_text",
            "normalized_text_sha256",
            "observed_value",
            "commercialization_boundary_acknowledged",
            "notes",
            "structured_evidence_sha256",
        },
    )
    if claim.get("contract_version") != OWNER_ATTESTATION_CONTRACT_VERSION:
        errors.append(f"{prefix} has unsupported contract version")
    if claim.get("evidence_revision") != evidence.get("revision") or evidence.get(
        "revision"
    ) != 1:
        errors.append(f"{prefix} revision must match the roof evidence revision")
    if (
        source.get("id") != _OWNER_ATTESTATION_SOURCE_ID
        or source.get("url") != _OWNER_ATTESTATION_URL
        or source.get("publisher") != "Daily MLB project owner"
    ):
        errors.append(f"{prefix} source identity does not match the reviewed attestation")
    if canonical_evidence_sha256(dict(source)) != _OWNER_ATTESTATION_SOURCE_RECORD_SHA256:
        errors.append(f"{prefix} source record does not match the reviewed attestation")
    owner_policy = evidence_contract.get("owner_attestation_policy")
    owner_policy = owner_policy if isinstance(owner_policy, Mapping) else {}
    authorized = _string_list(owner_policy.get("authorized_owner_identities")) or []
    identity = claim.get("attestor_identity")
    if (
        not isinstance(identity, str)
        or not _OWNER_IDENTITY_RE.fullmatch(identity)
        or identity not in authorized
        or identity not in _AUTHORIZED_OWNER_IDENTITIES
    ):
        errors.append(f"{prefix} attestor identity is not authorized")
    if claim.get("attestor_role") != "project_owner":
        errors.append(f"{prefix} attestor role must be project_owner")
    if claim.get("attested_field") not in _OWNER_ATTESTATION_ALLOWED_FIELDS:
        errors.append(f"{prefix} is restricted to approved static observable fields")
    if claim.get("fact_class") != "static_physical_venue_fact":
        errors.append(f"{prefix} fact class is not an approved static observation")
    if claim.get("observed_value") != RoofType.OPEN.value:
        errors.append(f"{prefix} may only attest an open physical roof classification")
    if (
        claim.get("team_key") != "ATH"
        or claim.get("physical_venue_key") != _EXHAUSTIVE_EXCLUSION
    ):
        errors.append(f"{prefix} canonical venue binding does not match Sutter Health Park")
    recorded_at = _aware_iso_datetime(claim.get("attestation_recorded_at"))
    retrieved_at = _aware_iso_datetime(source.get("retrieved_at"))
    if recorded_at is None:
        errors.append(f"{prefix} attestation_recorded_at must be timezone-aware")
    elif retrieved_at is not None and recorded_at != retrieved_at:
        errors.append(f"{prefix} recorded time must match the attestation source record")
    text = claim.get("exact_attestation_text")
    if text != evidence.get("minimal_evidence_text"):
        errors.append(f"{prefix} exact text must match the preserved evidence text")
    if text != _OWNER_ATTESTATION_TEXT:
        errors.append(f"{prefix} exact text does not match the owner-supplied attestation")
    if claim.get("normalized_text_sha256") != evidence.get("normalized_text_sha256"):
        errors.append(f"{prefix} text checksum must match the roof evidence checksum")
    notes = claim.get("notes")
    normalized_notes = str(notes or "").lower()
    if not isinstance(notes, str) or "firsthand" not in normalized_notes or (
        "not first-party venue documentation" not in normalized_notes
    ):
        errors.append(f"{prefix} notes must identify firsthand, non-provider provenance")
    if claim.get("commercialization_boundary_acknowledged") is not True:
        errors.append(f"{prefix} must acknowledge the commercialization boundary")
    if claim.get("observed_value") != evidence.get("observed_value"):
        errors.append(f"{prefix} outer observed value must match the attested value")
    if source.get("source_type") != "internal_project_owner_attestation" or not str(
        source.get("url", "")
    ).startswith("internal://"):
        errors.append(f"{prefix} source must be labeled as internal owner observation")
    _validate_structured_evidence_checksum(errors, prefix, claim)


def _validate_roof_evidence_source(
    errors: list[str],
    index: int,
    source: Mapping[str, Any],
    retrieval_zone: ZoneInfo | None,
    evidence_contract: Mapping[str, Any],
    stadium_records: list[dict[str, Any]],
) -> None:
    evidence = source.get("roof_evidence")
    if evidence is None:
        return
    if not isinstance(evidence, Mapping):
        errors.append(f"source {index}: roof_evidence must be an object")
        return
    source_title = source.get("source_title")
    if not isinstance(source_title, str) or not source_title.strip():
        errors.append(f"source {index}: source_title is required for roof evidence")
    retrieved_at = _aware_iso_datetime(source.get("retrieved_at"))
    if retrieved_at is None:
        errors.append(f"source {index}: retrieved_at must be a timezone-aware ISO datetime")
    elif (
        retrieval_zone is not None
        and source.get("retrieval_date")
        != retrieved_at.astimezone(retrieval_zone).date().isoformat()
    ):
        errors.append(
            f"source {index}: retrieval_date must match retrieved_at in the contract timezone"
        )
    if source.get("retrieval_date") != source.get("accessed_at"):
        errors.append(f"source {index}: retrieval_date must match accessed_at")
    revision = evidence.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        errors.append(f"source {index}: roof evidence revision must be a positive integer")
    if evidence.get("normalization_version") != EVIDENCE_TEXT_NORMALIZATION_VERSION:
        errors.append(f"source {index}: unsupported roof evidence normalization version")
    kind = evidence.get("kind")
    if kind not in _ROOF_EVIDENCE_KINDS:
        errors.append(f"source {index}: unsupported roof evidence kind")
    locator = evidence.get("locator")
    if locator is not None and (not isinstance(locator, str) or not locator.strip()):
        errors.append(f"source {index}: roof evidence locator must be null or non-empty")
    if kind != "exhaustive_classification":
        _validate_minimal_evidence_text(
            errors,
            f"source {index}: roof evidence",
            evidence.get("minimal_evidence_text"),
            evidence.get("normalized_text_sha256"),
        )
    elif "minimal_evidence_text" in evidence or "normalized_text_sha256" in evidence:
        errors.append(
            f"source {index}: exhaustive evidence must preserve its exact source fragments"
        )
    observed = evidence.get("observed_value")
    if kind in _SIMPLE_QUALIFYING_ROOF_EVIDENCE_KINDS | _DERIVED_ROOF_EVIDENCE_KINDS:
        if observed not in {RoofType.OPEN.value, RoofType.FIXED.value, RoofType.RETRACTABLE.value}:
            errors.append(f"source {index}: qualifying roof evidence has invalid observed_value")
    elif kind == "inference_only" and observed is not None:
        errors.append(f"source {index}: inference-only roof evidence observed_value must be null")
    notes = evidence.get("notes")
    if not isinstance(notes, str) or not notes.strip():
        errors.append(f"source {index}: roof evidence notes are required")
    if kind == "exhaustive_classification":
        _validate_exhaustive_classification(
            errors, index, source, evidence, stadium_records
        )
    elif kind == "owner_attested_direct_observation":
        _validate_owner_attestation(
            errors,
            index,
            source,
            evidence,
            evidence_contract,
        )


def _iso_date_key(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split("-")
    if len(parts) not in {1, 2, 3} or not all(part.isdigit() for part in parts):
        return None
    if (
        len(parts[0]) != 4
        or (len(parts) >= 2 and len(parts[1]) != 2)
        or (len(parts) == 3 and len(parts[2]) != 2)
    ):
        return None
    year = int(parts[0])
    month = int(parts[1]) if len(parts) >= 2 else 1
    day = int(parts[2]) if len(parts) == 3 else 1
    try:
        date(year, month, day)
    except ValueError:
        return None
    return year, month, day


def validate_stadium_catalog_data(catalog: Any, team_aliases: Any) -> list[str]:
    """Validate raw catalog data before constructing any keyed lookup."""
    errors: list[str] = []
    if not isinstance(catalog, Mapping):
        return ["stadium catalog must be an object"]
    if catalog.get("policy_version") != POLICY_VERSION:
        errors.append(f"policy_version must be {POLICY_VERSION}")
    if catalog.get("catalog_version") != CATALOG_VERSION:
        errors.append(f"catalog_version must be {CATALOG_VERSION}")
    evidence_contract = catalog.get("evidence_contract")
    retrieval_zone: ZoneInfo | None = None
    if not isinstance(evidence_contract, Mapping):
        errors.append("evidence_contract must be an object")
    else:
        if evidence_contract.get("version") != FIELD_EVIDENCE_CONTRACT_VERSION:
            errors.append(
                f"evidence_contract version must be {FIELD_EVIDENCE_CONTRACT_VERSION}"
            )
        if evidence_contract.get("catalog_revision") != CATALOG_VERSION:
            errors.append("evidence_contract catalog_revision must match catalog_version")
        if (
            evidence_contract.get("normalization_version")
            != EVIDENCE_TEXT_NORMALIZATION_VERSION
        ):
            errors.append("evidence_contract normalization_version is unsupported")
        supported_kinds = _string_list(evidence_contract.get("supported_evidence_kinds"))
        if supported_kinds is None or set(supported_kinds) != _ROOF_EVIDENCE_KINDS or len(
            supported_kinds
        ) != len(_ROOF_EVIDENCE_KINDS):
            errors.append("evidence_contract supported_evidence_kinds is incomplete")
        derivation_contracts = evidence_contract.get("derivation_contract_versions")
        if not isinstance(derivation_contracts, Mapping) or dict(derivation_contracts) != {
            "exhaustive_classification": EXHAUSTIVE_CLASSIFICATION_CONTRACT_VERSION,
            "owner_attested_direct_observation": OWNER_ATTESTATION_CONTRACT_VERSION,
            "roof_taxonomy": ROOF_TAXONOMY_CONTRACT_VERSION,
        }:
            errors.append("evidence_contract derivation contract versions are unsupported")
        owner_policy = evidence_contract.get("owner_attestation_policy")
        if not isinstance(owner_policy, Mapping):
            errors.append("evidence_contract owner_attestation_policy must be an object")
        else:
            permitted_fields = _string_list(owner_policy.get("permitted_fields"))
            if permitted_fields is None or set(permitted_fields) != _OWNER_ATTESTATION_ALLOWED_FIELDS:
                errors.append("owner attestation permitted_fields must be roof_type only")
            if owner_policy.get("permitted_fact_class") != "static_physical_venue_fact":
                errors.append("owner attestation permitted_fact_class is unsupported")
            prohibited = _string_list(owner_policy.get("prohibited_fact_classes"))
            if prohibited is None or set(prohibited) != _OWNER_ATTESTATION_PROHIBITED_FACT_CLASSES:
                errors.append("owner attestation prohibited_fact_classes is incomplete")
            identities = _string_list(owner_policy.get("authorized_owner_identities"))
            if (
                identities is None
                or not identities
                or len(set(identities)) != len(identities)
                or not all(_OWNER_IDENTITY_RE.fullmatch(item) for item in identities)
                or set(identities) != _AUTHORIZED_OWNER_IDENTITIES
            ):
                errors.append("owner attestation authorized_owner_identities is invalid")
            if owner_policy.get("commercial_revalidation_required") is not True:
                errors.append("owner attestation commercial revalidation must remain required")
            if not isinstance(owner_policy.get("commercialization_boundary"), str) or not str(
                owner_policy.get("commercialization_boundary", "")
            ).strip():
                errors.append("owner attestation commercialization boundary is required")
        retrieval_timezone = evidence_contract.get("retrieval_timezone")
        if not isinstance(retrieval_timezone, str) or not retrieval_timezone.strip():
            errors.append("evidence_contract retrieval_timezone must be an IANA timezone")
        else:
            try:
                retrieval_zone = ZoneInfo(retrieval_timezone)
            except ZoneInfoNotFoundError:
                errors.append("evidence_contract retrieval_timezone must be an IANA timezone")
    if _iso_date_key(catalog.get("effective_date")) is None:
        errors.append("effective_date must be an ISO calendar date")
    expanded = _expanded_records(catalog)
    raw_sources = catalog.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    if not isinstance(raw_sources, list):
        errors.append("sources must be a list")
    source_ids = {
        str(item.get("id"))
        for item in sources
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    sources_by_id = {
        str(item.get("id")): item
        for item in sources
        if isinstance(item, Mapping) and isinstance(item.get("id"), str)
    }
    _append_duplicate_errors(
        errors,
        (item.get("id") for item in sources if isinstance(item, Mapping)),
        "source id",
    )
    for index, source in enumerate(sources):
        if not isinstance(source, Mapping):
            errors.append(f"source {index} must be an object")
            continue
        for field in ("id", "publisher", "url", "accessed_at"):
            value = source.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"source {index}: {field} must be a non-empty string")
        if _iso_date_key(source.get("accessed_at")) is None:
            errors.append(f"source {index}: accessed_at must be an ISO calendar date")
        _validate_roof_evidence_source(
            errors,
            index,
            source,
            retrieval_zone,
            evidence_contract if isinstance(evidence_contract, Mapping) else {},
            expanded,
        )
    coordinate_validation = catalog.get("coordinate_validation")
    if not isinstance(coordinate_validation, Mapping):
        errors.append("coordinate_validation must be an object")
    else:
        if coordinate_validation.get("source_id") not in source_ids:
            errors.append("coordinate_validation source_id is unknown")
        if coordinate_validation.get("official_address_source_id") not in source_ids:
            errors.append("coordinate_validation official-address source is unknown")
        if coordinate_validation.get("request_count") != 30:
            errors.append("coordinate_validation request_count must be 30")
        if coordinate_validation.get("accepted_feature_category") != "leisure":
            errors.append("coordinate_validation accepted feature category must be leisure")
        if coordinate_validation.get("accepted_feature_type") != "stadium":
            errors.append("coordinate_validation accepted feature type must be stadium")
        if _iso_date_key(coordinate_validation.get("accessed_at")) is None:
            errors.append("coordinate_validation accessed_at must be an ISO date")
        tolerance = coordinate_validation.get("reconciliation_tolerance_meters")
        if not _is_finite_number(tolerance) or float(cast(int | float, tolerance)) <= 0:
            errors.append("coordinate_validation tolerance must be positive")
        if not isinstance(coordinate_validation.get("attribution"), str):
            errors.append("coordinate_validation attribution is required")
    raw_records = catalog.get("stadiums")
    if not isinstance(raw_records, list):
        return errors + ["stadiums must be a list"]
    if len(raw_records) != 30:
        errors.append(f"expected 30 MLB stadium records, found {len(raw_records)}")
    _append_duplicate_errors(
        errors,
        (record.get("team_key") for record in raw_records if isinstance(record, Mapping)),
        "team key",
    )
    _append_duplicate_errors(
        errors,
        (
            record.get("physical_venue_key")
            for record in raw_records
            if isinstance(record, Mapping)
        ),
        "physical venue key",
    )
    team_keys = {record.get("team_key") for record in expanded}
    missing = EXPECTED_TEAM_KEYS - team_keys
    extra = team_keys - EXPECTED_TEAM_KEYS
    if missing:
        errors.append("missing active MLB team keys: " + ", ".join(sorted(missing)))
    if extra:
        errors.append("unsupported MLB team keys: " + ", ".join(sorted(map(str, extra))))

    current_names: dict[str, set[str]] = {}
    historical_names: dict[str, set[str]] = {}
    former_names: dict[str, set[str]] = {}
    for record in expanded:
        team_key = str(record.get("team_key"))
        for field in (
            "team_name",
            "physical_venue_key",
            "base_venue_name",
            "current_display_name",
        ):
            value = record.get(field)
            if not isinstance(value, str) or not value or value != value.strip():
                errors.append(f"{team_key}: {field} must be a non-empty trimmed string")
        if record.get("active_club_association") is not True:
            errors.append(f"{team_key}: active_club_association must be true")
        for field, minimum, maximum, upper_inclusive in (
            ("latitude", -90.0, 90.0, True),
            ("longitude", -180.0, 180.0, True),
            ("outfield_bearing_degrees", 0.0, 360.0, False),
        ):
            value = record.get(field)
            if value is None and field == "outfield_bearing_degrees":
                continue
            if not _is_finite_number(value):
                errors.append(f"{team_key}: {field} must be finite numeric")
                continue
            numeric_value = float(cast(int | float, value))
            valid = minimum <= numeric_value <= maximum if upper_inclusive else minimum <= numeric_value < maximum
            if not valid:
                errors.append(f"{team_key}: {field} is outside its valid range")
        timezone = record.get("timezone")
        if not isinstance(timezone, str):
            errors.append(f"{team_key}: timezone must be an IANA string")
        else:
            try:
                ZoneInfo(timezone)
            except ZoneInfoNotFoundError:
                errors.append(f"{team_key}: timezone is not an installed IANA timezone")
        if record.get("roof_type") not in {item.value for item in RoofType}:
            errors.append(f"{team_key}: unsupported roof_type")
        display_effective_from = record.get("current_display_effective_from")
        if display_effective_from is not None and _iso_date_key(display_effective_from) is None:
            errors.append(f"{team_key}: current_display_effective_from must be an ISO date")
        if display_effective_from is not None and not is_field_verified(record, "current_display_name"):
            errors.append(f"{team_key}: effective display name lacks verified provenance")
        coordinate_evidence = record.get("coordinate_evidence")
        coordinate_verified = all(
            is_field_verified(record, field) for field in ("latitude", "longitude")
        )
        if coordinate_verified:
            if not isinstance(coordinate_evidence, Mapping):
                errors.append(f"{team_key}: verified coordinates require coordinate_evidence")
            else:
                if coordinate_evidence.get("source_id") not in source_ids:
                    errors.append(f"{team_key}: coordinate evidence source is unknown")
                if coordinate_evidence.get("osm_type") not in {"node", "way", "relation"}:
                    errors.append(f"{team_key}: coordinate evidence has invalid osm_type")
                osm_id = coordinate_evidence.get("osm_id")
                if not isinstance(osm_id, int) or isinstance(osm_id, bool) or osm_id <= 0:
                    errors.append(f"{team_key}: coordinate evidence has invalid osm_id")
                if coordinate_evidence.get("matched_official_address") is not True:
                    errors.append(f"{team_key}: coordinates do not reconcile to official address")
                if not isinstance(coordinate_evidence.get("query_name"), str):
                    errors.append(f"{team_key}: coordinate evidence query_name is required")
                if not isinstance(coordinate_evidence.get("result_name"), str):
                    errors.append(f"{team_key}: coordinate evidence result_name is required")
                if coordinate_evidence.get("observed_latitude") != record.get("latitude"):
                    errors.append(f"{team_key}: latitude differs from retained geospatial evidence")
                if coordinate_evidence.get("observed_longitude") != record.get("longitude"):
                    errors.append(f"{team_key}: longitude differs from retained geospatial evidence")
                difference = coordinate_evidence.get("legacy_difference_meters")
                tolerance = coordinate_evidence.get("reconciliation_tolerance_meters")
                if not _is_finite_number(difference) or not _is_finite_number(tolerance):
                    errors.append(f"{team_key}: coordinate reconciliation metrics are required")
                elif float(cast(int | float, difference)) > float(
                    cast(int | float, tolerance)
                ):
                    errors.append(f"{team_key}: legacy coordinate exceeds reconciliation tolerance")
        for name_field in ("aliases", "historical_display_names"):
            names = record.get(name_field)
            if not isinstance(names, list) or not all(
                isinstance(item, str) and item and item == item.strip() for item in names
            ):
                errors.append(f"{team_key}: {name_field} must contain trimmed strings")
            elif len(names) != len(set(names)):
                errors.append(f"{team_key}: duplicate names in {name_field}")
        history = record.get("display_name_history")
        record_historical_names = set(_names(record, "historical_display_names"))
        history_names: set[str] = set()
        if not isinstance(history, list):
            errors.append(f"{team_key}: display_name_history must be a list")
        else:
            for item in history:
                if not isinstance(item, Mapping):
                    errors.append(f"{team_key}: display name history entry must be an object")
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"{team_key}: display name history name is required")
                    continue
                if name in history_names:
                    errors.append(f"{team_key}: duplicate display name history: {name}")
                history_names.add(name)
                allowed_names = record_historical_names | {
                    str(record.get("base_venue_name")),
                    str(record.get("current_display_name")),
                }
                if name not in allowed_names:
                    errors.append(f"{team_key}: display history name is not a same-venue name: {name}")
                start = _iso_date_key(item.get("effective_from"))
                end = _iso_date_key(item.get("effective_to"))
                if start is None or end is None:
                    errors.append(f"{team_key}: display history dates must be supported ISO dates")
                elif start > end:
                    errors.append(f"{team_key}: display history date range is reversed: {name}")
                history_sources = item.get("source_ids")
                if not isinstance(history_sources, list) or not history_sources:
                    errors.append(f"{team_key}: display history sources are required: {name}")
                elif not all(isinstance(source_id, str) for source_id in history_sources):
                    errors.append(f"{team_key}: display history sources must be strings: {name}")
                elif set(history_sources) - source_ids:
                    errors.append(f"{team_key}: display history source is unknown: {name}")
        if history_names != record_historical_names:
            errors.append(f"{team_key}: display_name_history must cover historical_display_names")
        for field in record.get("field_verification", {}):
            _validate_verification(errors, record, field, sources_by_id)

        current = {
            str(record.get("base_venue_name")),
            str(record.get("current_display_name")),
            *list(_names(record, "aliases")),
        }
        for name in current:
            current_names.setdefault(name, set()).add(str(record.get("physical_venue_key")))
        for name in _names(record, "historical_display_names"):
            historical_names.setdefault(name, set()).add(str(record.get("physical_venue_key")))
        former = record.get("former_physical_venues")
        if not isinstance(former, list):
            errors.append(f"{team_key}: former_physical_venues must be a list")
        else:
            for old_venue in former:
                if not isinstance(old_venue, Mapping):
                    errors.append(f"{team_key}: former physical venue must be an object")
                    continue
                old_key = old_venue.get("physical_venue_key")
                old_names = old_venue.get("names")
                if not isinstance(old_key, str) or not old_key:
                    errors.append(f"{team_key}: former physical venue key is required")
                elif old_key == record.get("physical_venue_key"):
                    errors.append(f"{team_key}: former physical venue key equals current venue")
                if not isinstance(old_names, list) or not all(
                    isinstance(item, str) and item for item in old_names
                ):
                    errors.append(f"{team_key}: former physical venue names are required")
                    continue
                if len(old_names) != len(set(old_names)):
                    errors.append(f"{team_key}: duplicate former physical venue names")
                status = old_venue.get("status")
                if status not in {state.value for state in VerificationState}:
                    errors.append(f"{team_key}: former physical venue has invalid status")
                old_sources = old_venue.get("source_ids")
                if not isinstance(old_sources, list) or not all(
                    isinstance(source_id, str) for source_id in old_sources
                ):
                    errors.append(f"{team_key}: former physical venue sources are required")
                elif set(old_sources) - source_ids:
                    errors.append(f"{team_key}: former physical venue source is unknown")
                elif status == VerificationState.VERIFIED.value and not old_sources:
                    errors.append(f"{team_key}: VERIFIED former physical venue lacks provenance")
                if "association_seasons" in old_venue:
                    errors.append(
                        f"{team_key}: former physical venue association_seasons is ambiguous"
                    )
                association_start = old_venue.get("association_started_season")
                association_end = old_venue.get("association_ended_after_season")
                for label, season in (
                    ("association_started_season", association_start),
                    ("association_ended_after_season", association_end),
                ):
                    if season is not None and (
                        isinstance(season, bool)
                        or not isinstance(season, int)
                        or season < 1876
                    ):
                        errors.append(
                            f"{team_key}: former physical venue {label} is invalid"
                        )
                if (
                    isinstance(association_start, int)
                    and not isinstance(association_start, bool)
                    and isinstance(association_end, int)
                    and not isinstance(association_end, bool)
                    and association_start > association_end
                ):
                    errors.append(
                        f"{team_key}: former physical venue association range is reversed"
                    )
                for name in old_names:
                    former_names.setdefault(name, set()).add(str(old_key))
            if any(
                isinstance(old_venue, Mapping)
                and old_venue.get("status") != VerificationState.VERIFIED.value
                for old_venue in former
            ) and is_field_verified(record, "former_physical_venues"):
                errors.append(
                    f"{team_key}: former_physical_venues field is VERIFIED with unverified entries"
                )

    for name, venue_keys in sorted(current_names.items()):
        if len(venue_keys) > 1:
            errors.append(f"current venue alias collision: {name}")
    for name, venue_keys in sorted(historical_names.items()):
        combined = venue_keys | current_names.get(name, set())
        if len(combined) > 1:
            errors.append(f"historical venue alias collision: {name}")
    for name, venue_keys in sorted(former_names.items()):
        if name in current_names or name in historical_names:
            errors.append(f"former venue alias overlaps current physical venue: {name}")
        if len(venue_keys) > 1:
            errors.append(f"former venue alias collision: {name}")

    if not isinstance(team_aliases, Mapping):
        errors.append("team alias catalog must be an object")
    else:
        alias_teams = team_aliases.get("teams")
        if not isinstance(alias_teams, list):
            errors.append("team alias catalog teams must be a list")
        else:
            _append_duplicate_errors(
                errors,
                (item.get("key") for item in alias_teams if isinstance(item, Mapping)),
                "team alias key",
            )
            alias_keys = {
                item.get("key") for item in alias_teams if isinstance(item, Mapping)
            }
            missing_aliases = EXPECTED_TEAM_KEYS - alias_keys
            extra_aliases = alias_keys - EXPECTED_TEAM_KEYS
            if missing_aliases:
                errors.append(
                    "stadium teams missing from team alias catalog: "
                    + ", ".join(sorted(missing_aliases))
                )
            if extra_aliases:
                errors.append(
                    "team aliases without stadium records: "
                    + ", ".join(sorted(map(str, extra_aliases)))
                )
            team_alias_owners: dict[str, set[str]] = {}
            for item in alias_teams:
                if not isinstance(item, Mapping):
                    errors.append("team alias record must be an object")
                    continue
                key = item.get("key")
                aliases = item.get("aliases")
                if not isinstance(aliases, list) or not all(
                    isinstance(alias, str) and alias and alias == alias.strip()
                    for alias in aliases
                ):
                    errors.append(f"{key}: team aliases must contain trimmed strings")
                    continue
                if len(aliases) != len(set(aliases)):
                    errors.append(f"{key}: duplicate team aliases")
                for alias in aliases:
                    team_alias_owners.setdefault(alias, set()).add(str(key))
            for alias, owners in sorted(team_alias_owners.items()):
                if len(owners) > 1:
                    errors.append(f"team alias collision: {alias}")
    return errors


def validate_stadium_catalog(
    catalog_path: Path = DATA_PATH, team_alias_path: Path = TEAM_ALIASES_PATH
) -> list[str]:
    try:
        catalog = _read_json(catalog_path)
        aliases = _read_json(team_alias_path)
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return [str(exc)]
    return validate_stadium_catalog_data(catalog, aliases)


def stadium_catalog_inventory() -> dict[str, Any]:
    stadiums = load_stadiums()
    field_counts: dict[str, dict[str, int]] = {}
    for record in stadiums.values():
        for field, metadata in record["field_verification"].items():
            status = str(metadata["status"])
            field_counts.setdefault(field, {}).setdefault(status, 0)
            field_counts[field][status] += 1
    roof_counts: dict[str, int] = {}
    for record in stadiums.values():
        roof = str(record["roof_type"])
        roof_counts[roof] = roof_counts.get(roof, 0) + 1
    roof_verification = field_counts.get("roof_type", {})
    verified_roof_count = roof_verification.get(VerificationState.VERIFIED.value, 0)
    return {
        "policy_version": POLICY_VERSION,
        "catalog_version": load_stadium_catalog()["catalog_version"],
        "record_count": len(stadiums),
        "field_verification_counts": {
            field: dict(sorted(counts.items()))
            for field, counts in sorted(field_counts.items())
        },
        "roof_type_counts": dict(sorted(roof_counts.items())),
        "stadium_catalog_readiness": (
            "READY" if verified_roof_count == len(stadiums) else "BLOCKED"
        ),
        "current_alias_count": sum(len(record["aliases"]) for record in stadiums.values()),
        "historical_display_name_count": sum(
            len(record["historical_display_names"]) for record in stadiums.values()
        ),
        "former_physical_venue_count": sum(
            len(record["former_physical_venues"]) for record in stadiums.values()
        ),
        "teams_blocked_for_weather": sorted(
            team_key
            for team_key, record in stadiums.items()
            if material_weather_metadata_errors(record)
        ),
        "validation_errors": validate_stadium_catalog(),
    }


if __name__ == "__main__":
    print(json.dumps(stadium_catalog_inventory(), indent=2, sort_keys=True))
