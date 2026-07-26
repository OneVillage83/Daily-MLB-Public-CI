from __future__ import annotations

import itertools
import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.stats.contracts import FixtureResponse, StatsProviderPayloadError
from app.stats.providers.baseball_reference import BaseballReferenceProvider
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport

FIXTURES = Path(__file__).with_name("fixtures") / "stats"


def make_provider(
    tmp_path: Path,
    fixture: str | bytes,
    *,
    content_type: str = "text/html; charset=utf-8",
) -> tuple[BaseballReferenceProvider, FixtureStatsTransport]:
    ids = itertools.count(1)
    store = RawArtifactStore(
        tmp_path / "raw",
        capture_id_factory=lambda: f"capture_{next(ids):04d}",
    )
    transport = FixtureStatsTransport(
        store,
        {
            "bref": FixtureResponse(
                FIXTURES / fixture if isinstance(fixture, str) else fixture,
                content_type,
            )
        },
        clock=lambda: datetime(2026, 7, 14, 20, tzinfo=timezone.utc),
    )
    return BaseballReferenceProvider(transport), transport


def test_comment_wrapped_table_is_parsed_from_retained_html(tmp_path: Path) -> None:
    provider, transport = make_provider(tmp_path, "baseball_reference_team.html")

    table = provider.collect_table(
        "/teams/LAD/2026.shtml",
        table_id="team_batting",
        fixture_key="bref",
    )

    assert table.columns == ("player", "pa")
    assert table.rows[0] == {"player": "Example Hitter", "pa": "42"}
    assert table.rows[1]["pa"] == "0"
    assert table.raw.path.read_bytes() == (FIXTURES / "baseball_reference_team.html").read_bytes()
    request = transport.requests[0]
    assert request.url.startswith("https://www.baseball-reference.com/")
    assert request.minimum_interval_seconds == 6.0
    assert request.headers["Accept"].startswith("text/html")


def test_daily_batting_uses_exact_bounded_pybaseball_query_and_identity(
    tmp_path: Path,
) -> None:
    provider, transport = make_provider(
        tmp_path, "baseball_reference_daily_batting.html"
    )

    table = provider.collect_batting_stats_range(
        date(2026, 7, 10), date(2026, 7, 14), fixture_key="bref"
    )

    request = transport.requests[0]
    assert request.url == "https://www.baseball-reference.com/leagues/daily.fcgi"
    assert "/daily.cgi" not in request.url
    assert request.params == {
        "user_team": "",
        "bust_cache": "",
        "type": "b",
        "lastndays": "7",
        "dates": "fromandto",
        "fromandto": "2026-07-10.2026-07-14",
        "level": "mlb",
        "franch": "",
        "stat": "",
        "stat_value": "0",
    }
    assert request.timeout_seconds == 30.0
    assert request.minimum_interval_seconds == 6.0
    assert table.columns[-2:] == ("mlbID", "mlbID_href")
    assert table.rows[0]["mlbID"] == "660271"
    assert table.rows[0]["mlbID_href"].endswith("mlb_ID=660271")
    assert table.rows[0]["triples"] == "0"
    assert table.raw.path.read_bytes() == (
        FIXTURES / "baseball_reference_daily_batting.html"
    ).read_bytes()


def test_daily_pitching_uses_exact_type_and_preserves_zero(tmp_path: Path) -> None:
    provider, transport = make_provider(
        tmp_path, "baseball_reference_daily_pitching.html"
    )

    table = provider.collect_pitching_stats_range(
        date(2026, 7, 14), date(2026, 7, 14), fixture_key="bref"
    )

    assert transport.requests[0].params["type"] == "p"
    assert transport.requests[0].params["fromandto"] == "2026-07-14.2026-07-14"
    assert transport.requests[0].url == (
        "https://www.baseball-reference.com/leagues/daily.fcgi"
    )
    assert "/daily.cgi" not in transport.requests[0].url
    assert table.rows[0]["mlbID"] == "605400"
    assert table.rows[0]["bases_on_balls"] == "0"


@pytest.mark.parametrize(
    ("fixture", "collector", "expected"),
    [
        (
            "baseball_reference_daily_batting_live_shape.html",
            "batting",
            {
                "mlbID": "682928",
                "name_display": "Example Hitter",
                "team_name": "Washington",
                "game_log": "gl",
                "plate_appearances": "395",
                "home_runs": "20",
            },
        ),
        (
            "baseball_reference_daily_pitching_live_shape.html",
            "pitching",
            {
                "mlbID": "671096",
                "name_display": "Example Pitcher",
                "team_name": "Cincinnati",
                "game_log": "gl",
                "innings_pitched": "105.0",
                "pitches": "1837",
            },
        ),
    ],
)
def test_live_daily_data_stat_and_identity_shape_is_normalized_exactly(
    tmp_path: Path,
    fixture: str,
    collector: str,
    expected: dict[str, str],
) -> None:
    provider, _ = make_provider(tmp_path, fixture)

    table = (
        provider.collect_batting_stats_range(
            date(2026, 3, 25), date(2026, 7, 13), fixture_key="bref"
        )
        if collector == "batting"
        else provider.collect_pitching_stats_range(
            date(2026, 3, 25), date(2026, 7, 13), fixture_key="bref"
        )
    )

    assert {key: table.rows[0][key] for key in expected} == expected
    assert table.rows[0]["mlbID_href"].startswith(
        "/redirect.fcgi?player=1&mlb_ID="
    )
    assert "player" not in table.rows[0]
    assert "team_ID" not in table.rows[0]
    assert table.raw.path.read_bytes() == (FIXTURES / fixture).read_bytes()


def test_unrecognized_live_daily_data_stat_schema_fails_after_raw_retention(
    tmp_path: Path,
) -> None:
    source = (
        FIXTURES / "baseball_reference_daily_batting_live_shape.html"
    ).read_bytes()
    malformed = source.replace(b'data-stat="PA"', b'data-stat="unknown_pa"')
    provider, _ = make_provider(tmp_path, malformed)

    with pytest.raises(StatsProviderPayloadError, match="data-stat schema") as caught:
        provider.collect_batting_stats_range(
            date(2026, 3, 25), date(2026, 7, 13), fixture_key="bref"
        )

    assert caught.value.capture.path.read_bytes() == malformed


@pytest.mark.parametrize(
    "malformed_href",
    [
        "/redirect.fcgi?player=2&amp;mlb_ID=682928",
        "/redirect.fcgi?player=1&amp;mlb_ID=682928&amp;extra=value",
        "/redirect.fcgi?player=1&amp;mlb_ID=682928&amp;mlb_ID=682928",
        "/other.fcgi?player=1&amp;mlb_ID=682928",
        "https://www.baseball-reference.com/redirect.fcgi?player=1&amp;mlb_ID=682928",
    ],
)
def test_malformed_live_daily_identity_link_fails_after_raw_retention(
    tmp_path: Path,
    malformed_href: str,
) -> None:
    source = (
        FIXTURES / "baseball_reference_daily_batting_live_shape.html"
    ).read_text("utf-8")
    malformed = source.replace(
        "/redirect.fcgi?player=1&amp;mlb_ID=682928",
        malformed_href,
        1,
    ).encode()
    provider, _ = make_provider(tmp_path, malformed)

    with pytest.raises(StatsProviderPayloadError, match="identity") as caught:
        provider.collect_batting_stats_range(
            date(2026, 3, 25), date(2026, 7, 13), fixture_key="bref"
        )

    assert caught.value.capture.path.read_bytes() == malformed


def test_schedule_preserves_validated_boxscore_identity_and_future_row(
    tmp_path: Path,
) -> None:
    provider, _ = make_provider(tmp_path, "baseball_reference_schedule.html")

    table = provider.collect_table(
        "/teams/LAD/2026-schedule-scores.shtml",
        table_id="team_schedule",
        fixture_key="bref",
    )

    assert table.rows[0]["game_id"] == "LAN202607100"
    assert table.rows[0]["boxscore_href"] == "/boxes/LAN/LAN202607100.shtml"
    assert table.rows[1]["date_game"] == "2026-07-17"
    assert "game_id" not in table.rows[1]
    assert len(table.rows) == 2


@pytest.mark.parametrize(
    "invalid_href",
    [
        "/boxes/?date=2026-02-30",
        "/boxes/?date=2026-7-10",
        "/boxes/?Date=2026-07-10",
        "/boxes/?date=2026-07-10&amp;other=value",
        "/boxes/?date=2026-07-10&amp;date=2026-07-10",
        "/boxes/?date=%32%30%32%36-07-10",
        "/boxes/?date=2026-07-10#fragment",
        "https://www.baseball-reference.com/boxes/?date=2026-07-10",
        "/boxes/LAN/not-a-boxscore.shtml",
    ],
)
def test_invalid_schedule_date_index_links_fail_after_raw_retention(
    tmp_path: Path,
    invalid_href: str,
) -> None:
    source = (FIXTURES / "baseball_reference_schedule.html").read_text("utf-8")
    malformed = source.replace("/boxes/?date=2026-07-10", invalid_href, 1).encode()
    provider, _ = make_provider(tmp_path, malformed)

    with pytest.raises(StatsProviderPayloadError, match="boxscore anchor") as caught:
        provider.collect_table(
            "/teams/LAD/2026-schedule-scores.shtml",
            table_id="team_schedule",
            fixture_key="bref",
        )

    assert caught.value.capture.path.read_bytes() == malformed


def test_schedule_rejects_multiple_canonical_boxscore_identities(
    tmp_path: Path,
) -> None:
    source = (FIXTURES / "baseball_reference_schedule.html").read_bytes()
    original = b'<a href="/boxes/LAN/LAN202607100.shtml">W</a>'
    ambiguous = source.replace(
        original,
        original + b'<a href="/boxes/SDN/SDN202607100.shtml">duplicate</a>',
        1,
    )
    provider, _ = make_provider(tmp_path, ambiguous)

    with pytest.raises(StatsProviderPayloadError, match="ambiguous boxscore") as caught:
        provider.collect_table(
            "/teams/LAD/2026-schedule-scores.shtml",
            table_id="team_schedule",
            fixture_key="bref",
        )

    assert caught.value.capture.path.read_bytes() == ambiguous


def test_schedule_accepts_exact_compact_preview_row_shape(tmp_path: Path) -> None:
    provider, _ = make_provider(
        tmp_path, "baseball_reference_schedule_preview_compact.html"
    )

    table = provider.collect_table(
        "/teams/ARI/2026-schedule-scores.shtml",
        table_id="team_schedule",
        fixture_key="bref",
    )

    assert len(table.columns) == 24
    assert table.rows == (
        {
            "team_game": "97",
            "date_game": "Friday, Jul 17",
            "boxscore": "preview",
            "team_ID": "ARI",
            "homeORvis": "",
            "opp_ID": "STL",
            "starttime": "9:40 pm",
            "preview": "Game Preview, and Matchups",
            "day_or_night": "",
            "cli": "",
            "reschedule": "",
        },
    )
    assert "game_id" not in table.rows[0]
    assert table.raw.path.read_bytes() == (
        FIXTURES / "baseball_reference_schedule_preview_compact.html"
    ).read_bytes()


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        (b'colspan="10"', b'colspan="9"', "unexpected colspans"),
        (b'data-stat="preview"', b'data-stat="unknown"', "unrecognized compact"),
        (b'>ARI</td>', b'></td>', "lacks required team_ID"),
        (b'>preview</a>', b'>final</a>', "preview semantics"),
        (
            b'<td data-stat="reschedule">',
            b'<td data-stat="cli">',
            "duplicate row columns",
        ),
        (
            b'data-stat="homeORvis"></td>',
            b'data-stat="homeORvis">?</td>',
            "ambiguous home/away",
        ),
        (b'>STL</td>', b'>ARI</td>', "ambiguous team identity"),
    ],
)
def test_malformed_compact_schedule_rows_fail_after_raw_retention(
    tmp_path: Path,
    old: bytes,
    new: bytes,
    message: str,
) -> None:
    source = (
        FIXTURES / "baseball_reference_schedule_preview_compact.html"
    ).read_bytes()
    malformed = source.replace(old, new, 1)
    assert malformed != source
    provider, _ = make_provider(tmp_path, malformed)

    with pytest.raises(StatsProviderPayloadError, match=message) as caught:
        provider.collect_table(
            "/teams/ARI/2026-schedule-scores.shtml",
            table_id="team_schedule",
            fixture_key="bref",
        )

    assert caught.value.capture.path.read_bytes() == malformed


def test_compact_schedule_shape_is_not_accepted_for_other_tables(tmp_path: Path) -> None:
    source = (
        FIXTURES / "baseball_reference_schedule_preview_compact.html"
    ).read_bytes().replace(b'id="team_schedule"', b'id="team_batting"', 1)
    provider, _ = make_provider(tmp_path, source)

    with pytest.raises(StatsProviderPayloadError, match="row width") as caught:
        provider.collect_table(
            "/teams/ARI/2026.shtml",
            table_id="team_batting",
            fixture_key="bref",
        )

    assert caught.value.capture.path.read_bytes() == source


def test_daily_identity_schema_and_empty_fail_after_raw_retention(
    tmp_path: Path,
) -> None:
    source = (FIXTURES / "baseball_reference_daily_batting.html").read_bytes()
    malformed_identity, _ = make_provider(
        tmp_path / "identity", source.replace(b"mlb_ID=660271", b"mlb_ID=invalid")
    )
    with pytest.raises(StatsProviderPayloadError, match="identity") as identity_error:
        malformed_identity.collect_batting_stats_range(
            date(2026, 7, 14), date(2026, 7, 14), fixture_key="bref"
        )
    assert identity_error.value.capture.path.exists()

    schema_drift, _ = make_provider(
        tmp_path / "schema", source.replace(b">OPS</th>", b">OPS+</th>")
    )
    with pytest.raises(StatsProviderPayloadError, match="schema") as schema_error:
        schema_drift.collect_batting_stats_range(
            date(2026, 7, 14), date(2026, 7, 14), fixture_key="bref"
        )
    assert schema_error.value.capture.path.exists()

    empty_payload = re.sub(
        rb"<tbody>.*?</tbody>", b"<tbody></tbody>", source, flags=re.DOTALL
    )
    empty, _ = make_provider(tmp_path / "empty", empty_payload)
    with pytest.raises(StatsProviderPayloadError, match="empty") as empty_error:
        empty.collect_batting_stats_range(
            date(2026, 7, 14), date(2026, 7, 14), fixture_key="bref"
        )
    assert empty_error.value.capture.path.exists()


@pytest.mark.parametrize(
    ("start", "end", "error_type", "message"),
    [
        (datetime(2026, 7, 14), date(2026, 7, 14), TypeError, "calendar dates"),
        (date(2007, 12, 31), date(2008, 1, 1), ValueError, "2008"),
        (date(2026, 7, 15), date(2026, 7, 14), ValueError, "must not precede"),
        (date(2025, 1, 1), date(2026, 1, 2), ValueError, "366-day"),
    ],
)
def test_daily_range_validation_is_typed_and_bounded(
    tmp_path: Path,
    start: date,
    end: date,
    error_type: type[Exception],
    message: str,
) -> None:
    provider, transport = make_provider(
        tmp_path, "baseball_reference_daily_batting.html"
    )

    with pytest.raises(error_type, match=message):
        provider.collect_batting_stats_range(start, end, fixture_key="bref")

    assert transport.requests == []


def test_block_page_is_rejected_after_raw_retention(tmp_path: Path) -> None:
    provider, _ = make_provider(tmp_path, "baseball_reference_blocked.html")

    with pytest.raises(StatsProviderPayloadError, match="block page") as caught:
        provider.collect_table(
            "/teams/LAD/2026.shtml", table_id="team_batting", fixture_key="bref"
        )

    assert caught.value.capture.path.exists()
    assert b"Access Denied" in caught.value.capture.path.read_bytes()


def test_wrong_content_type_and_missing_table_fail_closed(tmp_path: Path) -> None:
    wrong_type, _ = make_provider(
        tmp_path / "wrong", "baseball_reference_team.html", content_type="text/plain"
    )
    with pytest.raises(StatsProviderPayloadError, match="content type"):
        wrong_type.collect_table(
            "/teams/LAD/2026.shtml", table_id="team_batting", fixture_key="bref"
        )

    missing, _ = make_provider(tmp_path / "missing", "baseball_reference_team.html")
    with pytest.raises(StatsProviderPayloadError, match="was not present"):
        missing.collect_table(
            "/teams/LAD/2026.shtml", table_id="pitching", fixture_key="bref"
        )


@pytest.mark.parametrize("path", ["http://evil.test/", "//evil.test/path", "/path?token=x"])
def test_path_cannot_escape_controlled_https_origin(tmp_path: Path, path: str) -> None:
    provider, _ = make_provider(tmp_path, "baseball_reference_team.html")

    with pytest.raises(ValueError, match="path|query"):
        provider.collect_table(path, table_id="team_batting", fixture_key="bref")
