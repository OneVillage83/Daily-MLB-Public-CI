from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.database import Database
from app.stats.repository import (
    InvalidStatsTransition,
    StatsInvariantError,
    StatsRepository,
)


RUN_ID = "run_20260714_22222222222222222222222222222222"
RUN_ID_TWO = "run_20260714_33333333333333333333333333333333"
TS = "2026-07-15T01:00:00+00:00"
TS_LATER = "2026-07-15T01:05:00+00:00"
TS_AFTER = "2026-07-15T03:00:00+00:00"
HASH_A = "a" * 64
HASH_B = "b" * 64


@pytest.fixture
def repository(tmp_path: Path) -> StatsRepository:
    database = Database(tmp_path / "stats.db")
    database.create_run(RUN_ID, "2026-07-14")
    return StatsRepository(database)


def _create_stats_run(repository: StatsRepository) -> None:
    repository.create_ingestion_run(
        stats_run_id="stats-run-1",
        run_id=RUN_ID,
        provider="statcast",
        scope_key="regular-season:2026",
        requested_through_date="2026-07-14",
        configuration_checksum=HASH_A,
        created_at=TS,
    )
    repository.transition_ingestion_run("stats-run-1", "running", transitioned_at=TS)


def _create_second_stats_run(repository: StatsRepository) -> None:
    repository.database.create_run(RUN_ID_TWO, "2026-07-14")
    repository.create_ingestion_run(
        stats_run_id="stats-run-2",
        run_id=RUN_ID_TWO,
        provider="statcast",
        scope_key="regular-season:2026",
        requested_through_date="2026-07-14",
        configuration_checksum=HASH_A,
        created_at=TS_LATER,
    )
    repository.transition_ingestion_run(
        "stats-run-2", "running", transitioned_at=TS_LATER
    )


def _identity_record(kind: str, identifier: str, name: str) -> dict[str, object]:
    return {
        f"{kind}_identity_id": f"{kind}:{identifier}",
        "provider": "statcast",
        f"provider_{kind}_id": identifier,
        "current_name" if kind == "team" else "full_name": name,
        "active": True,
        "first_seen_at": TS,
        "last_seen_at": TS,
        "identity_checksum": HASH_A,
    }


def _seed_identities(repository: StatsRepository) -> None:
    repository.upsert_team_identity(
        {**_identity_record("team", "119", "Los Angeles Dodgers"), "canonical_team_key": "lad"}
    )
    repository.upsert_team_identity(
        {**_identity_record("team", "137", "San Francisco Giants"), "canonical_team_key": "sf"}
    )
    repository.upsert_player_identity(_identity_record("player", "1", "Batter One"))
    repository.upsert_player_identity(_identity_record("player", "2", "Pitcher Two"))
    repository.upsert_game_identity(
        {
            "game_identity_id": "game:777",
            "provider": "statcast",
            "provider_game_id": "777",
            "season": 2026,
            "game_type": "R",
            "official_date": "2026-07-14",
            "scheduled_start": "2026-07-15T02:00:00Z",
            "home_team_identity_id": "team:119",
            "away_team_identity_id": "team:137",
            "venue_provider_id": "22",
            "venue_name": "Dodger Stadium",
            "first_seen_at": TS,
            "last_seen_at": TS,
            "identity_checksum": HASH_A,
        }
    )


def _raw_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "raw_payload_id": "raw:1",
        "stats_run_id": "stats-run-1",
        "provider": "statcast",
        "endpoint_category": "schedule",
        "source_capture_id": "capture-1",
        "retrieved_at": TS,
        "content_type": "application/json",
        "checksum_sha256": HASH_A,
        "artifact_relpath": "raw/statcast/schedule/a.json",
        "metadata": {"game_id": 777},
    }
    record.update(overrides)
    return record


def _snapshot(**values: object) -> dict[str, object]:
    return {
        "stats_run_id": "stats-run-1",
        "raw_payload_id": "raw:1",
        "provider_updated_at": TS,
        "retrieved_at": TS,
        "source_checksum": HASH_A,
        **values,
    }


def test_run_checkpoint_and_exact_dual_watermarks(repository: StatsRepository) -> None:
    _create_stats_run(repository)
    checkpoint = repository.create_checkpoint(
        {
            "checkpoint_id": "checkpoint-1",
            "stats_run_id": "stats-run-1",
            "dataset_key": "game-feed",
            "scope_key": "regular-season:2026",
            "created_at": TS,
            "cursor_before": {"date": "2026-07-13"},
        }
    )
    assert checkpoint["status"] == "pending"
    repository.transition_checkpoint("checkpoint-1", "running", transitioned_at=TS)
    checkpoint = repository.transition_checkpoint(
        "checkpoint-1",
        "partial",
        transitioned_at=TS_LATER,
        cursor_after={"date": "2026-07-14"},
        records_seen=15,
        records_persisted=14,
        records_rejected=1,
        observed_through=TS_LATER,
    )
    assert checkpoint["status"] == "partial"

    reconciliation = repository.record_reconciliation(
        {
            "reconciliation_id": "recon-1",
            "stats_run_id": "stats-run-1",
            "checkpoint_id": "checkpoint-1",
            "dataset_key": "game-feed",
            "scope_key": "regular-season:2026",
            "status": "warnings",
            "expected_count": 15,
            "observed_count": 14,
            "conflict_count": 0,
            "started_at": TS,
            "completed_at": TS_LATER,
            "details": {"postponed": True},
            "source_checksum": HASH_A,
        }
    )
    assert reconciliation["status"] == "warnings"
    watermark = repository.advance_completeness_watermark(
        provider="statcast",
        dataset_key="game-feed",
        scope_key="regular-season:2026",
        requested_through_date="2026-07-14",
        source_observed_at=TS_LATER,
        latest_ingested_completed_game_date="2026-07-14",
        contiguous_regular_season_complete_through_date="2026-07-12",
        partial_date="2026-07-13",
        partial_date_reason="postponed game remains unresolved",
        source_stats_run_id="stats-run-1",
        reconciliation_id="recon-1",
        source_checksum=HASH_A,
        updated_at=TS_LATER,
    )
    assert watermark["latest_ingested_completed_game_date"] == "2026-07-14"
    assert watermark["contiguous_regular_season_complete_through_date"] == "2026-07-12"
    assert watermark["partial_date_reason"] == "postponed game remains unresolved"

    run = repository.transition_ingestion_run(
        "stats-run-1",
        "completed_with_warnings",
        transitioned_at=TS_LATER,
        source_observed_at=TS_LATER,
        latest_ingested_completed_game_date="2026-07-14",
        contiguous_regular_season_complete_through_date="2026-07-12",
        partial_date="2026-07-13",
        partial_date_reason="postponed game remains unresolved",
    )
    assert run["latest_ingested_completed_game_date"] == "2026-07-14"
    assert run["contiguous_regular_season_complete_through_date"] == "2026-07-12"
    assert run["source_version"] == "statcast:2026"
    assert run["adapter_version"] == "DSE_STATS_ACQUISITION_V1"
    with pytest.raises(sqlite3.IntegrityError, match="provenance is immutable"):
        with repository.database.connect(write=True) as connection:
            connection.execute(
                "UPDATE stats_ingestion_runs SET source_version='changed' "
                "WHERE stats_run_id='stats-run-1'"
            )


def test_watermarks_advance_independently_and_cannot_regress(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    repository.record_reconciliation(
        {
            "reconciliation_id": "recon-1",
            "stats_run_id": "stats-run-1",
            "dataset_key": "games",
            "scope_key": "2026",
            "status": "passed",
            "started_at": TS,
            "completed_at": TS_LATER,
            "details": {},
            "source_checksum": HASH_A,
        }
    )
    common: dict[str, Any] = {
        "provider": "statcast",
        "dataset_key": "games",
        "scope_key": "2026",
        "requested_through_date": "2026-07-14",
        "source_observed_at": TS_LATER,
        "source_stats_run_id": "stats-run-1",
        "reconciliation_id": "recon-1",
        "source_checksum": HASH_A,
        "updated_at": TS_LATER,
    }
    first = repository.advance_completeness_watermark(
        **common,
        latest_ingested_completed_game_date="2026-07-13",
        contiguous_regular_season_complete_through_date="2026-07-10",
    )
    second = repository.advance_completeness_watermark(
        **common,
        latest_ingested_completed_game_date="2026-07-14",
        contiguous_regular_season_complete_through_date=None,
    )
    assert first["revision"] == 1
    assert second["revision"] == 2
    assert second["contiguous_regular_season_complete_through_date"] == "2026-07-10"
    repeated = repository.advance_completeness_watermark(
        **common,
        latest_ingested_completed_game_date="2026-07-14",
        contiguous_regular_season_complete_through_date=None,
    )
    assert repeated["revision"] == 2
    lower = repository.advance_completeness_watermark(
        **common,
        latest_ingested_completed_game_date="2026-07-12",
        contiguous_regular_season_complete_through_date="2026-07-09",
    )
    assert lower["revision"] == 2
    assert lower["latest_ingested_completed_game_date"] == "2026-07-14"
    assert lower["contiguous_regular_season_complete_through_date"] == "2026-07-10"
    older_request = repository.advance_completeness_watermark(
        **{
            **common,
            "requested_through_date": "2026-07-13",
            "source_observed_at": TS_AFTER,
            "updated_at": TS_AFTER,
        },
        latest_ingested_completed_game_date="2026-07-13",
        contiguous_regular_season_complete_through_date="2026-07-11",
    )
    assert older_request["revision"] == 2
    assert older_request["requested_through_date"] == "2026-07-14"


def test_raw_metadata_is_idempotent_redacted_and_path_safe(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    first = repository.record_raw_payload_metadata(_raw_record(metadata={"game_id": 777, "apiKey": "do-not-store"}))
    second = repository.record_raw_payload_metadata(_raw_record(metadata={"game_id": 777, "apiKey": "do-not-store"}))
    assert first == second
    assert "do-not-store" not in first["metadata_json"]
    assert json.loads(first["metadata_json"])["game_id"] == 777

    with pytest.raises(ValueError, match="allowlisted"):
        repository.record_raw_payload_metadata(
            _raw_record(
                raw_payload_id="raw:bad-url",
                source_capture_id="capture-bad-url",
                endpoint_category="https://example.test/feed?apiKey=secret",
            )
        )
    with pytest.raises(ValueError, match="contained"):
        repository.record_raw_payload_metadata(
            _raw_record(
                raw_payload_id="raw:bad-path",
                source_capture_id="capture-bad-path",
                artifact_relpath="../outside.json",
            )
        )


def test_raw_metadata_and_observations_reject_cross_run_provenance(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _create_second_stats_run(repository)
    repository.create_checkpoint(
        {
            "checkpoint_id": "checkpoint-run-1",
            "stats_run_id": "stats-run-1",
            "dataset_key": "schedule",
            "scope_key": "regular-season:2026",
            "created_at": TS,
        }
    )

    with pytest.raises(
        sqlite3.IntegrityError,
        match="stats raw payload checkpoint belongs to another run",
    ):
        repository.record_raw_payload_metadata(
            _raw_record(
                raw_payload_id="raw:cross-checkpoint",
                stats_run_id="stats-run-2",
                checkpoint_id="checkpoint-run-1",
                source_capture_id="capture-cross-checkpoint",
                retrieved_at=TS_LATER,
                artifact_relpath="raw/statcast/schedule/cross-checkpoint.json",
            )
        )

    same_run_raw = repository.record_raw_payload_metadata(
        _raw_record(
            raw_payload_id="raw:run-1-checkpoint",
            checkpoint_id="checkpoint-run-1",
            source_capture_id="capture-run-1-checkpoint",
            artifact_relpath="raw/statcast/schedule/run-1-checkpoint.json",
        )
    )
    assert same_run_raw["checkpoint_id"] == "checkpoint-run-1"

    repository.record_raw_payload_metadata(
        _raw_record(
            raw_payload_id="raw:run-2",
            stats_run_id="stats-run-2",
            source_capture_id="capture-run-2",
            retrieved_at=TS_LATER,
            artifact_relpath="raw/statcast/schedule/run-2.json",
        )
    )
    _seed_identities(repository)
    cross_run_status = _snapshot(
        raw_payload_id="raw:run-2",
        game_identity_id="game:777",
        abstract_state="Final",
        detailed_state="Final",
        status_code="F",
        scheduled_start="2026-07-15T02:00:00Z",
        status={"codedGameState": "F"},
    )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="stats_game_status_observations raw payload belongs to another run",
    ):
        repository.record_game_status(cross_run_status)

    nullable_raw_status = repository.record_game_status(
        {**cross_run_status, "raw_payload_id": None}
    )
    assert nullable_raw_status["raw_payload_id"] is None
    assert nullable_raw_status["stats_run_id"] == "stats-run-1"


def test_full_stats_evidence_graph_and_revisions(repository: StatsRepository) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())

    status = repository.record_game_status(
        _snapshot(
            game_identity_id="game:777",
            abstract_state="Final",
            detailed_state="Final",
            status_code="F",
            scheduled_start="2026-07-15T02:00:00Z",
            status={"codedGameState": "F"},
        )
    )
    duplicate = repository.record_game_status(
        _snapshot(
            game_identity_id="game:777",
            abstract_state="Final",
            detailed_state="Final",
            status_code="F",
            scheduled_start="2026-07-15T02:00:00Z",
            status={"codedGameState": "F"},
        )
    )
    assert status["status_observation_id"] == duplicate["status_observation_id"]
    repository.record_game_team_snapshot(
        _snapshot(
            game_identity_id="game:777",
            team_identity_id="team:119",
            side="home",
            snapshot_kind="boxscore_final",
            stats={"runs": 4},
        )
    )
    repository.record_game_player_snapshot(
        _snapshot(
            game_identity_id="game:777",
            team_identity_id="team:119",
            player_identity_id="player:1",
            role="batting",
            stats={"hits": 2},
        )
    )
    repository.record_lineup_snapshot(
        _snapshot(
            lineup_snapshot_id="lineup:1",
            game_identity_id="game:777",
            team_identity_id="team:119",
            lineup_state="official",
        ),
        [
            {
                "player_identity_id": "player:1",
                "batting_order": 1,
                "position_code": "RF",
                "lineup_role": "starter",
                "entry": {"slot": 1},
            }
        ],
    )
    repository.upsert_play_identity(
        {
            "play_identity_id": "play:1",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "provider_play_id": "1",
            "at_bat_index": 0,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
    )
    play_revision = repository.record_play_revision(
        _snapshot(
            play_identity_id="play:1",
            revision_number=1,
            revision_kind="initial",
            inning=1,
            half_inning="top",
            event_type="single",
            batter_identity_id="player:1",
            pitcher_identity_id="player:2",
            started_at=TS,
            ended_at=TS_LATER,
            play={"result": "Single"},
        )
    )
    assert (
        repository.record_play_revision(
            _snapshot(
                play_identity_id="play:1",
                revision_number=1,
                revision_kind="initial",
                inning=1,
                half_inning="top",
                event_type="single",
                batter_identity_id="player:1",
                pitcher_identity_id="player:2",
                started_at=TS,
                ended_at=TS_LATER,
                play={"result": "Single"},
            )
        )["play_revision_id"]
        == play_revision["play_revision_id"]
    )
    with pytest.raises(StatsInvariantError, match="checksum collision"):
        repository.record_play_revision(
            _snapshot(
                play_identity_id="play:1",
                revision_number=2,
                revision_kind="correction",
                inning=1,
                half_inning="top",
                event_type="home_run",
                batter_identity_id="player:1",
                pitcher_identity_id="player:2",
                started_at=TS,
                ended_at=TS_LATER,
                play={"result": "Home Run"},
            )
        )
    with pytest.raises(StatsInvariantError, match="different fields"):
        repository.record_play_revision(
            {
                **_snapshot(
                    play_identity_id="play:1",
                    revision_number=1,
                    revision_kind="initial",
                    inning=1,
                    half_inning="top",
                    event_type="home_run",
                    batter_identity_id="player:1",
                    pitcher_identity_id="player:2",
                    started_at=TS,
                    ended_at=TS_LATER,
                    play={"result": "Home Run"},
                ),
                "source_checksum": HASH_B,
            }
        )


    repository.upsert_pitch_identity(
        {
            "pitch_identity_id": "pitch:1",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "play_identity_id": "play:1",
            "provider_pitch_id": "777:0:1",
            "game_pk": 777,
            "at_bat_number": 0,
            "pitch_number": 1,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
    )
    repository.record_statcast_revision(
        _snapshot(
            pitch_identity_id="pitch:1",
            revision_number=1,
            revision_kind="initial",
            metrics={"release_speed_mph": 97.2},
        )
    )
    repository.record_season_snapshot(
        _snapshot(
            season_snapshot_id="season:team:119:2026",
            provider="statcast",
            season=2026,
            entity_kind="team",
            team_identity_id="team:119",
            split_key="season",
            snapshot_as_of=TS,
            stats={"wins": 60},
        )
    )
    repository.record_feature_snapshot(
        {
            "feature_snapshot_id": "feature:game:777",
            "stats_run_id": "stats-run-1",
            "feature_version": "mlb-features-v1",
            "entity_kind": "game",
            "game_identity_id": "game:777",
            "feature_as_of": TS,
            "completeness_state": "degraded",
            "observed_through": TS,
            "input_checksum": HASH_A,
            "feature_checksum": HASH_B,
            "features": {"home_rest_days": None},
            "created_at": TS,
        }
    )
    conflict = repository.record_conflict(
        {
            "conflict_id": "conflict:1",
            "stats_run_id": "stats-run-1",
            "conflict_code": "identity_name_mismatch",
            "entity_kind": "player",
            "entity_key": "player:1",
            "existing_value": {"name": "Batter One"},
            "observed_value": {"name": "B. One"},
            "detected_at": TS,
            "source_checksum": HASH_A,
        }
    )
    assert conflict["conflict_code"] == "identity_name_mismatch"
    repository.record_conflict_resolution(
        {
            "conflict_resolution_id": "resolution:1",
            "conflict_id": "conflict:1",
            "resolution": "deferred",
            "resolver_id": "system",
            "resolved_at": TS_LATER,
            "details": {"reason": "manual review required"},
            "source_checksum": HASH_B,
        }
    )
    repository.link_raw_entity(
        {
            "raw_payload_id": "raw:1",
            "link_role": "contains",
            "game_identity_id": "game:777",
        }
    )

    integrity = repository.database.integrity_check()
    assert integrity["integrity_check"] == ["ok"]
    assert integrity["foreign_key_violations"] == []


def test_lineup_identity_preserves_duplicate_player_slots_and_null_order(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    snapshot = _snapshot(
        lineup_snapshot_id="lineup:duplicate-slots",
        game_identity_id="game:777",
        team_identity_id="team:119",
        lineup_state="official",
    )
    entries: list[dict[str, object]] = [
        {
            "player_identity_id": "player:1",
            "batting_order": 3,
            "position_code": "5",
            "lineup_role": "starter",
            "entry": {"provider_player_id": "speah101", "slot": 3},
        },
        {
            "player_identity_id": "player:1",
            "batting_order": 4,
            "position_code": "5",
            "lineup_role": "starter",
            "entry": {"provider_player_id": "speah101", "slot": 4},
        },
        {
            "player_identity_id": "player:2",
            "batting_order": None,
            "position_code": None,
            "lineup_role": "bench",
            "entry": {"provider_player_id": "reserve001"},
        },
    ]

    repository.record_lineup_snapshot(snapshot, entries)
    repository.record_lineup_snapshot(snapshot, entries)

    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT player_identity_id,batting_order,lineup_role "
            "FROM stats_lineup_entries "
            "WHERE lineup_snapshot_id='lineup:duplicate-slots' "
            "ORDER BY lineup_entry_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("player:1", 3, "starter"),
        ("player:1", 4, "starter"),
        ("player:2", None, "bench"),
    ]


def test_invalid_transitions_and_failed_reconciliation_are_rejected(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    with pytest.raises(InvalidStatsTransition):
        repository.transition_ingestion_run("stats-run-1", "queued", transitioned_at=TS)
    with pytest.raises(ValueError, match="source_observed_at"):
        repository.transition_ingestion_run("stats-run-1", "completed", transitioned_at=TS)
    with pytest.raises(ValueError, match="failure_stage"):
        repository.transition_ingestion_run("stats-run-1", "failed", transitioned_at=TS)

    repository.record_reconciliation(
        {
            "reconciliation_id": "failed-recon",
            "stats_run_id": "stats-run-1",
            "dataset_key": "games",
            "scope_key": "2026",
            "status": "failed",
            "started_at": TS,
            "completed_at": TS_LATER,
            "details": {},
            "source_checksum": HASH_A,
        }
    )
    with pytest.raises(StatsInvariantError, match="failed reconciliation"):
        repository.advance_completeness_watermark(
            provider="statcast",
            dataset_key="games",
            scope_key="2026",
            requested_through_date="2026-07-14",
            source_observed_at=TS_LATER,
            latest_ingested_completed_game_date="2026-07-14",
            contiguous_regular_season_complete_through_date="2026-07-14",
            source_stats_run_id="stats-run-1",
            reconciliation_id="failed-recon",
            source_checksum=HASH_A,
        )


def test_failed_run_terminalizes_only_active_checkpoints(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    for suffix in ("pending", "running", "completed", "partial"):
        repository.create_checkpoint(
            {
                "checkpoint_id": f"checkpoint-{suffix}",
                "stats_run_id": "stats-run-1",
                "dataset_key": suffix,
                "scope_key": "regular-season:2026",
                "created_at": TS,
            }
        )
    for suffix in ("running", "completed", "partial"):
        repository.transition_checkpoint(
            f"checkpoint-{suffix}", "running", transitioned_at=TS
        )
    repository.transition_checkpoint(
        "checkpoint-completed", "completed", transitioned_at=TS_LATER
    )
    repository.transition_checkpoint(
        "checkpoint-partial", "partial", transitioned_at=TS_LATER
    )

    failed = repository.fail_active_checkpoints(
        "stats-run-1",
        transitioned_at=TS_AFTER,
        reason_code="parent_run_failed",
        details={"failure_stage": "acquisition"},
    )

    assert [row["checkpoint_id"] for row in failed] == [
        "checkpoint-pending",
        "checkpoint-running",
    ]
    with repository.database.connect() as connection:
        rows = {
            str(row["checkpoint_id"]): row
            for row in connection.execute(
                "SELECT * FROM stats_checkpoints ORDER BY checkpoint_id"
            ).fetchall()
        }
    assert rows["checkpoint-pending"]["status"] == "failed"
    assert rows["checkpoint-running"]["status"] == "failed"
    assert rows["checkpoint-completed"]["status"] == "completed"
    assert rows["checkpoint-partial"]["status"] == "partial"
    assert rows["checkpoint-pending"]["started_at"] == TS_AFTER
    assert rows["checkpoint-running"]["started_at"] == TS
    assert rows["checkpoint-pending"]["completed_at"] == TS_AFTER
    assert json.loads(rows["checkpoint-running"]["error_json"]) == {
        "code": "parent_run_failed",
        "details": {"failure_stage": "acquisition"},
    }


def test_startup_reconciliation_repairs_active_checkpoints_under_terminal_run(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    for suffix in ("running", "completed", "partial"):
        repository.create_checkpoint(
            {
                "checkpoint_id": f"checkpoint-{suffix}",
                "stats_run_id": "stats-run-1",
                "dataset_key": suffix,
                "scope_key": "regular-season:2026",
                "created_at": TS,
            }
        )
        repository.transition_checkpoint(
            f"checkpoint-{suffix}", "running", transitioned_at=TS
        )
    repository.transition_checkpoint(
        "checkpoint-completed", "completed", transitioned_at=TS_LATER
    )
    repository.transition_checkpoint(
        "checkpoint-partial", "partial", transitioned_at=TS_LATER
    )
    repository.transition_ingestion_run(
        "stats-run-1",
        "failed",
        transitioned_at=TS_LATER,
        failure_stage="acquisition",
        error={"code": "legacy_failure"},
    )

    repaired = repository.reconcile_terminal_run_checkpoints(
        transitioned_at=TS_AFTER
    )

    assert [row["checkpoint_id"] for row in repaired] == ["checkpoint-running"]
    with repository.database.connect() as connection:
        rows = {
            str(row["checkpoint_id"]): row
            for row in connection.execute(
                "SELECT * FROM stats_checkpoints ORDER BY checkpoint_id"
            ).fetchall()
        }
    assert rows["checkpoint-running"]["status"] == "failed"
    assert json.loads(rows["checkpoint-running"]["error_json"]) == {
        "code": "terminal_parent_run_reconciliation",
        "details": {"parent_status": "failed"},
    }
    assert rows["checkpoint-completed"]["status"] == "completed"
    assert rows["checkpoint-partial"]["status"] == "partial"


def test_identity_upsert_is_idempotent_and_monotonic(repository: StatsRepository) -> None:
    record: Mapping[str, object] = {
        **_identity_record("team", "119", "Los Angeles Dodgers"),
        "canonical_team_key": "lad",
    }
    first = repository.upsert_team_identity(record)
    second = repository.upsert_team_identity(record)
    assert first == second
    with pytest.raises(StatsInvariantError, match="cannot regress"):
        repository.upsert_team_identity({**record, "last_seen_at": "2026-07-14T23:59:00+00:00"})


def test_team_identity_lookup_uses_exact_provider_identity(
    repository: StatsRepository,
) -> None:
    record: Mapping[str, object] = {
        **_identity_record("team", "119", "Los Angeles Dodgers"),
        "canonical_team_key": "lad",
    }
    repository.upsert_team_identity(record)

    assert repository.get_team_identity("STATCAST", "119") == repository.upsert_team_identity(
        record
    )
    assert repository.get_team_identity("statcast", "0119") is None


def test_statcast_pitch_identity_uses_the_exact_provider_tuple(
    repository: StatsRepository,
) -> None:
    _seed_identities(repository)
    record = {
        "pitch_identity_id": "pitch:777:4:2",
        "provider": "statcast",
        "game_identity_id": "game:777",
        "provider_pitch_id": "777:4:2",
        "game_pk": 777,
        "at_bat_number": 4,
        "pitch_number": 2,
        "first_seen_at": TS,
        "last_seen_at": TS,
    }
    created = repository.upsert_pitch_identity(record)
    assert (created["game_pk"], created["at_bat_number"], created["pitch_number"]) == (
        777,
        4,
        2,
    )
    with pytest.raises(StatsInvariantError, match="game_pk:at_bat_number"):
        repository.upsert_pitch_identity(
            {**record, "pitch_identity_id": "pitch:wrong", "provider_pitch_id": "other"}
        )
    with pytest.raises(StatsInvariantError, match="different ID"):
        repository.upsert_pitch_identity(
            {**record, "pitch_identity_id": "pitch:duplicate-tuple"}
        )


def test_pitch_identity_batch_preloads_updates_and_rolls_back_conflicts(
    repository: StatsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_identities(repository)
    monkeypatch.setattr("app.stats.repository._IDENTITY_QUERY_PARAMETER_LIMIT", 4)

    def pitch(at_bat: int, **overrides: object) -> dict[str, object]:
        provider_id = f"777:{at_bat}:1"
        record: dict[str, object] = {
            "pitch_identity_id": f"pitch:{provider_id}",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "provider_pitch_id": provider_id,
            "game_pk": 777,
            "at_bat_number": at_bat,
            "pitch_number": 1,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
        record.update(overrides)
        return record

    initial = repository.upsert_pitch_identity(pitch(10))
    results = repository.upsert_pitch_identities(
        (
            pitch(10, last_seen_at=TS_LATER),
            pitch(10, last_seen_at=TS_LATER),
            pitch(11),
            pitch(11, last_seen_at=TS_LATER),
        )
    )
    assert results[0]["pitch_identity_id"] == initial["pitch_identity_id"]
    assert results[0]["last_seen_at"] == TS_LATER
    assert results[1] == results[0]
    assert results[2]["last_seen_at"] == TS
    assert results[3]["last_seen_at"] == TS_LATER

    with pytest.raises(StatsInvariantError, match="preserved fields"):
        repository.upsert_pitch_identities(
            (
                pitch(12),
                pitch(12, play_identity_id="play:unexpected"),
            )
        )
    with pytest.raises(StatsInvariantError, match="different ID"):
        repository.upsert_pitch_identities(
            (
                pitch(13),
                pitch(13, pitch_identity_id="pitch:different-in-batch"),
            )
        )
    with pytest.raises(StatsInvariantError, match="cannot regress"):
        repository.upsert_pitch_identities(
            (
                pitch(14, last_seen_at=TS_LATER),
                pitch(14),
            )
        )

    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT * FROM statcast_pitch_identities "
            "WHERE at_bat_number BETWEEN 10 AND 14 ORDER BY at_bat_number"
        ).fetchall()
    assert [(row["at_bat_number"], row["last_seen_at"]) for row in rows] == [
        (10, TS_LATER),
        (11, TS_LATER),
    ]


def test_play_identity_batch_preserves_natural_identity_and_is_atomic(
    repository: StatsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_identities(repository)
    monkeypatch.setattr("app.stats.repository._IDENTITY_QUERY_PARAMETER_LIMIT", 3)

    def play(index: int, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "play_identity_id": f"play:batch:{index}",
            "provider": "retrosheet",
            "game_identity_id": "game:777",
            "provider_play_id": f"777:{index}",
            "at_bat_index": index,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
        record.update(overrides)
        return record

    repository.upsert_play_identity(play(1))
    results = repository.upsert_play_identities(
        (
            play(1, last_seen_at=TS_LATER),
            play(2),
            play(2, last_seen_at=TS_LATER),
        )
    )
    assert [result["last_seen_at"] for result in results] == [
        TS_LATER,
        TS,
        TS_LATER,
    ]

    with pytest.raises(StatsInvariantError, match="different ID"):
        repository.upsert_play_identities(
            (
                play(3),
                play(3, play_identity_id="play:different-in-batch"),
            )
        )
    with pytest.raises(StatsInvariantError, match="preserved fields"):
        repository.upsert_play_identities(
            (
                play(4),
                play(4, at_bat_index=99),
            )
        )
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        repository.upsert_play_identities(
            (
                play(5),
                play(6, play_identity_id="play:batch:5"),
            )
        )

    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT play_identity_id,last_seen_at FROM stats_play_identities "
            "WHERE play_identity_id LIKE 'play:batch:%' ORDER BY play_identity_id"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("play:batch:1", TS_LATER),
        ("play:batch:2", TS_LATER),
    ]


def test_source_equivalent_game_team_and_player_snapshots_are_idempotent(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    status_record = _snapshot(
        game_identity_id="game:777",
        abstract_state="Final",
        status_code="F",
        status={"codedGameState": "F"},
    )
    team_record = _snapshot(
        game_identity_id="game:777",
        team_identity_id="team:119",
        side="home",
        snapshot_kind="boxscore_final",
        stats={"runs": 4},
    )
    player_record = _snapshot(
        game_identity_id="game:777",
        team_identity_id="team:119",
        player_identity_id="player:1",
        role="batting",
        stats={"hits": 2},
    )
    initial = (
        repository.record_game_status(status_record),
        repository.record_game_team_snapshot(team_record),
        repository.record_game_player_snapshot(player_record),
    )

    _create_second_stats_run(repository)
    repository.record_raw_payload_metadata(
        _raw_record(
            raw_payload_id="raw:2",
            stats_run_id="stats-run-2",
            source_capture_id="capture-2",
            retrieved_at=TS_LATER,
            artifact_relpath="raw/statcast/schedule/b.json",
        )
    )
    rerun = {
        "stats_run_id": "stats-run-2",
        "raw_payload_id": "raw:2",
        "retrieved_at": TS_LATER,
    }
    repeated = (
        repository.record_game_status({**status_record, **rerun}),
        repository.record_game_team_snapshot({**team_record, **rerun}),
        repository.record_game_player_snapshot({**player_record, **rerun}),
    )

    assert [row["revision_number"] for row in repeated] == [1, 1, 1]
    assert [row[key] for row, key in zip(
        repeated,
        ("status_observation_id", "team_snapshot_id", "player_snapshot_id"),
        strict=True,
    )] == [
        initial[0]["status_observation_id"],
        initial[1]["team_snapshot_id"],
        initial[2]["player_snapshot_id"],
    ]
    assert all(row["stats_run_id"] == "stats-run-1" for row in repeated)

    equivalent_from_changed_aggregate = repository.record_game_player_snapshot(
        {
            **player_record,
            **rerun,
            "raw_payload_id": None,
            "retrieved_at": TS_AFTER,
            "source_checksum": "c" * 64,
        }
    )
    assert (
        equivalent_from_changed_aggregate["player_snapshot_id"]
        == initial[2]["player_snapshot_id"]
    )
    assert equivalent_from_changed_aggregate["revision_number"] == 1

    with pytest.raises(StatsInvariantError, match="conflicting normalized evidence"):
        repository.record_game_player_snapshot(
            {**player_record, **rerun, "stats": {"hits": 3}}
        )

    correction = repository.record_game_player_snapshot(
        {
            **player_record,
            **rerun,
            "raw_payload_id": None,
            "retrieved_at": TS_AFTER,
            "provider_updated_at": TS_AFTER,
            "source_checksum": HASH_B,
            "stats": {"hits": 3},
        }
    )
    assert correction["revision_number"] == 2
    assert correction["revision_kind"] == "correction"
    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT revision_number,stats_json FROM stats_game_player_snapshots "
            "ORDER BY revision_number"
        ).fetchall()
    assert [(row["revision_number"], json.loads(row["stats_json"])["hits"]) for row in rows] == [
        (1, 2),
        (2, 3),
    ]


def test_identity_bulk_write_is_atomic_and_conflicts_preserve_prior_facts(
    repository: StatsRepository,
) -> None:
    repository.upsert_player_identity(_identity_record("player", "1", "Batter One"))
    advanced = repository.upsert_player_identity(
        {
            **_identity_record("player", "1", "Batter One"),
            "first_seen_at": TS_LATER,
            "last_seen_at": TS_LATER,
        }
    )
    assert advanced["first_seen_at"] == TS
    assert advanced["last_seen_at"] == TS_LATER

    with pytest.raises(StatsInvariantError, match="conflict would overwrite"):
        repository.upsert_player_identities(
            (
                _identity_record("player", "3", "New Player"),
                {
                    **_identity_record("player", "1", "Changed Name"),
                    "last_seen_at": TS_AFTER,
                    "identity_checksum": HASH_B,
                },
            )
        )

    with repository.database.connect() as connection:
        names = connection.execute(
            "SELECT provider_player_id,full_name FROM stats_player_identities "
            "ORDER BY provider_player_id"
        ).fetchall()
    assert [tuple(row) for row in names] == [("1", "Batter One")]


def test_point_in_time_history_uses_latest_revision_strictly_before_cutoff(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    player = _snapshot(
        game_identity_id="game:777",
        team_identity_id="team:119",
        player_identity_id="player:1",
        role="batting",
        stats={"hits": 1},
    )
    team = _snapshot(
        game_identity_id="game:777",
        team_identity_id="team:119",
        side="home",
        snapshot_kind="boxscore_final",
        stats={"runs": 2},
    )
    repository.record_game_player_snapshot(player)
    repository.record_game_team_snapshot(team)
    repository.record_game_player_snapshot(
        {
            **player,
            "raw_payload_id": None,
            # Provider effective time predates the cutoff, but this correction was
            # not known to the project until its later retrieval.
            "provider_updated_at": TS,
            "retrieved_at": TS_AFTER,
            "source_checksum": HASH_B,
            "stats": {"hits": 2},
        }
    )
    repository.record_game_team_snapshot(
        {
            **team,
            "raw_payload_id": None,
            # Knowledge gating must use retrieval time, not provider effective time.
            "provider_updated_at": TS,
            "retrieved_at": TS_AFTER,
            "source_checksum": HASH_B,
            "stats": {"runs": 3},
        }
    )

    early_players = repository.load_player_game_history(
        "player:1",
        before_game_date="2026-07-15",
        observed_before="2026-07-15T02:00:00+00:00",
    )
    late_players = repository.load_player_game_history(
        "player:1",
        before_game_date="2026-07-15",
        observed_before="2026-07-15T04:00:00+00:00",
    )
    early_teams = repository.load_team_game_history(
        "team:119",
        before_game_date="2026-07-15",
        observed_before="2026-07-15T02:00:00+00:00",
    )
    assert early_players[0]["revision_number"] == 1
    assert early_players[0]["stats"]["hits"] == 1
    assert late_players[0]["revision_number"] == 2
    assert late_players[0]["stats"]["hits"] == 2
    assert early_teams[0]["stats"]["runs"] == 2
    assert repository.load_player_game_history(
        "player:1",
        before_game_date="2026-07-14",
        observed_before="2026-07-15T04:00:00+00:00",
    ) == ()


def test_statcast_revision_is_checksum_idempotent_and_point_in_time_safe(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    repository.upsert_pitch_identity(
        {
            "pitch_identity_id": "pitch:777:4:2",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "provider_pitch_id": "777:4:2",
            "game_pk": 777,
            "at_bat_number": 4,
            "pitch_number": 2,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
    )
    initial_record = _snapshot(
        pitch_identity_id="pitch:777:4:2",
        revision_number=1,
        revision_kind="initial",
        metrics={"release_speed": 95.0},
    )
    initial = repository.record_statcast_revision(initial_record)
    with repository.database.connect() as connection:
        stored_initial = connection.execute(
            "SELECT * FROM statcast_pitch_revisions "
            "WHERE statcast_revision_id=?",
            (initial["statcast_revision_id"],),
        ).fetchone()
    assert stored_initial is not None
    assert initial == dict(stored_initial)
    repeated = repository.record_statcast_revision(
        {**initial_record, "revision_number": 2, "revision_kind": "correction"}
    )
    assert repeated["statcast_revision_id"] == initial["statcast_revision_id"]

    with pytest.raises(StatsInvariantError, match="checksum collision"):
        repository.record_statcast_revision(
            {
                **initial_record,
                "revision_number": 2,
                "revision_kind": "correction",
                "metrics": {"release_speed": 96.0},
            }
        )
    with pytest.raises(StatsInvariantError, match="different fields"):
        repository.record_statcast_revision(
            {
                **initial_record,
                "source_checksum": HASH_B,
                "metrics": {"release_speed": 96.0},
            }
        )

    correction = repository.record_statcast_revision(
        {
            **initial_record,
            "raw_payload_id": None,
            "revision_number": 2,
            "revision_kind": "correction",
            # Knowledge gating must use retrieval time, not provider effective time.
            "provider_updated_at": TS,
            "retrieved_at": TS_AFTER,
            "source_checksum": HASH_B,
            "metrics": {"release_speed": 96.0},
        }
    )
    assert correction["revision_number"] == 2
    with pytest.raises(StatsInvariantError, match="next sequential"):
        repository.record_statcast_revision(
            {
                **initial_record,
                "revision_number": 4,
                "revision_kind": "correction",
                "source_checksum": "c" * 64,
            }
        )

    repository.upsert_pitch_identity(
        {
            "pitch_identity_id": "pitch:777:5:1",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "provider_pitch_id": "777:5:1",
            "game_pk": 777,
            "at_bat_number": 5,
            "pitch_number": 1,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }
    )
    second_pitch = {
        **initial_record,
        "pitch_identity_id": "pitch:777:5:1",
    }
    with pytest.raises(StatsInvariantError, match="first revision must be initial"):
        repository.record_statcast_revision(
            {**second_pitch, "revision_kind": "correction"}
        )
    repository.record_statcast_revision(second_pitch)
    with pytest.raises(
        StatsInvariantError, match="later revisions must be correction or tombstone"
    ):
        repository.record_statcast_revision(
            {
                **second_pitch,
                "revision_number": 2,
                "revision_kind": "initial",
                "source_checksum": HASH_B,
            }
        )
    repository.record_statcast_revision(
        {
            **second_pitch,
            "revision_number": 2,
            "revision_kind": "correction",
            "source_checksum": HASH_B,
            "metrics": {"release_speed": 96.0},
        }
    )
    tombstone = repository.record_statcast_revision(
        {
            **second_pitch,
            "revision_number": 3,
            "revision_kind": "tombstone",
            "source_checksum": "c" * 64,
            "metrics": {},
        }
    )
    assert tombstone["revision_number"] == 3
    assert tombstone["revision_kind"] == "tombstone"

    early = repository.load_statcast_pitch_history(
        "game:777", observed_before="2026-07-15T02:00:00+00:00"
    )
    late = repository.load_statcast_pitch_history(
        "game:777", observed_before="2026-07-15T04:00:00+00:00"
    )
    assert early[0]["metrics"]["release_speed"] == 95.0
    assert late[0]["metrics"]["release_speed"] == 96.0


def test_revision_batches_preload_chunked_histories_and_validate_in_order(
    repository: StatsRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    monkeypatch.setattr(
        "app.stats.repository._REVISION_HISTORY_QUERY_CHUNK_SIZE", 2
    )

    def pitch_identity(at_bat_number: int) -> dict[str, object]:
        provider_id = f"777:{at_bat_number}:1"
        return {
            "pitch_identity_id": f"pitch:{provider_id}",
            "provider": "statcast",
            "game_identity_id": "game:777",
            "provider_pitch_id": provider_id,
            "game_pk": 777,
            "at_bat_number": at_bat_number,
            "pitch_number": 1,
            "first_seen_at": TS,
            "last_seen_at": TS,
        }

    repository.upsert_pitch_identities(
        tuple(pitch_identity(at_bat) for at_bat in (6, 7, 8, 9))
    )
    pitch_6 = _snapshot(
        pitch_identity_id="pitch:777:6:1",
        revision_number=1,
        revision_kind="initial",
        metrics={"release_speed": 95.0},
    )
    initial_6 = repository.record_statcast_revision(pitch_6)

    results = repository.record_statcast_revisions(
        (
            {**pitch_6, "revision_number": 2, "revision_kind": "correction"},
            {
                **pitch_6,
                "revision_number": 2,
                "revision_kind": "correction",
                "source_checksum": HASH_B,
                "metrics": {"release_speed": 96.0},
            },
            _snapshot(
                pitch_identity_id="pitch:777:7:1",
                revision_number=1,
                revision_kind="initial",
                metrics={"release_speed": 90.0},
            ),
            _snapshot(
                pitch_identity_id="pitch:777:7:1",
                revision_number=2,
                revision_kind="correction",
                metrics={"release_speed": 90.0},
            ),
            _snapshot(
                pitch_identity_id="pitch:777:8:1",
                revision_number=1,
                revision_kind="initial",
                metrics={"release_speed": 91.0},
            ),
        )
    )
    assert results[0]["statcast_revision_id"] == initial_6[
        "statcast_revision_id"
    ]
    assert results[1]["revision_number"] == 2
    assert results[2]["statcast_revision_id"] == results[3][
        "statcast_revision_id"
    ]
    assert results[4]["revision_number"] == 1

    with pytest.raises(StatsInvariantError, match="checksum collision"):
        repository.record_statcast_revisions(
            (
                _snapshot(
                    pitch_identity_id="pitch:777:8:1",
                    revision_number=2,
                    revision_kind="correction",
                    source_checksum=HASH_B,
                    metrics={"release_speed": 92.0},
                ),
                _snapshot(
                    pitch_identity_id="pitch:777:8:1",
                    revision_number=3,
                    revision_kind="correction",
                    source_checksum=HASH_B,
                    metrics={"release_speed": 93.0},
                ),
            )
        )

    with pytest.raises(StatsInvariantError, match="different fields"):
        repository.record_statcast_revisions(
            (
                _snapshot(
                    pitch_identity_id="pitch:777:9:1",
                    revision_number=1,
                    revision_kind="initial",
                    metrics={"release_speed": 94.0},
                ),
                _snapshot(
                    pitch_identity_id="pitch:777:9:1",
                    revision_number=1,
                    revision_kind="initial",
                    source_checksum=HASH_B,
                    metrics={"release_speed": 95.0},
                ),
            )
        )

    with repository.database.connect() as connection:
        counts = {
            str(row["pitch_identity_id"]): int(row["revision_count"])
            for row in connection.execute(
                "SELECT pitch_identity_id,COUNT(*) AS revision_count "
                "FROM statcast_pitch_revisions "
                "WHERE pitch_identity_id LIKE 'pitch:777:%:1' "
                "GROUP BY pitch_identity_id"
            ).fetchall()
        }
    assert counts == {
        "pitch:777:6:1": 2,
        "pitch:777:7:1": 1,
        "pitch:777:8:1": 1,
    }


def test_canonical_player_crosswalk_preserves_lookup_provenance_and_conflicts(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    repository.upsert_player_identities(
        (
            _identity_record("player", "660271", "Example Player"),
            {
                **_identity_record("player", "example001", "Example Player"),
                "player_identity_id": "player:retrosheet:example001",
                "provider": "retrosheet",
            },
        )
    )
    repository.record_canonical_player(
        {
            "canonical_player_id": "mlb-player:example",
            "created_stats_run_id": "stats-run-1",
            "display_name": "Example Player",
            "birth_date": "1998-04-03",
            "created_at": TS,
            "canonical_checksum": HASH_A,
        }
    )
    mapping = {
        "mapping_id": "mapping:mlbam",
        "stats_run_id": "stats-run-1",
        "canonical_player_id": "mlb-player:example",
        "player_identity_id": "player:660271",
        "mapping_method": "pybaseball_lookup",
        "verification_status": "verified",
        "source_version": "pybaseball-2.2.7",
        "adapter_version": "DSE_STATS_ACQUISITION_V1",
        "observed_at": TS,
        "provenance": {"lookup": "playerid_reverse_lookup", "player_id": 660271},
        "source_checksum": HASH_A,
    }
    first = repository.record_player_identifier_mapping(mapping)
    repeated = repository.record_player_identifier_mapping(
        {**mapping, "mapping_id": "mapping:retry", "observed_at": TS_LATER}
    )
    assert repeated["mapping_id"] == first["mapping_id"]
    repository.record_player_identifier_mapping(
        {
            **mapping,
            "mapping_id": "mapping:retrosheet",
            "player_identity_id": "player:retrosheet:example001",
            "mapping_method": "source_declared",
            "source_version": "retrosheet-2025",
            "source_checksum": HASH_B,
        }
    )

    crosswalk = repository.load_player_crosswalk("mlb-player:example")
    assert {(row["provider"], row["provider_player_id"]) for row in crosswalk} == {
        ("statcast", "660271"),
        ("retrosheet", "example001"),
    }
    assert crosswalk[0]["provenance"]
    resolved = repository.resolve_canonical_player("statcast", "660271")
    assert resolved is not None
    assert resolved["canonical_player_id"] == "mlb-player:example"

    repository.record_canonical_player(
        {
            "canonical_player_id": "mlb-player:conflict",
            "created_stats_run_id": "stats-run-1",
            "display_name": "Conflicting Identity",
            "created_at": TS_LATER,
            "canonical_checksum": HASH_B,
        }
    )
    repository.record_player_identifier_mapping(
        {
            **mapping,
            "mapping_id": "mapping:conflict",
            "canonical_player_id": "mlb-player:conflict",
            "mapping_method": "manual_review",
            "source_checksum": "c" * 64,
        }
    )
    with pytest.raises(StatsInvariantError, match="conflicting verified"):
        repository.resolve_canonical_player("statcast", "660271")


def test_excluded_source_rows_are_immutable_queryable_and_rerun_idempotent(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    repository.record_raw_payload_metadata(_raw_record(provider="retrosheet"))
    record = {
        "excluded_row_id": "excluded:all-star:1",
        "stats_run_id": "stats-run-1",
        "raw_payload_id": "raw:1",
        "provider": "retrosheet",
        "dataset_key": "gameinfo.csv",
        "source_row_id": "ALS202607140",
        "classification": "all_star",
        "reason_code": "game_type_not_regular_season",
        "source_effective_date": "2026-07-14",
        "observed_at": TS,
        "source_row_checksum": HASH_A,
        "details": {"game_type": "A"},
    }
    first = repository.record_excluded_source_row(record)
    repeated = repository.record_excluded_source_row(
        {**record, "excluded_row_id": "excluded:retry", "observed_at": TS_LATER}
    )
    assert repeated["excluded_row_id"] == first["excluded_row_id"]
    partial = repository.record_excluded_source_row(
        {
            **record,
            "excluded_row_id": "excluded:partial:1",
            "classification": "partial",
            "reason_code": "completed_game_validation_incomplete",
        }
    )
    assert partial["classification"] == "partial"
    _create_second_stats_run(repository)
    repository.record_raw_payload_metadata(
        _raw_record(
            raw_payload_id="raw:2",
            stats_run_id="stats-run-2",
            provider="retrosheet",
            source_capture_id="capture-2",
            retrieved_at=TS_LATER,
            artifact_relpath="raw/retrosheet/schedule/b.json",
        )
    )
    repeated_in_new_capture = repository.record_excluded_source_row(
        {
            **record,
            "excluded_row_id": "excluded:all-star:2",
            "stats_run_id": "stats-run-2",
            "raw_payload_id": "raw:2",
            "observed_at": TS_LATER,
        }
    )
    assert repeated_in_new_capture["excluded_row_id"] == "excluded:all-star:2"
    assert repeated_in_new_capture["raw_payload_id"] == "raw:2"
    with repository.database.connect() as connection:
        rows = connection.execute(
            "SELECT raw_payload_id,classification,reason_code "
            "FROM stats_excluded_source_rows "
            "ORDER BY raw_payload_id,classification"
        ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("raw:1", "all_star", "game_type_not_regular_season"),
        ("raw:1", "partial", "completed_game_validation_incomplete"),
        ("raw:2", "all_star", "game_type_not_regular_season"),
    ]


def test_feature_and_season_snapshot_natural_keys_defeat_nullable_unique_gaps(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    feature = {
        "feature_snapshot_id": "feature:1",
        "stats_run_id": "stats-run-1",
        "feature_version": "mlb-features-v1",
        "entity_kind": "game",
        "game_identity_id": "game:777",
        "feature_as_of": TS,
        "completeness_state": "complete",
        "observed_through": TS,
        "complete_through": TS,
        "input_checksum": HASH_A,
        "feature_checksum": HASH_B,
        "features": {"games": 10},
        "created_at": TS,
    }
    first_feature = repository.record_feature_snapshot(feature)
    repeated_feature = repository.record_feature_snapshot(
        {**feature, "feature_snapshot_id": "feature:retry", "created_at": TS_LATER}
    )
    assert repeated_feature["feature_snapshot_id"] == first_feature["feature_snapshot_id"]
    with pytest.raises(StatsInvariantError, match="changed preserved output"):
        repository.record_feature_snapshot(
            {
                **feature,
                "feature_snapshot_id": "feature:conflict",
                "feature_checksum": "c" * 64,
                "features": {"games": 11},
            }
        )

    season = _snapshot(
        season_snapshot_id="season:1",
        provider="statcast",
        season=2026,
        entity_kind="player",
        player_identity_id="player:1",
        split_key="season",
        snapshot_as_of=TS,
        stats={"hits": 100},
    )
    first_season = repository.record_season_snapshot(season)
    repeated_season = repository.record_season_snapshot(
        {
            **season,
            "season_snapshot_id": "season:retry",
            "snapshot_as_of": TS_LATER,
            "retrieved_at": TS_LATER,
        }
    )
    assert repeated_season["season_snapshot_id"] == first_season["season_snapshot_id"]

    traded_total = repository.record_season_snapshot(
        {
            **season,
            "season_snapshot_id": "season:traded:total",
            "source_team_id": "TOT",
            "source_stint_key": "TOT:1",
        }
    )
    traded_club = repository.record_season_snapshot(
        {
            **season,
            "season_snapshot_id": "season:traded:club",
            "source_team_id": "LAD",
            "source_stint_key": "LAD:1",
        }
    )
    assert traded_total["season_snapshot_id"] != traded_club["season_snapshot_id"]


def test_player_features_use_canonical_foreign_keys_and_bulk_writes_are_atomic(
    repository: StatsRepository,
) -> None:
    _create_stats_run(repository)
    _seed_identities(repository)
    repository.record_raw_payload_metadata(_raw_record())
    repository.record_canonical_player(
        {
            "canonical_player_id": "mlb-player:1",
            "created_stats_run_id": "stats-run-1",
            "display_name": "Batter One",
            "created_at": TS,
            "canonical_checksum": HASH_A,
        }
    )
    player_feature = {
        "feature_snapshot_id": "feature:player:1",
        "stats_run_id": "stats-run-1",
        "feature_version": "mlb-features-v1",
        "entity_kind": "player",
        "canonical_player_id": "mlb-player:1",
        "feature_as_of": TS,
        "completeness_state": "complete",
        "observed_through": TS,
        "complete_through": TS,
        "input_checksum": HASH_A,
        "feature_checksum": HASH_B,
        "features": {"ops": 0.825},
        "created_at": TS,
    }
    created = repository.record_feature_snapshots((player_feature,))[0]
    assert created["canonical_player_id"] == "mlb-player:1"
    assert created["player_identity_id"] is None

    with pytest.raises(ValueError, match="canonical_player_id"):
        repository.record_feature_snapshot(
            {
                **player_feature,
                "feature_snapshot_id": "feature:provider-only",
                "canonical_player_id": None,
                "player_identity_id": "player:1",
            }
        )

    with repository.database.connect() as connection:
        feature_count = int(
            connection.execute("SELECT COUNT(*) FROM stats_feature_snapshots").fetchone()[0]
        )
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        repository.record_feature_snapshots(
            (
                {
                    **player_feature,
                    "feature_snapshot_id": "feature:team:atomic",
                    "entity_kind": "team",
                    "canonical_player_id": None,
                    "team_identity_id": "team:119",
                    "input_checksum": "c" * 64,
                    "feature_checksum": "d" * 64,
                },
                {
                    **player_feature,
                    "feature_snapshot_id": "feature:missing-player",
                    "canonical_player_id": "mlb-player:missing",
                    "input_checksum": "e" * 64,
                    "feature_checksum": "f" * 64,
                },
            )
        )
    with repository.database.connect() as connection:
        assert (
            int(connection.execute("SELECT COUNT(*) FROM stats_feature_snapshots").fetchone()[0])
            == feature_count
        )

    valid_season = {
        **_snapshot(
            season_snapshot_id="season:atomic:valid",
            provider="statcast",
            season=2026,
            entity_kind="player",
            player_identity_id="player:1",
            split_key="season",
            snapshot_as_of=TS,
            stats={"hits": 100},
        ),
        "source_checksum": "c" * 64,
    }
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        repository.record_season_snapshots(
            (
                valid_season,
                {
                    **valid_season,
                    "season_snapshot_id": "season:atomic:missing",
                    "player_identity_id": "player:missing",
                    "source_checksum": "d" * 64,
                },
            )
        )
    with repository.database.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM stats_season_snapshots "
            "WHERE season_snapshot_id LIKE 'season:atomic:%'"
        ).fetchone()[0] == 0
