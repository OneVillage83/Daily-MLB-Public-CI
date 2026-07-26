from __future__ import annotations

import csv
import io
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.identifiers import generate_run_id
from app.stats.acquisition import (
    AcquisitionCommand,
    AcquisitionExecutionError,
    AcquisitionMode,
    AcquisitionOutcome,
    AcquisitionRequest,
    StatsAcquisitionService,
    _canonical_statcast_inning_half,
    _classify_baseball_reference_schedule_row,
    _completed_statcast_game,
    _counting_reconciliation_is_ready,
    _reconcile_completed_statcast_games,
    build_stats_transport,
)
from app.stats.contracts import (
    StatsRequest,
    StatsResponse,
    StatsTransport,
    StatsTransportError,
)
from app.stats.identities import TEAM_SOURCE_ALIASES
from app.stats.normalization import (
    GameStatus,
    ScheduleGame,
    normalize_statcast_rows,
)
from app.stats.raw_store import RawArtifactStore
from app.stats.reporting import report_checksum
from app.stats.repository import StatsRepository


NOW = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
FIXTURE_ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "stats"
STATCAST_COLUMNS = (
    "game_pk",
    "game_date",
    "game_type",
    "at_bat_number",
    "pitch_number",
    "batter",
    "pitcher",
    "home_team",
    "away_team",
    "pitch_type",
    "pitch_name",
    "release_speed",
    "release_spin_rate",
    "release_extension",
    "effective_speed",
    "spin_axis",
    "release_pos_x",
    "release_pos_y",
    "release_pos_z",
    "arm_angle",
    "api_break_x_arm",
    "api_break_x_batter_in",
    "api_break_z_with_gravity",
    "bat_speed",
    "swing_length",
    "attack_angle",
    "attack_direction",
    "swing_path_tilt",
    "miss_distance",
    "hyper_speed",
    "pfx_x",
    "pfx_z",
    "zone",
    "plate_x",
    "plate_z",
    "balls",
    "strikes",
    "description",
    "des",
    "events",
    "inning",
    "inning_topbot",
    "outs_when_up",
    "launch_speed",
    "launch_angle",
    "launch_speed_angle",
    "estimated_ba_using_speedangle",
    "estimated_slg_using_speedangle",
    "estimated_woba_using_speedangle",
    "woba_value",
    "woba_denom",
    "babip_value",
    "iso_value",
    "home_score",
    "away_score",
    "post_home_score",
    "post_away_score",
    "game_number",
    "delta_home_win_exp",
    "delta_run_exp",
    "stand",
    "p_throws",
    "bb_type",
)


def _schedule_html(july_16_status: str) -> str:
    def row(
        day: int,
        status: str,
        *,
        runs: str = "",
        runs_allowed: str = "",
    ) -> str:
        game_id = f"LAN202607{day:02d}0"
        result = "W" if status == "final" else ""
        return (
            "<tr>"
            f'<td data-stat="date_game">2026-07-{day:02d}</td>'
            '<td data-stat="opp_ID">SFG</td>'
            '<td data-stat="homeORvis"></td>'
            f'<td data-stat="status">{status}</td>'
            f'<td data-stat="win_loss_result">{result}</td>'
            f'<td data-stat="R">{runs}</td>'
            f'<td data-stat="RA">{runs_allowed}</td>'
            '<td data-stat="game_type">R</td>'
            f'<td data-stat="boxscore_word"><a href="/boxes/LAN/{game_id}.shtml">boxscore</a></td>'
            "</tr>"
        )

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
    july_16_runs = ("5", "2") if july_16_status == "final" else ("", "")
    return (
        '<html><body><table id="team_schedule"><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + row(12, "final", runs="4", runs_allowed="1")
        + row(
            16,
            july_16_status,
            runs=july_16_runs[0],
            runs_allowed=july_16_runs[1],
        )
        + "</tbody></table></body></html>"
    )


def _away_schedule_html(july_16_status: str) -> str:
    def row(
        day: int,
        status: str,
        *,
        home_runs: str = "",
        away_runs: str = "",
    ) -> str:
        game_id = f"LAN202607{day:02d}0"
        result = "L" if status == "final" else ""
        return (
            "<tr>"
            f'<td data-stat="date_game">2026-07-{day:02d}</td>'
            '<td data-stat="opp_ID">LAD</td>'
            '<td data-stat="homeORvis">@</td>'
            f'<td data-stat="status">{status}</td>'
            f'<td data-stat="win_loss_result">{result}</td>'
            f'<td data-stat="R">{away_runs}</td>'
            f'<td data-stat="RA">{home_runs}</td>'
            '<td data-stat="game_type">R</td>'
            f'<td data-stat="boxscore_word"><a href="/boxes/LAN/{game_id}.shtml">boxscore</a></td>'
            "</tr>"
        )

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
    july_16_runs = ("5", "2") if july_16_status == "final" else ("", "")
    return (
        '<html><body><table id="team_schedule"><thead><tr>'
        + header
        + "</tr></thead><tbody>"
        + row(12, "final", home_runs="4", away_runs="1")
        + row(
            16,
            july_16_status,
            home_runs=july_16_runs[0],
            away_runs=july_16_runs[1],
        )
        + "</tbody></table></body></html>"
    )


def _with_april_opening_day(schedule_html: str) -> str:
    opening_row = (
        "<tr>"
        '<td data-stat="date_game">2026-04-01</td>'
        '<td data-stat="opp_ID">SFG</td>'
        '<td data-stat="homeORvis"></td>'
        '<td data-stat="status">final</td>'
        '<td data-stat="win_loss_result">W</td>'
        '<td data-stat="R">4</td>'
        '<td data-stat="RA">1</td>'
        '<td data-stat="game_type">R</td>'
        '<td data-stat="boxscore_word">'
        '<a href="/boxes/LAN/LAN202604010.shtml">boxscore</a></td>'
        "</tr>"
    )
    return schedule_html.replace("<tbody>", f"<tbody>{opening_row}", 1)


def _append_home_schedule_game(
    schedule_html: str,
    *,
    day: int,
    status: str,
) -> str:
    row = (
        "<tr>"
        f'<td data-stat="date_game">2026-07-{day:02d}</td>'
        '<td data-stat="opp_ID">SFG</td>'
        '<td data-stat="homeORvis"></td>'
        f'<td data-stat="status">{status}</td>'
        '<td data-stat="win_loss_result"></td>'
        '<td data-stat="R"></td>'
        '<td data-stat="RA"></td>'
        '<td data-stat="game_type">R</td>'
        '<td data-stat="boxscore_word">'
        f'<a href="/boxes/LAN/LAN202607{day:02d}0.shtml">boxscore</a></td>'
        "</tr>"
    )
    return schedule_html.replace("</tbody>", f"{row}</tbody>", 1)


def _pitch_row(
    day: int,
    *,
    speed: str = "95.0",
    batter: str | None = None,
    pitcher: str | None = None,
    game_pk: str | None = None,
    at_bat_number: int = 1,
    pitch_number: int = 1,
    inning: int = 1,
    inning_half: str = "Top",
    outs_when_up: int = 0,
    event: str = "strikeout",
    home_score: int = 0,
    away_score: int = 0,
    post_home_score: int = 0,
    post_away_score: int = 0,
    game_number: int | None = None,
) -> dict[str, str]:
    values = {column: "" for column in STATCAST_COLUMNS}
    values.update(
        {
            "game_pk": game_pk or f"{day}001",
            "game_date": f"2026-07-{day:02d}",
            "game_type": "R",
            "at_bat_number": str(at_bat_number),
            "pitch_number": str(pitch_number),
            "batter": batter or f"{day}101",
            "pitcher": pitcher or f"{day}201",
            "home_team": "LAD",
            "away_team": "SF",
            "pitch_type": "FF",
            "pitch_name": "4-Seam Fastball",
            "release_speed": speed,
            "release_spin_rate": "2200",
            "release_extension": "6.0",
            "pfx_x": "0.0",
            "pfx_z": "1.0",
            "zone": "5",
            "plate_x": "0",
            "plate_z": "2.5",
            "balls": "0",
            "strikes": "0",
            "description": "called_strike",
            "events": event,
            "inning": str(inning),
            "inning_topbot": inning_half,
            "outs_when_up": str(outs_when_up),
            "launch_speed": "0",
            "launch_angle": "0",
            "launch_speed_angle": "0",
            "estimated_ba_using_speedangle": "0",
            "estimated_slg_using_speedangle": "0",
            "estimated_woba_using_speedangle": "0",
            "woba_value": "0",
            "woba_denom": "1",
            "babip_value": "0",
            "iso_value": "0",
            "home_score": str(home_score),
            "away_score": str(away_score),
            "post_home_score": str(post_home_score),
            "post_away_score": str(post_away_score),
            "game_number": str(game_number) if game_number is not None else "",
            "delta_home_win_exp": "0",
            "delta_run_exp": "0",
            "stand": "R",
            "p_throws": "R",
            "bb_type": "",
        }
    )
    return values


def _completed_game_rows(
    day: int,
    *,
    game_pk: str | None = None,
    home_score: int | None = None,
    away_score: int | None = None,
    game_number: int | None = None,
) -> list[dict[str, str]]:
    final_home = home_score if home_score is not None else (4 if day == 12 else 5)
    final_away = away_score if away_score is not None else (1 if day == 12 else 2)
    rows: list[dict[str, str]] = []
    at_bat = 0
    for inning in range(1, 10):
        halves = ("Top", "Bot") if inning < 9 else ("Top",)
        for half in halves:
            for outs_before in range(3):
                at_bat += 1
                row = _pitch_row(
                    day,
                    game_pk=game_pk,
                    at_bat_number=at_bat,
                    inning=inning,
                    inning_half=half,
                    outs_when_up=outs_before,
                    home_score=final_home,
                    away_score=final_away,
                    post_home_score=final_home,
                    post_away_score=final_away,
                    game_number=game_number,
                )
                if not (inning == 9 and half == "Top" and outs_before == 2):
                    row["launch_speed"] = ""
                    row["launch_angle"] = ""
                    row["launch_speed_angle"] = ""
                    row["estimated_ba_using_speedangle"] = ""
                    row["estimated_slg_using_speedangle"] = ""
                    row["estimated_woba_using_speedangle"] = ""
                rows.append(row)
    return rows


def _final_schedule_game(
    *,
    occurrence: int,
    home_score: int,
    away_score: int,
    source_sequence: int = 0,
) -> tuple[ScheduleGame, str, Any]:
    return (
        ScheduleGame(
            provider_game_id=(
                f"bref:2026-07-16:SF:LAD:{occurrence}"
            ),
            official_date=date(2026, 7, 16),
            home_team_key="LAD",
            away_team_key="SF",
            status=GameStatus.FINAL,
            status_reason="fixture_final",
            home_score=home_score,
            away_score=away_score,
            source_row={"game_id": f"LAN20260716{source_sequence}"},
        ),
        "raw",
        object(),
    )


def test_one_pitch_statcast_payload_is_not_complete_game_evidence() -> None:
    normalized = normalize_statcast_rows([_pitch_row(16)])

    assert _completed_statcast_game(normalized.pitches) is None
    reconciliation = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert reconciliation.schedule_to_game_pk == {}
    assert reconciliation.completed_game_pks == frozenset()


def test_statcast_inning_half_canonicalization_is_strict() -> None:
    assert _canonical_statcast_inning_half("Top") == "top"
    assert _canonical_statcast_inning_half("TOP") == "top"
    assert _canonical_statcast_inning_half("Bot") == "bottom"
    assert _canonical_statcast_inning_half("Bottom") == "bottom"
    assert _canonical_statcast_inning_half("middle") is None
    assert _canonical_statcast_inning_half("") is None
    assert _canonical_statcast_inning_half(None) is None


def test_unknown_statcast_inning_half_fails_completed_game_contract() -> None:
    rows = _completed_game_rows(16)
    rows[0]["inning_topbot"] = "Middle"

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is None


def test_bottom_statcast_label_remains_backward_compatible() -> None:
    rows = _completed_game_rows(16)
    for row in rows:
        if row["inning_topbot"] == "Bot":
            row["inning_topbot"] = "Bottom"

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is not None


def test_schema_valid_statcast_payload_with_a_missing_pitch_fails_closed() -> None:
    rows = _completed_game_rows(16)
    rows[10]["pitch_number"] = "2"

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is None
    reconciliation = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert reconciliation.schedule_to_game_pk == {}


def test_final_schedule_confirms_legitimate_pitch_only_at_bat_gap() -> None:
    rows = _completed_game_rows(16)
    for row in rows:
        at_bat_number = int(row["at_bat_number"])
        if at_bat_number >= 10:
            row["at_bat_number"] = str(at_bat_number + 1)

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is None
    reconciliation = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert reconciliation.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001
    }


def test_final_schedule_confirms_half_ending_without_a_pitch_event() -> None:
    rows = _completed_game_rows(16)
    transition = next(
        row
        for row in rows
        if row["inning"] == "4"
        and row["inning_topbot"] == "Bot"
        and row["outs_when_up"] == "2"
    )
    transition["events"] = "single"
    transition["description"] = "hit_into_play"

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is None
    reconciliation = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert reconciliation.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001
    }


def test_final_schedule_confirms_provider_truncated_terminal_plate_appearance() -> None:
    rows = _completed_game_rows(16)
    terminal = rows[-1]
    terminal["events"] = "truncated_pa"
    terminal["description"] = "ball"

    normalized = normalize_statcast_rows(rows)

    assert _completed_statcast_game(normalized.pitches) is None
    reconciliation = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert reconciliation.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001
    }


def test_complete_statcast_game_requires_terminal_score_reconciliation() -> None:
    normalized = normalize_statcast_rows(_completed_game_rows(16))

    evidence = _completed_statcast_game(normalized.pitches)
    assert evidence is not None
    assert (evidence.home_score, evidence.away_score) == (5, 2)
    matching = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=5, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert matching.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001
    }

    mismatched = _reconcile_completed_statcast_games(
        [_final_schedule_game(occurrence=1, home_score=6, away_score=2)],
        normalized.pitches,
        normalized.conflicts,
    )
    assert mismatched.schedule_to_game_pk == {}
    assert mismatched.validation_failed_schedule_ids == frozenset(
        {"bref:2026-07-16:SF:LAD:1"}
    )


def test_doubleheader_matches_by_final_score_without_count_order() -> None:
    normalized = normalize_statcast_rows(
        [
            *_completed_game_rows(
                16, game_pk="16002", home_score=3, away_score=1
            ),
            *_completed_game_rows(
                16, game_pk="16001", home_score=5, away_score=2
            ),
        ]
    )
    schedules = [
        _final_schedule_game(occurrence=2, home_score=3, away_score=1, source_sequence=2),
        _final_schedule_game(occurrence=1, home_score=5, away_score=2, source_sequence=1),
    ]

    reconciliation = _reconcile_completed_statcast_games(
        schedules, normalized.pitches, normalized.conflicts
    )

    assert reconciliation.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001,
        "bref:2026-07-16:SF:LAD:2": 16002,
    }


def test_same_score_doubleheader_fails_closed_without_explicit_statcast_sequence() -> None:
    normalized = normalize_statcast_rows(
        [
            *_completed_game_rows(
                16, game_pk="16001", home_score=5, away_score=2
            ),
            *_completed_game_rows(
                16, game_pk="16002", home_score=5, away_score=2
            ),
        ]
    )
    schedules = [
        _final_schedule_game(occurrence=1, home_score=5, away_score=2, source_sequence=1),
        _final_schedule_game(occurrence=2, home_score=5, away_score=2, source_sequence=2),
    ]

    reconciliation = _reconcile_completed_statcast_games(
        schedules, normalized.pitches, normalized.conflicts
    )

    assert reconciliation.schedule_to_game_pk == {}
    assert reconciliation.completed_game_pks == frozenset()


def test_same_score_doubleheader_uses_explicit_sequences_on_both_sources() -> None:
    normalized = normalize_statcast_rows(
        [
            *_completed_game_rows(
                16,
                game_pk="16002",
                home_score=5,
                away_score=2,
                game_number=2,
            ),
            *_completed_game_rows(
                16,
                game_pk="16001",
                home_score=5,
                away_score=2,
                game_number=1,
            ),
        ]
    )
    schedules = [
        _final_schedule_game(occurrence=2, home_score=5, away_score=2, source_sequence=2),
        _final_schedule_game(occurrence=1, home_score=5, away_score=2, source_sequence=1),
    ]

    reconciliation = _reconcile_completed_statcast_games(
        schedules, normalized.pitches, normalized.conflicts
    )

    assert reconciliation.schedule_to_game_pk == {
        "bref:2026-07-16:SF:LAD:1": 16001,
        "bref:2026-07-16:SF:LAD:2": 16002,
    }


def _csv_bytes(rows: Sequence[Mapping[str, str]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=STATCAST_COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _fixture_root(
    root: Path,
    *,
    july_16_status: str,
    july_16_rows: Sequence[Mapping[str, str]] | None,
) -> Path:
    root.mkdir(parents=True)
    (root / "baseball_reference_schedule_LAD.html").write_text(
        _schedule_html(july_16_status), encoding="utf-8"
    )
    (root / "baseball_reference_batting.html").write_text(
        (FIXTURE_ROOT / "baseball_reference_daily_batting.html").read_text("utf-8"),
        encoding="utf-8",
    )
    (root / "baseball_reference_pitching.html").write_text(
        (FIXTURE_ROOT / "baseball_reference_daily_pitching.html").read_text("utf-8"),
        encoding="utf-8",
    )
    (root / "statcast_empty.csv").write_bytes(_csv_bytes([]))
    (root / "statcast_2026-07-12.csv").write_bytes(
        _csv_bytes(_completed_game_rows(12))
    )
    (root / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(july_16_rows or [])
    )
    return root


def _align_aggregate_fixtures_with_default_statcast(
    fixtures: Path, *, pitcher_batters_faced: int
) -> None:
    batting_path = fixtures / "baseball_reference_batting.html"
    batting = batting_path.read_text(encoding="utf-8")
    replacements = {
        "mlb_ID=660271": "mlb_ID=12101",
        '<td data-stat="plate_appearances">4</td>': '<td data-stat="plate_appearances">51</td>',
        '<td data-stat="at_bats">4</td>': '<td data-stat="at_bats">51</td>',
        '<td data-stat="hits">2</td>': '<td data-stat="hits">0</td>',
        '<td data-stat="home_runs">0</td>': '<td data-stat="home_runs">0</td>',
        '<td data-stat="bases_on_balls">0</td>': '<td data-stat="bases_on_balls">0</td>',
        '<td data-stat="strikeouts">1</td>': '<td data-stat="strikeouts">51</td>',
    }
    for before, after in replacements.items():
        batting = batting.replace(before, after)
    batting_path.write_text(batting, encoding="utf-8")

    pitching_path = fixtures / "baseball_reference_pitching.html"
    pitching = pitching_path.read_text(encoding="utf-8")
    pitching_replacements = {
        "mlb_ID=605400": "mlb_ID=12201",
        '<td data-stat="hits">4</td>': '<td data-stat="hits">0</td>',
        '<td data-stat="strikeouts">8</td>': '<td data-stat="strikeouts">51</td>',
        '<td data-stat="batters_faced">22</td>': (
            f'<td data-stat="batters_faced">{pitcher_batters_faced}</td>'
        ),
    }
    for before, after in pitching_replacements.items():
        pitching = pitching.replace(before, after)
    pitching_path.write_text(pitching, encoding="utf-8")


def _service(
    root: Path,
    fixtures: Path,
    *,
    transport: StatsTransport | None = None,
) -> tuple[StatsAcquisitionService, Database, StatsTransport]:
    database = Database(root / "stats.db")
    raw_store = RawArtifactStore(root / "raw")
    selected = transport or build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/offline-tests",
        clock=lambda: NOW,
    )
    return (
        StatsAcquisitionService(
            database=database,
            raw_store=raw_store,
            transport=selected,
            report_path=root / "validation.json",
            clock=lambda: NOW,
        ),
        database,
        selected,
    )


def _execute(
    service: StatsAcquisitionService,
    command: AcquisitionCommand,
    *,
    resume_run_id: str | None = None,
    requested_date: date = date(2026, 7, 16),
) -> Any:
    return service.execute(
        command,
        AcquisitionRequest(
            requested_date,
            mode=AcquisitionMode.OFFLINE,
            resume_run_id=resume_run_id,
        ),
    )


def _scalar(database: Database, sql: str, parameters: tuple[object, ...] = ()) -> int:
    with database.connect() as connection:
        return int(connection.execute(sql, parameters).fetchone()[0])


def test_team_identity_reuses_stable_facts_for_out_of_order_artifacts(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path / "run", FIXTURE_ROOT)
    latest = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    earliest = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)

    first = service._team_identity("baseball_reference", "LAD", latest)
    second = service._team_identity("baseball_reference", "LAD", earliest)
    stored = service.repository.get_team_identity("baseball_reference", "LAD")

    assert first == second == ("team:baseball_reference:LAD", "LAD")
    assert stored is not None
    assert stored["first_seen_at"] == latest.isoformat()
    assert stored["last_seen_at"] == latest.isoformat()


def test_team_identity_rejects_existing_canonical_fact_conflict(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path / "run", FIXTURE_ROOT)
    service.repository.upsert_team_identity(
        {
            "team_identity_id": "team:baseball_reference:LAD",
            "provider": "baseball_reference",
            "provider_team_id": "LAD",
            "canonical_team_key": "NYY",
            "current_name": "NYY",
            "active": True,
            "first_seen_at": NOW,
            "last_seen_at": NOW,
            "identity_checksum": "a" * 64,
        }
    )

    with pytest.raises(
        AcquisitionExecutionError,
        match="conflicts with the stable canonical mapping",
    ):
        service._team_identity("baseball_reference", "LAD", NOW)


def test_game_identity_reuses_identical_facts_for_older_replayed_capture(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path / "run", FIXTURE_ROOT)
    latest = datetime(2026, 7, 14, 20, 0, tzinfo=timezone.utc)
    earliest = datetime(2026, 7, 14, 18, 0, tzinfo=timezone.utc)
    home_id, _ = service._team_identity("baseball_reference", "LAD", latest)
    away_id, _ = service._team_identity("baseball_reference", "SFG", latest)

    first = service._stable_game_identity(
        provider="baseball_reference",
        provider_game_id="bref:2026-07-12:SF:LAD:1",
        official_date=date(2026, 7, 12),
        home_team_identity_id=home_id,
        away_team_identity_id=away_id,
        observed_at=latest,
    )
    second = service._stable_game_identity(
        provider="baseball_reference",
        provider_game_id="bref:2026-07-12:SF:LAD:1",
        official_date=date(2026, 7, 12),
        home_team_identity_id=home_id,
        away_team_identity_id=away_id,
        observed_at=earliest,
    )
    stored = service.repository.get_game_identity(
        "baseball_reference", "bref:2026-07-12:SF:LAD:1"
    )

    assert first == second == "game:baseball_reference:bref:2026-07-12:SF:LAD:1"
    assert stored is not None
    assert stored["last_seen_at"] == latest.isoformat()


def test_game_identity_replay_does_not_hide_conflicting_facts(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path / "run", FIXTURE_ROOT)
    home_id, _ = service._team_identity("baseball_reference", "LAD", NOW)
    away_id, _ = service._team_identity("baseball_reference", "SFG", NOW)
    service._stable_game_identity(
        provider="baseball_reference",
        provider_game_id="bref:2026-07-12:SF:LAD:1",
        official_date=date(2026, 7, 12),
        home_team_identity_id=home_id,
        away_team_identity_id=away_id,
        observed_at=NOW,
    )

    with pytest.raises(AcquisitionExecutionError, match="stable canonical mapping"):
        service._stable_game_identity(
            provider="baseball_reference",
            provider_game_id="bref:2026-07-12:SF:LAD:1",
            official_date=date(2026, 7, 12),
            home_team_identity_id=away_id,
            away_team_identity_id=home_id,
            observed_at=NOW,
        )


def test_cached_persistence_rerun_reuses_team_identity_after_partial_failure(
    tmp_path: Path,
) -> None:
    root = tmp_path / "run"
    initial, _, _ = _service(root, FIXTURE_ROOT)
    cached_retrieval = datetime(2026, 7, 14, 21, 0, tzinfo=timezone.utc)
    earlier_cached_schedule = datetime(2026, 7, 14, 19, 0, tzinfo=timezone.utc)
    initial._team_identity("baseball_reference", "LAD", cached_retrieval)

    resumed, _, _ = _service(root, FIXTURE_ROOT)
    assert resumed._team_identity(
        "baseball_reference", "LAD", earlier_cached_schedule
    ) == ("team:baseball_reference:LAD", "LAD")
    assert resumed._team_identity(
        "baseball_reference", "LAD", earlier_cached_schedule
    ) == ("team:baseball_reference:LAD", "LAD")
    stored = resumed.repository.get_team_identity("baseball_reference", "LAD")

    assert stored is not None
    assert stored["first_seen_at"] == cached_retrieval.isoformat()
    assert stored["last_seen_at"] == cached_retrieval.isoformat()


@pytest.mark.parametrize(
    "status",
    ["scheduled", "in_progress", "postponed_rescheduled", "suspended_pending"],
)
def test_nonfinal_july_16_is_not_queried_or_persisted_past_effective_final_date(
    tmp_path: Path,
    status: str,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status=status,
        july_16_rows=[_pitch_row(16)],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=51
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert result.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 12
    )
    assert result.completeness.partial_date == date(2026, 7, 16)
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_identities WHERE game_pk=16001",
    ) == 0
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_feature_snapshots "
        "WHERE features_json LIKE '%player:statcast:16101%'",
    ) == 0
    raw_payloads = list((tmp_path / "run" / "raw").rglob("*.bin"))
    assert not any(b"16001" in path.read_bytes() for path in raw_payloads)


@pytest.mark.parametrize(
    "status",
    ["scheduled", "in_progress", "postponed_rescheduled", "suspended_pending"],
)
def test_daily_nonfinal_july_16_retains_raw_without_completed_facts(
    tmp_path: Path,
    status: str,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status=status,
        july_16_rows=[_pitch_row(16)],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=51
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    initial = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    assert initial.completeness is not None
    assert initial.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 12
    )

    service, database, transport = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert result.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 12
    )
    assert result.completeness.partial_date == date(2026, 7, 16)
    assert result.completeness.partial_date_reason == (
        "scheduled_regular_season_game_not_final"
        if status == "scheduled"
        else (
            "regular_season_game_in_progress"
            if status == "in_progress"
            else (
                "postponed_regular_season_game_unresolved"
                if status == "postponed_rescheduled"
                else "suspended_regular_season_game_pending"
            )
        )
    )
    assert "statcast_2026-07-16" in [
        request.fixture_key for request in transport.requests  # type: ignore[attr-defined]
    ]
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_identities WHERE game_pk=16001",
    ) == 0
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_feature_snapshots "
        "WHERE features_json LIKE '%player:statcast:16101%'",
    ) == 0
    raw_payloads = list((tmp_path / "run" / "raw").rglob("*.bin"))
    assert any(b"16001" in path.read_bytes() for path in raw_payloads)


def test_daily_uses_generic_statcast_fixture_only_without_date_fixture(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    (fixtures / "statcast_2026-07-16.csv").unlink()
    (fixtures / "statcast.csv").write_bytes(_csv_bytes(_completed_game_rows(16)))
    service, _, transport = _service(tmp_path / "run", fixtures)

    _execute(service, AcquisitionCommand.DAILY)

    fixture_keys = [
        request.fixture_key for request in transport.requests  # type: ignore[attr-defined]
    ]
    assert "statcast" in fixture_keys
    assert "statcast_empty" not in fixture_keys


def test_final_game_advances_only_after_successful_pitch_normalization(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=[],
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    incomplete = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    assert incomplete.completeness is not None
    assert incomplete.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 12
    )
    assert incomplete.completeness.partial_date_reason == (
        "final_regular_season_game_not_fully_validated"
    )

    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(_completed_game_rows(16))
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    complete = _execute(service, AcquisitionCommand.DAILY)
    assert complete.completeness is not None
    assert complete.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 16
    )
    assert complete.completeness.latest_ingested_completed_game_date == date(2026, 7, 16)
    assert complete.completeness.partial_date is None
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_revisions revision "
        "JOIN statcast_pitch_identities identity "
        "ON identity.pitch_identity_id=revision.pitch_identity_id "
        "WHERE identity.game_pk=16001",
    ) == len(_completed_game_rows(16))
    with database.connect() as connection:
        status_row = connection.execute(
            "SELECT status.status_json,status.source_checksum,raw.checksum_sha256 "
            "FROM stats_game_status_observations AS status "
            "JOIN stats_game_identities AS game "
            "ON game.game_identity_id=status.game_identity_id "
            "JOIN stats_raw_payload_metadata AS raw "
            "ON raw.raw_payload_id=status.raw_payload_id "
            "WHERE game.provider='statcast' AND game.provider_game_id='16001'"
        ).fetchone()
        assert status_row is not None
        status_payload = json.loads(str(status_row["status_json"]))
    assert status_row["source_checksum"] != status_row["checksum_sha256"]
    assert status_payload["completion_source_evidence"]["raw_capture_checksum"] == (
        status_row["checksum_sha256"]
    )
    assert status_payload["completion_source_evidence"]["contract"] == (
        "DSE_STATCAST_GAME_COMPLETION_EVIDENCE_V1"
    )
    assert status_payload["completion_contract"] == (
        "DSE_STATCAST_SCHEDULE_CONFIRMED_FINAL_GAME_V1"
    )
    assert status_payload["baseball_reference_game_id"] == (
        "bref:2026-07-16:SF:LAD:1"
    )


def test_final_schedule_with_truncated_statcast_stays_raw_only(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=[_pitch_row(16)],
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date is None
    assert result.completeness.partial_date == date(2026, 7, 16)
    assert result.completeness.partial_date_reason == (
        "final_regular_season_game_not_fully_validated"
    )
    assert _scalar(database, "SELECT COUNT(*) FROM statcast_pitch_revisions") == 0
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_identities WHERE provider='statcast'",
    ) == 0
    retained = list((tmp_path / "run" / "raw").rglob("*.bin"))
    assert any(b"16001" in path.read_bytes() for path in retained)


def test_final_daily_capture_supersedes_partial_raw_without_deleting_evidence(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[_pitch_row(16)],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=51
    )
    service, _, _ = _service(tmp_path / "run", fixtures)
    _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    service, database, _ = _service(tmp_path / "run", fixtures)
    partial = _execute(service, AcquisitionCommand.DAILY)
    assert partial.completeness is not None
    assert partial.completeness.partial_date == date(2026, 7, 16)

    partial_paths = [
        path
        for path in (tmp_path / "run" / "raw").rglob("*.bin")
        if b"16001" in path.read_bytes()
    ]
    assert len(partial_paths) == 1
    partial_path = partial_paths[0]
    partial_bytes = partial_path.read_bytes()

    (fixtures / "baseball_reference_schedule_LAD.html").write_text(
        _schedule_html("final"), encoding="utf-8"
    )
    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(_completed_game_rows(16))
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    completed = _execute(service, AcquisitionCommand.DAILY)

    assert completed.completeness is not None
    assert completed.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 16
    )
    assert completed.completeness.latest_ingested_completed_game_date == date(2026, 7, 16)
    assert completed.completeness.partial_date is None
    assert partial_path.read_bytes() == partial_bytes
    completed_paths = [
        path
        for path in (tmp_path / "run" / "raw").rglob("*.bin")
        if b"16001" in path.read_bytes()
    ]
    assert len(completed_paths) == 2
    assert len({path.name for path in completed_paths}) == 2
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_identities WHERE game_pk=16001",
    ) == len(_completed_game_rows(16))
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_revisions revision "
        "JOIN statcast_pitch_identities identity "
        "ON identity.pitch_identity_id=revision.pitch_identity_id "
        "WHERE identity.game_pk=16001",
    ) == len(_completed_game_rows(16))


def test_repairing_an_earlier_partial_date_preserves_a_later_unresolved_date(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="suspended_pending",
        july_16_rows=[_pitch_row(16)],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=51
    )
    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule_path.write_text(
        _append_home_schedule_game(
            schedule_path.read_text("utf-8"),
            day=18,
            status="scheduled",
        ),
        encoding="utf-8",
    )
    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(_completed_game_rows(16))
    )
    service, _, _ = _service(tmp_path / "run", fixtures)
    initial = _execute(
        service,
        AcquisitionCommand.BACKFILL_CURRENT,
        requested_date=date(2026, 7, 18),
    )
    assert initial.completeness is not None
    assert initial.completeness.partial_date == date(2026, 7, 16)
    assert initial.completeness.latest_ingested_completed_game_date == date(
        2026, 7, 12
    )

    schedule_path.write_text(
        _append_home_schedule_game(
            _schedule_html("final"),
            day=18,
            status="scheduled",
        ),
        encoding="utf-8",
    )
    service, _, _ = _service(tmp_path / "run", fixtures)
    repaired = _execute(service, AcquisitionCommand.DAILY)

    assert repaired.completeness is not None
    assert repaired.completeness.latest_ingested_completed_game_date == date(
        2026, 7, 16
    )
    assert repaired.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 16
    )
    assert repaired.completeness.partial_date == date(2026, 7, 18)
    assert (
        repaired.completeness.partial_date_reason
        == "scheduled_regular_season_game_not_final"
    )
def test_rerun_is_idempotent_and_conflicting_pitch_is_revisioned(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    initial = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    games_before = _scalar(database, "SELECT COUNT(*) FROM stats_game_identities")
    revisions_before = _scalar(database, "SELECT COUNT(*) FROM statcast_pitch_revisions")
    assert initial.counts["statcast_pitches"] == revisions_before
    assert initial.counts["statcast_revisions_inserted"] == revisions_before

    service, database, _ = _service(tmp_path / "run", fixtures)
    repeated = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    assert _scalar(database, "SELECT COUNT(*) FROM stats_game_identities") == games_before
    assert _scalar(database, "SELECT COUNT(*) FROM statcast_pitch_revisions") == revisions_before
    assert repeated.counts["statcast_pitches"] == revisions_before
    assert repeated.counts["statcast_revisions_inserted"] == 0

    corrected_rows = _completed_game_rows(16)
    corrected_rows[-1]["release_speed"] = "96.0"
    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(corrected_rows)
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    corrected = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)
    assert _scalar(database, "SELECT COUNT(*) FROM statcast_pitch_revisions") == (
        revisions_before + 1
    )
    assert corrected.counts["statcast_pitches"] == revisions_before
    assert corrected.counts["statcast_revisions_inserted"] == 1
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_revisions WHERE revision_kind='correction'",
    ) == 1


def test_conflicting_duplicate_pitch_in_one_capture_is_recorded(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=[
            *_completed_game_rows(16),
            {**_completed_game_rows(16)[-1], "release_speed": "97.0"},
        ],
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    result = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    assert "statcast_pitch_conflicts" in result.warnings
    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert (
        result.completeness.contiguous_regular_season_complete_through_date
        == date(2026, 7, 12)
    )
    assert result.completeness.partial_date == date(2026, 7, 16)
    assert (
        result.completeness.partial_date_reason
        == "regular_season_game_validation_failed"
    )
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_conflicts "
        "WHERE conflict_code='statcast_pitch_revision_conflict'",
    ) == 1
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM statcast_pitch_revisions revision "
        "JOIN statcast_pitch_identities identity "
        "ON identity.pitch_identity_id=revision.pitch_identity_id "
        "WHERE identity.game_pk=16001",
    ) == 0


class _InterruptingTransport:
    def __init__(self, delegate: StatsTransport, *, interrupt_fixture: str) -> None:
        self.delegate = delegate
        self.interrupt_fixture = interrupt_fixture
        self.requests: list[str] = []

    def fetch(self, request: StatsRequest) -> StatsResponse:
        self.requests.append(request.fixture_key)
        if request.fixture_key == self.interrupt_fixture:
            raise KeyboardInterrupt("simulated process interruption")
        return self.delegate.fetch(request)


class _OpaqueFixtureTransport:
    """Exercise live coverage rules without exposing the fixture delegate type."""

    def __init__(self, delegate: StatsTransport) -> None:
        self._delegate = delegate

    def fetch(self, request: StatsRequest) -> StatsResponse:
        return self._delegate.fetch(request)


class _TerminalFailureTransport:
    def fetch(self, request: StatsRequest) -> StatsResponse:
        raise StatsTransportError(
            "terminal provider error for ?token=test-only-sensitive-value",
            attempts=3,
            captures=(),
        )


def test_terminal_transport_failure_uses_typed_report_contract(
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "validation.json"
    raw_store = RawArtifactStore(tmp_path / "raw")
    service = StatsAcquisitionService(
        database=Database(tmp_path / "stats.db"),
        raw_store=raw_store,
        transport=_TerminalFailureTransport(),
        report_path=report_path,
        clock=lambda: NOW,
    )

    with pytest.raises(AcquisitionExecutionError):
        service.execute(
            AcquisitionCommand.BOOTSTRAP_RETROSHEET,
            AcquisitionRequest(date(2025, 12, 31), mode=AcquisitionMode.LIVE),
        )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    failure = report["acquisition_runs"][0]
    assert failure["outcome_contract_version"] == (
        "DSE_STATS_ACQUISITION_OUTCOME_V1"
    )
    assert failure["outcome"] == AcquisitionOutcome.FAILED.value
    assert failure["status"] == "failed"
    assert failure["exit_code"] == 1
    assert failure["partial_date"] is None
    assert failure["partial_date_reason"] is None
    assert failure["counts"] == {
        "provider_attempts": 3,
        "raw_captures_observed": 0,
    }
    assert failure["run_id"]
    assert failure["stats_run_id"]
    assert failure["requested_through_date"] == "2025-12-31"
    assert failure["failure"]["error_type"] == "StatsTransportError"
    assert failure["failure"]["failure_stage"] == "acquisition"
    assert "test-only-sensitive-value" not in failure["failure"]["message"]
    result_payload = {
        key: value
        for key, value in failure.items()
        if key not in {"result_checksum", "validation_checksum"}
    }
    assert failure["result_checksum"] == report_checksum(result_payload)
    validation_payload = {
        key: value for key, value in failure.items() if key != "validation_checksum"
    }
    assert failure["validation_checksum"] == report_checksum(validation_payload)


def test_live_schedule_collection_requires_all_30_active_club_sources(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    raw_store = RawArtifactStore(tmp_path / "run" / "raw")
    delegate = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/offline-tests",
        clock=lambda: NOW,
    )
    service, _, _ = _service(
        tmp_path / "run",
        fixtures,
        transport=_OpaqueFixtureTransport(delegate),
    )
    service._schedule_sources = lambda: ["LAD"]  # type: ignore[method-assign]

    with pytest.raises(AcquisitionExecutionError, match="all 30 active clubs"):
        _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    report = json.loads((tmp_path / "run" / "validation.json").read_text("utf-8"))
    failure = report["acquisition_runs"][0]
    assert failure["outcome_contract_version"] == (
        "DSE_STATS_ACQUISITION_OUTCOME_V1"
    )
    assert failure["outcome"] == "failed"
    assert failure["status"] == "failed"
    assert failure["exit_code"] == 1
    assert failure["failure"]["failure_stage"] == "acquisition"
    assert failure["failure"]["error_type"] == "AcquisitionExecutionError"
    assert failure["partial_date"] is None
    assert failure["partial_date_reason"] is None
    assert failure["result_checksum"]


def test_live_schedule_collection_rejects_an_empty_club_response(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    empty_schedule = (
        '<html><body><table id="team_schedule"><thead><tr>'
        '<th data-stat="date_game">date_game</th>'
        '</tr></thead><tbody></tbody></table></body></html>'
    )
    for source_id in TEAM_SOURCE_ALIASES["baseball_reference"]:
        (fixtures / f"baseball_reference_schedule_{source_id}.html").write_text(
            empty_schedule,
            encoding="utf-8",
        )
    raw_store = RawArtifactStore(tmp_path / "run" / "raw")
    delegate = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/offline-tests",
        clock=lambda: NOW,
    )
    service, _, _ = _service(
        tmp_path / "run",
        fixtures,
        transport=_OpaqueFixtureTransport(delegate),
    )

    with pytest.raises(
        AcquisitionExecutionError,
        match="no positively classified regular-season rows",
    ):
        _execute(service, AcquisitionCommand.BACKFILL_CURRENT)


def test_schedule_missing_game_type_fails_closed_and_retains_excluded_rows(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    for schedule_path in fixtures.glob("baseball_reference_schedule_*.html"):
        without_game_type = schedule_path.read_text(encoding="utf-8").replace(
            '<th data-stat="game_type">game_type</th>', ""
        ).replace('<td data-stat="game_type">R</td>', "")
        schedule_path.write_text(without_game_type, encoding="utf-8")
    service, database, _ = _service(tmp_path / "run", fixtures)

    with pytest.raises(
        AcquisitionExecutionError,
        match="no positively classified regular-season rows",
    ):
        _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    with database.connect() as connection:
        raw_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_raw_payload_metadata "
                "WHERE provider='baseball_reference'"
            ).fetchone()[0]
        )
        excluded = connection.execute(
            "SELECT classification,reason_code FROM stats_excluded_source_rows "
            "WHERE provider='baseball_reference' ORDER BY source_row_id"
        ).fetchall()
        game_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_game_identities "
                "WHERE provider='baseball_reference'"
            ).fetchone()[0]
        )
    assert raw_count == 1
    assert game_count == 0
    assert len(excluded) == 2
    assert {str(row["classification"]) for row in excluded} == {
        "malformed"
    }
    assert {str(row["reason_code"]) for row in excluded} == {
        "missing_positive_regular_season_classification"
    }


def test_schedule_structured_game_number_is_positive_regular_season_evidence(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule = schedule_path.read_text(encoding="utf-8").replace(
        '<th data-stat="game_type">game_type</th>',
        '<th data-stat="game_number">Gm#</th>',
    )
    schedule = schedule.replace(
        '<td data-stat="game_type">R</td>',
        '<td data-stat="game_number">1</td>',
        1,
    ).replace(
        '<td data-stat="game_type">R</td>',
        '<td data-stat="game_number">2</td>',
        1,
    )
    schedule_path.write_text(schedule, encoding="utf-8")
    service, database, _ = _service(tmp_path / "run", fixtures)

    _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    with database.connect() as connection:
        rows = connection.execute(
            "SELECT status_json FROM stats_game_status_observations "
            "ORDER BY status_observation_id"
        ).fetchall()
    source_rows = [
        payload["source_row"]
        for row in rows
        if "source_row" in (payload := json.loads(str(row["status_json"])))
    ]
    assert source_rows
    for source_row in source_rows:
        assert source_row["game_type"] == "regular_season"
        assert source_row["_dse_game_type_source"] == (
            "baseball_reference_structured_regular_season_game_number"
        )


def test_observed_team_game_ordinal_is_positive_regular_season_evidence() -> None:
    classification = _classify_baseball_reference_schedule_row(
        {"team_game": "97", "date_game": "Friday, Jul 17"}
    )

    assert classification.is_regular_season is True
    assert classification.normalized_game_type == "regular_season"
    assert classification.evidence_source == (
        "baseball_reference_structured_regular_season_team_game"
    )


def test_sanitized_compact_live_schedule_shape_runs_through_service(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    for schedule_path in fixtures.glob("baseball_reference_schedule_*.html"):
        schedule_path.unlink()
    (fixtures / "baseball_reference_schedule_ARI.html").write_bytes(
        (FIXTURE_ROOT / "baseball_reference_schedule_preview_compact.html").read_bytes()
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(
        service,
        AcquisitionCommand.DAILY,
        requested_date=date(2026, 7, 17),
    )

    assert result.counts["schedule_rows"] == 1
    assert result.counts["games"] == 1
    with database.connect() as connection:
        row = connection.execute(
            "SELECT status_json FROM stats_game_status_observations "
            "ORDER BY status_observation_id LIMIT 1"
        ).fetchone()
    assert row is not None
    source_row = json.loads(str(row["status_json"]))["source_row"]
    assert source_row["team_game"] == "97"
    assert source_row["_dse_game_type_source"] == (
        "baseball_reference_structured_regular_season_team_game"
    )


@pytest.mark.parametrize(
    ("game_number", "team_game", "is_regular"),
    [
        ("97", "97", True),
        ("97", "98", False),
        ("97", "", False),
        ("", "97", False),
        ("97", "not-a-number", False),
        ("097", "097", False),
    ],
)
def test_dual_schedule_ordinals_must_be_consistent_positive_integers(
    game_number: str,
    team_game: str,
    is_regular: bool,
) -> None:
    classification = _classify_baseball_reference_schedule_row(
        {"game_number": game_number, "team_game": team_game}
    )

    assert classification.is_regular_season is is_regular
    if is_regular:
        assert classification.evidence_source == (
            "baseball_reference_structured_regular_season_"
            "consistent_game_number_and_team_game"
        )
    else:
        assert classification.exclusion_reason == (
            "ambiguous_regular_season_game_ordinal"
        )
        assert classification.excluded_classification == "malformed"


@pytest.mark.parametrize("game_type", ["postseason", "exhibition"])
def test_explicit_nonregular_type_overrides_consistent_schedule_ordinals(
    game_type: str,
) -> None:
    classification = _classify_baseball_reference_schedule_row(
        {"game_type": game_type, "game_number": "97", "team_game": "97"}
    )

    assert classification.is_regular_season is False
    assert classification.exclusion_reason == (
        "explicit_non_regular_season_game_type"
    )


@pytest.mark.parametrize(
    "row,reason,excluded_classification",
    [
        (
            {"date_game": "2026-07-10"},
            "missing_positive_regular_season_classification",
            "malformed",
        ),
        (
            {"postseason_round": "NLDS", "game_number": "1"},
            "explicit_non_regular_season_game_type",
            "postseason",
        ),
        (
            {"competition_type": "exhibition", "game_number": "1"},
            "explicit_non_regular_season_game_type",
            "exhibition",
        ),
        (
            {"game_type": "maybe", "game_number": "1"},
            "ambiguous_game_type",
            "malformed",
        ),
    ],
)
def test_schedule_nonregular_or_ambiguous_classification_cannot_fall_back(
    row: Mapping[str, str], reason: str, excluded_classification: str
) -> None:
    classification = _classify_baseball_reference_schedule_row(row)

    assert classification.is_regular_season is False
    assert classification.exclusion_reason == reason
    assert classification.excluded_classification == excluded_classification


def test_explicit_regular_season_classification_preserves_interleague_game() -> None:
    classification = _classify_baseball_reference_schedule_row(
        {"game_type": "R", "league_context": "interleague", "game_number": "42"}
    )

    assert classification.is_regular_season is True
    assert classification.normalized_game_type == "r"
    assert classification.evidence_source == "explicit_source_field:game_type"


def test_resume_skips_checksum_verified_completed_units(tmp_path: Path) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    (fixtures / "statcast_2026-07-14.csv").write_bytes(_csv_bytes([]))
    database = Database(tmp_path / "run" / "stats.db")
    raw_store = RawArtifactStore(tmp_path / "run" / "raw")
    delegate = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/offline-tests",
        clock=lambda: NOW,
    )
    interrupted_transport = _InterruptingTransport(
        delegate, interrupt_fixture="statcast_2026-07-14"
    )
    interrupted = StatsAcquisitionService(
        database=database,
        raw_store=raw_store,
        transport=interrupted_transport,
        report_path=tmp_path / "run" / "validation.json",
        clock=lambda: NOW,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        _execute(interrupted, AcquisitionCommand.BACKFILL_CURRENT)

    with database.connect() as connection:
        stats_run_id = str(
            connection.execute(
                "SELECT stats_run_id FROM stats_ingestion_runs "
                "WHERE status='running' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()[0]
        )
        assert connection.execute(
            "SELECT status FROM stats_checkpoints "
            "WHERE stats_run_id=? AND dataset_key='statcast:2026-07-13'",
            (stats_run_id,),
        ).fetchone()[0] == "completed"

    resumed, _, resumed_transport = _service(tmp_path / "run", fixtures)
    _execute(
        resumed,
        AcquisitionCommand.BACKFILL_CURRENT,
        resume_run_id=stats_run_id,
    )
    fixture_keys = [request.fixture_key for request in resumed_transport.requests]  # type: ignore[attr-defined]
    assert "baseball_reference_schedule_LAD" not in fixture_keys
    assert "baseball_reference_batting" not in fixture_keys
    assert "baseball_reference_pitching" not in fixture_keys
    assert "statcast_2026-07-12" not in fixture_keys
    assert "statcast_2026-07-13" not in fixture_keys
    assert "statcast_2026-07-14" in fixture_keys


def test_second_ingestion_resume_replays_deduplicated_schedule_and_aggregates(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    for day in (13, 14, 15):
        (fixtures / f"statcast_2026-07-{day:02d}.csv").write_bytes(_csv_bytes([]))
    root = tmp_path / "run"
    first_service, database, _ = _service(root, fixtures)
    first = _execute(first_service, AcquisitionCommand.BACKFILL_CURRENT)

    with database.connect() as connection:
        immutable_counts_before = {
            "schedule": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_game_status_observations AS status "
                    "JOIN stats_game_identities AS game "
                    "ON game.game_identity_id=status.game_identity_id "
                    "WHERE game.provider='baseball_reference'"
                ).fetchone()[0]
            ),
            "season_snapshots": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_season_snapshots"
                ).fetchone()[0]
            ),
            "pitch_identities": int(
                connection.execute(
                    "SELECT COUNT(*) FROM statcast_pitch_identities"
                ).fetchone()[0]
            ),
            "pitch_revisions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM statcast_pitch_revisions"
                ).fetchone()[0]
            ),
            "features": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_feature_snapshots"
                ).fetchone()[0]
            ),
        }
        feature_checksums_before = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT feature_checksum FROM stats_feature_snapshots "
                "ORDER BY feature_checksum"
            ).fetchall()
        )
        reconciliation_count_before = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_reconciliations"
            ).fetchone()[0]
        )

    raw_store = RawArtifactStore(root / "raw")
    delegate = build_stats_transport(
        mode=AcquisitionMode.OFFLINE,
        raw_store=raw_store,
        fixture_root=fixtures,
        user_agent="DailyMLBStats/offline-tests",
        clock=lambda: NOW,
    )
    interrupted_transport = _InterruptingTransport(
        delegate, interrupt_fixture="statcast_2026-07-14"
    )
    interrupted = StatsAcquisitionService(
        database=database,
        raw_store=raw_store,
        transport=interrupted_transport,
        report_path=root / "validation.json",
        clock=lambda: NOW,
    )
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        _execute(interrupted, AcquisitionCommand.BACKFILL_CURRENT)

    with database.connect() as connection:
        stats_run_id = str(
            connection.execute(
                "SELECT stats_run_id FROM stats_ingestion_runs "
                "WHERE status='running' ORDER BY rowid DESC LIMIT 1"
            ).fetchone()[0]
        )
        checkpoints = {
            str(row["dataset_key"]): dict(row)
            for row in connection.execute(
                "SELECT dataset_key,status,cursor_after_json,records_persisted "
                "FROM stats_checkpoints WHERE stats_run_id=?",
                (stats_run_id,),
            ).fetchall()
        }
        run_schedule_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_game_status_observations AS status "
                "JOIN stats_game_identities AS game "
                "ON game.game_identity_id=status.game_identity_id "
                "WHERE status.stats_run_id=? "
                "AND game.provider='baseball_reference'",
                (stats_run_id,),
            ).fetchone()[0]
        )
        run_aggregate_rows = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_season_snapshots "
                "WHERE stats_run_id=?",
                (stats_run_id,),
            ).fetchone()[0]
        )
        aggregate_documents = tuple(
            json.loads(str(row[0]))
            for row in connection.execute(
                "SELECT stats_json FROM stats_season_snapshots"
            ).fetchall()
        )
    assert checkpoints["baseball_reference_schedule"]["status"] == "completed"
    assert checkpoints["baseball_reference_batting_pitching"]["status"] == "completed"
    assert (
        json.loads(
            str(checkpoints["baseball_reference_schedule"]["cursor_after_json"])
        )["contract"]
        == "DSE_BREF_SCHEDULE_CHECKPOINT_V2"
    )
    assert (
        all(
            capture["purposes"]
            for capture in json.loads(
                str(
                    checkpoints["baseball_reference_batting_pitching"][
                        "cursor_after_json"
                    ]
                )
            )["captures"]
        )
    )
    assert all("capture_purposes" not in item for item in aggregate_documents)
    assert (
        json.loads(
            str(
                checkpoints["baseball_reference_batting_pitching"][
                    "cursor_after_json"
                ]
            )
        )["contract"]
        == "DSE_BREF_AGGREGATE_CHECKPOINT_V2"
    )
    assert run_schedule_rows == 0
    assert run_aggregate_rows == 0
    assert checkpoints["baseball_reference_batting_pitching"]["records_persisted"] == 0

    resumed, database, resumed_transport = _service(root, fixtures)
    second = _execute(
        resumed,
        AcquisitionCommand.BACKFILL_CURRENT,
        resume_run_id=stats_run_id,
    )
    fixture_keys = [request.fixture_key for request in resumed_transport.requests]  # type: ignore[attr-defined]
    assert not any(key.startswith("baseball_reference_") for key in fixture_keys)
    assert "statcast_2026-07-12" not in fixture_keys
    assert "statcast_2026-07-13" not in fixture_keys
    assert "statcast_2026-07-14" in fixture_keys
    assert second.completeness == first.completeness
    assert second.counts["counting_reconciliation_entities"] > 0

    with database.connect() as connection:
        immutable_counts_after = {
            "schedule": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_game_status_observations AS status "
                    "JOIN stats_game_identities AS game "
                    "ON game.game_identity_id=status.game_identity_id "
                    "WHERE game.provider='baseball_reference'"
                ).fetchone()[0]
            ),
            "season_snapshots": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_season_snapshots"
                ).fetchone()[0]
            ),
            "pitch_identities": int(
                connection.execute(
                    "SELECT COUNT(*) FROM statcast_pitch_identities"
                ).fetchone()[0]
            ),
            "pitch_revisions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM statcast_pitch_revisions"
                ).fetchone()[0]
            ),
            "features": int(
                connection.execute(
                    "SELECT COUNT(*) FROM stats_feature_snapshots"
                ).fetchone()[0]
            ),
        }
        feature_checksums_after = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT feature_checksum FROM stats_feature_snapshots "
                "ORDER BY feature_checksum"
            ).fetchall()
        )
        resumed_reconciliation = connection.execute(
            "SELECT status FROM stats_reconciliations WHERE stats_run_id=?",
            (stats_run_id,),
        ).fetchone()
        reconciliation_count_after = int(
            connection.execute(
                "SELECT COUNT(*) FROM stats_reconciliations"
            ).fetchone()[0]
        )
    assert immutable_counts_after == immutable_counts_before
    assert feature_checksums_after == feature_checksums_before
    assert resumed_reconciliation is not None
    assert resumed_reconciliation["status"] in {"passed", "warnings"}
    assert reconciliation_count_after == reconciliation_count_before + 1


def test_home_and_away_schedule_views_reconcile_to_one_game(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    (fixtures / "baseball_reference_schedule_SFG.html").write_text(
        _away_schedule_html("final"), encoding="utf-8"
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    assert result.counts["schedule_rows"] == 2
    assert result.counts["games"] == 1
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_identities "
        "WHERE provider='baseball_reference' AND official_date='2026-07-16'",
    ) == 1
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_status_observations AS observation "
        "JOIN stats_game_identities AS game "
        "ON game.game_identity_id=observation.game_identity_id "
        "WHERE game.provider='baseball_reference' "
        "AND game.official_date='2026-07-16'",
    ) == 2
    with database.connect() as connection:
        persisted_reconciliation = connection.execute(
            "SELECT item.severity,item.details_json "
            "FROM stats_reconciliation_items AS item "
            "WHERE item.code='schedule_rows_persisted_count_matches'"
        ).fetchone()
    assert persisted_reconciliation is not None
    assert persisted_reconciliation["severity"] == "info"
    assert json.loads(str(persisted_reconciliation["details_json"])) == {
        "expected": 2,
        "observed": 2,
    }


def test_same_cutoff_counting_differences_are_persisted_with_classification(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    completed_rows = _completed_game_rows(12)
    for row in completed_rows:
        row["batter"] = "660271"
        row["pitcher"] = "605400"
    (fixtures / "statcast_2026-07-12.csv").write_bytes(
        _csv_bytes(completed_rows)
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    assert result.counts["counting_reconciliation_entities"] == 2
    assert result.counts["counting_reconciliation_fields"] == 11
    assert result.counts["counting_reconciliation_explained_differences"] == 0
    assert result.counts["counting_reconciliation_unexplained_differences"] >= 1
    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert (
        result.completeness.contiguous_regular_season_complete_through_date
        == date(2026, 7, 12)
    )
    assert (
        "baseball_reference_statcast_unexplained_reconciliation_difference"
        in result.warnings
    )
    assert (
        StatsRepository(database).get_completeness_watermark(
            provider="current_mlb_stats",
            dataset_key="regular_season_games",
            season=2026,
        )
        is None
    )
    with database.connect() as connection:
        run_reconciliation = connection.execute(
            "SELECT details_json FROM stats_reconciliations "
            "WHERE stats_run_id=? ORDER BY completed_at DESC LIMIT 1",
            (result.stats_run_id,),
        ).fetchone()
    assert run_reconciliation is not None
    reconciliation_details = json.loads(str(run_reconciliation["details_json"]))
    assert reconciliation_details["completeness_watermark_eligible"] is False
    assert reconciliation_details["completeness_watermark_block_reason"] == (
        "unexplained_cross_source_reconciliation_difference"
    )
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_status_observations AS status "
        "JOIN stats_game_identities AS game "
        "ON game.game_identity_id=status.game_identity_id "
        "WHERE game.provider='baseball_reference' "
        "AND game.official_date='2026-07-12' AND status.status_code='final'",
    ) >= 1
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT code,severity,entity_key,details_json "
            "FROM stats_reconciliation_items "
            "WHERE code LIKE 'baseball_reference_statcast_%' "
            "ORDER BY reconciliation_item_id"
        ).fetchall()
    decoded = [
        (str(row["code"]), str(row["severity"]), row["entity_key"], json.loads(row["details_json"]))
        for row in rows
    ]
    summary = next(
        details
        for code, _, _, details in decoded
        if code == "baseball_reference_statcast_counting_reconciliation"
    )
    assert summary["through_date"] == "2026-07-12"
    assert summary["statcast_method"] == "terminal_completed_plate_appearance_event"
    assert any(
        severity == "warning"
        and entity_key == "pitcher:mlb-player:mlbam:605400"
        and details["field"] == "BF"
        and details["difference_classification"] == "unexplained"
        for _, severity, entity_key, details in decoded
    )
    assert any(
        severity == "warning"
        and entity_key == "batter:mlb-player:mlbam:660271"
        and details["field"] == "PA"
        and details["difference_classification"] == "unexplained"
        for _, severity, entity_key, details in decoded
    )

    validation = service.validate(
        AcquisitionRequest(date(2026, 7, 16), mode=AcquisitionMode.OFFLINE)
    )
    assert validation.validation_details is not None
    assert validation.validation_details["source_readiness"]["current_season"][
        "counting_reconciliation"
    ] == {
        "ready": False,
        "unexplained_differences_allowed": False,
    }
    assert (
        "baseball_reference_statcast_counting_reconciliation_not_validated"
        in validation.warnings
    )


@pytest.mark.parametrize(
    ("pitcher_batters_faced", "expected_explained_differences"),
    [(51, 0)],
)
def test_clean_or_explained_reconciliation_advances_both_watermarks(
    tmp_path: Path,
    pitcher_batters_faced: int,
    expected_explained_differences: int,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=pitcher_batters_faced
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.BACKFILL_CURRENT)

    assert result.counts["counting_reconciliation_unexplained_differences"] == 0
    assert (
        result.counts["counting_reconciliation_explained_differences"]
        == expected_explained_differences
    )
    watermark = StatsRepository(database).get_completeness_watermark(
        provider="current_mlb_stats",
        dataset_key="regular_season_games",
        season=2026,
    )
    assert watermark is not None
    assert watermark["latest_ingested_completed_game_date"] == "2026-07-12"
    assert (
        watermark["contiguous_regular_season_complete_through_date"]
        == "2026-07-12"
    )
    assert (
        "baseball_reference_statcast_unexplained_reconciliation_difference"
        not in result.warnings
    )


def test_unexplained_reconciliation_preserves_prior_dual_watermarks(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    _align_aggregate_fixtures_with_default_statcast(
        fixtures, pitcher_batters_faced=51
    )
    run_root = tmp_path / "run"
    initial_service, database, _ = _service(run_root, fixtures)
    initial = _execute(initial_service, AcquisitionCommand.BACKFILL_CURRENT)
    assert initial.counts["counting_reconciliation_unexplained_differences"] == 0

    (fixtures / "baseball_reference_schedule_LAD.html").write_text(
        _schedule_html("final"), encoding="utf-8"
    )
    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(_completed_game_rows(16))
    )
    later_service, _, _ = _service(run_root, fixtures)

    later = _execute(later_service, AcquisitionCommand.DAILY)

    assert later.completeness is not None
    assert later.completeness.latest_ingested_completed_game_date == date(2026, 7, 16)
    assert later.counts["counting_reconciliation_unexplained_differences"] > 0
    watermark = StatsRepository(database).get_completeness_watermark(
        provider="current_mlb_stats",
        dataset_key="regular_season_games",
        season=2026,
    )
    assert watermark is not None
    assert watermark["latest_ingested_completed_game_date"] == "2026-07-12"
    assert (
        watermark["contiguous_regular_season_complete_through_date"]
        == "2026-07-12"
    )
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_status_observations AS status "
        "JOIN stats_game_identities AS game "
        "ON game.game_identity_id=status.game_identity_id "
        "WHERE game.provider='baseball_reference' "
        "AND game.official_date='2026-07-16' AND status.status_code='final'",
    ) >= 1


def test_counting_readiness_allows_only_documented_explained_differences() -> None:
    base = {
        "contract_version": "DSE_BREF_STATCAST_COUNTING_RECONCILIATION_V3",
        "entities_compared": 2,
        "fields_compared": 11,
        "fields_matched": 9,
        "explained_difference_count": 2,
        "unexplained_difference_count": 0,
    }

    assert _counting_reconciliation_is_ready(json.dumps(base)) is True
    assert (
        _counting_reconciliation_is_ready(
            json.dumps({**base, "unexplained_difference_count": 1})
        )
        is False
    )


def test_conflicting_home_and_away_schedule_status_fails_closed(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    (fixtures / "baseball_reference_schedule_SFG.html").write_text(
        _away_schedule_html("scheduled"), encoding="utf-8"
    )
    service, _, _ = _service(tmp_path / "run", fixtures)

    with pytest.raises(AcquisitionExecutionError, match="disagree on game status"):
        _execute(service, AcquisitionCommand.DAILY)


def test_conflicting_home_and_away_final_scores_fail_closed(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    conflicting = _away_schedule_html("final").replace(
        '<td data-stat="R">2</td>', '<td data-stat="R">3</td>'
    )
    (fixtures / "baseball_reference_schedule_SFG.html").write_text(
        conflicting, encoding="utf-8"
    )
    service, _, _ = _service(tmp_path / "run", fixtures)

    with pytest.raises(AcquisitionExecutionError, match="disagree on final score"):
        _execute(service, AcquisitionCommand.DAILY)


def test_daily_scope_collects_only_the_requested_date(tmp_path: Path) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    service, database, transport = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    fixture_keys = [request.fixture_key for request in transport.requests]  # type: ignore[attr-defined]
    assert result.counts["games"] == 1
    assert result.counts["schedule_rows"] == 1
    assert "statcast_2026-07-16" in fixture_keys
    assert "statcast_2026-07-12" not in fixture_keys
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_identities "
        "WHERE provider='baseball_reference' AND official_date<'2026-07-16'",
    ) == 0
    with database.connect() as connection:
        provenance = connection.execute(
            "SELECT source_version,adapter_version FROM stats_ingestion_runs "
            "WHERE stats_run_id=?",
            (result.stats_run_id,),
        ).fetchone()
    assert provenance is not None
    assert provenance["source_version"] == (
        "baseball-reference-standard-2026+"
        "baseball-savant-statcast-search-csv-2026"
    )
    assert provenance["adapter_version"] == (
        "DSE_MLB_STATS_ACQUISITION_V1;pybaseball-parity=2.2.7"
    )
    acquisition_provenance = dict(result.as_dict()["acquisition_provenance"])
    providers = acquisition_provenance.pop("providers")
    assert acquisition_provenance == {
        "canonical_transport": "project_controlled_exact_raw_bytes",
        "canonical_adapter_version": "DSE_MLB_STATS_ACQUISITION_V1",
        "source_contract_version": (
            "baseball-reference-standard-2026+"
            "baseball-savant-statcast-search-csv-2026"
        ),
        "pybaseball_version": "2.2.7",
        "pybaseball_role": "noncanonical_parity_reference",
    }
    assert providers["retrosheet"]["adapter_version"] == (
        "DSE_RETROSHEET_ADAPTER_V1"
    )
    assert providers["retrosheet"]["attribution_version"] == (
        "DSE_RETROSHEET_ATTRIBUTION_V1"
    )
    assert providers["retrosheet"]["attribution_text"] == (
        "The information used here was obtained free of charge from and is "
        "copyrighted by Retrosheet. Interested parties may contact Retrosheet at "
        "20 Sunset Rd., Newark, DE 19711."
    )
    assert providers["baseball_reference"]["adapter_version"] == (
        "DSE_BASEBALL_REFERENCE_ADAPTER_V1"
    )
    assert providers["statcast"]["adapter_version"] == "DSE_STATCAST_ADAPTER_V3"
    assert providers["statcast"]["parallel"] is False
    assert providers["statcast"]["request_policy"] == (
        "https;serial-cross-process;one-live-request-at-a-time"
    )
    assert providers["pybaseball"]["resource_revision"] == (
        "7e23e7dfaff51b3ae72c16393703eda7e5ecad27"
    )
    assert providers["pybaseball"]["resource_revision"] in (
        providers["pybaseball"]["resource_identity"]
    )
    assert providers["pybaseball"]["resource_revision"] in (
        providers["pybaseball"]["raw_endpoint_category"]
    )


def test_current_aggregate_windows_use_typed_exact_ranges_and_split_keys(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule_path.write_text(
        _with_april_opening_day(schedule_path.read_text("utf-8")),
        encoding="utf-8",
    )
    service, database, transport = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    aggregate_requests = [
        request
        for request in transport.requests  # type: ignore[attr-defined]
        if request.fixture_key
        in {"baseball_reference_batting", "baseball_reference_pitching"}
    ]
    assert len(aggregate_requests) == 10
    assert {
        (str(request.params["type"]), str(request.params["fromandto"]))
        for request in aggregate_requests
    } == {
        ("b", "2026-04-01.2026-07-15"),
        ("p", "2026-04-01.2026-07-15"),
        ("b", "2026-07-09.2026-07-15"),
        ("p", "2026-07-09.2026-07-15"),
        ("b", "2026-07-02.2026-07-15"),
        ("p", "2026-07-02.2026-07-15"),
        ("b", "2026-06-16.2026-07-15"),
        ("p", "2026-06-16.2026-07-15"),
        ("b", "2026-04-01.2026-07-16"),
        ("p", "2026-04-01.2026-07-16"),
    }
    assert result.counts["baseball_reference_snapshots"] == 10
    with database.connect() as connection:
        split_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT split_key FROM stats_season_snapshots "
                "WHERE provider='baseball_reference'"
            ).fetchall()
        }
        d1_feature_versions = {
            str(row["feature_version"]): int(row["snapshot_count"])
            for row in connection.execute(
                """
                SELECT feature_version, COUNT(*) AS snapshot_count
                FROM stats_feature_snapshots
                WHERE canonical_player_id='mlb-player:mlbam:660271'
                GROUP BY feature_version
                """
            ).fetchall()
        }
    assert split_keys == {
        "batting:season_to_date:2026-04-01:2026-07-15",
        "pitching:season_to_date:2026-04-01:2026-07-15",
        "batting:rolling_7_days:2026-07-09:2026-07-15",
        "pitching:rolling_7_days:2026-07-09:2026-07-15",
        "batting:rolling_14_days:2026-07-02:2026-07-15",
        "pitching:rolling_14_days:2026-07-02:2026-07-15",
        "batting:rolling_30_days:2026-06-16:2026-07-15",
        "pitching:rolling_30_days:2026-06-16:2026-07-15",
        "batting:season_to_date:2026-04-01:2026-07-16",
        "pitching:season_to_date:2026-04-01:2026-07-16",
    }
    assert d1_feature_versions == {
        "DSE_MLB_STATS_FEATURES_V2": 1,
        "DSE_MLB_STATS_FEATURES_V3": 1,
    }


def test_live_baseball_reference_aggregate_shape_is_not_rejected(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    (fixtures / "baseball_reference_batting.html").write_bytes(
        (
            FIXTURE_ROOT / "baseball_reference_daily_batting_live_shape.html"
        ).read_bytes()
    )
    (fixtures / "baseball_reference_pitching.html").write_bytes(
        (
            FIXTURE_ROOT / "baseball_reference_daily_pitching_live_shape.html"
        ).read_bytes()
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(
        service,
        AcquisitionCommand.BACKFILL_CURRENT,
        requested_date=date(2026, 7, 14),
    )

    assert result.counts["baseball_reference_snapshot_rejections"] == 0
    assert result.counts["baseball_reference_snapshots"] == 10
    assert "baseball_reference_aggregate_rows_rejected" not in result.warnings
    with database.connect() as connection:
        source_rows = [
            json.loads(str(row[0]))["source_row"]
            for row in connection.execute(
                "SELECT stats_json FROM stats_season_snapshots "
                "ORDER BY split_key,player_identity_id"
            ).fetchall()
        ]
    assert {row["name_display"] for row in source_rows} == {
        "Example Hitter",
        "Example Pitcher",
    }
    assert {row["team_name"] for row in source_rows} == {
        "Cincinnati",
        "Washington",
    }
    assert all("team_ID" not in row for row in source_rows)


def test_all_star_break_feature_windows_end_on_d1_calendar_date(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule_path.write_text(
        _with_april_opening_day(schedule_path.read_text("utf-8")),
        encoding="utf-8",
    )
    service, _, transport = _service(tmp_path / "run", fixtures)

    result = _execute(
        service,
        AcquisitionCommand.BACKFILL_CURRENT,
        requested_date=date(2026, 7, 14),
    )

    aggregate_ranges = {
        str(request.params["fromandto"])
        for request in transport.requests  # type: ignore[attr-defined]
        if request.fixture_key
        in {"baseball_reference_batting", "baseball_reference_pitching"}
    }
    assert aggregate_ranges == {
        "2026-04-01.2026-07-13",
        "2026-07-07.2026-07-13",
        "2026-06-30.2026-07-13",
        "2026-06-14.2026-07-13",
        "2026-04-01.2026-07-12",
    }
    assert result.completeness is not None
    assert result.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)
    assert result.counts["baseball_reference_snapshot_captures"] == 10


def test_missing_aggregate_count_is_rejected_without_becoming_zero(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    batting_path = fixtures / "baseball_reference_batting.html"
    batting_path.write_text(
        batting_path.read_text("utf-8").replace(
            '<td data-stat="plate_appearances">4</td>',
            '<td data-stat="plate_appearances"></td>',
        ),
        encoding="utf-8",
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    result = _execute(service, AcquisitionCommand.DAILY)

    assert result.counts["baseball_reference_snapshot_rejections"] == 5
    assert "baseball_reference_aggregate_rows_rejected" in result.warnings
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_season_snapshots "
        "WHERE split_key LIKE 'batting:%'",
    ) == 0
    with database.connect() as connection:
        pitching_stats = json.loads(
            str(
                connection.execute(
                    "SELECT stats_json FROM stats_season_snapshots "
                    "WHERE split_key LIKE 'pitching:season_to_date:%'"
                ).fetchone()[0]
            )
        )
    assert pitching_stats["normalized_counts"]["walks"] == 0


def test_same_day_statcast_is_retained_but_excluded_from_d1_features(
    tmp_path: Path,
) -> None:
    completed_rows = _completed_game_rows(16)
    for row in completed_rows:
        row["batter"] = "660271"
        row["pitcher"] = "605400"

    completed_rows[0].update(
        {
            "effective_speed": "95.0",
            "spin_axis": "359",
            "release_pos_x": "-2.0",
            "release_pos_y": "54.0",
            "release_pos_z": "6.0",
            "arm_angle": "45.0",
            "api_break_x_arm": "8.0",
            "api_break_x_batter_in": "-8.0",
            "api_break_z_with_gravity": "14.0",
            "bat_speed": "70.0",
            "swing_length": "7.0",
            "attack_angle": "10.0",
            "attack_direction": "2.0",
            "swing_path_tilt": "20.0",
            "miss_distance": "0.5",
            "hyper_speed": "90.0",
        }
    )
    completed_rows[1].update(
        {
            "effective_speed": "97.0",
            "spin_axis": "1",
            "release_pos_x": "-1.8",
            "release_pos_y": "54.2",
            "release_pos_z": "6.2",
            "arm_angle": "47.0",
            "api_break_x_arm": "10.0",
            "api_break_x_batter_in": "-10.0",
            "api_break_z_with_gravity": "16.0",
            "bat_speed": "78.0",
            "swing_length": "7.4",
            "attack_angle": "12.0",
            "attack_direction": "4.0",
            "swing_path_tilt": "22.0",
            "miss_distance": "0.7",
            "hyper_speed": "100.0",
        }
    )

    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=completed_rows,
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    _execute(service, AcquisitionCommand.DAILY)

    repository = StatsRepository(database)
    batter = repository.resolve_canonical_player("statcast", "660271")
    pitcher = repository.resolve_canonical_player("statcast", "605400")
    assert batter is not None
    assert batter["canonical_player_id"] == "mlb-player:mlbam:660271"
    assert pitcher is not None
    assert pitcher["canonical_player_id"] == "mlb-player:mlbam:605400"
    with database.connect() as connection:
        payloads_v2 = {
            str(row["canonical_player_id"]): json.loads(str(row["features_json"]))
            for row in connection.execute(
                "SELECT canonical_player_id,features_json "
                "FROM stats_feature_snapshots "
                "WHERE entity_kind='player' "
                "AND feature_version='DSE_MLB_STATS_FEATURES_V2'"
            ).fetchall()
        }
        payloads_v3 = {
            str(row["canonical_player_id"]): json.loads(str(row["features_json"]))
            for row in connection.execute(
                "SELECT canonical_player_id,features_json "
                "FROM stats_feature_snapshots "
                "WHERE entity_kind='player' "
                "AND feature_version='DSE_MLB_STATS_FEATURES_V3'"
            ).fetchall()
        }
        aggregate_end_dates = {
            json.loads(str(row[0]))["range_end"]
            for row in connection.execute(
                "SELECT stats_json FROM stats_season_snapshots"
            ).fetchall()
        }
    assert aggregate_end_dates == {"2026-07-15", "2026-07-16"}
    assert payloads_v2["mlb-player:mlbam:660271"]["feature_as_of"] == "2026-07-16"
    assert payloads_v2["mlb-player:mlbam:660271"]["hitting"]["season_to_date"]["pa"] == 4
    assert payloads_v2["mlb-player:mlbam:660271"]["batted_ball"]["batted_ball_count"] == 0
    assert payloads_v2["mlb-player:mlbam:605400"]["pitch_traits"]["pitch_count"] == 0

    assert set(payloads_v3) == set(payloads_v2)
    assert (
        payloads_v3["mlb-player:mlbam:660271"]["contract_version"]
        == "DSE_MLB_STATS_FEATURES_V3"
    )
    assert (
        payloads_v3["mlb-player:mlbam:660271"]["swing_metrics"][
            "swing_observation_count"
        ]
        == 0
    )
    assert (
        payloads_v3["mlb-player:mlbam:605400"]["pitch_physics"][
            "pitch_observation_count"
        ]
        == 0
    )
    assert _scalar(database, "SELECT COUNT(*) FROM statcast_pitch_revisions") == len(
        completed_rows
    )

    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule_path.write_text(
        _schedule_html("scheduled")
        .replace("2026-07-16", "2026-07-17")
        .replace("LAN202607160", "LAN202607170"),
        encoding="utf-8",
    )
    next_service, database, _ = _service(tmp_path / "run", fixtures)
    _execute(
        next_service,
        AcquisitionCommand.DAILY,
        requested_date=date(2026, 7, 17),
    )

    with database.connect() as connection:
        later_payloads_v2 = {
            str(row["canonical_player_id"]): json.loads(str(row["features_json"]))
            for row in connection.execute(
                """
                SELECT canonical_player_id,features_json
                FROM stats_feature_snapshots
                WHERE entity_kind='player'
                  AND feature_version='DSE_MLB_STATS_FEATURES_V2'
                  AND feature_as_of='2026-07-17T00:00:00+00:00'
                """
            ).fetchall()
        }
        later_payloads_v3 = {
            str(row["canonical_player_id"]): json.loads(str(row["features_json"]))
            for row in connection.execute(
                """
                SELECT canonical_player_id,features_json
                FROM stats_feature_snapshots
                WHERE entity_kind='player'
                  AND feature_version='DSE_MLB_STATS_FEATURES_V3'
                  AND feature_as_of='2026-07-17T00:00:00+00:00'
                """
            ).fetchall()
        }
        version_counts = {
            str(row["canonical_player_id"]): int(row["version_count"])
            for row in connection.execute(
                """
                SELECT canonical_player_id,
                       COUNT(DISTINCT feature_version) AS version_count
                FROM stats_feature_snapshots
                WHERE entity_kind='player'
                  AND feature_as_of='2026-07-17T00:00:00+00:00'
                  AND canonical_player_id IN (
                      'mlb-player:mlbam:660271',
                      'mlb-player:mlbam:605400'
                  )
                GROUP BY canonical_player_id
                """
            ).fetchall()
        }
    assert later_payloads_v2["mlb-player:mlbam:660271"]["feature_as_of"] == "2026-07-17"
    assert (
        later_payloads_v2["mlb-player:mlbam:660271"]["batted_ball"][
            "batted_ball_count"
        ]
        > 0
    )
    batted_splits = later_payloads_v2["mlb-player:mlbam:660271"]["batted_ball"][
        "splits"
    ]
    assert batted_splits["home_away"]["away"]["batted_ball_count"] == 1
    assert batted_splits["home_away"]["home"]["batted_ball_count"] == 0
    assert batted_splits["batter_handedness"]["right"]["batted_ball_count"] == 1
    assert (
        batted_splits["opposing_pitcher_handedness"]["right"][
            "batted_ball_count"
        ]
        == 1
    )
    assert (
        later_payloads_v2["mlb-player:mlbam:605400"]["pitch_traits"]["pitch_count"]
        == len(completed_rows)
    )
    opposing_handedness = later_payloads_v2["mlb-player:mlbam:605400"][
        "pitch_traits"
    ]["splits"]["opposing_batter_handedness"]
    assert opposing_handedness["right"]["pitch_count"] == len(completed_rows)
    assert opposing_handedness["unknown"]["pitch_count"] == 0
    hitter_standard = later_payloads_v2["mlb-player:mlbam:660271"]["hitting"][
        "statcast_standard_splits"
    ]
    assert hitter_standard["home_away"]["away"]["pa"] == 27
    assert (
        hitter_standard["opposing_pitcher_handedness"]["right"]["k_rate"]
        == 1.0
    )
    pitcher_standard = later_payloads_v2["mlb-player:mlbam:605400"]["pitching"][
        "statcast_standard_splits"
    ]
    assert pitcher_standard["home_away"]["home"]["bf"] == 27
    assert pitcher_standard["home_away"]["away"]["bf"] == 24
    assert (
        pitcher_standard["opposing_batter_handedness"]["right"]["k_rate"]
        == 1.0
    )
    assert pitcher_standard["home_away"]["home"]["era"] is None

    assert version_counts["mlb-player:mlbam:660271"] == 2
    assert version_counts["mlb-player:mlbam:605400"] == 2

    for player_id in (
        "mlb-player:mlbam:660271",
        "mlb-player:mlbam:605400",
    ):
        for key, value in later_payloads_v2[player_id].items():
            if key in {"contract_version", "feature_checksum"}:
                continue
            assert later_payloads_v3[player_id][key] == value

    hitter_v3 = later_payloads_v3["mlb-player:mlbam:660271"]
    assert hitter_v3["swing_metrics"]["swing_observation_count"] == 2
    assert hitter_v3["swing_metrics"]["bat_speed_samples"] == 2
    assert hitter_v3["swing_metrics"]["bat_speed_mean"] == 74.0
    assert hitter_v3["swing_metrics"]["hyper_speed_mean"] == 95.0

    pitcher_v3 = later_payloads_v3["mlb-player:mlbam:605400"]
    assert pitcher_v3["pitch_physics"]["pitch_observation_count"] == len(
        completed_rows
    )
    assert pitcher_v3["pitch_physics"]["effective_speed_samples"] == 2
    assert pitcher_v3["pitch_physics"]["effective_speed_mean"] == 96.0
    assert pitcher_v3["pitch_physics"]["spin_axis_samples"] == 2
    assert pitcher_v3["pitch_physics"]["spin_axis_circular_mean"] == 0.0


def test_scheduled_without_boxscore_and_later_final_share_one_game_identity(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    schedule_path = fixtures / "baseball_reference_schedule_LAD.html"
    schedule_path.write_text(
        _schedule_html("scheduled").replace(
            '<a href="/boxes/LAN/LAN202607160.shtml">boxscore</a>', ""
        ),
        encoding="utf-8",
    )
    service, database, _ = _service(tmp_path / "run", fixtures)

    scheduled = _execute(service, AcquisitionCommand.DAILY)

    assert scheduled.completeness is not None
    assert scheduled.completeness.partial_date == date(2026, 7, 16)
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_identities "
        "WHERE provider='baseball_reference'",
    ) == 1

    schedule_path.write_text(_schedule_html("final"), encoding="utf-8")
    statcast_path = fixtures / "statcast_2026-07-16.csv"
    statcast_path.write_bytes(_csv_bytes(_completed_game_rows(16)))
    final_service, database, _ = _service(tmp_path / "run", fixtures)

    final = _execute(final_service, AcquisitionCommand.DAILY)

    assert final.completeness is not None
    assert final.completeness.latest_ingested_completed_game_date == date(2026, 7, 16)
    assert _scalar(
        database,
        "SELECT COUNT(*) FROM stats_game_identities "
        "WHERE provider='baseball_reference'",
    ) == 1
    with database.connect() as connection:
        identities = connection.execute(
            "SELECT provider_game_id FROM stats_game_identities "
            "WHERE provider='baseball_reference'"
        ).fetchall()
        states = connection.execute(
            "SELECT abstract_state FROM stats_game_status_observations "
            "ORDER BY retrieved_at,stats_run_id"
        ).fetchall()
    assert [str(row["provider_game_id"]) for row in identities] == [
        "bref:2026-07-16:SF:LAD:1"
    ]
    assert {str(row["abstract_state"]) for row in states} == {"scheduled", "final"}


def test_validate_selects_exact_current_provider_and_season_scope(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="final",
        july_16_rows=_completed_game_rows(16),
    )
    service, database, _ = _service(tmp_path / "run", fixtures)
    _execute(service, AcquisitionCommand.DAILY)

    repository = StatsRepository(database)
    historical_date = date(2025, 12, 31)
    historical_run_id = generate_run_id(historical_date)
    historical_stats_run_id = "stats_" + "b" * 32
    database.create_run(historical_run_id, historical_date)
    repository.create_ingestion_run(
        stats_run_id=historical_stats_run_id,
        run_id=historical_run_id,
        provider="retrosheet",
        scope_key="regular-season:2025",
        requested_through_date=historical_date,
        configuration_checksum="b" * 64,
        created_at=NOW,
    )
    repository.record_reconciliation(
        {
            "reconciliation_id": "historical-reconciliation",
            "stats_run_id": historical_stats_run_id,
            "dataset_key": "regular_season_games",
            "scope_key": "season:2025",
            "status": "passed",
            "started_at": NOW,
            "completed_at": NOW,
            "details": {},
            "source_checksum": "b" * 64,
        }
    )
    repository.advance_completeness_watermark(
        provider="retrosheet",
        dataset_key="regular_season_games",
        scope_key="season:2025",
        requested_through_date=historical_date,
        source_observed_at=NOW,
        latest_ingested_completed_game_date=date(2025, 9, 28),
        contiguous_regular_season_complete_through_date=date(2025, 9, 28),
        source_stats_run_id=historical_stats_run_id,
        reconciliation_id="historical-reconciliation",
        source_checksum="b" * 64,
        updated_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )

    result = service.validate(
        AcquisitionRequest(date(2026, 7, 16), mode=AcquisitionMode.OFFLINE)
    )

    assert result.completeness is not None
    assert result.completeness.requested_through_date == date(2026, 7, 16)
    assert result.counts["stats_ingestion_runs"] == 1
    assert result.validation_details is not None
    details = result.validation_details
    assert details["contract_version"] == "DSE_STATS_VALIDATION_DETAILS_V1"
    assert result.status == "completed_with_warnings"
    assert result.outcome.value == "failed"
    assert result.exit_code == 1
    assert "retrosheet_through_prior_season_not_validated" in result.warnings
    assert "baseball_reference_schedule_coverage_not_validated" in result.warnings
    assert details["source_readiness"]["ready"] is False
    assert (
        details["source_readiness"]["current_season"]["schedule"][
            "observed_source_count"
        ]
        == 1
    )
    assert details["database"]["schema_matches_application"] is True
    assert details["database"]["integrity_check"] == ["ok"]
    assert details["database"]["foreign_key_violations"] == []
    assert details["baseball_reference"]["row_counts"]["games"] == 1
    assert details["baseball_reference"]["row_counts"]["batting_rows"] == 5
    assert details["baseball_reference"]["row_counts"]["pitching_rows"] == 5
    assert details["statcast"]["pitches"] == len(_completed_game_rows(16))
    assert details["statcast"]["first_game_date"] == "2026-07-16"
    assert details["statcast"]["latest_game_date"] == "2026-07-16"
    assert details["raw_retention"]["file_count"] > 0
    assert details["raw_retention"]["byte_count"] > 0
    raw_inventory = details["raw_retention"]["checksum_inventory"]
    assert raw_inventory["contract_version"] == (
        "DSE_STATS_RAW_CHECKSUM_INVENTORY_V1"
    )
    assert raw_inventory["metadata_record_count"] == details["raw_retention"][
        "file_count"
    ]
    assert raw_inventory["verified_checksum_count"] == raw_inventory[
        "metadata_record_count"
    ]
    assert raw_inventory["failed_verification_count"] == 0
    assert len(raw_inventory["root_digest_sha256"]) == 64
    assert details["duplicates"]["game_identity_duplicates"] == 0
    assert details["duplicates"]["statcast_pitch_tuple_duplicates"] == 0
    assert details["duplicates"]["player_game_logical_duplicates"] == 0
    assert details["duplicates"]["player_game_duplicate_revision_rows"] == 0
    assert details["duplicates"]["player_game_duplicate_normalized_rows"] == 0
    player_lines = details["player_game_line_inventory"]
    assert player_lines["snapshot_rows"] >= player_lines["logical_identities"]
    assert player_lines["snapshot_rows"] == (
        player_lines["logical_identities"] + player_lines["correction_revisions"]
    )
    feature_inventory = details["feature_inventory"]
    assert feature_inventory["contract_version"] == "DSE_STATS_FEATURE_INVENTORY_V1"
    assert feature_inventory["stats_run_id"] is not None
    assert feature_inventory["snapshot_count"] > 0
    assert {row["entity_kind"] for row in feature_inventory["by_entity_type"]} == {
        "player",
        "team",
    }
    assert all(
        row["feature_as_of"] == "2026-07-16T00:00:00+00:00"
        for row in feature_inventory["by_entity_type_and_feature_as_of"]
    )
    assert details["schema_drift"]["database_schema_drift_detected"] is False
    report = json.loads((tmp_path / "run" / "validation.json").read_text("utf-8"))
    assert "initial_validation" not in report
    validation_entry = report["incremental_validations"][0]
    assert validation_entry["command"] == "validate"
    assert validation_entry["validation_scope"] == {
        "provider": "current_mlb_stats",
        "season": 2026,
    }
    assert validation_entry["validation_details"] == details


def test_validate_older_cutoff_uses_its_exact_run_after_later_ingestion(
    tmp_path: Path,
) -> None:
    fixtures = _fixture_root(
        tmp_path / "fixtures",
        july_16_status="scheduled",
        july_16_rows=[],
    )
    service, _, _ = _service(tmp_path / "run", fixtures)
    initial = _execute(
        service,
        AcquisitionCommand.BACKFILL_CURRENT,
        requested_date=date(2026, 7, 14),
    )
    assert initial.completeness is not None
    assert initial.completeness.latest_ingested_completed_game_date == date(2026, 7, 12)

    (fixtures / "baseball_reference_schedule_LAD.html").write_text(
        _schedule_html("final"), encoding="utf-8"
    )
    (fixtures / "statcast_2026-07-16.csv").write_bytes(
        _csv_bytes(_completed_game_rows(16))
    )
    service, _, _ = _service(tmp_path / "run", fixtures)
    later = _execute(service, AcquisitionCommand.DAILY)
    assert later.completeness is not None
    assert later.completeness.latest_ingested_completed_game_date == date(2026, 7, 16)

    service, _, _ = _service(tmp_path / "run", fixtures)
    validation = service.validate(
        AcquisitionRequest(date(2026, 7, 14), mode=AcquisitionMode.OFFLINE)
    )

    assert validation.completeness is not None
    assert validation.completeness.requested_through_date == date(2026, 7, 14)
    assert validation.completeness.latest_ingested_completed_game_date == date(
        2026, 7, 12
    )
    assert validation.completeness.contiguous_regular_season_complete_through_date == date(
        2026, 7, 12
    )
    assert validation.completeness.partial_date is None
    assert validation.validation_details is not None
    current = validation.validation_details["source_readiness"]["current_season"]
    assert current["stats_run_id"] == initial.stats_run_id
    assert current["stats_run_id"] != later.stats_run_id
    assert current["projected_completeness"][
        "latest_ingested_completed_game_date"
    ] == "2026-07-12"
    report = json.loads((tmp_path / "run" / "validation.json").read_text("utf-8"))
    assert report["initial_validation"][
        "effective_regular_season_complete_through_date"
    ] == "2026-07-12"
    assert report["initial_validation"][
        "latest_ingested_completed_game_date"
    ] == "2026-07-12"
