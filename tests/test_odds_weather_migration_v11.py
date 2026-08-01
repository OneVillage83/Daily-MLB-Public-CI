from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

import app.migrations as migrations
from app.baseball_intelligence import assemble_baseball_intelligence
from app.daily_slate import (
    DailySlateDoubleheaderStatus,
    DailySlateProvenanceV1,
    DailySlateV1,
)
from app.database import Database
from app.game_state import GameStateProvenanceV1, GameStateV1
from app.migrations import (
    BackupVerificationError,
    CURRENT_SCHEMA_VERSION,
    DiagnosticWriteError,
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_STATEMENTS,
    FORMAL_SCHEMA_V9_STATEMENTS,
    FORMAL_SCHEMA_V10_FINGERPRINT,
    FORMAL_SCHEMA_V10_STATEMENTS,
    FORMAL_SCHEMA_V11_FINGERPRINT,
    FORMAL_SCHEMA_V11_STATEMENTS,
    MIGRATION_HISTORY,
    MIGRATION_V10_CHECKSUM,
    MIGRATION_V11_CHECKSUM,
    MIGRATION_V11_NAME,
    ensure_schema,
    schema_fingerprint,
)
from app.odds_weather import (
    OddsProviderEventV1,
    OddsWeatherV1,
    WeatherForecastEvidenceV1,
    WeatherProvider,
    assemble_odds_weather,
)
from app.odds_weather.contracts import canonical_sha256
from app.odds_weather_migration import (
    ODDS_WEATHER_SCHEMA_V11_IMMUTABILITY_TRIGGER_STATEMENTS,
    ODDS_WEATHER_SCHEMA_V11_INDEX_STATEMENTS,
    ODDS_WEATHER_SCHEMA_V11_TABLE_STATEMENTS,
    ODDS_WEATHER_SCHEMA_V11_VALIDATION_TRIGGER_STATEMENTS,
)
from tests.test_baseball_intelligence_migration_v10_roundtrip import (
    AS_OF,
    RAW_CHECKSUM,
    REQUESTED_DATE,
    RUN_ID,
    SLATE_OBSERVED,
    Fixture,
    _build_database,
    _fixture,
    _insert_bia,
    _persist_feature_inventory,
    _persist_upstream,
    _seal,
)
from tests.test_odds_weather_assembly import _odds_event, _weather


V10_CHECKSUM = "877bdccccb64814a0844adb57279a87d477c79a0e8659ebc3a3dfc08d3bb071b"
V10_FINGERPRINT = "13a8ed8e477c23954c94a7b6a697c6dae74efd5d3806f0187b1fa2abeb5933c6"
V11_CHECKSUM = "a54865d8b5623e96c4f571d6c9d7f899e9ced0d1911128b5a874b41e29df75dd"
V11_FINGERPRINT = "5b9635e1aac05d98fd61dadaf2ac5d435e4aaae9214c79501b5dd642673c75b8"

TABLES = {
    "odds_weather_attempt_evidence",
    "odds_weather_snapshots",
    "odds_weather_games",
    "odds_weather_warnings",
    "odds_weather_raw_captures",
    "odds_weather_snapshot_raw_captures",
    "odds_weather_provider_events",
    "odds_weather_bookmakers",
    "odds_weather_markets",
    "odds_weather_outcomes",
    "odds_weather_odds_revisions",
    "odds_weather_weather_revisions",
    "odds_weather_weather_raw_captures",
    "odds_weather_game_weather_selections",
}
INDEXES = {
    "idx_ow_attempt_lookup",
    "idx_ow_attempt_cutoff",
    "idx_ow_snapshots_run",
    "idx_ow_snapshots_upstream_bia",
    "idx_ow_snapshots_cutoff",
    "idx_ow_snapshots_artifact",
    "idx_ow_games_source",
    "idx_ow_games_provider_event",
    "idx_ow_warnings_lookup",
    "idx_ow_raw_captures_provider",
    "idx_ow_provider_events_cutoff",
    "idx_ow_provider_events_match",
    "idx_ow_bookmakers_key",
    "idx_ow_markets_key_update",
    "idx_ow_outcomes_identity",
    "idx_ow_odds_revisions_cutoff",
    "uq_ow_odds_revisions_unpointed_identity",
    "uq_ow_odds_revisions_pointed_identity",
    "idx_ow_weather_revisions_cutoff",
    "idx_ow_weather_selections_provider",
}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _install_formal_v10(path: Path, *, populated: bool = False) -> None:
    statement_groups = (
        FORMAL_SCHEMA_V1_STATEMENTS,
        FORMAL_SCHEMA_V2_STATEMENTS,
        FORMAL_SCHEMA_V3_STATEMENTS,
        FORMAL_SCHEMA_V4_STATEMENTS,
        FORMAL_SCHEMA_V5_STATEMENTS,
        FORMAL_SCHEMA_V6_STATEMENTS,
        FORMAL_SCHEMA_V7_STATEMENTS,
        FORMAL_SCHEMA_V8_STATEMENTS,
        FORMAL_SCHEMA_V9_STATEMENTS,
        FORMAL_SCHEMA_V10_STATEMENTS,
    )
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        for version, statements, history in zip(
            range(1, 11), statement_groups, MIGRATION_HISTORY[:10], strict=True
        ):
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("BEGIN IMMEDIATE")
            for statement in statements:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version,name,checksum,applied_at) VALUES (?,?,?,?)",
                (*history, "2026-07-31T12:00:00+00:00"),
            )
            connection.execute(f"PRAGMA user_version={version}")
            connection.commit()
        if populated:
            connection.execute("PRAGMA foreign_keys=ON")
            fixture = _fixture()
            slate_id, state_id = _persist_upstream(connection, fixture)
            _persist_feature_inventory(connection, fixture)
            _insert_bia(connection, fixture, slate_id, state_id)
            _seal(connection, fixture)
            connection.commit()
        assert schema_fingerprint(connection) == FORMAL_SCHEMA_V10_FINGERPRINT
    finally:
        connection.close()


def _advance_to_odds_weather(
    connection: sqlite3.Connection, fixture: Fixture
) -> None:
    assembly = fixture.assembly
    when = assembly.observed_at.isoformat()
    connection.execute(
        """
        UPDATE pipeline_run_phases
        SET status='succeeded_with_warnings',completed_at=?,updated_at=?,
            output_checksum=?,artifact_relpath=?
        WHERE run_id=? AND phase_key='baseball_intelligence_assembly'
        """,
        (
            when,
            when,
            assembly.checksum,
            f"baseball_intelligence/snapshots/{assembly.checksum}/baseball_intelligence_v1.json",
            RUN_ID,
        ),
    )
    connection.execute(
        """
        UPDATE pipeline_run_phases
        SET status='running',attempt_count=1,started_at=?,updated_at=?
        WHERE run_id=? AND phase_key='odds_weather'
        """,
        (when, when, RUN_ID),
    )


def _build_phase4_fixture(
    tmp_path: Path,
) -> tuple[
    sqlite3.Connection,
    Fixture,
    OddsWeatherV1,
    OddsProviderEventV1,
    tuple[WeatherForecastEvidenceV1, ...],
]:
    connection, fixture = _build_database(tmp_path)
    _seal(connection, fixture)
    _advance_to_odds_weather(connection, fixture)
    observed_at = fixture.assembly.observed_at + timedelta(minutes=10)
    event = _odds_event(
        "event-schema-v11",
        commence=fixture.slate.games[0].scheduled_start_time,
        retrieved_at=observed_at - timedelta(minutes=2),
    )
    forecasts = (
        _weather(
            fixture.slate.games[0],
            WeatherProvider.NWS,
            retrieved_at=observed_at - timedelta(minutes=1),
        ),
        _weather(
            fixture.slate.games[0],
            WeatherProvider.OPENWEATHER,
            retrieved_at=observed_at - timedelta(minutes=1),
            variant=1,
        ),
    )
    snapshot = assemble_odds_weather(
        slate=fixture.slate,
        baseball_intelligence=fixture.assembly,
        odds_events=(event,),
        weather_evidence=forecasts,
        observed_at=observed_at,
    ).snapshot
    return connection, fixture, snapshot, event, forecasts


def _insert_attempt(
    connection: sqlite3.Connection,
    fixture: Fixture,
    snapshot: OddsWeatherV1,
) -> None:
    connection.execute(
        """
        INSERT INTO odds_weather_attempt_evidence(
          run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
          phase_input_checksum,upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
          upstream_game_state_snapshot_id,upstream_game_state_checksum,
          upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
          outcome,snapshot_checksum,evidence_manifest_relpath,evidence_manifest_checksum,
          evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            "odds_weather",
            1,
            snapshot.requested_date,
            snapshot.as_of_time.isoformat(),
            snapshot.observed_at.isoformat(),
            _sha("phase-input-v11"),
            f"slate:{fixture.slate.checksum}",
            fixture.slate.checksum,
            f"state:{fixture.state.checksum}",
            fixture.state.checksum,
            fixture.snapshot_id,
            fixture.assembly.checksum,
            "assembled",
            snapshot.checksum,
            "odds_weather/attempts/fixture/attempt_0001.json",
            _sha("attempt-manifest-v11"),
            128,
            _json([warning.as_dict() for warning in snapshot.warnings]),
            len(snapshot.warnings),
            snapshot.observed_at.isoformat(),
            snapshot.observed_at.isoformat(),
        ),
    )


def _insert_raw_and_provider_evidence(
    connection: sqlite3.Connection,
    snapshot: OddsWeatherV1,
    event: OddsProviderEventV1,
    forecasts: tuple[WeatherForecastEvidenceV1, ...],
) -> None:
    raw_items = (
        (event.raw_capture_checksum, "the_odds_api", "mlb_odds", None, event.provider_event_id, event.retrieved_at),
        *(
            (
                forecast.raw_capture_checksums[0],
                forecast.provider.value,
                "hourly_forecast" if forecast.provider is WeatherProvider.NWS else "one_call",
                forecast.source_game_id,
                None,
                forecast.retrieved_at,
            )
            for forecast in forecasts
        ),
    )
    for ordinal, (checksum, provider, endpoint, game_id, event_id, retrieved_at) in enumerate(raw_items, 1):
        connection.execute(
            """
            INSERT INTO odds_weather_raw_captures(
              run_id,phase_attempt,ordinal,provider,endpoint_category,source_game_id,
              provider_event_id,retrieved_at,provider_timestamp,raw_relpath,
              raw_capture_checksum,raw_byte_count,canonical_metadata_json,row_checksum
            ) VALUES (?,1,?,?,?,?,?,?,NULL,?,?,?,?,?)
            """,
            (
                RUN_ID,
                ordinal,
                provider,
                endpoint,
                game_id,
                event_id,
                retrieved_at.isoformat(),
                f"raw/{checksum}.json",
                checksum,
                100 + ordinal,
                "{}",
                _sha(f"raw-row:{checksum}"),
            ),
        )

    event_payload = event.mutable_event()
    connection.execute(
        """
        INSERT INTO odds_weather_provider_events(
          run_id,phase_attempt,provider_event_id,retrieved_at,revision_ordinal,sport_key,
          commence_time,away_team_id,home_team_id,raw_capture_checksum,contract_version,
          event_checksum,canonical_event_json,row_checksum
        ) VALUES (?,1,?,?,1,'baseball_mlb',?,?,?,?,?,?,?,?)
        """,
        (
            RUN_ID,
            event.provider_event_id,
            event.retrieved_at.isoformat(),
            event_payload["commence_time"],
            event.away_team_id,
            event.home_team_id,
            event.raw_capture_checksum,
            event.contract_version,
            canonical_sha256(event_payload),
            _json(event_payload),
            canonical_sha256(event.as_dict()),
        ),
    )
    revision_ordinal = 0
    for book_ordinal, bookmaker in enumerate(event_payload["bookmakers"], 1):
        connection.execute(
            """
            INSERT INTO odds_weather_bookmakers(
              run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
              bookmaker_key,title,bookmaker_last_update,canonical_json,row_checksum
            ) VALUES (?,1,?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                event.provider_event_id,
                event.retrieved_at.isoformat(),
                book_ordinal,
                bookmaker["key"],
                bookmaker["title"],
                bookmaker.get("last_update"),
                _json(bookmaker),
                canonical_sha256(bookmaker),
            ),
        )
        for market_ordinal, market in enumerate(bookmaker["markets"], 1):
            connection.execute(
                """
                INSERT INTO odds_weather_markets(
                  run_id,phase_attempt,provider_event_id,event_retrieved_at,bookmaker_key,
                  ordinal,market_key,market_last_update,canonical_json,row_checksum
                ) VALUES (?,1,?,?,?,?,?,?,?,?)
                """,
                (
                    RUN_ID,
                    event.provider_event_id,
                    event.retrieved_at.isoformat(),
                    bookmaker["key"],
                    market_ordinal,
                    market["key"],
                    market.get("last_update"),
                    _json(market),
                    canonical_sha256(market),
                ),
            )
            for outcome_ordinal, outcome in enumerate(market["outcomes"], 1):
                connection.execute(
                    """
                    INSERT INTO odds_weather_outcomes(
                      run_id,phase_attempt,provider_event_id,event_retrieved_at,
                      bookmaker_key,market_key,ordinal,outcome_name,price_american,
                      point,canonical_json,row_checksum
                    ) VALUES (?,1,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        RUN_ID,
                        event.provider_event_id,
                        event.retrieved_at.isoformat(),
                        bookmaker["key"],
                        market["key"],
                        outcome_ordinal,
                        outcome["name"],
                        outcome["price"],
                        outcome.get("point"),
                        _json(outcome),
                        canonical_sha256(outcome),
                    ),
                )
                revision_ordinal += 1
                connection.execute(
                    """
                    INSERT INTO odds_weather_odds_revisions(
                      run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
                      bookmaker_key,market_key,outcome_name,price_american,point,
                      provider_last_update,bookmaker_last_update,market_last_update,
                      retrieved_at,canonical_json,row_checksum
                    ) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        RUN_ID,
                        event.provider_event_id,
                        event.retrieved_at.isoformat(),
                        revision_ordinal,
                        bookmaker["key"],
                        market["key"],
                        outcome["name"],
                        outcome["price"],
                        outcome.get("point"),
                        event_payload.get("last_update"),
                        bookmaker.get("last_update"),
                        market.get("last_update"),
                        event.retrieved_at.isoformat(),
                        _json(outcome),
                        canonical_sha256({"revision": revision_ordinal, **outcome}),
                    ),
                )

    for forecast in forecasts:
        forecast_payload = dict(forecast.forecast)
        connection.execute(
            """
            INSERT INTO odds_weather_weather_revisions(
              run_id,phase_attempt,source_game_id,provider,retrieved_at,revision_ordinal,
              forecast_time,forecast_offset_minutes,contract_version,forecast_checksum,
              canonical_json,row_checksum
            ) VALUES (?,1,?,?,?,1,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                forecast.source_game_id,
                forecast.provider.value,
                forecast.retrieved_at.isoformat(),
                forecast_payload["forecast_time"],
                forecast_payload["forecast_offset_minutes"],
                forecast.contract_version,
                canonical_sha256(forecast_payload),
                _json(forecast.as_dict()),
                canonical_sha256(forecast.as_dict()),
            ),
        )
        connection.execute(
            """
            INSERT INTO odds_weather_weather_raw_captures(
              run_id,phase_attempt,source_game_id,provider,weather_retrieved_at,
              ordinal,raw_capture_checksum
            ) VALUES (?,1,?,?,?,1,?)
            """,
            (
                RUN_ID,
                forecast.source_game_id,
                forecast.provider.value,
                forecast.retrieved_at.isoformat(),
                forecast.raw_capture_checksums[0],
            ),
        )


def _insert_snapshot_and_children(
    connection: sqlite3.Connection,
    fixture: Fixture,
    snapshot: OddsWeatherV1,
    event: OddsProviderEventV1,
    forecasts: tuple[WeatherForecastEvidenceV1, ...],
    *,
    artifact_relpath: str | None = None,
    artifact_byte_count: int | None = None,
    canonical_json: str | None = None,
    null_snapshot_id: bool = False,
) -> str:
    snapshot_id: str | None = (
        None if null_snapshot_id else f"odds-weather:{snapshot.checksum}"
    )
    artifact = snapshot.canonical_json_bytes()
    connection.execute(
        """
        INSERT INTO odds_weather_snapshots(
          snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
          sport,league,contract_version,phase_input_checksum,
          upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
          upstream_game_state_snapshot_id,upstream_game_state_checksum,
          upstream_baseball_intelligence_snapshot_id,upstream_baseball_intelligence_checksum,
          snapshot_checksum,source_raw_capture_checksums_json,warnings_json,warning_count,
          canonical_json,game_count,selected_raw_capture_count,weather_selection_count,
          artifact_relpath,artifact_checksum,artifact_byte_count,sealed_at,created_at
        ) VALUES (?,?,'odds_weather',1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?)
        """,
        (
            snapshot_id,
            RUN_ID,
            snapshot.requested_date,
            snapshot.as_of_time.isoformat(),
            snapshot.observed_at.isoformat(),
            snapshot.sport,
            snapshot.league,
            snapshot.contract_version,
            _sha("phase-input-v11"),
            f"slate:{fixture.slate.checksum}",
            fixture.slate.checksum,
            f"state:{fixture.state.checksum}",
            fixture.state.checksum,
            fixture.snapshot_id,
            fixture.assembly.checksum,
            snapshot.checksum,
            _json(list(snapshot.source_raw_capture_checksums)),
            _json([warning.as_dict() for warning in snapshot.warnings]),
            len(snapshot.warnings),
            canonical_json or artifact.decode(),
            len(snapshot.games),
            len(snapshot.source_raw_capture_checksums),
            sum(
                [
                    int(game.weather.nws is not None)
                    + int(game.weather.openweather is not None)
                    for game in snapshot.games
                ]
            ),
            (
                f"odds_weather/snapshots/{snapshot.checksum}/odds_weather_v1.json"
                if artifact_relpath is None
                else artifact_relpath
            ),
            hashlib.sha256(artifact).hexdigest(),
            len(artifact) if artifact_byte_count is None else artifact_byte_count,
            snapshot.observed_at.isoformat(),
        ),
    )
    for ordinal, checksum in enumerate(snapshot.source_raw_capture_checksums, 1):
        connection.execute(
            """
            INSERT INTO odds_weather_snapshot_raw_captures(
              snapshot_id,run_id,phase_attempt,ordinal,raw_capture_checksum
            ) VALUES (?,?,1,?,?)
            """,
            (snapshot_id, RUN_ID, ordinal, checksum),
        )

    game = snapshot.games[0]
    game_payload = game.as_dict()
    odds_payload = cast(dict[str, Any], game_payload["odds"])
    weather_payload = cast(dict[str, Any], game_payload["weather"])
    slate_game = fixture.slate.games[0]
    state_game = fixture.state.games[0]
    bia_game = fixture.assembly.games[0]
    venue_context = game.weather.venue_context
    assert game.scheduled_start_time is not None
    assert game.odds.retrieved_at is not None
    assert game.weather.primary_source is not None
    assert venue_context is not None
    connection.execute(
        """
        INSERT INTO odds_weather_games(
          snapshot_id,run_id,phase_attempt,ordinal,edge_event_id,daily_mlb_game_id,
          source_game_id,official_date,away_team_id,home_team_id,source_away_team_id,
          source_home_team_id,venue_id,game_status,scheduled_start_time,
          upstream_daily_slate_game_checksum,upstream_game_state_game_checksum,
          upstream_baseball_intelligence_game_checksum,odds_availability,
          selected_provider_event_id,odds_retrieved_at,odds_raw_capture_checksum,
          event_match_offset_minutes,odds_summary_json,odds_summary_checksum,
          normalized_market_count,raw_snapshot_count,freshness_counts_json,
          odds_calculation_version,odds_consensus_contract_version,weather_status,
          weather_relevance,weather_primary_source,venue_context_json,
          venue_context_checksum,weather_comparison_json,baseball_wind_impact_json,
          weather_revision_count,canonical_json,row_checksum
        ) VALUES (?,?,1,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id,
            RUN_ID,
            game.edge_event_id,
            game.daily_mlb_game_id,
            game.source_game_id,
            slate_game.official_date,
            game.away_team_id,
            game.home_team_id,
            state_game.away.source_team_id,
            state_game.home.source_team_id,
            bia_game.venue_id,
            bia_game.game_status,
            game.scheduled_start_time.isoformat(),
            game.upstream_daily_slate_game_checksum,
            state_game.checksum,
            game.upstream_baseball_intelligence_game_checksum,
            game.odds.availability.value,
            game.odds.provider_event_id,
            game.odds.retrieved_at.isoformat(),
            game.odds.raw_capture_checksum,
            game.odds.event_match_offset_minutes,
            _json(odds_payload["summary"]),
            game.odds.summary_checksum,
            game.odds.normalized_market_count,
            game.odds.raw_snapshot_count,
            _json(odds_payload["freshness_counts"]),
            game.odds.calculation_version,
            game.odds.odds_consensus_contract_version,
            game.weather.status.value,
            game.weather.relevance.value,
            game.weather.primary_source.value,
            _json(weather_payload["venue_context"]),
            canonical_sha256(venue_context.as_dict()),
            _json(weather_payload["comparison"]),
            _json(weather_payload["baseball_wind_impact"]),
            2,
            _json(game_payload),
            game.checksum,
        ),
    )
    for forecast in forecasts:
        connection.execute(
            """
            INSERT INTO odds_weather_game_weather_selections(
              snapshot_id,run_id,phase_attempt,source_game_id,provider,
              weather_retrieved_at,forecast_checksum,is_primary
            ) VALUES (?,?,1,?,?,?,?,?)
            """,
            (
                snapshot_id,
                RUN_ID,
                forecast.source_game_id,
                forecast.provider.value,
                forecast.retrieved_at.isoformat(),
                canonical_sha256(dict(forecast.forecast)),
                int(forecast.provider is WeatherProvider.NWS),
            ),
        )
    connection.execute(
        "UPDATE odds_weather_snapshots SET sealed_at=? WHERE snapshot_id=?",
        (snapshot.observed_at.isoformat(), snapshot_id),
    )
    assert snapshot_id is not None
    return snapshot_id


@pytest.mark.parametrize(
    ("price_delta", "row_checksum"),
    (
        pytest.param(0.0, None, id="exact_duplicate_evidence"),
        pytest.param(7.0, _sha("conflicting-h2h-revision"), id="conflicting_evidence"),
    ),
)
def test_h2h_revision_semantic_identity_is_null_safe(
    tmp_path: Path,
    price_delta: float,
    row_checksum: str | None,
) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        source = connection.execute(
            """
            SELECT bookmaker_key,market_key,outcome_name,price_american,point,
                   provider_last_update,bookmaker_last_update,market_last_update,
                   retrieved_at,canonical_json,row_checksum
            FROM odds_weather_odds_revisions
            WHERE run_id=? AND phase_attempt=1 AND market_key='h2h'
              AND point IS NULL
            ORDER BY ordinal LIMIT 1
            """,
            (RUN_ID,),
        ).fetchone()
        assert source is not None
        before = connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions"
        ).fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO odds_weather_odds_revisions(
                  run_id,phase_attempt,provider_event_id,event_retrieved_at,
                  ordinal,bookmaker_key,market_key,outcome_name,price_american,
                  point,provider_last_update,bookmaker_last_update,
                  market_last_update,retrieved_at,canonical_json,row_checksum
                ) VALUES (?,1,?,?,100,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    RUN_ID,
                    event.provider_event_id,
                    event.retrieved_at.isoformat(),
                    source[0],
                    source[1],
                    source[2],
                    source[3] + price_delta,
                    source[4],
                    source[5],
                    source[6],
                    source[7],
                    source[8],
                    source[9],
                    row_checksum or source[10],
                ),
            )
        assert connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions"
        ).fetchone()[0] == before
    finally:
        connection.close()


def test_revision_identity_preserves_time_and_point_dimensions(tmp_path: Path) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)

        def source_for(market_key: str) -> tuple[Any, ...]:
            row = connection.execute(
                """
                SELECT bookmaker_key,market_key,outcome_name,price_american,point,
                       provider_last_update,bookmaker_last_update,market_last_update,
                       retrieved_at,canonical_json
                FROM odds_weather_odds_revisions
                WHERE run_id=? AND phase_attempt=1 AND market_key=?
                ORDER BY ordinal LIMIT 1
                """,
                (RUN_ID, market_key),
            ).fetchone()
            assert row is not None
            return cast(tuple[Any, ...], row)

        def insert_copy(
            source: tuple[Any, ...],
            *,
            ordinal: int,
            retrieved_at: str,
            point: float | None,
            salt: str,
        ) -> None:
            connection.execute(
                """
                INSERT INTO odds_weather_odds_revisions(
                  run_id,phase_attempt,provider_event_id,event_retrieved_at,
                  ordinal,bookmaker_key,market_key,outcome_name,price_american,
                  point,provider_last_update,bookmaker_last_update,
                  market_last_update,retrieved_at,canonical_json,row_checksum
                ) VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    RUN_ID,
                    event.provider_event_id,
                    event.retrieved_at.isoformat(),
                    ordinal,
                    source[0],
                    source[1],
                    source[2],
                    source[3],
                    point,
                    source[5],
                    source[6],
                    source[7],
                    retrieved_at,
                    source[9],
                    _sha(salt),
                ),
            )

        h2h = source_for("h2h")
        later_retrieval = (
            datetime.fromisoformat(h2h[8]) + timedelta(minutes=1)
        ).isoformat()
        insert_copy(
            h2h,
            ordinal=100,
            retrieved_at=later_retrieval,
            point=None,
            salt="h2h-later-retrieval",
        )

        for ordinal, (market_key, distinct_point) in enumerate(
            (("spreads", -2.5), ("totals", 9.5)),
            start=101,
        ):
            source = source_for(market_key)
            insert_copy(
                source,
                ordinal=ordinal,
                retrieved_at=source[8],
                point=distinct_point,
                salt=f"{market_key}-distinct-point",
            )
            with pytest.raises(sqlite3.IntegrityError):
                insert_copy(
                    source,
                    ordinal=ordinal + 10,
                    retrieved_at=source[8],
                    point=distinct_point,
                    salt=f"{market_key}-duplicate-point",
                )

        assert connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions WHERE market_key='h2h' AND retrieved_at=?",
            (later_retrieval,),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions WHERE market_key='spreads' AND point=-2.5"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions WHERE market_key='totals' AND point=9.5"
        ).fetchone()[0] == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "artifact_relpath",
    (
        f"odds_weather/snapshots/{'0' * 64}/odds_weather_v1.json",
        f"odds_weather/snapshots/{'f' * 64}/odds_weather_v1.json",
        "odds_weather/snapshots/not-a-checksum/odds_weather_v1.json",
        "odds_weather/snapshots/../odds_weather_v1.json",
        "odds_weather/snapshots/fixture/wrong.json",
        "",
    ),
)
def test_snapshot_artifact_path_is_bound_to_snapshot_checksum(
    tmp_path: Path,
    artifact_relpath: str,
) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot_and_children(
                connection,
                fixture,
                snapshot,
                event,
                forecasts,
                artifact_relpath=artifact_relpath,
            )
    finally:
        connection.close()


def test_snapshot_artifact_requires_nonempty_bytes_and_matching_canonical_checksum(
    tmp_path: Path,
) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot_and_children(
                connection,
                fixture,
                snapshot,
                event,
                forecasts,
                artifact_byte_count=0,
            )
    finally:
        connection.close()


def test_snapshot_primary_identity_is_explicitly_nonnull(tmp_path: Path) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot_and_children(
                connection,
                fixture,
                snapshot,
                event,
                forecasts,
                null_snapshot_id=True,
            )
    finally:
        connection.close()

    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        payload = snapshot.as_dict()
        payload["checksum"] = "0" * 64
        with pytest.raises(sqlite3.IntegrityError):
            _insert_snapshot_and_children(
                connection,
                fixture,
                snapshot,
                event,
                forecasts,
                canonical_json=_json(payload),
            )
    finally:
        connection.close()


def _doubleheader_fixture() -> Fixture:
    base = _fixture()
    first_slate_game = replace(
        base.slate.games[0],
        doubleheader_status=DailySlateDoubleheaderStatus.DOUBLEHEADER,
    )
    assert first_slate_game.scheduled_start_time is not None
    second_source_game_id = "900002"
    second_slate_game = replace(
        first_slate_game,
        edge_event_id=f"edge:mlb:{second_source_game_id}",
        daily_mlb_game_id=f"game:mlb:{second_source_game_id}",
        source_game_id=second_source_game_id,
        scheduled_start_time=first_slate_game.scheduled_start_time
        + timedelta(hours=4),
        game_number=2,
        provenance=replace(
            first_slate_game.provenance,
            source_record_id=second_source_game_id,
        ),
    )
    slate = replace(base.slate, games=(first_slate_game, second_slate_game))

    first_state_game = replace(
        base.state.games[0],
        edge_event_id=first_slate_game.edge_event_id,
        daily_mlb_game_id=first_slate_game.daily_mlb_game_id,
        source_game_id=first_slate_game.source_game_id,
    )
    second_state_game = replace(
        first_state_game,
        edge_event_id=second_slate_game.edge_event_id,
        daily_mlb_game_id=second_slate_game.daily_mlb_game_id,
        source_game_id=second_slate_game.source_game_id,
        provenance=replace(
            first_state_game.provenance,
            source_record_id=second_source_game_id,
        ),
    )
    state = replace(
        base.state,
        upstream_daily_slate_checksum=slate.checksum,
        games=(first_state_game, second_state_game),
        provenance=replace(
            base.state.provenance,
            upstream_checksum=slate.checksum,
        ),
    )
    assembly = assemble_baseball_intelligence(
        slate=slate,
        game_state=state,
        feature_snapshots=base.features,
        observed_at=base.assembly.observed_at,
    ).assembly
    return Fixture(
        slate,
        state,
        assembly,
        base.features,
        f"bia:{assembly.checksum}",
    )


def _insert_unavailable_snapshot_children(
    connection: sqlite3.Connection,
    fixture: Fixture,
    snapshot: OddsWeatherV1,
) -> str:
    snapshot_id = f"odds-weather:{snapshot.checksum}"
    artifact = snapshot.canonical_json_bytes()
    connection.execute(
        """
        INSERT INTO odds_weather_snapshots(
          snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,
          observed_at,sport,league,contract_version,phase_input_checksum,
          upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
          upstream_game_state_snapshot_id,upstream_game_state_checksum,
          upstream_baseball_intelligence_snapshot_id,
          upstream_baseball_intelligence_checksum,snapshot_checksum,
          source_raw_capture_checksums_json,warnings_json,warning_count,
          canonical_json,game_count,selected_raw_capture_count,
          weather_selection_count,artifact_relpath,artifact_checksum,
          artifact_byte_count,sealed_at,created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            snapshot_id,
            RUN_ID,
            "odds_weather",
            1,
            snapshot.requested_date,
            snapshot.as_of_time.isoformat(),
            snapshot.observed_at.isoformat(),
            snapshot.sport,
            snapshot.league,
            snapshot.contract_version,
            _sha("phase-input-v11"),
            f"slate:{fixture.slate.checksum}",
            fixture.slate.checksum,
            f"state:{fixture.state.checksum}",
            fixture.state.checksum,
            fixture.snapshot_id,
            fixture.assembly.checksum,
            snapshot.checksum,
            "[]",
            _json([warning.as_dict() for warning in snapshot.warnings]),
            len(snapshot.warnings),
            artifact.decode(),
            len(snapshot.games),
            0,
            0,
            f"odds_weather/snapshots/{snapshot.checksum}/odds_weather_v1.json",
            hashlib.sha256(artifact).hexdigest(),
            len(artifact),
            None,
            snapshot.observed_at.isoformat(),
        ),
    )
    for ordinal, warning in enumerate(snapshot.warnings, 1):
        payload = warning.as_dict()
        connection.execute(
            """
            INSERT INTO odds_weather_warnings(
              snapshot_id,ordinal,code,domain,message,source_game_id,provider,
              provider_event_id,canonical_json,row_checksum
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                snapshot_id,
                ordinal,
                warning.code,
                warning.domain.value,
                warning.message,
                warning.source_game_id,
                warning.provider,
                warning.provider_event_id,
                _json(payload),
                canonical_sha256(payload),
            ),
        )
    for ordinal, game in enumerate(snapshot.games, 1):
        slate_game = fixture.slate.games[ordinal - 1]
        state_game = fixture.state.games[ordinal - 1]
        bia_game = fixture.assembly.games[ordinal - 1]
        game_payload = game.as_dict()
        odds_payload = cast(dict[str, Any], game_payload["odds"])
        weather_payload = cast(dict[str, Any], game_payload["weather"])
        connection.execute(
            """
            INSERT INTO odds_weather_games(
              snapshot_id,run_id,phase_attempt,ordinal,edge_event_id,
              daily_mlb_game_id,source_game_id,official_date,away_team_id,
              home_team_id,source_away_team_id,source_home_team_id,venue_id,
              game_status,scheduled_start_time,upstream_daily_slate_game_checksum,
              upstream_game_state_game_checksum,
              upstream_baseball_intelligence_game_checksum,odds_availability,
              selected_provider_event_id,odds_retrieved_at,odds_raw_capture_checksum,
              event_match_offset_minutes,odds_summary_json,odds_summary_checksum,
              normalized_market_count,raw_snapshot_count,freshness_counts_json,
              odds_calculation_version,odds_consensus_contract_version,
              weather_status,weather_relevance,weather_primary_source,
              venue_context_json,venue_context_checksum,weather_comparison_json,
              baseball_wind_impact_json,weather_revision_count,canonical_json,
              row_checksum
            ) VALUES (?,?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,NULL,0,0,?,?,?, ?,?,NULL,NULL,NULL,?,?,0,?,?)
            """,
            (
                snapshot_id,
                RUN_ID,
                ordinal,
                game.edge_event_id,
                game.daily_mlb_game_id,
                game.source_game_id,
                slate_game.official_date,
                game.away_team_id,
                game.home_team_id,
                state_game.away.source_team_id,
                state_game.home.source_team_id,
                bia_game.venue_id,
                bia_game.game_status,
                game.scheduled_start_time.isoformat()
                if game.scheduled_start_time is not None
                else None,
                game.upstream_daily_slate_game_checksum,
                state_game.checksum,
                game.upstream_baseball_intelligence_game_checksum,
                game.odds.availability.value,
                _json(odds_payload["freshness_counts"]),
                game.odds.calculation_version,
                game.odds.odds_consensus_contract_version,
                game.weather.status.value,
                game.weather.relevance.value,
                _json(weather_payload["comparison"]),
                _json(weather_payload["baseball_wind_impact"]),
                _json(game_payload),
                game.checksum,
            ),
        )
    connection.execute(
        "UPDATE odds_weather_snapshots SET sealed_at=? WHERE snapshot_id=?",
        (snapshot.observed_at.isoformat(), snapshot_id),
    )
    return snapshot_id


def test_v11_identity_and_historical_chain_are_pinned() -> None:
    assert CURRENT_SCHEMA_VERSION == 11
    assert MIGRATION_V11_NAME == "odds_weather_v1_temporal_persistence"
    assert MIGRATION_V11_CHECKSUM == V11_CHECKSUM
    assert FORMAL_SCHEMA_V11_FINGERPRINT == V11_FINGERPRINT
    assert MIGRATION_V10_CHECKSUM == V10_CHECKSUM
    assert FORMAL_SCHEMA_V10_FINGERPRINT == V10_FINGERPRINT
    assert MIGRATION_HISTORY[-1] == (11, MIGRATION_V11_NAME, MIGRATION_V11_CHECKSUM)
    assert tuple(version for version, _, _ in MIGRATION_HISTORY) == tuple(range(1, 12))


def test_fresh_v11_has_exact_objects_and_no_pre_v11_backup(tmp_path: Path) -> None:
    path = tmp_path / "fresh-v11.sqlite3"
    result = ensure_schema(path)
    assert result.version == 11
    assert result.schema_fingerprint == FORMAL_SCHEMA_V11_FINGERPRINT
    assert not list(path.parent.rglob("*.pre-v11-*.sqlite3"))
    connection = sqlite3.connect(path)
    try:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        triggers = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert TABLES <= tables
        assert INDEXES <= indexes
        assert len([name for name in triggers if name.startswith("odds_weather_")]) == 38
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 11
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_exact_v10_upgrade_creates_verified_backup_and_preserves_rows(tmp_path: Path) -> None:
    path = tmp_path / "populated-v10.sqlite3"
    _install_formal_v10(path, populated=True)
    before = sqlite3.connect(path)
    try:
        preserved = {
            table: before.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "pipeline_runs",
                "daily_slate_snapshots",
                "game_state_snapshots",
                "baseball_intelligence_snapshots",
                "stats_feature_snapshots",
            )
        }
    finally:
        before.close()

    result = ensure_schema(path)
    assert result.version == 11
    assert result.backup_path is not None and ".pre-v11-" in result.backup_path.name
    assert result.diagnostic_path is not None
    assert result.diagnostic_path.name.startswith("migration-v11-")
    backup = sqlite3.connect(result.backup_path)
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V10_FINGERPRINT
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert backup.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        backup.close()
    verification = sqlite3.connect(path)
    try:
        for table, rows in preserved.items():
            assert verification.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V11_FINGERPRINT
        diagnostic = json.loads(result.diagnostic_path.read_text(encoding="utf-8"))
        assert diagnostic["source_version"] == 10
        assert diagnostic["target_version"] == 11
        assert diagnostic["outcome"] == "migration_verified"
    finally:
        verification.close()


def test_v11_failure_rolls_back_to_readable_v10(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rollback-v10.sqlite3"
    _install_formal_v10(path)
    original = migrations._execute_statements

    def fail_v11(connection: sqlite3.Connection, statements: object) -> None:
        if statements is migrations.FORMAL_SCHEMA_V11_STATEMENTS:
            raise sqlite3.OperationalError("injected schema v11 failure")
        original(connection, statements)  # type: ignore[arg-type]

    monkeypatch.setattr(migrations, "_execute_statements", fail_v11)
    with pytest.raises(sqlite3.OperationalError, match="injected schema v11"):
        ensure_schema(path)
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 10
        assert verification.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 10
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V10_FINGERPRINT
    finally:
        verification.close()
    backup_path = next(path.parent.rglob("*.pre-v11-*.sqlite3"))
    backup = sqlite3.connect(backup_path)
    try:
        assert schema_fingerprint(backup) == FORMAL_SCHEMA_V10_FINGERPRINT
        assert backup.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        backup.close()


@pytest.mark.parametrize("failure", ["backup", "diagnostic", "precommit"])
def test_v11_preflight_and_precommit_failures_never_record_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = tmp_path / f"{failure}-v10.sqlite3"
    _install_formal_v10(path)
    expected: type[Exception]
    if failure == "backup":
        monkeypatch.setattr(
            migrations,
            "_create_verified_v11_backup",
            lambda *_: (_ for _ in ()).throw(
                BackupVerificationError("injected v11 backup failure")
            ),
        )
        expected = BackupVerificationError
    elif failure == "diagnostic":
        monkeypatch.setattr(
            migrations,
            "_write_atomic_diagnostic",
            lambda *_: (_ for _ in ()).throw(
                DiagnosticWriteError("injected v11 diagnostic failure")
            ),
        )
        expected = DiagnosticWriteError
    else:
        original = migrations._assert_target_schema

        def fail_precommit(connection: sqlite3.Connection, version: int) -> None:
            if version == 11 and connection.in_transaction:
                raise migrations.SchemaVerificationError(
                    "injected v11 precommit verification failure"
                )
            original(connection, version)

        monkeypatch.setattr(migrations, "_assert_target_schema", fail_precommit)
        expected = migrations.SchemaVerificationError

    with pytest.raises(expected):
        ensure_schema(path)
    verification = sqlite3.connect(path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 10
        assert verification.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 10
        assert schema_fingerprint(verification) == FORMAL_SCHEMA_V10_FINGERPRINT
    finally:
        verification.close()


def test_one_game_contract_evidence_seals_and_is_immutable(tmp_path: Path) -> None:
    connection, fixture, snapshot, event, forecasts = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        _insert_raw_and_provider_evidence(connection, snapshot, event, forecasts)
        first_book = event.mutable_event()["bookmakers"][0]
        first_market = first_book["markets"][0]
        first_outcome = first_market["outcomes"][0]
        future_revision_at = snapshot.observed_at + timedelta(minutes=1)
        connection.execute(
            """
            INSERT INTO odds_weather_odds_revisions(
              run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
              bookmaker_key,market_key,outcome_name,price_american,point,
              provider_last_update,bookmaker_last_update,market_last_update,
              retrieved_at,canonical_json,row_checksum
            ) VALUES (?,1,?,?,13,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                event.provider_event_id,
                event.retrieved_at.isoformat(),
                first_book["key"],
                first_market["key"],
                first_outcome["name"],
                first_outcome["price"],
                first_outcome.get("point"),
                event.mutable_event().get("last_update"),
                first_book.get("last_update"),
                first_market.get("last_update"),
                future_revision_at.isoformat(),
                _json(first_outcome),
                canonical_sha256({"future": True, **first_outcome}),
            ),
        )
        future_weather_checksum = _sha("future-weather-raw")
        connection.execute(
            """
            INSERT INTO odds_weather_raw_captures(
              run_id,phase_attempt,ordinal,provider,endpoint_category,source_game_id,
              provider_event_id,retrieved_at,provider_timestamp,raw_relpath,
              raw_capture_checksum,raw_byte_count,canonical_metadata_json,row_checksum
            ) VALUES (?,1,4,'nws','hourly_forecast',?,NULL,?,NULL,?,?,100,'{}',?)
            """,
            (
                RUN_ID,
                forecasts[0].source_game_id,
                future_revision_at.isoformat(),
                f"raw/{future_weather_checksum}.json",
                future_weather_checksum,
                _sha("future-weather-row"),
            ),
        )
        future_forecast = dict(forecasts[0].forecast)
        connection.execute(
            """
            INSERT INTO odds_weather_weather_revisions(
              run_id,phase_attempt,source_game_id,provider,retrieved_at,revision_ordinal,
              forecast_time,forecast_offset_minutes,contract_version,forecast_checksum,
              canonical_json,row_checksum
            ) VALUES (?,1,?,'nws',?,2,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                forecasts[0].source_game_id,
                future_revision_at.isoformat(),
                future_forecast["forecast_time"],
                future_forecast["forecast_offset_minutes"],
                forecasts[0].contract_version,
                canonical_sha256({"future": True, **future_forecast}),
                _json(future_forecast),
                canonical_sha256({"future-row": True, **future_forecast}),
            ),
        )
        connection.execute(
            """
            INSERT INTO odds_weather_weather_raw_captures(
              run_id,phase_attempt,source_game_id,provider,weather_retrieved_at,
              ordinal,raw_capture_checksum
            ) VALUES (?,1,?,'nws',?,1,?)
            """,
            (
                RUN_ID,
                forecasts[0].source_game_id,
                future_revision_at.isoformat(),
                future_weather_checksum,
            ),
        )
        snapshot_id = _insert_snapshot_and_children(
            connection, fixture, snapshot, event, forecasts
        )
        connection.commit()
        row = connection.execute(
            "SELECT canonical_json,game_count,selected_raw_capture_count,weather_selection_count,sealed_at FROM odds_weather_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        assert row is not None
        assert json.loads(row[0]) == snapshot.as_dict()
        assert row[1:4] == (1, 3, 2)
        assert row[4] is not None
        assert connection.execute("SELECT count(*) FROM odds_weather_bookmakers").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM odds_weather_markets").fetchone()[0] == 6
        assert connection.execute("SELECT count(*) FROM odds_weather_outcomes").fetchone()[0] == 12
        assert connection.execute("SELECT count(*) FROM odds_weather_odds_revisions").fetchone()[0] == 13
        assert connection.execute("SELECT count(*) FROM odds_weather_weather_revisions").fetchone()[0] == 3
        assert connection.execute(
            "SELECT count(*) FROM odds_weather_odds_revisions WHERE retrieved_at>?",
            (snapshot.observed_at.isoformat(),),
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM odds_weather_weather_revisions WHERE retrieved_at>?",
            (snapshot.observed_at.isoformat(),),
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for sql, params in (
            ("DELETE FROM odds_weather_snapshots WHERE snapshot_id=?", (snapshot_id,)),
            ("DELETE FROM odds_weather_games WHERE snapshot_id=?", (snapshot_id,)),
            ("UPDATE odds_weather_snapshots SET canonical_json='{}' WHERE snapshot_id=?", (snapshot_id,)),
            ("UPDATE odds_weather_snapshots SET artifact_relpath='odds_weather/snapshots/wrong/odds_weather_v1.json' WHERE snapshot_id=?", (snapshot_id,)),
            ("UPDATE odds_weather_snapshots SET sealed_at=? WHERE snapshot_id=?", (snapshot.observed_at.isoformat(), snapshot_id)),
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql, params)
    finally:
        connection.close()


def test_doubleheader_games_retain_distinct_immutable_identity_and_warning_order(
    tmp_path: Path,
) -> None:
    fixture = _doubleheader_fixture()
    path = tmp_path / "doubleheader-v11.sqlite3"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        slate_id, state_id = _persist_upstream(connection, fixture)
        _persist_feature_inventory(connection, fixture)
        _insert_bia(connection, fixture, slate_id, state_id)
        _seal(connection, fixture)
        _advance_to_odds_weather(connection, fixture)
        snapshot = assemble_odds_weather(
            slate=fixture.slate,
            baseball_intelligence=fixture.assembly,
            observed_at=fixture.assembly.observed_at + timedelta(minutes=10),
        ).snapshot
        assert len(snapshot.games) == 2
        assert snapshot.source_raw_capture_checksums == ()
        assert [game.source_game_id for game in snapshot.games] == ["900001", "900002"]
        assert len(snapshot.warnings) == 4

        _insert_attempt(connection, fixture, snapshot)
        snapshot_id = _insert_unavailable_snapshot_children(
            connection, fixture, snapshot
        )
        connection.commit()

        games = connection.execute(
            """
            SELECT ordinal,edge_event_id,daily_mlb_game_id,source_game_id,
                   selected_provider_event_id
            FROM odds_weather_games WHERE snapshot_id=? ORDER BY ordinal
            """,
            (snapshot_id,),
        ).fetchall()
        assert games == [
            (1, "edge:mlb:900001", "game:mlb:900001", "900001", None),
            (2, "edge:mlb:900002", "game:mlb:900002", "900002", None),
        ]
        warning_rows = connection.execute(
            """
            SELECT ordinal,canonical_json FROM odds_weather_warnings
            WHERE snapshot_id=? ORDER BY ordinal
            """,
            (snapshot_id,),
        ).fetchall()
        assert [json.loads(row[1]) for row in warning_rows] == [
            warning.as_dict() for warning in snapshot.warnings
        ]
        assert connection.execute(
            "SELECT game_count,warning_count,sealed_at FROM odds_weather_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone() == (
            2,
            4,
            snapshot.observed_at.isoformat(),
        )
        assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_zero_game_snapshot_seals_and_failed_attempt_cannot_masquerade_as_success(
    tmp_path: Path,
) -> None:
    slate = DailySlateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-v1",
        games=(),
        provenance=DailySlateProvenanceV1(
            "mlb",
            None,
            SLATE_OBSERVED,
            source_version="statsapi-v1",
            raw_status="schedule",
            upstream_checksum=RAW_CHECKSUM,
        ),
    )
    state = GameStateV1(
        requested_date=REQUESTED_DATE,
        as_of_time=AS_OF,
        observed_at=SLATE_OBSERVED,
        source_authority="mlb",
        source_version="statsapi-game-feed-v1.1",
        upstream_daily_slate_checksum=slate.checksum,
        games=(),
        provenance=GameStateProvenanceV1(
            "mlb",
            None,
            SLATE_OBSERVED,
            source_version="statsapi-game-feed-v1.1",
            raw_status="snapshot",
            upstream_checksum=slate.checksum,
        ),
    )
    bia = assemble_baseball_intelligence(
        slate=slate, game_state=state, observed_at=SLATE_OBSERVED
    ).assembly
    fixture = Fixture(slate, state, bia, (), f"bia:{bia.checksum}")
    path = tmp_path / "zero-v11.sqlite3"
    Database(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        slate_id, state_id = _persist_upstream(connection, fixture)
        _insert_bia(connection, fixture, slate_id, state_id)
        _seal(connection, fixture)
        _advance_to_odds_weather(connection, fixture)
        snapshot = assemble_odds_weather(
            slate=slate,
            baseball_intelligence=bia,
            observed_at=SLATE_OBSERVED,
        ).snapshot
        assert snapshot.games == ()
        assert snapshot.source_raw_capture_checksums == ()
        assert snapshot.warnings == ()
        _insert_attempt(connection, fixture, snapshot)
        snapshot_id = f"odds-weather:{snapshot.checksum}"
        artifact = snapshot.canonical_json_bytes()
        connection.execute(
            """
            INSERT INTO odds_weather_snapshots(
              snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,
              observed_at,sport,league,contract_version,phase_input_checksum,
              upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
              upstream_game_state_snapshot_id,upstream_game_state_checksum,
              upstream_baseball_intelligence_snapshot_id,
              upstream_baseball_intelligence_checksum,snapshot_checksum,
              source_raw_capture_checksums_json,warnings_json,warning_count,
              canonical_json,game_count,selected_raw_capture_count,
              weather_selection_count,artifact_relpath,artifact_checksum,
              artifact_byte_count,sealed_at,created_at
            ) VALUES (?,?, 'odds_weather',1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,0,0,0,?,?,?,?,?)
            """,
            (
                snapshot_id,
                RUN_ID,
                snapshot.requested_date,
                snapshot.as_of_time.isoformat(),
                snapshot.observed_at.isoformat(),
                snapshot.sport,
                snapshot.league,
                snapshot.contract_version,
                _sha("phase-input-v11"),
                slate_id,
                slate.checksum,
                state_id,
                state.checksum,
                fixture.snapshot_id,
                bia.checksum,
                snapshot.checksum,
                "[]",
                "[]",
                artifact.decode(),
                f"odds_weather/snapshots/{snapshot.checksum}/odds_weather_v1.json",
                hashlib.sha256(artifact).hexdigest(),
                len(artifact),
                None,
                snapshot.observed_at.isoformat(),
            ),
        )
        connection.execute(
            "UPDATE odds_weather_snapshots SET sealed_at=? WHERE snapshot_id=?",
            (snapshot.observed_at.isoformat(), snapshot_id),
        )
        assert connection.execute(
            "SELECT game_count,sealed_at FROM odds_weather_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone() == (0, snapshot.observed_at.isoformat())
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()

    failed_path = tmp_path / "failed-zero-v11.sqlite3"
    Database(failed_path)
    failed = sqlite3.connect(failed_path)
    failed.execute("PRAGMA foreign_keys=ON")
    try:
        slate_id, state_id = _persist_upstream(failed, fixture)
        _insert_bia(failed, fixture, slate_id, state_id)
        _seal(failed, fixture)
        _advance_to_odds_weather(failed, fixture)
        failed.execute(
            """
            INSERT INTO odds_weather_attempt_evidence(
              run_id,phase_key,phase_attempt,requested_date,as_of_time,observed_at,
              phase_input_checksum,upstream_daily_slate_snapshot_id,
              upstream_daily_slate_checksum,upstream_game_state_snapshot_id,
              upstream_game_state_checksum,upstream_baseball_intelligence_snapshot_id,
              upstream_baseball_intelligence_checksum,outcome,snapshot_checksum,
              evidence_manifest_relpath,evidence_manifest_checksum,
              evidence_manifest_byte_count,warnings_json,warning_count,created_at,completed_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                RUN_ID,
                "odds_weather",
                1,
                REQUESTED_DATE,
                AS_OF.isoformat(),
                SLATE_OBSERVED.isoformat(),
                _sha("failed-phase-input"),
                slate_id,
                slate.checksum,
                state_id,
                state.checksum,
                fixture.snapshot_id,
                bia.checksum,
                "acquisition_failed",
                None,
                "odds_weather/attempts/failed.json",
                _sha("failed-manifest"),
                0,
                "[]",
                0,
                SLATE_OBSERVED.isoformat(),
                SLATE_OBSERVED.isoformat(),
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            failed.execute(
                """
                INSERT INTO odds_weather_snapshots(
                  snapshot_id,run_id,phase_key,phase_attempt,requested_date,as_of_time,
                  observed_at,sport,league,contract_version,phase_input_checksum,
                  upstream_daily_slate_snapshot_id,upstream_daily_slate_checksum,
                  upstream_game_state_snapshot_id,upstream_game_state_checksum,
                  upstream_baseball_intelligence_snapshot_id,
                  upstream_baseball_intelligence_checksum,snapshot_checksum,
                  source_raw_capture_checksums_json,warnings_json,warning_count,
                  canonical_json,game_count,selected_raw_capture_count,
                  weather_selection_count,artifact_relpath,artifact_checksum,
                  artifact_byte_count,sealed_at,created_at
                ) VALUES (?,?, 'odds_weather',1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,0,0,0,?,?,?,?,?)
                """,
                (
                    f"odds-weather:{snapshot.checksum}",
                    RUN_ID,
                    REQUESTED_DATE,
                    AS_OF.isoformat(),
                    SLATE_OBSERVED.isoformat(),
                    "MLB",
                    "MLB",
                    "DSE_ODDS_WEATHER_V1",
                    _sha("failed-phase-input"),
                    slate_id,
                    slate.checksum,
                    state_id,
                    state.checksum,
                    fixture.snapshot_id,
                    bia.checksum,
                    snapshot.checksum,
                    "[]",
                    "[]",
                    artifact.decode(),
                    f"odds_weather/snapshots/{snapshot.checksum}/odds_weather_v1.json",
                    hashlib.sha256(artifact).hexdigest(),
                    len(artifact),
                    None,
                    SLATE_OBSERVED.isoformat(),
                ),
            )
    finally:
        failed.close()


def test_attempt_parentage_identity_and_replay_conflicts_fail_closed(tmp_path: Path) -> None:
    connection, fixture, snapshot, event, _ = _build_phase4_fixture(tmp_path)
    try:
        _insert_attempt(connection, fixture, snapshot)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_attempt(connection, fixture, snapshot)
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE odds_weather_attempt_evidence SET warning_count=1 WHERE run_id=? AND phase_attempt=1",
                (RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "DELETE FROM odds_weather_attempt_evidence WHERE run_id=? AND phase_attempt=1",
                (RUN_ID,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO odds_weather_bookmakers(
                  run_id,phase_attempt,provider_event_id,event_retrieved_at,ordinal,
                  bookmaker_key,title,bookmaker_last_update,canonical_json,row_checksum
                ) VALUES (?,1,?,?,1,'book','Book',NULL,'{}',?)
                """,
                (RUN_ID, event.provider_event_id, event.retrieved_at.isoformat(), _sha("orphan")),
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO odds_weather_raw_captures(
                  run_id,phase_attempt,ordinal,provider,endpoint_category,retrieved_at,
                  raw_relpath,raw_capture_checksum,raw_byte_count,canonical_metadata_json,
                  row_checksum
                ) VALUES (?,2,1,'the_odds_api','mlb_odds',?,'raw/orphan.json',?,1,'{}',?)
                """,
                (RUN_ID, snapshot.observed_at.isoformat(), _sha("orphan-raw"), _sha("orphan-row")),
            )
    finally:
        connection.close()


def test_schema_statement_inventory_is_deterministic() -> None:
    assert len(ODDS_WEATHER_SCHEMA_V11_TABLE_STATEMENTS) == 14
    assert len(ODDS_WEATHER_SCHEMA_V11_INDEX_STATEMENTS) == 20
    assert len(ODDS_WEATHER_SCHEMA_V11_VALIDATION_TRIGGER_STATEMENTS) == 10
    assert len(ODDS_WEATHER_SCHEMA_V11_IMMUTABILITY_TRIGGER_STATEMENTS) == 28
    assert len(FORMAL_SCHEMA_V11_STATEMENTS) == 138
