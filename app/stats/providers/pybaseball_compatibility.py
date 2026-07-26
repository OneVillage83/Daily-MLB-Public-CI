from __future__ import annotations

import importlib
import platform
import sys
from contextlib import ExitStack
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any, Mapping
from unittest.mock import patch

from app.stats.providers.pybaseball_parity import (
    PYBASEBALL_REQUIRED_VERSION,
    PybaseballParityAdapter,
    PybaseballParityError,
)


PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION = "DSE_PYBASEBALL_PY312_COMPATIBILITY_V1"
_CAPABILITIES = (
    "batting_stats_range",
    "pitching_stats_range",
    "schedule_and_record",
    "statcast",
    "playerid_lookup",
    "playerid_reverse_lookup",
)


class PybaseballCompatibilityError(PybaseballParityError):
    pass


@dataclass(frozen=True, slots=True)
class PybaseballCompatibilityResult:
    python_version: str
    pybaseball_version: str
    capabilities: Mapping[str, Mapping[str, Any]]

    def __post_init__(self) -> None:
        normalized = {
            name: MappingProxyType(dict(value))
            for name, value in sorted(self.capabilities.items())
        }
        if tuple(normalized) != tuple(sorted(_CAPABILITIES)):
            raise PybaseballCompatibilityError(
                "pybaseball compatibility result does not cover all required capabilities"
            )
        object.__setattr__(self, "capabilities", MappingProxyType(normalized))

    def as_dict(self) -> dict[str, Any]:
        return {
            "contract_version": PYBASEBALL_COMPATIBILITY_CONTRACT_VERSION,
            "python_target": "3.12",
            "python_version": self.python_version,
            "package": "pybaseball",
            "pybaseball_version": self.pybaseball_version,
            "role": "noncanonical_parity_reference",
            "canonical_transport": False,
            "offline": True,
            "network_requests": 0,
            "status": "passed",
            "capabilities": {
                name: dict(value) for name, value in self.capabilities.items()
            },
        }


class _RecordingSession:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def get(self, url: str, *args: Any, **kwargs: Any) -> SimpleNamespace:
        del args, kwargs
        self.urls.append(url)
        return SimpleNamespace(content=b"")


class _OfflinePlayerClient:
    def search(
        self, last: str, first: str | None = None, fuzzy: bool = False
    ) -> Any:
        pandas = importlib.import_module("pandas")
        if (last.casefold(), str(first).casefold(), fuzzy) != (
            "ohtani",
            "shohei",
            False,
        ):
            return pandas.DataFrame()
        return pandas.DataFrame(
            [
                {
                    "name_last": "Ohtani",
                    "name_first": "Shohei",
                    "key_mlbam": 660271,
                    "key_retro": "ohtas001",
                }
            ]
        )

    def reverse_lookup(self, player_ids: list[object], key_type: str) -> Any:
        pandas = importlib.import_module("pandas")
        if player_ids != [660271] or key_type != "mlbam":
            return pandas.DataFrame()
        return pandas.DataFrame(
            [
                {
                    "name_last": "Ohtani",
                    "name_first": "Shohei",
                    "key_mlbam": 660271,
                    "key_retro": "ohtas001",
                }
            ]
        )


def _batting_frame(pandas: Any) -> Any:
    numeric = (
        "Age",
        "#days",
        "G",
        "PA",
        "AB",
        "R",
        "H",
        "2B",
        "3B",
        "HR",
        "RBI",
        "BB",
        "IBB",
        "SO",
        "HBP",
        "SH",
        "SF",
        "GDP",
        "SB",
        "CS",
        "BA",
        "OBP",
        "SLG",
        "OPS",
        "mlbID",
    )
    row: dict[str, Any] = {name: 0 for name in numeric}
    row.update({"Name": "Offline Hitter", "": "fixture"})
    return pandas.DataFrame([row])


def _pitching_frame(pandas: Any) -> Any:
    numeric = (
        "Age",
        "#days",
        "G",
        "GS",
        "W",
        "L",
        "SV",
        "IP",
        "H",
        "R",
        "ER",
        "BB",
        "SO",
        "HR",
        "HBP",
        "ERA",
        "AB",
        "2B",
        "3B",
        "IBB",
        "GDP",
        "SF",
        "SB",
        "CS",
        "PO",
        "BF",
        "Pit",
        "WHIP",
        "BAbip",
        "SO9",
        "SO/W",
        "mlbID",
    )
    row: dict[str, Any] = {name: 0 for name in numeric}
    row.update(
        {
            "Name": "Offline Pitcher",
            "Str": "0%",
            "StL": "0%",
            "StS": "0%",
            "GB/FB": "0%",
            "LD": "0%",
            "PU": "0%",
            "": "fixture",
        }
    )
    return pandas.DataFrame([row])


def _schedule_frame(pandas: Any) -> Any:
    return pandas.DataFrame(
        [
            {
                "Date": "2026-07-14",
                "Tm": "LAD",
                "Opp": "SF",
                "R": "0",
                "RA": "0",
                "Inn": "9",
                "Rank": "1",
                "Attendance": "0",
                "Streak": "+",
            }
        ]
    )


def _statcast_frame(pandas: Any) -> Any:
    return pandas.DataFrame(
        [
            {
                "game_date": "2026-07-14",
                "game_pk": 999001,
                "at_bat_number": 1,
                "pitch_number": 1,
            }
        ]
    )


def _recording_soup(module: Any, path: str) -> Any:
    module.session.get(f"http://www.baseball-reference.com/{path}")
    return object()


def _network_forbidden(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise PybaseballCompatibilityError(
        "offline pybaseball compatibility probe attempted network access"
    )


def run_installed_pybaseball_compatibility_probe() -> PybaseballCompatibilityResult:
    """Invoke every required installed callable using deterministic offline inputs."""

    if sys.version_info[:2] != (3, 12):
        raise PybaseballCompatibilityError(
            "pybaseball compatibility must be validated with Python 3.12"
        )
    pandas = importlib.import_module("pandas")
    pybaseball = importlib.import_module("pybaseball")
    batting_module = importlib.import_module("pybaseball.league_batting_stats")
    pitching_module = importlib.import_module("pybaseball.league_pitching_stats")
    schedule_module = importlib.import_module("pybaseball.team_results")
    statcast_module = importlib.import_module("pybaseball.statcast")
    player_module = importlib.import_module("pybaseball.playerid_lookup")
    requests_sessions = importlib.import_module("requests.sessions")
    urllib_request = importlib.import_module("urllib.request")

    batting_session = _RecordingSession()
    pitching_session = _RecordingSession()
    schedule_session = _RecordingSession()
    cache_module = getattr(pybaseball, "cache")
    cache_was_enabled = bool(cache_module.config.enabled)
    cache_module.disable()
    try:
        with ExitStack() as stack:
            stack.enter_context(
                patch.object(requests_sessions.Session, "request", _network_forbidden)
            )
            stack.enter_context(
                patch.object(urllib_request, "urlopen", _network_forbidden)
            )
            stack.enter_context(patch.object(batting_module, "session", batting_session))
            stack.enter_context(
                patch.object(pitching_module, "session", pitching_session)
            )
            stack.enter_context(
                patch.object(schedule_module, "session", schedule_session)
            )
            stack.enter_context(
                patch.object(
                    batting_module,
                    "get_soup",
                    lambda start, end: _recording_soup(
                        batting_module, f"offline-batting?from={start}&through={end}"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    batting_module,
                    "get_table",
                    lambda soup: _batting_frame(pandas),
                )
            )
            stack.enter_context(
                patch.object(
                    pitching_module,
                    "get_soup",
                    lambda start, end: _recording_soup(
                        pitching_module, f"offline-pitching?from={start}&through={end}"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    pitching_module,
                    "get_table",
                    lambda soup: _pitching_frame(pandas),
                )
            )
            stack.enter_context(
                patch.object(schedule_module, "get_first_season", lambda team: 1884)
            )
            stack.enter_context(
                patch.object(
                    schedule_module,
                    "get_soup",
                    lambda season, team: _recording_soup(
                        schedule_module, f"offline-schedule/{team}/{season}"
                    ),
                )
            )
            stack.enter_context(
                patch.object(
                    schedule_module,
                    "get_table",
                    lambda soup, team: _schedule_frame(pandas),
                )
            )
            stack.enter_context(
                patch.object(
                    statcast_module,
                    "_handle_request",
                    lambda start, end, step, verbose, team, parallel: _statcast_frame(
                        pandas
                    ),
                )
            )
            stack.enter_context(
                patch.object(player_module, "_client", _OfflinePlayerClient())
            )

            adapter = PybaseballParityAdapter.from_installed()
            results = {
                "batting_stats_range": adapter.batting_stats_range(
                    "2026-07-14", "2026-07-14"
                ),
                "pitching_stats_range": adapter.pitching_stats_range(
                    "2026-07-14", "2026-07-14"
                ),
                "schedule_and_record": adapter.schedule_and_record(2026, "LAD"),
                "statcast": adapter.statcast("2026-07-14", "2026-07-14"),
                "playerid_lookup": adapter.playerid_lookup(
                    "Ohtani",
                    "Shohei",
                    expected_id_field="key_mlbam",
                    expected_id=660271,
                ),
                "playerid_reverse_lookup": adapter.playerid_reverse_lookup(
                    [660271], key_type="key_mlbam"
                ),
            }
    finally:
        if cache_was_enabled:
            cache_module.enable()

    urls = batting_session.urls + pitching_session.urls + schedule_session.urls
    if len(urls) != 3 or any(not value.startswith("https://") for value in urls):
        raise PybaseballCompatibilityError(
            "legacy Baseball-Reference parity paths were not forced to HTTPS"
        )
    capabilities: dict[str, Mapping[str, Any]] = {}
    for name in _CAPABILITIES:
        result = results[name]
        if len(result.records) != 1:
            raise PybaseballCompatibilityError(
                f"installed pybaseball capability {name} returned unexpected offline rows"
            )
        capabilities[name] = {
            "invoked": True,
            "record_count": len(result.records),
            "canonical": False,
            "parameters": dict(result.parameters),
        }
    capabilities["batting_stats_range"] = {
        **capabilities["batting_stats_range"],
        "baseball_reference_https": True,
    }
    capabilities["pitching_stats_range"] = {
        **capabilities["pitching_stats_range"],
        "baseball_reference_https": True,
    }
    capabilities["schedule_and_record"] = {
        **capabilities["schedule_and_record"],
        "baseball_reference_https": True,
    }
    capabilities["statcast"] = {
        **capabilities["statcast"],
        "parallel": results["statcast"].parameters["parallel"],
    }
    return PybaseballCompatibilityResult(
        python_version=platform.python_version(),
        pybaseball_version=PYBASEBALL_REQUIRED_VERSION,
        capabilities=capabilities,
    )
