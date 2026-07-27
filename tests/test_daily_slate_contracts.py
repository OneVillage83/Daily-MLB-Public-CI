from __future__ import annotations

import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.daily_slate import (
    DAILY_SLATE_ARTIFACT_RELPATH,
    DailySlateContractError,
    DailySlateDoubleheaderStatus,
    DailySlateGameStatus,
    DailySlateGameV1,
    DailySlateProvenanceV1,
    DailySlateV1,
    ProbableStarterV1,
    VenueMappingStatus,
    canonical_authoritative_game_id,
    daily_mlb_game_id,
    edge_event_id,
    resolve_canonical_team_id,
    resolve_canonical_venue_id,
    write_daily_slate_artifact,
)
from app.stadiums import stadium_for_team


NOW = datetime(2026, 7, 27, 15, tzinfo=timezone.utc)
START = datetime(2026, 7, 27, 23, 10, tzinfo=timezone.utc)


def _provenance(
    source_record_id: str | None,
    *,
    observed_at: datetime = NOW,
    raw_status: str | None = "Scheduled",
) -> DailySlateProvenanceV1:
    return DailySlateProvenanceV1(
        source_provider="mlb",
        source_record_id=source_record_id,
        observed_at=observed_at,
        source_updated_at=observed_at - timedelta(minutes=1),
        source_version="schedule-v1",
        raw_status=raw_status,
        upstream_checksum="a" * 64,
    )


def _game(
    source_game_id: str,
    *,
    start: datetime | None = START,
    away: str = "SF",
    home: str = "LAD",
    venue_id: str | None = "dodger-stadium-los-angeles",
    venue_mapping_status: VenueMappingStatus = VenueMappingStatus.RESOLVED,
    source_venue_name: str = "Dodger Stadium",
    game_number: int | None = 1,
    doubleheader: DailySlateDoubleheaderStatus = DailySlateDoubleheaderStatus.SINGLE,
    status: DailySlateGameStatus = DailySlateGameStatus.SCHEDULED,
    away_starter: ProbableStarterV1 | None = None,
    home_starter: ProbableStarterV1 | None = None,
) -> DailySlateGameV1:
    return DailySlateGameV1(
        edge_event_id=edge_event_id(source_game_id),
        daily_mlb_game_id=daily_mlb_game_id(source_game_id),
        official_date="2026-07-27",
        scheduled_start_time=start,
        away_team_id=away,
        home_team_id=home,
        venue_id=venue_id,
        venue_mapping_status=venue_mapping_status,
        game_number=game_number,
        doubleheader_status=doubleheader,
        game_status=status,
        source_game_id=source_game_id,
        source_provider="mlb",
        observed_at=NOW,
        provenance=_provenance(source_game_id),
        source_home_team_id="119",
        source_away_team_id="137",
        source_venue_id="22",
        source_venue_name=source_venue_name,
        source_updated_at=NOW - timedelta(minutes=1),
        away_probable_starter=away_starter,
        home_probable_starter=home_starter,
    )


def _slate(*games: DailySlateGameV1, source_version: str = "schedule-v1") -> DailySlateV1:
    return DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        source_authority="mlb",
        source_version=source_version,
        games=tuple(games),
        provenance=_provenance(None),
    )


def _starter(source_player_id: str = "660271") -> ProbableStarterV1:
    return ProbableStarterV1(
        source_player_id=source_player_id,
        full_name="Authoritative Probable Pitcher",
        source_provider="mlb",
        observed_at=NOW,
        provenance=_provenance(source_player_id),
    )


def test_contracts_are_frozen_and_normalize_mutable_input_order() -> None:
    later = _game("200", start=START + timedelta(hours=3))
    earlier = _game("100")
    slate = _slate(later, earlier)

    assert isinstance(slate.games, tuple)
    assert [game.source_game_id for game in slate.games] == ["100", "200"]
    with pytest.raises(FrozenInstanceError):
        slate.requested_date = "2026-07-28"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        slate.games[0].game_status = DailySlateGameStatus.FINAL  # type: ignore[misc]


def test_nested_input_mutation_cannot_change_slate_or_checksum() -> None:
    mutable_games = [_game("200"), _game("100")]
    slate = DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=NOW,
        observed_at=NOW,
        source_authority="mlb",
        source_version="schedule-v1",
        games=mutable_games,  # type: ignore[arg-type]
        provenance=_provenance(None),
    )
    original_games = slate.games
    original_checksum = slate.checksum

    mutable_games.clear()

    assert slate.games == original_games
    assert slate.checksum == original_checksum


def test_zero_game_slate_has_deterministic_canonical_evidence() -> None:
    first = _slate()
    second = _slate()

    assert first.games == ()
    assert first.canonical_json_bytes() == second.canonical_json_bytes()
    assert first.checksum == second.checksum
    assert json.loads(first.canonical_json_bytes())["games"] == []


@pytest.mark.parametrize(
    "requested_date",
    ["2026-7-27", "2026-07-27Z", "2026-02-30", ""],
)
def test_requested_date_requires_exact_valid_iso_date(requested_date: str) -> None:
    with pytest.raises((DailySlateContractError, ValueError)):
        DailySlateV1(
            requested_date=requested_date,
            as_of_time=NOW,
            observed_at=NOW,
            source_authority="mlb",
            source_version=None,
            games=(),
            provenance=_provenance(None),
        )


def test_temporal_fields_require_timezone_awareness() -> None:
    naive = datetime(2026, 7, 27, 15)
    with pytest.raises(DailySlateContractError, match="as_of_time"):
        DailySlateV1(
            requested_date="2026-07-27",
            as_of_time=naive,
            observed_at=NOW,
            source_authority="mlb",
            source_version=None,
            games=(),
            provenance=_provenance(None),
        )
    with pytest.raises(DailySlateContractError, match="observed_at"):
        DailySlateProvenanceV1(
            source_provider="mlb",
            source_record_id=None,
            observed_at=naive,
        )


def test_equivalent_timestamp_offsets_normalize_to_canonical_utc() -> None:
    offset = timezone(timedelta(hours=-7))
    equivalent_now = NOW.astimezone(offset)
    equivalent_start = START.astimezone(offset)
    left = _slate(_game("100"))
    right_game = replace(
        _game("100"),
        scheduled_start_time=equivalent_start,
        observed_at=equivalent_now,
        source_updated_at=(NOW - timedelta(minutes=1)).astimezone(offset),
        provenance=replace(
            _provenance("100"),
            observed_at=equivalent_now,
            source_updated_at=(NOW - timedelta(minutes=1)).astimezone(offset),
        ),
    )
    right = DailySlateV1(
        requested_date="2026-07-27",
        as_of_time=equivalent_now,
        observed_at=equivalent_now,
        source_authority="mlb",
        source_version="schedule-v1",
        games=(right_game,),
        provenance=replace(
            _provenance(None),
            observed_at=equivalent_now,
            source_updated_at=(NOW - timedelta(minutes=1)).astimezone(offset),
        ),
    )

    assert left.canonical_json_bytes() == right.canonical_json_bytes()
    payload = json.loads(left.canonical_json_bytes())
    assert payload["as_of_time"].endswith("+00:00")
    assert payload["observed_at"].endswith("+00:00")
    assert payload["games"][0]["scheduled_start_time"].endswith("+00:00")


def test_requested_date_does_not_overwrite_authoritative_official_date() -> None:
    game = replace(_game("100"), official_date="2026-07-28")
    slate = _slate(game)

    assert slate.requested_date == "2026-07-27"
    assert slate.games[0].official_date == "2026-07-28"


def test_identity_is_stable_provider_independent_and_doubleheader_safe() -> None:
    first = _game(
        "900001",
        game_number=1,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    second = _game(
        "900002",
        start=START + timedelta(hours=4),
        game_number=2,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    repeated = _game(
        "900001",
        status=DailySlateGameStatus.PREGAME,
        game_number=1,
        doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )

    assert first.edge_event_id == repeated.edge_event_id == "edge:mlb:900001"
    assert first.daily_mlb_game_id == repeated.daily_mlb_game_id == "game:mlb:900001"
    assert first.edge_event_id != second.edge_event_id
    assert first.daily_mlb_game_id != second.daily_mlb_game_id
    assert len(_slate(second, first).games) == 2


def test_numeric_authoritative_game_id_has_one_canonical_decimal_form() -> None:
    assert canonical_authoritative_game_id(900001) == "900001"
    assert canonical_authoritative_game_id("000900001") == "900001"
    assert edge_event_id(900001) == edge_event_id("000900001")
    assert daily_mlb_game_id(900001) == daily_mlb_game_id("000900001")
    canonical = replace(_game("900001"), source_game_id="000900001")
    assert canonical.source_game_id == "900001"

    for invalid in (0, -1, True, "", " 900001", "game-900001"):
        with pytest.raises(DailySlateContractError):
            canonical_authoritative_game_id(invalid)


def test_primary_identity_cannot_be_replaced_with_schedule_string_or_odds_id() -> None:
    with pytest.raises(DailySlateContractError, match="edge_event_id"):
        replace(_game("900001"), edge_event_id="odds-api-event-id")


def test_duplicate_event_and_authoritative_game_identity_are_rejected() -> None:
    game = _game("900001")
    with pytest.raises(DailySlateContractError, match="duplicate edge_event_id"):
        _slate(game, game)


def test_home_and_away_must_be_distinct_canonical_team_ids() -> None:
    with pytest.raises(DailySlateContractError, match="must differ"):
        _game("900001", away="LAD", home="LAD")
    with pytest.raises(DailySlateContractError, match="canonical MLB team"):
        _game("900001", away="TYPO")


def test_team_identity_reuses_exact_repository_alias_mapping_and_fails_closed() -> None:
    assert resolve_canonical_team_id("Los Angeles Dodgers") == "LAD"
    assert resolve_canonical_team_id("LAD") == "LAD"
    with pytest.raises(DailySlateContractError, match="does not resolve"):
        resolve_canonical_team_id("Los Angles Dodgrs")


def test_venue_identity_reuses_physical_venue_key_without_fuzzy_guessing() -> None:
    dodger = stadium_for_team("LAD")
    assert dodger is not None
    resolved = resolve_canonical_venue_id(
        str(dodger["current_display_name"]), home_team_id="LAD"
    )
    assert resolved.status is VenueMappingStatus.RESOLVED
    assert resolved.venue_id == dodger["physical_venue_key"]

    unresolved = resolve_canonical_venue_id(
        "Almost Dodger Stadum", home_team_id="LAD"
    )
    assert unresolved.status is VenueMappingStatus.UNRESOLVED
    assert unresolved.venue_id is None


def test_unresolved_venue_is_explicit_and_preserves_source_evidence() -> None:
    game = _game(
        "900001",
        venue_id=None,
        venue_mapping_status=VenueMappingStatus.UNRESOLVED,
        source_venue_name="Unmapped Neutral Site",
    )
    assert game.venue_id is None
    assert game.source_venue_name == "Unmapped Neutral Site"
    with pytest.raises(DailySlateContractError, match="source venue evidence"):
        DailySlateGameV1(
            edge_event_id=edge_event_id("900002"),
            daily_mlb_game_id=daily_mlb_game_id("900002"),
            official_date="2026-07-27",
            scheduled_start_time=START,
            away_team_id="SF",
            home_team_id="LAD",
            venue_id=None,
            venue_mapping_status=VenueMappingStatus.UNRESOLVED,
            game_number=None,
            doubleheader_status=DailySlateDoubleheaderStatus.UNKNOWN,
            game_status=DailySlateGameStatus.UNKNOWN,
            source_game_id="900002",
            source_provider="mlb",
            observed_at=NOW,
            provenance=_provenance("900002"),
        )


def test_probable_starter_is_optional_authoritative_and_never_invented() -> None:
    without_starter = _game("900001")
    with_starter = _game("900002", away_starter=_starter())
    assert without_starter.away_probable_starter is None
    assert with_starter.away_probable_starter is not None
    assert (
        with_starter.away_probable_starter.classification.value
        == "authoritative_probable"
    )
    assert with_starter.away_probable_starter.canonical_player_id is None


def test_unknown_game_status_and_raw_provider_status_remain_observable() -> None:
    game = _game("900001", status=DailySlateGameStatus.UNKNOWN)
    assert game.game_status is DailySlateGameStatus.UNKNOWN
    assert game.provenance.raw_status == "Scheduled"


def test_canonical_json_and_checksums_ignore_input_order_and_key_order() -> None:
    first = _game("100")
    second = _game("200", start=START + timedelta(hours=1))
    left = _slate(first, second)
    right = _slate(second, first)

    assert left.as_dict() == right.as_dict()
    assert left.canonical_json_bytes() == right.canonical_json_bytes()
    assert left.checksum == right.checksum
    assert json.loads(left.canonical_json_bytes())["checksum"] == left.checksum
    assert len(left.checksum) == 64
    assert len(first.checksum) == 64


@pytest.mark.parametrize(
    "changed",
    [
        _game("100", start=START + timedelta(minutes=5)),
        _game("100", status=DailySlateGameStatus.PREGAME),
        _game("100", venue_id="oracle-park-san-francisco"),
        _game("100", away_starter=_starter()),
        _game(
            "100",
            game_number=2,
            doubleheader=DailySlateDoubleheaderStatus.DOUBLEHEADER,
        ),
    ],
)
def test_material_game_changes_change_game_and_slate_checksums(
    changed: DailySlateGameV1,
) -> None:
    original = _game("100")
    assert changed.checksum != original.checksum
    assert _slate(changed).checksum != _slate(original).checksum


def test_artifact_is_canonical_contained_and_byte_checksum_matches(
    tmp_path: Path,
) -> None:
    slate = _slate(_game("100"))
    artifact = write_daily_slate_artifact(slate, tmp_path)
    expected_relpath = DAILY_SLATE_ARTIFACT_RELPATH.format(
        snapshot_checksum=slate.checksum
    )
    path = tmp_path / expected_relpath

    assert artifact.relpath == expected_relpath
    assert path.read_bytes() == slate.canonical_json_bytes()
    assert artifact.checksum == hashlib.sha256(path.read_bytes()).hexdigest()
    assert artifact.byte_count == len(path.read_bytes())


def test_artifact_path_is_content_addressed_across_attempts(tmp_path: Path) -> None:
    first = _slate(_game("100"))
    second = _slate(_game("100", status=DailySlateGameStatus.PREGAME))

    first_artifact = write_daily_slate_artifact(first, tmp_path)
    second_artifact = write_daily_slate_artifact(second, tmp_path)

    assert first_artifact.relpath != second_artifact.relpath
    assert (tmp_path / first_artifact.relpath).read_bytes() == first.canonical_json_bytes()
    assert (tmp_path / second_artifact.relpath).read_bytes() == second.canonical_json_bytes()


def test_artifact_rejects_traversal_and_configured_secrets(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="traversal"):
        write_daily_slate_artifact(
            _slate(_game("100")), tmp_path, relpath="../daily_slate.json"
        )
    synthetic_secret = "synthetic-configured-value-for-test"
    with pytest.raises(DailySlateContractError, match="credential"):
        write_daily_slate_artifact(
            _slate(_game("100"), source_version=synthetic_secret),
            tmp_path,
            secret_values=(synthetic_secret,),
        )


@pytest.mark.parametrize(
    "unsafe_value",
    [
        "Authorization: Bearer synthetic-token",
        "Bearer synthetic-token",
        "https://synthetic-user:synthetic-password@example.invalid/path",
        "https://example.invalid/path?api_key=synthetic-value",
        "api_key=synthetic-value",
    ],
)
def test_domain_rejects_generic_credential_bearing_provenance(
    unsafe_value: str,
) -> None:
    with pytest.raises(DailySlateContractError, match="credential-bearing"):
        _slate(_game("100"), source_version=unsafe_value)


def test_domain_rejects_configured_secret_before_serialization() -> None:
    synthetic_secret = "synthetic-configured-value-for-test"
    with pytest.raises(DailySlateContractError, match="credential-bearing") as error:
        DailySlateV1(
            requested_date="2026-07-27",
            as_of_time=NOW,
            observed_at=NOW,
            source_authority="mlb",
            source_version=synthetic_secret,
            games=(_game("100"),),
            provenance=_provenance(None),
            secret_values=(synthetic_secret,),
        )
    assert synthetic_secret not in str(error.value)
