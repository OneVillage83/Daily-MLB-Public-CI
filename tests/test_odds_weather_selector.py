from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.odds_weather import (
    OddsWeatherRepository,
    OddsWeatherIntegrityError,
    OddsWeatherSelectorError,
)
from tests.test_odds_weather_repository import (
    _one_game_repository,
    _zero_game_odds_weather_repository,
)


def _persist_one_game(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repository, result, inventory, run_id = _one_game_repository(tmp_path, monkeypatch)
    persisted = repository.persist_assembly(
        run_id=run_id,
        phase_attempt=1,
        result=result,
        inventory=inventory,
    )
    return repository, persisted, inventory, run_id


def test_selector_reconstructs_deterministic_complete_retained_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, supplied, run_id = _persist_one_game(tmp_path, monkeypatch)
    with repository.database.connect() as connection:
        first = repository.selector.load_inventory(
            connection=connection,
            run_id=run_id,
            phase_attempt=1,
            source_warnings=supplied.source_warnings,
            final_warnings=supplied.final_warnings,
            selected_raw_capture_checksums=supplied.selected_raw_capture_checksums,
        )
        second = repository.selector.load_inventory(
            connection=connection,
            run_id=run_id,
            phase_attempt=1,
            source_warnings=supplied.source_warnings,
            final_warnings=supplied.final_warnings,
            selected_raw_capture_checksums=reversed(
                supplied.selected_raw_capture_checksums
            ),
        )

    assert first == supplied
    assert second == supplied
    assert first.checksum == second.checksum
    assert len(first.raw_captures) == 7
    assert len(first.provider_events) == 2
    assert len(first.future_provider_event_revisions) == 1
    assert len(first.weather_revisions) == 3
    assert len(first.future_weather_revisions) == 1
    assert len(first.odds_revision_inventory) == 5
    assert len(first.provider_events[0].event.mutable_event()["bookmakers"]) == 2
    assert {
        market["key"]
        for bookmaker in first.provider_events[0].event.mutable_event()["bookmakers"]
        for market in bookmaker["markets"]
    } == {"h2h", "spreads", "totals"}


def test_selector_zero_game_inventory_has_no_provider_evidence(tmp_path: Path) -> None:
    repository = _zero_game_odds_weather_repository(tmp_path)
    upstream = repository.baseball_intelligence.get_latest_for_run(
        "run_20260729_11111111111111111111111111111111"
    )
    assert upstream is not None
    inventory = repository.build_inventory(
        run_id=upstream.run_id,
        phase_attempt=1,
        observed_at=upstream.sealed_at,
        phase_input_checksum="0" * 64,
        raw_captures=(),
    )
    result, completed = repository.assemble_inventory(inventory)
    repository.persist_assembly(
        run_id=upstream.run_id,
        phase_attempt=1,
        result=result,
        inventory=completed,
    )

    assert repository.load_retained_inventory(upstream.run_id, 1).raw_captures == ()


def test_selector_rejects_tampered_raw_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository, _, supplied, run_id = _persist_one_game(tmp_path, monkeypatch)
    capture = supplied.raw_captures[0]
    (repository.artifact_root / capture.raw_relpath).write_bytes(b"{}")

    with pytest.raises(OddsWeatherIntegrityError, match="inventory"):
        repository.load_retained_inventory(run_id, 1)


@pytest.mark.parametrize(
    ("table", "trigger", "mutation", "message"),
    (
        (
            "odds_weather_bookmakers",
            "odds_weather_bookmakers_reject_delete",
            "DELETE FROM odds_weather_bookmakers WHERE bookmaker_key='book-a'",
            "bookmaker rows are incomplete",
        ),
        (
            "odds_weather_markets",
            "odds_weather_markets_reject_update",
            "UPDATE odds_weather_markets SET row_checksum='" + "0" * 64 + "' WHERE market_key='h2h'",
            "market row disagrees",
        ),
        (
            "odds_weather_outcomes",
            "odds_weather_outcomes_reject_update",
            "UPDATE odds_weather_outcomes SET price_american=999 WHERE outcome_name='Los Angeles Dodgers' AND market_key='h2h'",
            "outcome row disagrees",
        ),
        (
            "odds_weather_weather_revisions",
            "odds_weather_weather_revisions_reject_update",
            "UPDATE odds_weather_weather_revisions SET forecast_checksum='" + "0" * 64 + "' WHERE provider='nws'",
            "weather revision relational evidence",
        ),
    ),
)
def test_selector_rejects_relational_child_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    table: str,
    trigger: str,
    mutation: str,
    message: str,
) -> None:
    del table
    repository, _, _, run_id = _persist_one_game(tmp_path, monkeypatch)
    connection = sqlite3.connect(repository.database.path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(OddsWeatherIntegrityError, match="inventory") as raised:
        repository.load_retained_inventory(run_id, 1)
    assert isinstance(raised.value.__cause__, OddsWeatherSelectorError)
    assert message in str(raised.value.__cause__)


def test_selector_and_repository_make_no_network_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    repository, persisted, _, run_id = _persist_one_game(tmp_path, monkeypatch)
    reopened = OddsWeatherRepository(
        type(repository.database)(repository.database.path),
        artifact_root=repository.artifact_root,
    )

    assert reopened.load_retained_inventory(run_id, 1).provider_events
    assert reopened.get_by_snapshot_id(persisted.snapshot_id).snapshot.games
