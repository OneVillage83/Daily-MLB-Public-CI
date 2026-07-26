from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.database import Database, DatabaseInvariantError
from app.migrations import DSE_MLB_ML_CANDIDATE_V1_GATE_CODES


RUN_ID = "run_20260716_11111111111111111111111111111111"
EVENT_ID = "event-release"
NOW = "2026-07-16T16:00:00+00:00"
POLICY_VERSION = "DSE_MLB_ML_CANDIDATE_V1"
PREDICTION_VERSION = "DSE_REVIEWED_ANALYST_V1"


def checksum(character: str) -> str:
    return character * 64


def seeded_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "release.db")
    database.create_run(RUN_ID, "2026-07-16")
    database.upsert_game(
        {
            "id": EVENT_ID,
            "sport_key": "baseball_mlb",
            "commence_time": "2026-07-16T23:10:00+00:00",
            "home_team": "Seattle Mariners",
            "away_team": "Detroit Tigers",
        },
        "SEA",
        "DET",
        run_id=RUN_ID,
    )
    return database


def insert_prediction(database: Database) -> dict[str, object]:
    return database.insert_sealed_prediction(
        evidence={
            "evidence_id": "evidence-1",
            "run_id": RUN_ID,
            "event_id": EVENT_ID,
            "contract_version": PREDICTION_VERSION,
            "analyst_id": "owner",
            "method_version": "manual-v1",
            "evidence_checksum": checksum("a"),
            "source_checksum": checksum("b"),
            "created_at": NOW,
            "evidence": {
                "starting_pitching": {"state": "unknown"},
                "bullpen": {"state": "unknown"},
                "material_unknowns": ["lineups"],
            },
        },
        prediction={
            "prediction_id": "prediction-1",
            "evidence_id": "evidence-1",
            "run_id": RUN_ID,
            "event_id": EVENT_ID,
            "contract_version": PREDICTION_VERSION,
            "analyst_id": "owner",
            "method_version": "manual-v1",
            "home_probability": 0.56,
            "away_probability": 0.44,
            "lower_bound": 0.51,
            "upper_bound": 0.61,
            "feature_checksum": checksum("c"),
            "evidence_checksum": checksum("a"),
            "prediction_checksum": checksum("d"),
            "sealed_at": NOW,
            "payload": {"probability": 0.56, "bounds": [0.51, 0.61]},
        },
    )


def insert_evaluation(database: Database) -> dict[str, object]:
    database.insert_market_evaluation(
        {
            "evaluation_id": "market-evaluation-1",
            "prediction_id": "prediction-1",
            "run_id": RUN_ID,
            "event_id": EVENT_ID,
            "policy_version": POLICY_VERSION,
            "market_snapshot_checksum": checksum("e"),
            "market_no_vig_probability": 0.52,
            "best_price": 105,
            "best_price_books": ["book-a", "book-b"],
            "break_even_probability": 0.487804878,
            "edge_percentage_points": 4.0,
            "expected_value_per_unit_risk": 0.148,
            "bookmaker_count": 4,
            "source_checksum": checksum("f"),
            "evaluated_at": NOW,
        }
    )
    return database.insert_policy_evaluation(
        {
            "policy_evaluation_id": "policy-evaluation-1",
            "evaluation_id": "market-evaluation-1",
            "prediction_id": "prediction-1",
            "policy_version": POLICY_VERSION,
            "outcome": "CANDIDATE_REQUIRES_REVIEW",
            "all_gates_passed": True,
            "source_checksum": checksum("1"),
            "evaluated_at": NOW,
            "payload": {
                "selected_team_key": "SEA",
                "confidence_grade": "B",
            },
        },
        [
            {
                "gate_result_id": f"gate-{index}",
                "gate_code": gate_code,
                "threshold": True,
                "observed": True,
                "passed": True,
                "reason": f"{gate_code} passed",
                "source_checksum": checksum("abcdef0123456789"[index]),
                "evaluated_at": NOW,
            }
            for index, gate_code in enumerate(DSE_MLB_ML_CANDIDATE_V1_GATE_CODES)
        ],
    )


def insert_draft_and_approval(database: Database) -> None:
    database.insert_card_draft(
        {
            "draft_id": "draft-1",
            "run_id": RUN_ID,
            "requested_date": "2026-07-16",
            "policy_version": POLICY_VERSION,
            "candidate_count": 1,
            "draft_checksum": checksum("4"),
            "source_checksum": checksum("5"),
            "created_at": NOW,
            "payload": {"review_required": True},
        },
        ["policy-evaluation-1"],
    )
    database.insert_review_decision(
        {
            "decision_id": "decision-1",
            "draft_id": "draft-1",
            "policy_evaluation_id": "policy-evaluation-1",
            "reviewer_id": "owner",
            "decision": "approved",
            "reason": "approved after review",
            "draft_checksum": checksum("4"),
            "prediction_checksum": checksum("d"),
            "policy_version": POLICY_VERSION,
            "source_checksum": checksum("6"),
            "decided_at": NOW,
        }
    )


def publication_batch() -> dict[str, object]:
    return {
        "batch_id": "batch-1",
        "draft_id": "draft-1",
        "run_id": RUN_ID,
        "requested_date": "2026-07-16",
        "policy_version": POLICY_VERSION,
        "draft_checksum": checksum("4"),
        "source_checksum": checksum("5"),
        "reviewer_id": "owner",
        "published_at": NOW,
        "payload": {"publication_mode": "local_reviewed_package"},
    }


def publication_play(**overrides: object) -> dict[str, object]:
    play: dict[str, object] = {
        "play_id": "play-1",
        "policy_evaluation_id": "policy-evaluation-1",
        "prediction_id": "prediction-1",
        "event_id": EVENT_ID,
        "market_key": "h2h",
        "selection": "SEA",
        "publication_price": 105,
        "bookmaker_key": "book-a",
        "market_no_vig_probability": 0.52,
        "break_even_probability": 0.487804878,
        "edge_percentage_points": 4.0,
        "expected_value_per_unit_risk": 0.148,
        "confidence_grade": "B",
        "prediction_checksum": checksum("d"),
        "evidence_checksum": checksum("a"),
        "policy_version": POLICY_VERSION,
        "source_checksum": checksum("6"),
        "gate_results": [{"gate_code": "minimum_edge", "passed": True}],
        "published_at": NOW,
    }
    play.update(overrides)
    return play


def insert_publication(database: Database) -> None:
    database.insert_publication_batch(publication_batch(), [publication_play()])


def test_release_evidence_round_trip_and_append_only_settlement(tmp_path: Path) -> None:
    database = seeded_database(tmp_path)
    prediction = insert_prediction(database)
    policy = insert_evaluation(database)
    insert_draft_and_approval(database)
    insert_publication(database)

    assert prediction["prediction_checksum"] == checksum("d")
    gate_results = policy["gate_results"]
    assert isinstance(gate_results, list)
    assert len(gate_results) == len(DSE_MLB_ML_CANDIDATE_V1_GATE_CODES)
    draft = database.get_card_draft("draft-1")
    assert draft is not None
    assert draft["policy_evaluation_ids"] == ["policy-evaluation-1"]
    publication = database.get_publication_batch("batch-1")
    assert publication is not None
    assert isinstance(publication["plays"], list)
    assert len(publication["plays"]) == 1

    original = database.insert_settlement_event(
        {
            "settlement_event_id": "settlement-1",
            "play_id": "play-1",
            "event_kind": "original",
            "result": "win",
            "settled_at": "2026-07-17T04:00:00+00:00",
            "recorded_at": "2026-07-17T04:05:00+00:00",
            "source_checksum": checksum("9"),
        }
    )
    correction = database.insert_settlement_event(
        {
            "settlement_event_id": "settlement-2",
            "play_id": "play-1",
            "event_kind": "correction",
            "result": "void",
            "corrects_event_id": "settlement-1",
            "settled_at": "2026-07-17T04:00:00+00:00",
            "recorded_at": "2026-07-17T05:00:00+00:00",
            "source_checksum": checksum("0"),
            "notes": "official result correction",
        }
    )

    assert original["result"] == "win"
    assert correction["corrects_event_id"] == "settlement-1"
    assert [row["result"] for row in database.list_settlement_ledger(play_id="play-1")] == [
        "win",
        "void",
    ]


@pytest.mark.parametrize(
    "table",
    [
        "reviewed_prediction_evidence",
        "reviewed_predictions",
        "market_evaluations",
        "policy_evaluations",
        "policy_gate_results",
        "card_drafts",
        "draft_policy_evaluations",
        "review_decisions",
        "publication_batches",
        "publication_plays",
        "settlement_events",
    ],
)
def test_release_evidence_tables_reject_updates_and_deletes(
    tmp_path: Path, table: str
) -> None:
    database = seeded_database(tmp_path)
    insert_prediction(database)
    insert_evaluation(database)
    insert_draft_and_approval(database)
    insert_publication(database)
    database.insert_settlement_event(
        {
            "settlement_event_id": "settlement-1",
            "play_id": "play-1",
            "event_kind": "original",
            "result": "win",
            "settled_at": NOW,
            "recorded_at": NOW,
            "source_checksum": checksum("9"),
        }
    )

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect(write=True) as connection:
            connection.execute(f"UPDATE {table} SET rowid=rowid")
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with database.connect(write=True) as connection:
            connection.execute(f"DELETE FROM {table}")


def test_policy_insert_rolls_back_when_gate_batch_is_invalid(tmp_path: Path) -> None:
    database = seeded_database(tmp_path)
    insert_prediction(database)
    database.insert_market_evaluation(
        {
            "evaluation_id": "market-evaluation-1",
            "prediction_id": "prediction-1",
            "run_id": RUN_ID,
            "event_id": EVENT_ID,
            "policy_version": POLICY_VERSION,
            "market_snapshot_checksum": checksum("e"),
            "market_no_vig_probability": 0.52,
            "best_price": 105,
            "best_price_books": ["book-a"],
            "break_even_probability": 0.487804878,
            "edge_percentage_points": 4.0,
            "expected_value_per_unit_risk": 0.148,
            "bookmaker_count": 4,
            "source_checksum": checksum("f"),
            "evaluated_at": NOW,
        }
    )
    gate = {
        "gate_result_id": "duplicate-gate-id",
        "gate_code": "minimum_edge",
        "threshold": 3.0,
        "observed": 4.0,
        "passed": True,
        "reason": "passed",
        "source_checksum": checksum("2"),
        "evaluated_at": NOW,
    }

    with pytest.raises(sqlite3.IntegrityError):
        database.insert_policy_evaluation(
            {
                "policy_evaluation_id": "policy-rollback",
                "evaluation_id": "market-evaluation-1",
                "prediction_id": "prediction-1",
                "policy_version": POLICY_VERSION,
                "outcome": "CANDIDATE_REQUIRES_REVIEW",
                "all_gates_passed": True,
                "source_checksum": checksum("1"),
                "evaluated_at": NOW,
            },
            [gate, gate],
        )

    assert database.get_policy_evaluation("policy-rollback") is None


def test_collection_read_helpers_keep_run_and_event_boundaries(tmp_path: Path) -> None:
    database = seeded_database(tmp_path)

    assert [game["event_id"] for game in database.list_run_games(RUN_ID)] == [EVENT_ID]
    assert database.list_odds_snapshots(RUN_ID, event_id=EVENT_ID) == []
    assert database.list_weather_snapshots(RUN_ID, event_id=EVENT_ID) == []
    assert database.integrity_check()["ok"] is True


@pytest.mark.parametrize("gate_variant", ["missing_phase2", "extra_gate"])
def test_draft_link_rejects_any_nonexact_candidate_gate_set(
    tmp_path: Path, gate_variant: str
) -> None:
    database = seeded_database(tmp_path)
    insert_prediction(database)
    database.insert_market_evaluation(
        {
            "evaluation_id": "market-nonexact",
            "prediction_id": "prediction-1",
            "run_id": RUN_ID,
            "event_id": EVENT_ID,
            "policy_version": POLICY_VERSION,
            "market_snapshot_checksum": checksum("e"),
            "market_no_vig_probability": 0.52,
            "best_price": 105,
            "best_price_books": ["book-a"],
            "break_even_probability": 0.487804878,
            "edge_percentage_points": 4.0,
            "expected_value_per_unit_risk": 0.148,
            "bookmaker_count": 4,
            "source_checksum": checksum("f"),
            "evaluated_at": NOW,
        }
    )
    gate_codes = list(DSE_MLB_ML_CANDIDATE_V1_GATE_CODES)
    if gate_variant == "missing_phase2":
        gate_codes.remove("phase2_live_weather_accepted")
    else:
        gate_codes.append("unapproved_extra_gate")
    database.insert_policy_evaluation(
        {
            "policy_evaluation_id": "policy-nonexact",
            "evaluation_id": "market-nonexact",
            "prediction_id": "prediction-1",
            "policy_version": POLICY_VERSION,
            "outcome": "CANDIDATE_REQUIRES_REVIEW",
            "all_gates_passed": True,
            "source_checksum": checksum("1"),
            "evaluated_at": NOW,
            "payload": {"selected_team_key": "SEA", "confidence_grade": "B"},
        },
        [
            {
                "gate_result_id": f"nonexact-{index}",
                "gate_code": code,
                "threshold": True,
                "observed": True,
                "passed": True,
                "reason": "passed",
                "source_checksum": checksum("a"),
                "evaluated_at": NOW,
            }
            for index, code in enumerate(gate_codes)
        ],
    )

    with pytest.raises(sqlite3.IntegrityError, match="exact passing policy gate set"):
        database.insert_card_draft(
            {
                "draft_id": "draft-nonexact",
                "run_id": RUN_ID,
                "requested_date": "2026-07-16",
                "policy_version": POLICY_VERSION,
                "candidate_count": 1,
                "draft_checksum": checksum("4"),
                "source_checksum": checksum("5"),
                "created_at": NOW,
            },
            ["policy-nonexact"],
        )
    assert database.get_card_draft("draft-nonexact") is None


def test_publication_insert_rejects_caller_price_and_rolls_back_batch(
    tmp_path: Path,
) -> None:
    database = seeded_database(tmp_path)
    insert_prediction(database)
    insert_evaluation(database)
    insert_draft_and_approval(database)

    with pytest.raises(DatabaseInvariantError, match="publication_price"):
        database.insert_publication_batch(
            publication_batch(), [publication_play(publication_price=999)]
        )
    assert database.get_publication_batch("batch-1") is None


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("event_id", "other-event"),
        ("selection", "DET"),
        ("publication_price", 999),
        ("bookmaker_key", "book-z"),
        ("market_no_vig_probability", 0.60),
        ("break_even_probability", 0.60),
        ("edge_percentage_points", 9.0),
        ("expected_value_per_unit_risk", 0.9),
        ("confidence_grade", "A"),
        ("policy_version", "DSE_MLB_ML_CANDIDATE_V2"),
        ("gate_results_json", "tamper"),
        ("payload_json", "tamper"),
    ],
)
def test_direct_sql_cannot_bypass_publication_evidence_binding(
    tmp_path: Path, field: str, tampered: object
) -> None:
    database = seeded_database(tmp_path)
    insert_prediction(database)
    insert_evaluation(database)
    insert_draft_and_approval(database)
    database.insert_publication_batch(publication_batch(), [])
    with database.connect() as connection:
        gates = connection.execute(
            """
            SELECT * FROM policy_gate_results
            WHERE policy_evaluation_id='policy-evaluation-1' ORDER BY gate_code
            """
        ).fetchall()
        snapshots = connection.execute(
            """
            SELECT prediction.payload_json AS prediction_payload_json,
                   evidence.evidence_json
            FROM reviewed_predictions AS prediction
            JOIN reviewed_prediction_evidence AS evidence
              ON evidence.evidence_id = prediction.evidence_id
            WHERE prediction.prediction_id='prediction-1'
            """
        ).fetchone()
    assert snapshots is not None
    gate_results = [
        {
            "gate_code": gate["gate_code"],
            "threshold": json.loads(gate["threshold_json"]),
            "observed": json.loads(gate["observed_json"]),
            "passed": bool(gate["passed"]),
            "reason": gate["reason"],
            "source_checksum": gate["source_checksum"],
            "evaluated_at": gate["evaluated_at"],
            "policy_version": POLICY_VERSION,
        }
        for gate in gates
    ]
    authoritative_play = publication_play()
    payload_fields = {
        key: authoritative_play[key]
        for key in (
            "prediction_id",
            "event_id",
            "market_key",
            "selection",
            "publication_price",
            "bookmaker_key",
            "market_no_vig_probability",
            "break_even_probability",
            "edge_percentage_points",
            "expected_value_per_unit_risk",
            "confidence_grade",
            "prediction_checksum",
            "evidence_checksum",
            "policy_version",
            "source_checksum",
            "published_at",
        )
    }
    values: dict[str, object] = {
        **authoritative_play,
        "gate_results_json": json.dumps(gate_results, sort_keys=True),
        "payload_json": json.dumps(
            {
                **payload_fields,
                "gate_results": gate_results,
                "prediction_snapshot": json.loads(
                    snapshots["prediction_payload_json"]
                ),
                "evidence_snapshot": json.loads(snapshots["evidence_json"]),
            },
            sort_keys=True,
        ),
    }
    if field == "gate_results_json":
        gate_results[0]["passed"] = False
        values[field] = json.dumps(gate_results, sort_keys=True)
    elif field == "payload_json":
        payload = json.loads(str(values[field]))
        payload["prediction_snapshot"]["home_probability"] = 0.99
        values[field] = json.dumps(payload, sort_keys=True)
    else:
        values[field] = tampered

    with pytest.raises(
        sqlite3.IntegrityError,
        match="does not match approved authoritative evaluation",
    ):
        with database.connect(write=True) as connection:
            connection.execute(
                """
                INSERT INTO publication_plays(
                    play_id, batch_id, policy_evaluation_id, prediction_id,
                    event_id, market_key, selection, publication_price,
                    bookmaker_key, market_no_vig_probability,
                    break_even_probability, edge_percentage_points,
                    expected_value_per_unit_risk, confidence_grade,
                    prediction_checksum, evidence_checksum, policy_version,
                    source_checksum, gate_results_json, payload_json, published_at
                ) VALUES (?, 'batch-1', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"play-bypass-{field}",
                    values["policy_evaluation_id"],
                    values["prediction_id"],
                    values["event_id"],
                    values["market_key"],
                    values["selection"],
                    values["publication_price"],
                    values["bookmaker_key"],
                    values["market_no_vig_probability"],
                    values["break_even_probability"],
                    values["edge_percentage_points"],
                    values["expected_value_per_unit_risk"],
                    values["confidence_grade"],
                    values["prediction_checksum"],
                    values["evidence_checksum"],
                    values["policy_version"],
                    values["source_checksum"],
                    values["gate_results_json"],
                    values["payload_json"],
                    values["published_at"],
                ),
            )
