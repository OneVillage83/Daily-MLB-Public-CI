from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from app.database import Database
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionMode,
    AcquisitionRequest,
    SourceRunReplayTransport,
    StatsAcquisitionService,
    _SourceRunReplayEntry,
    build_source_run_replay_transport,
    build_stats_transport,
)
from app.stats.contracts import (
    FixtureResponse,
    StatcastQuery,
    StatsProvider,
    StatsProviderPayloadError,
    StatsRequest,
    StatsTransportError,
)
from app.stats.identities import TEAM_SOURCE_ALIASES
from app.stats.providers.statcast import (
    STATCAST_REQUIRED_ANALYTICAL_COLUMNS,
    STATCAST_REQUIRED_IDENTITY_COLUMNS,
    StatcastProvider,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.transport import FixtureStatsTransport


NOW = datetime(2026, 7, 17, 5, 0, tzinfo=timezone.utc)
REQUESTED = date(2026, 7, 14)
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "stats"


def _schedule_page(
    *,
    subject: str,
    opponent: str,
    home: str,
    is_home: bool,
) -> str:
    game_id = f"{home}202607120"
    result = "W" if is_home else "L"
    runs, runs_allowed = (("4", "2") if is_home else ("2", "4"))
    columns = (
        "date_game",
        "opp_ID",
        "homeORvis",
        "status",
        "win_loss_result",
        "R",
        "RA",
        "game_type",
        "boxscore_word",
    )
    header = "".join(f'<th data-stat="{name}">{name}</th>' for name in columns)
    row = (
        "<tr>"
        '<td data-stat="date_game">2026-07-12</td>'
        f'<td data-stat="opp_ID">{opponent}</td>'
        f'<td data-stat="homeORvis">{"" if is_home else "@"}</td>'
        '<td data-stat="status">final</td>'
        f'<td data-stat="win_loss_result">{result}</td>'
        f'<td data-stat="R">{runs}</td>'
        f'<td data-stat="RA">{runs_allowed}</td>'
        '<td data-stat="game_type">R</td>'
        '<td data-stat="boxscore_word">'
        f'<a href="/boxes/{home}/{game_id}.shtml">boxscore</a></td>'
        "</tr>"
    )
    assert subject != opponent
    return (
        '<html><body><table id="team_schedule"><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + row
        + "</tbody></table></body></html>"
    )


def _empty_statcast_csv() -> bytes:
    columns = sorted(
        STATCAST_REQUIRED_IDENTITY_COLUMNS
        | STATCAST_REQUIRED_ANALYTICAL_COLUMNS
        | {"game_number"}
    )
    stream = io.StringIO(newline="")
    csv.DictWriter(stream, fieldnames=columns, lineterminator="\n").writeheader()
    return stream.getvalue().encode("utf-8")


def test_statcast_missing_play_description_column_fails_closed(
    tmp_path: Path,
) -> None:
    columns = sorted(
        (STATCAST_REQUIRED_IDENTITY_COLUMNS | STATCAST_REQUIRED_ANALYTICAL_COLUMNS)
        - {"des"}
    )
    stream = io.StringIO(newline="")
    csv.DictWriter(stream, fieldnames=columns, lineterminator="\n").writeheader()
    transport = FixtureStatsTransport(
        RawArtifactStore(tmp_path / "raw"),
        {
            "missing-des": FixtureResponse(
                stream.getvalue().encode("utf-8"), "text/csv"
            )
        },
        clock=lambda: NOW,
    )

    with pytest.raises(StatsProviderPayloadError, match="'des'"):
        StatcastProvider(transport).collect(
            StatcastQuery(REQUESTED, REQUESTED), fixture_key="missing-des"
        )


def _complete_fixture_root(root: Path) -> Path:
    root.mkdir(parents=True)
    sources = sorted(TEAM_SOURCE_ALIASES["baseball_reference"])
    for index in range(0, len(sources), 2):
        home, away = sources[index : index + 2]
        (root / f"baseball_reference_schedule_{home}.html").write_text(
            _schedule_page(
                subject=home,
                opponent=away,
                home=home,
                is_home=True,
            ),
            encoding="utf-8",
        )
        (root / f"baseball_reference_schedule_{away}.html").write_text(
            _schedule_page(
                subject=away,
                opponent=home,
                home=home,
                is_home=False,
            ),
            encoding="utf-8",
        )
    (root / "baseball_reference_batting.html").write_bytes(
        (FIXTURES / "baseball_reference_daily_batting.html").read_bytes()
    )
    (root / "baseball_reference_pitching.html").write_bytes(
        (FIXTURES / "baseball_reference_daily_pitching.html").read_bytes()
    )
    (root / "statcast_2026-07-12.csv").write_bytes(_empty_statcast_csv())
    return root


def _service(
    *,
    database: Database,
    raw_store: RawArtifactStore,
    transport: object,
    report_path: Path,
) -> StatsAcquisitionService:
    return StatsAcquisitionService(
        database=database,
        raw_store=raw_store,
        transport=transport,  # type: ignore[arg-type]
        report_path=report_path,
        clock=lambda: NOW,
    )


def test_terminal_source_run_replays_without_network_or_duplicate_raw_files(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "stats.db")
    raw_store = RawArtifactStore(tmp_path / "raw")
    fixtures = _complete_fixture_root(tmp_path / "fixtures")
    fixture_transport = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/replay-tests",
        clock=lambda: NOW,
    )
    source = _service(
        database=database,
        raw_store=raw_store,
        transport=fixture_transport,
        report_path=tmp_path / "source.json",
    ).execute(
        AcquisitionCommand.BACKFILL_CURRENT,
        AcquisitionRequest(REQUESTED, mode=AcquisitionMode.OFFLINE),
    )
    assert source.stats_run_id is not None
    source_files = sorted(raw_store.root.rglob("*.bin"))

    replay_transport = build_source_run_replay_transport(
        database=database,
        raw_store=raw_store,
        source_stats_run_id=source.stats_run_id,
        requested_through_date=REQUESTED,
    )
    replay = _service(
        database=database,
        raw_store=raw_store,
        transport=replay_transport,
        report_path=tmp_path / "replay.json",
    ).execute(
        AcquisitionCommand.BACKFILL_CURRENT,
        AcquisitionRequest(
            REQUESTED,
            mode=AcquisitionMode.OFFLINE,
            source_stats_run_id=source.stats_run_id,
        ),
    )

    assert replay.source_stats_run_id == source.stats_run_id
    assert replay.counts["provider_attempts"] == 0
    assert replay.counts["network_requests"] == 0
    assert replay.counts["replayed_source_captures"] == len(source_files)
    assert replay.counts["raw_capture_files"] == 0
    assert sorted(raw_store.root.rglob("*.bin")) == source_files
    with database.connect() as connection:
        source_raw = connection.execute(
            "SELECT source_capture_id,checksum_sha256,retrieved_at "
            "FROM stats_raw_payload_metadata WHERE stats_run_id=? ORDER BY 1",
            (source.stats_run_id,),
        ).fetchall()
        replay_raw = connection.execute(
            "SELECT source_capture_id,checksum_sha256,retrieved_at "
            "FROM stats_raw_payload_metadata WHERE stats_run_id=? ORDER BY 1",
            (replay.stats_run_id,),
        ).fetchall()
    assert [tuple(row) for row in replay_raw] == [tuple(row) for row in source_raw]


def test_replay_transport_fails_closed_on_request_drift_and_checksum_drift(
    tmp_path: Path,
) -> None:
    raw_store = RawArtifactStore(tmp_path / "raw")
    artifact = raw_store.retain_bytes(
        provider=StatsProvider.STATCAST,
        endpoint_category="search_csv",
        payload=b"a,b\n1,2\n",
        retrieved_at=NOW,
        content_type="text/csv",
    )
    expected = StatsRequest(
        provider=StatsProvider.STATCAST,
        endpoint_category="search_csv",
        fixture_key="statcast_2026-07-12",
        url="https://baseballsavant.mlb.com/statcast_search/csv",
    )
    transport = SourceRunReplayTransport(
        raw_store,
        source_stats_run_id="stats_" + "a" * 32,
        entries=(_SourceRunReplayEntry("raw-source", expected, artifact),),
    )
    changed = StatsRequest(
        provider=StatsProvider.STATCAST,
        endpoint_category="search_csv",
        fixture_key=expected.fixture_key,
        url=expected.url,
        params={"unexpected": "value"},
    )
    with pytest.raises(StatsTransportError, match="contract"):
        transport.fetch(changed)
    artifact.path.write_bytes(b"changed")
    with pytest.raises(Exception, match="size|checksum"):
        transport.fetch(expected)
