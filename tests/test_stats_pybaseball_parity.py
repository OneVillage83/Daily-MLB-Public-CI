from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pytest

import app.stats.providers.pybaseball_parity as parity_module
from app.stats.providers.pybaseball_parity import (
    PYBASEBALL_REQUIRED_VERSION,
    HttpsSessionPatch,
    HttpsUrlPatch,
    PybaseballFunctions,
    PybaseballIdentityError,
    PybaseballParityAdapter,
    PybaseballParityError,
    PybaseballVersionError,
)


@dataclass
class Recorded:
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


class FakePybaseball:
    def __init__(self) -> None:
        self.calls: list[Recorded] = []
        self.lookup_rows: list[dict[str, object]] = [
            {
                "name_last": "Ohtani",
                "name_first": "Shohei",
                "key_mlbam": 660271,
                "key_retro": "ohtas001",
            }
        ]
        self.reverse_rows: list[dict[str, object]] = [
            {"key_mlbam": 2, "name_last": "Second", "name_first": "Player"},
            {"key_mlbam": 1, "name_last": "First", "name_first": "Player"},
        ]

    def _table(self, name: str, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        self.calls.append(Recorded(name, args, kwargs))
        return [{"source": name, "zero": 0}]

    def batting_stats_range(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        return self._table("batting_stats_range", *args, **kwargs)

    def pitching_stats_range(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        return self._table("pitching_stats_range", *args, **kwargs)

    def schedule_and_record(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        return self._table("schedule_and_record", *args, **kwargs)

    def statcast(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        return self._table("statcast", *args, **kwargs)

    def playerid_lookup(self, *args: Any, **kwargs: Any) -> list[dict[str, object]]:
        self.calls.append(Recorded("playerid_lookup", args, kwargs))
        return self.lookup_rows

    def playerid_reverse_lookup(
        self, *args: Any, **kwargs: Any
    ) -> list[dict[str, object]]:
        self.calls.append(Recorded("playerid_reverse_lookup", args, kwargs))
        return self.reverse_rows

    def functions(self) -> PybaseballFunctions:
        return PybaseballFunctions(
            batting_stats_range=self.batting_stats_range,
            pitching_stats_range=self.pitching_stats_range,
            schedule_and_record=self.schedule_and_record,
            statcast=self.statcast,
            playerid_lookup=self.playerid_lookup,
            playerid_reverse_lookup=self.playerid_reverse_lookup,
        )


def adapter(fake: FakePybaseball) -> PybaseballParityAdapter:
    return PybaseballParityAdapter(
        fake.functions(), installed_version=PYBASEBALL_REQUIRED_VERSION
    )


def test_core_imports_do_not_load_optional_pybaseball_or_pandas() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
import app.stats
import app.stats.contracts
import app.stats.transport
import app.stats.providers.retrosheet
import app.stats.providers.baseball_reference
import app.stats.providers.statcast
assert 'pybaseball' not in sys.modules
assert 'pandas' not in sys.modules
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_exact_version_is_required_without_eager_import() -> None:
    fake = FakePybaseball()
    with pytest.raises(PybaseballVersionError, match="2.2.7"):
        PybaseballParityAdapter(fake.functions(), installed_version="2.2.6")

    imported = False

    def forbidden_import(name: str) -> object:
        nonlocal imported
        imported = True
        raise AssertionError(name)

    original_import = parity_module.importlib.import_module
    parity_module.importlib.import_module = forbidden_import  # type: ignore[assignment]
    try:
        with pytest.raises(PybaseballVersionError, match="2.2.7"):
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(parity_module.importlib.metadata, "version", lambda name: "2.2.6")
                PybaseballParityAdapter.from_installed()
    finally:
        parity_module.importlib.import_module = original_import
    assert imported is False


def test_noncanonical_wrappers_preserve_exact_arguments_and_provenance() -> None:
    fake = FakePybaseball()
    client = adapter(fake)

    batting = client.batting_stats_range(date(2026, 4, 1), "2026-07-14")
    pitching = client.pitching_stats_range("2026-04-01", "2026-07-14")
    schedule = client.schedule_and_record(2026, "LAD")
    statcast = client.statcast("2026-07-10", "2026-07-11", team="LAD")

    assert [call.name for call in fake.calls] == [
        "batting_stats_range",
        "pitching_stats_range",
        "schedule_and_record",
        "statcast",
    ]
    assert fake.calls[0].args == ("2026-04-01", "2026-07-14")
    assert fake.calls[2].args == (2026, "LAD")
    assert fake.calls[3].kwargs == {
        "team": "LAD",
        "verbose": False,
        "parallel": False,
    }
    for result in (batting, pitching, schedule, statcast):
        assert result.provenance.version == "2.2.7"
        assert result.provenance.canonical is False
        assert result.provenance.role == "noncanonical_parity_reference"
        assert result.records[0]["zero"] == 0
    assert statcast.parameters["parallel"] is False


def test_https_patch_is_scheme_only_and_restored_after_call() -> None:
    class UrlOwner:
        DATA_URL = "http://provider.example/data?kind=stats"

    fake = FakePybaseball()

    def batting(*args: Any, **kwargs: Any) -> list[dict[str, object]]:
        del args, kwargs
        assert UrlOwner.DATA_URL == "https://provider.example/data?kind=stats"
        return [{"ok": True}]

    functions = fake.functions()
    functions = PybaseballFunctions(
        batting_stats_range=batting,
        pitching_stats_range=functions.pitching_stats_range,
        schedule_and_record=functions.schedule_and_record,
        statcast=functions.statcast,
        playerid_lookup=functions.playerid_lookup,
        playerid_reverse_lookup=functions.playerid_reverse_lookup,
    )
    client = PybaseballParityAdapter(
        functions,
        installed_version="2.2.7",
        https_patches=(
            HttpsUrlPatch(
                UrlOwner,
                "DATA_URL",
                "https://provider.example/data?kind=stats",
            ),
        ),
    )

    client.batting_stats_range("2026-04-01", "2026-07-14")

    assert UrlOwner.DATA_URL == "http://provider.example/data?kind=stats"
    with pytest.raises(ValueError, match="HTTPS"):
        HttpsUrlPatch(UrlOwner, "DATA_URL", "http://provider.example/data")
    bad_destination = PybaseballParityAdapter(
        functions,
        installed_version="2.2.7",
        https_patches=(
            HttpsUrlPatch(UrlOwner, "DATA_URL", "https://evil.example/data?kind=stats"),
        ),
    )
    with pytest.raises(PybaseballParityError, match="destination changes"):
        bad_destination.batting_stats_range("2026-04-01", "2026-07-14")


def test_legacy_baseball_reference_session_is_https_only_and_restored() -> None:
    class RecordingSession:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get(self, url: str) -> list[dict[str, object]]:
            self.urls.append(url)
            return [{"ok": True}]

    class SessionOwner:
        session = RecordingSession()

    fake = FakePybaseball()

    def batting(*args: Any, **kwargs: Any) -> list[dict[str, object]]:
        del args, kwargs
        return SessionOwner.session.get(
            "http://www.baseball-reference.com/leagues/daily.cgi?type=b"
        )

    functions = fake.functions()
    client = PybaseballParityAdapter(
        PybaseballFunctions(
            batting_stats_range=batting,
            pitching_stats_range=functions.pitching_stats_range,
            schedule_and_record=functions.schedule_and_record,
            statcast=functions.statcast,
            playerid_lookup=functions.playerid_lookup,
            playerid_reverse_lookup=functions.playerid_reverse_lookup,
        ),
        installed_version="2.2.7",
        https_session_patches=(HttpsSessionPatch(SessionOwner),),
    )
    original = SessionOwner.session

    client.batting_stats_range("2026-04-01", "2026-07-14")

    assert SessionOwner.session is original
    assert original.urls == [
        "https://www.baseball-reference.com/leagues/daily.cgi?type=b"
    ]

    def unexpected_host(*args: Any, **kwargs: Any) -> list[dict[str, object]]:
        del args, kwargs
        return SessionOwner.session.get("http://example.test/leagues/daily.cgi")

    rejected = PybaseballParityAdapter(
        PybaseballFunctions(
            batting_stats_range=unexpected_host,
            pitching_stats_range=functions.pitching_stats_range,
            schedule_and_record=functions.schedule_and_record,
            statcast=functions.statcast,
            playerid_lookup=functions.playerid_lookup,
            playerid_reverse_lookup=functions.playerid_reverse_lookup,
        ),
        installed_version="2.2.7",
        https_session_patches=(HttpsSessionPatch(SessionOwner),),
    )
    with pytest.raises(PybaseballParityError, match="unexpected host"):
        rejected.batting_stats_range("2026-04-01", "2026-07-14")


def test_player_lookup_disables_fuzzy_matching_and_requires_one_exact_identity() -> None:
    fake = FakePybaseball()
    client = adapter(fake)

    result = client.playerid_lookup(
        "Ohtani",
        "Shohei",
        expected_id_field="key_mlbam",
        expected_id=660271,
    )

    assert result.records[0]["key_retro"] == "ohtas001"
    assert fake.calls[-1].kwargs == {"fuzzy": False}
    assert result.parameters["fuzzy"] is False

    fake.lookup_rows.append(dict(fake.lookup_rows[0]))
    with pytest.raises(PybaseballIdentityError, match="one identity"):
        client.playerid_lookup("Ohtani", "Shohei")


def test_player_lookup_never_fuzzy_merges_similar_names() -> None:
    fake = FakePybaseball()
    fake.lookup_rows = [
        {
            "name_last": "Molina",
            "name_first": "Yadier",
            "key_mlbam": 425877,
        }
    ]
    client = adapter(fake)

    with pytest.raises(PybaseballIdentityError, match="one identity"):
        client.playerid_lookup("Molina", "Yadi")


def test_reverse_lookup_requires_unique_ids_and_exactly_one_row_per_id() -> None:
    fake = FakePybaseball()
    client = adapter(fake)

    result = client.playerid_reverse_lookup([1, 2], key_type="key_mlbam")

    assert [row["key_mlbam"] for row in result.records] == [1, 2]
    assert fake.calls[-1].args == ([1, 2],)
    assert fake.calls[-1].kwargs == {"key_type": "mlbam"}

    with pytest.raises(PybaseballIdentityError, match="unique"):
        client.playerid_reverse_lookup([1, 1])
    fake.reverse_rows = [{"key_mlbam": 1}]
    with pytest.raises(PybaseballIdentityError, match="every requested"):
        client.playerid_reverse_lookup([1, 2])


@pytest.mark.parametrize(
    "call",
    [
        lambda client: client.batting_stats_range("2026-7-1", "2026-07-14"),
        lambda client: client.schedule_and_record(2026, "lad"),
        lambda client: client.statcast("2026-07-15", "2026-07-14"),
        lambda client: client.playerid_lookup(" Ohtani", "Shohei"),
    ],
)
def test_parity_boundary_rejects_implicit_or_normalized_inputs(call: Any) -> None:
    client = adapter(FakePybaseball())

    with pytest.raises(ValueError):
        call(client)
