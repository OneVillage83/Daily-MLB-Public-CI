from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from app.redaction import redact_text
from app.fielding_grain_migration import FORMAL_SCHEMA_V6_STATEMENTS


CURRENT_SCHEMA_VERSION = 8
MIGRATION_V1_NAME = "formal_phase1_schema"
MIGRATION_V2_NAME = "phase1_odds_history_and_freshness"
MIGRATION_V3_NAME = "release_candidate_evidence_ledger"
MIGRATION_V4_NAME = "release_candidate_policy_enforcement"
MIGRATION_V5_NAME = "mlb_stats_persistence_foundation"
MIGRATION_V6_NAME = "retrosheet_fielding_source_row_grain"
MIGRATION_V7_NAME = "manual_pipeline_run_controller"
MIGRATION_V8_NAME = "daily_slate_v1_temporal_persistence"

DSE_MLB_ML_CANDIDATE_V1_GATE_CODES = (
    "prediction_valid",
    "market_independent",
    "identity_valid",
    "market_supported",
    "minimum_book_count",
    "odds_fresh",
    "best_price_eligible",
    "data_quality_clear",
    "minimum_edge",
    "minimum_ev",
    "uncertainty_clears_market",
    "minimum_confidence",
    "weather_gate_clear",
    "phase2_live_weather_accepted",
    "event_pregame",
)
_DSE_MLB_ML_CANDIDATE_V1_GATE_SQL = ", ".join(
    f"'{code}'" for code in DSE_MLB_ML_CANDIDATE_V1_GATE_CODES
)

RUN_STATUSES = (
    "queued",
    "running",
    "completed",
    "completed_with_warnings",
    "failed",
)
FAILURE_STAGES = (
    "before_worker_start",
    "startup_reconciliation",
    "worker_execution",
    "collector",
    "persistence",
    "artifact_generation",
)

_RUN_STATUS_SQL = ", ".join(f"'{value}'" for value in RUN_STATUSES)
_FAILURE_STAGE_SQL = ", ".join(f"'{value}'" for value in FAILURE_STAGES)
PIPELINE_RUN_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "succeeded_with_warnings",
    "degraded",
    "failed",
)
PIPELINE_PHASE_STATUSES = (
    "pending",
    "running",
    "succeeded",
    "succeeded_with_warnings",
    "degraded",
    "failed",
    "skipped",
    "reused",
)
PIPELINE_PHASE_KEYS = (
    "daily_slate",
    "game_state",
    "baseball_intelligence_assembly",
    "odds_weather",
    "data_quality",
    "matchup_packet",
    "model_feature_set",
    "predictions",
    "value_engine",
    "recommendation_gate",
    "rankings",
    "pdf_report",
    "infographic",
    "final_qc",
    "human_review",
)
PIPELINE_TRANSITION_TYPES = (
    "created",
    "state_transition",
    "resume",
    "retry",
    "reuse",
    "forced_refresh",
)
_PIPELINE_RUN_STATUS_SQL = ", ".join(
    f"'{value}'" for value in PIPELINE_RUN_STATUSES
)
_PIPELINE_PHASE_STATUS_SQL = ", ".join(
    f"'{value}'" for value in PIPELINE_PHASE_STATUSES
)
_PIPELINE_PHASE_KEY_SQL = ", ".join(f"'{value}'" for value in PIPELINE_PHASE_KEYS)
_PIPELINE_TRANSITION_TYPE_SQL = ", ".join(
    f"'{value}'" for value in PIPELINE_TRANSITION_TYPES
)
ODDS_FRESHNESS_STATUSES = ("fresh", "aging", "stale", "unknown")
_ODDS_FRESHNESS_SQL = ", ".join(
    f"'{value}'" for value in ODDS_FRESHNESS_STATUSES
)
ODDS_WARNING_CODES = (
    "unknown_team",
    "malformed_event",
    "malformed_bookmaker",
    "malformed_market",
    "malformed_outcome",
    "unsupported_market",
    "incomplete_two_way_market",
    "invalid_spread_pair",
    "mismatched_total_pair",
    "missing_provider_timestamp",
    "stale_market",
    "insufficient_bookmakers",
    "primary_line_tie",
    "duplicate_normalized_offer",
    "invalid_provider_timestamp",
    "future_provider_timestamp",
)
_ODDS_WARNING_SQL = ", ".join(f"'{value}'" for value in ODDS_WARNING_CODES)


class MigrationError(RuntimeError):
    """Base class for schema initialization failures."""


class UnknownSchemaError(MigrationError):
    pass


class NewerSchemaVersionError(MigrationError):
    pass


class MigrationChecksumError(MigrationError):
    pass


class SchemaVerificationError(MigrationError):
    pass


class BackupVerificationError(MigrationError):
    pass


class DiagnosticWriteError(MigrationError):
    pass


@dataclass(frozen=True)
class MigrationResult:
    version: int
    schema_fingerprint: str
    migrated: bool
    source_kind: str
    backup_path: Path | None = None
    diagnostic_path: Path | None = None


# This is the exact unversioned schema shipped by the starter repository. An
# unversioned database must match its fingerprint before it may be upgraded.
LEGACY_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS collector_runs (
    run_id TEXT PRIMARY KEY,
    requested_date TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_message TEXT,
    artifact_zip TEXT
);

CREATE TABLE IF NOT EXISTS games (
    event_id TEXT PRIMARY KEY,
    sport_key TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    home_team_key TEXT,
    away_team_key TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    bookmaker_key TEXT NOT NULL,
    bookmaker_title TEXT,
    market_key TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    price REAL,
    point REAL,
    bookmaker_last_update TEXT,
    retrieved_at TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES collector_runs(run_id),
    FOREIGN KEY(event_id) REFERENCES games(event_id)
);

CREATE INDEX IF NOT EXISTS idx_odds_event_time ON odds_snapshots(event_id, retrieved_at);
CREATE INDEX IF NOT EXISTS idx_odds_run ON odds_snapshots(run_id);

CREATE TABLE IF NOT EXISTS weather_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    forecast_time TEXT,
    temperature_f REAL,
    humidity_pct REAL,
    precipitation_probability_pct REAL,
    wind_speed_mph REAL,
    wind_direction_deg REAL,
    short_forecast TEXT,
    raw_json TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES collector_runs(run_id),
    FOREIGN KEY(event_id) REFERENCES games(event_id)
);

CREATE INDEX IF NOT EXISTS idx_weather_event_time ON weather_snapshots(event_id, retrieved_at);

CREATE TABLE IF NOT EXISTS collector_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    event_id TEXT,
    message TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES collector_runs(run_id)
);
"""


FORMAL_SCHEMA_V1_STATEMENTS = (
    """
    CREATE TABLE schema_migrations (
        version INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        checksum TEXT NOT NULL,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE migration_diagnostics (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        migration_version INTEGER NOT NULL,
        source_kind TEXT NOT NULL CHECK (source_kind IN ('empty', 'legacy_inline')),
        source_fingerprint TEXT NOT NULL,
        target_fingerprint TEXT NOT NULL,
        backup_path TEXT,
        diagnostic_filename TEXT NOT NULL,
        started_at TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'completed'),
        details_json TEXT NOT NULL,
        FOREIGN KEY(migration_version) REFERENCES schema_migrations(version)
    )
    """,
    f"""
    CREATE TABLE collector_runs (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version = 1),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    f"""
    CREATE TABLE collector_run_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        from_status TEXT CHECK (from_status IN ({_RUN_STATUS_SQL})),
        to_status TEXT NOT NULL CHECK (to_status IN ({_RUN_STATUS_SQL})),
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        transitioned_at TEXT NOT NULL,
        CHECK (
            (to_status = 'failed' AND failure_stage IS NOT NULL)
            OR (to_status <> 'failed' AND failure_stage IS NULL)
        ),
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_run_transitions_run_time
    ON collector_run_transitions(run_id, transitioned_at, id)
    """,
    """
    CREATE TABLE games (
        event_id TEXT PRIMARY KEY,
        sport_key TEXT NOT NULL,
        commence_time TEXT NOT NULL,
        home_team TEXT NOT NULL,
        away_team TEXT NOT NULL,
        home_team_key TEXT,
        away_team_key TEXT,
        raw_json TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE run_games (
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        associated_at TEXT NOT NULL,
        PRIMARY KEY(run_id, event_id),
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(event_id) REFERENCES games(event_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_run_games_event ON run_games(event_id, run_id)
    """,
    """
    CREATE TABLE raw_provider_payloads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_id TEXT,
        provider TEXT NOT NULL,
        endpoint_category TEXT NOT NULL,
        provider_timestamp TEXT,
        retrieved_at TEXT NOT NULL,
        content_type TEXT NOT NULL,
        checksum_sha256 TEXT NOT NULL,
        artifact_relpath TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id, event_id) REFERENCES run_games(run_id, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_raw_payloads_run_provider
    ON raw_provider_payloads(run_id, provider, retrieved_at)
    """,
    """
    CREATE INDEX idx_raw_payloads_run_event
    ON raw_provider_payloads(run_id, event_id, retrieved_at)
    """,
    """
    CREATE TABLE odds_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        bookmaker_key TEXT NOT NULL,
        bookmaker_title TEXT,
        market_key TEXT NOT NULL,
        market_last_update TEXT,
        outcome_name TEXT NOT NULL,
        price REAL,
        point REAL,
        bookmaker_last_update TEXT,
        retrieved_at TEXT NOT NULL,
        raw_json TEXT NOT NULL,
        FOREIGN KEY(run_id, event_id) REFERENCES run_games(run_id, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_odds_event_time
    ON odds_snapshots(event_id, retrieved_at)
    """,
    """
    CREATE INDEX idx_odds_run ON odds_snapshots(run_id)
    """,
    """
    CREATE TABLE weather_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        forecast_time TEXT,
        temperature_f REAL,
        humidity_pct REAL,
        precipitation_probability_pct REAL,
        wind_speed_mph REAL,
        wind_direction_deg REAL,
        short_forecast TEXT,
        raw_json TEXT NOT NULL,
        retrieved_at TEXT NOT NULL,
        FOREIGN KEY(run_id, event_id) REFERENCES run_games(run_id, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_weather_event_time
    ON weather_snapshots(event_id, retrieved_at)
    """,
    """
    CREATE INDEX idx_weather_run ON weather_snapshots(run_id)
    """,
    """
    CREATE TABLE collector_errors (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        provider TEXT,
        event_id TEXT,
        message TEXT NOT NULL,
        details TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id, event_id) REFERENCES run_games(run_id, event_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_collector_errors_run
    ON collector_errors(run_id, created_at)
    """,
)


# SQLite cannot alter a CHECK constraint in place. Rebuilding collector_runs is
# required because schema_version was constrained to 1 in the immutable v1
# migration and new runs must record schema version 2.
FORMAL_SCHEMA_V2_STATEMENTS = (
    f"""
    CREATE TABLE _collector_runs_v2 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2)),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    INSERT INTO _collector_runs_v2(
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    )
    SELECT
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    FROM collector_runs
    """,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v2 RENAME TO collector_runs",
    """
    ALTER TABLE odds_snapshots ADD COLUMN provider_age_seconds REAL
        CHECK (provider_age_seconds IS NULL OR provider_age_seconds >= 0)
    """,
    """
    ALTER TABLE odds_snapshots ADD COLUMN bookmaker_age_seconds REAL
        CHECK (bookmaker_age_seconds IS NULL OR bookmaker_age_seconds >= 0)
    """,
    """
    ALTER TABLE odds_snapshots ADD COLUMN market_age_seconds REAL
        CHECK (market_age_seconds IS NULL OR market_age_seconds >= 0)
    """,
    f"""
    ALTER TABLE odds_snapshots
    ADD COLUMN freshness_status TEXT NOT NULL DEFAULT 'unknown'
        CHECK (freshness_status IN ({_ODDS_FRESHNESS_SQL}))
    """,
    f"""
    CREATE TABLE odds_normalization_warnings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        event_id TEXT,
        bookmaker_key TEXT,
        market_key TEXT,
        code TEXT NOT NULL CHECK (code IN ({_ODDS_WARNING_SQL})),
        message TEXT NOT NULL CHECK (length(trim(message)) > 0),
        created_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_odds_warnings_run_event
    ON odds_normalization_warnings(run_id, event_id, created_at, id)
    """,
)


_CHECKSUM_SQL = "length({column}) = 64 AND {column} NOT GLOB '*[^0-9a-f]*'"


def _immutable_triggers(table_name: str) -> tuple[str, str]:
    return (
        f"""
        CREATE TRIGGER {table_name}_reject_update
        BEFORE UPDATE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, '{table_name} records are immutable');
        END
        """,
        f"""
        CREATE TRIGGER {table_name}_reject_delete
        BEFORE DELETE ON {table_name}
        BEGIN
            SELECT RAISE(ABORT, '{table_name} records are immutable');
        END
        """,
    )


def _raw_payload_run_trigger(table_name: str) -> str:
    return f"""
        CREATE TRIGGER {table_name}_validate_raw_run
        BEFORE INSERT ON {table_name}
        WHEN NEW.raw_payload_id IS NOT NULL
        BEGIN
            SELECT CASE WHEN NOT EXISTS (
                SELECT 1 FROM stats_raw_payload_metadata AS raw
                WHERE raw.raw_payload_id = NEW.raw_payload_id
                  AND raw.stats_run_id = NEW.stats_run_id
            ) THEN RAISE(
                ABORT, '{table_name} raw payload belongs to another run'
            ) END;
        END
        """


# Release-candidate evidence is append-only. Rebuilding collector_runs only widens
# its schema-version check; all previously applied migration statements remain
# unchanged and retain their original checksums.
FORMAL_SCHEMA_V3_STATEMENTS = (
    f"""
    CREATE TABLE _collector_runs_v3 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2, 3)),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    INSERT INTO _collector_runs_v3(
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    )
    SELECT
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    FROM collector_runs
    """,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v3 RENAME TO collector_runs",
    f"""
    CREATE TABLE reviewed_prediction_evidence (
        evidence_id TEXT PRIMARY KEY CHECK (length(trim(evidence_id)) > 0),
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        contract_version TEXT NOT NULL CHECK (length(trim(contract_version)) > 0),
        analyst_id TEXT NOT NULL CHECK (length(trim(analyst_id)) > 0),
        method_version TEXT NOT NULL CHECK (length(trim(method_version)) > 0),
        evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json)),
        evidence_checksum TEXT NOT NULL UNIQUE CHECK (
            {_CHECKSUM_SQL.format(column='evidence_checksum')}
        ),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        FOREIGN KEY(run_id, event_id)
            REFERENCES run_games(run_id, event_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE reviewed_predictions (
        prediction_id TEXT PRIMARY KEY CHECK (length(trim(prediction_id)) > 0),
        evidence_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        contract_version TEXT NOT NULL CHECK (length(trim(contract_version)) > 0),
        analyst_id TEXT NOT NULL CHECK (length(trim(analyst_id)) > 0),
        method_version TEXT NOT NULL CHECK (length(trim(method_version)) > 0),
        home_probability REAL NOT NULL CHECK (home_probability BETWEEN 0 AND 1),
        away_probability REAL NOT NULL CHECK (away_probability BETWEEN 0 AND 1),
        lower_bound REAL NOT NULL CHECK (lower_bound BETWEEN 0 AND 1),
        upper_bound REAL NOT NULL CHECK (upper_bound BETWEEN 0 AND 1),
        feature_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='feature_checksum')}
        ),
        evidence_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='evidence_checksum')}
        ),
        prediction_checksum TEXT NOT NULL UNIQUE CHECK (
            {_CHECKSUM_SQL.format(column='prediction_checksum')}
        ),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        sealed_at TEXT NOT NULL CHECK (length(trim(sealed_at)) > 0),
        CHECK (abs((home_probability + away_probability) - 1.0) <= 0.000000001),
        CHECK (lower_bound <= home_probability AND home_probability <= upper_bound),
        FOREIGN KEY(evidence_id) REFERENCES reviewed_prediction_evidence(evidence_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(run_id, event_id)
            REFERENCES run_games(run_id, event_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER reviewed_predictions_validate_evidence
    BEFORE INSERT ON reviewed_predictions
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM reviewed_prediction_evidence AS evidence
            WHERE evidence.evidence_id = NEW.evidence_id
              AND evidence.run_id = NEW.run_id
              AND evidence.event_id = NEW.event_id
              AND evidence.analyst_id = NEW.analyst_id
              AND evidence.contract_version = NEW.contract_version
              AND evidence.method_version = NEW.method_version
              AND evidence.evidence_checksum = NEW.evidence_checksum
        ) THEN RAISE(ABORT, 'prediction evidence identity mismatch') END;
    END
    """,
    """
    CREATE INDEX idx_reviewed_predictions_run_event
    ON reviewed_predictions(run_id, event_id, sealed_at)
    """,
    f"""
    CREATE TABLE market_evaluations (
        evaluation_id TEXT PRIMARY KEY CHECK (length(trim(evaluation_id)) > 0),
        prediction_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        market_snapshot_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='market_snapshot_checksum')}
        ),
        market_no_vig_probability REAL
            CHECK (market_no_vig_probability IS NULL OR market_no_vig_probability BETWEEN 0 AND 1),
        best_price REAL CHECK (best_price IS NULL OR best_price <> 0),
        best_price_books_json TEXT NOT NULL CHECK (json_valid(best_price_books_json)),
        break_even_probability REAL
            CHECK (break_even_probability IS NULL OR break_even_probability BETWEEN 0 AND 1),
        edge_percentage_points REAL,
        expected_value_per_unit_risk REAL,
        bookmaker_count INTEGER NOT NULL CHECK (bookmaker_count >= 0),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        evaluated_at TEXT NOT NULL CHECK (length(trim(evaluated_at)) > 0),
        FOREIGN KEY(prediction_id) REFERENCES reviewed_predictions(prediction_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(run_id, event_id)
            REFERENCES run_games(run_id, event_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER market_evaluations_validate_prediction
    BEFORE INSERT ON market_evaluations
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM reviewed_predictions AS prediction
            WHERE prediction.prediction_id = NEW.prediction_id
              AND prediction.run_id = NEW.run_id
              AND prediction.event_id = NEW.event_id
        ) THEN RAISE(ABORT, 'market evaluation prediction identity mismatch') END;
    END
    """,
    """
    CREATE INDEX idx_market_evaluations_run_event
    ON market_evaluations(run_id, event_id, evaluated_at)
    """,
    f"""
    CREATE TABLE policy_evaluations (
        policy_evaluation_id TEXT PRIMARY KEY
            CHECK (length(trim(policy_evaluation_id)) > 0),
        evaluation_id TEXT NOT NULL,
        prediction_id TEXT NOT NULL,
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        outcome TEXT NOT NULL
            CHECK (outcome IN ('PASS', 'CANDIDATE_REQUIRES_REVIEW')),
        all_gates_passed INTEGER NOT NULL CHECK (all_gates_passed IN (0, 1)),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        evaluated_at TEXT NOT NULL CHECK (length(trim(evaluated_at)) > 0),
        CHECK (
            (outcome = 'CANDIDATE_REQUIRES_REVIEW' AND all_gates_passed = 1)
            OR (outcome = 'PASS' AND all_gates_passed = 0)
        ),
        FOREIGN KEY(evaluation_id) REFERENCES market_evaluations(evaluation_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(prediction_id) REFERENCES reviewed_predictions(prediction_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER policy_evaluations_validate_sources
    BEFORE INSERT ON policy_evaluations
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM market_evaluations AS evaluation
            WHERE evaluation.evaluation_id = NEW.evaluation_id
              AND evaluation.prediction_id = NEW.prediction_id
              AND evaluation.policy_version = NEW.policy_version
        ) THEN RAISE(ABORT, 'policy evaluation source mismatch') END;
    END
    """,
    f"""
    CREATE TABLE policy_gate_results (
        gate_result_id TEXT PRIMARY KEY CHECK (length(trim(gate_result_id)) > 0),
        policy_evaluation_id TEXT NOT NULL,
        gate_code TEXT NOT NULL CHECK (length(trim(gate_code)) > 0),
        threshold_json TEXT NOT NULL CHECK (json_valid(threshold_json)),
        observed_json TEXT NOT NULL CHECK (json_valid(observed_json)),
        passed INTEGER NOT NULL CHECK (passed IN (0, 1)),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        evaluated_at TEXT NOT NULL CHECK (length(trim(evaluated_at)) > 0),
        UNIQUE(policy_evaluation_id, gate_code),
        FOREIGN KEY(policy_evaluation_id)
            REFERENCES policy_evaluations(policy_evaluation_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_policy_gate_results_evaluation
    ON policy_gate_results(policy_evaluation_id, gate_code)
    """,
    f"""
    CREATE TABLE card_drafts (
        draft_id TEXT PRIMARY KEY CHECK (length(trim(draft_id)) > 0),
        run_id TEXT NOT NULL,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        candidate_count INTEGER NOT NULL CHECK (candidate_count >= 0),
        draft_checksum TEXT NOT NULL UNIQUE CHECK (
            {_CHECKSUM_SQL.format(column='draft_checksum')}
        ),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE draft_policy_evaluations (
        draft_id TEXT NOT NULL,
        policy_evaluation_id TEXT NOT NULL,
        PRIMARY KEY(draft_id, policy_evaluation_id),
        FOREIGN KEY(draft_id) REFERENCES card_drafts(draft_id) ON DELETE RESTRICT,
        FOREIGN KEY(policy_evaluation_id)
            REFERENCES policy_evaluations(policy_evaluation_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER draft_policy_evaluations_validate_candidate
    BEFORE INSERT ON draft_policy_evaluations
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM card_drafts AS draft
            JOIN policy_evaluations AS policy
              ON policy.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN market_evaluations AS evaluation
              ON evaluation.evaluation_id = policy.evaluation_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = evaluation.run_id
              AND draft.policy_version = policy.policy_version
              AND policy.outcome = 'CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed = 1
        ) THEN RAISE(ABORT, 'draft may contain only eligible candidates') END;
    END
    """,
    f"""
    CREATE TABLE review_decisions (
        decision_id TEXT PRIMARY KEY CHECK (length(trim(decision_id)) > 0),
        draft_id TEXT NOT NULL,
        policy_evaluation_id TEXT NOT NULL,
        reviewer_id TEXT NOT NULL CHECK (length(trim(reviewer_id)) > 0),
        decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected_to_pass')),
        reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
        draft_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='draft_checksum')}
        ),
        prediction_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='prediction_checksum')}
        ),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        decided_at TEXT NOT NULL CHECK (length(trim(decided_at)) > 0),
        UNIQUE(draft_id, policy_evaluation_id),
        FOREIGN KEY(draft_id, policy_evaluation_id)
            REFERENCES draft_policy_evaluations(draft_id, policy_evaluation_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER review_decisions_validate_evidence
    BEFORE INSERT ON review_decisions
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM card_drafts AS draft
            JOIN policy_evaluations AS policy
              ON policy.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN reviewed_predictions AS prediction
              ON prediction.prediction_id = policy.prediction_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.draft_checksum = NEW.draft_checksum
              AND draft.policy_version = NEW.policy_version
              AND policy.policy_version = NEW.policy_version
              AND policy.outcome = 'CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed = 1
              AND prediction.prediction_checksum = NEW.prediction_checksum
        ) THEN RAISE(ABORT, 'review decision evidence mismatch') END;
    END
    """,
    f"""
    CREATE TABLE publication_batches (
        batch_id TEXT PRIMARY KEY CHECK (length(trim(batch_id)) > 0),
        draft_id TEXT NOT NULL UNIQUE,
        run_id TEXT NOT NULL,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        draft_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='draft_checksum')}
        ),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        reviewer_id TEXT NOT NULL CHECK (length(trim(reviewer_id)) > 0),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        published_at TEXT NOT NULL CHECK (length(trim(published_at)) > 0),
        FOREIGN KEY(draft_id) REFERENCES card_drafts(draft_id) ON DELETE RESTRICT,
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER publication_batches_validate_draft
    BEFORE INSERT ON publication_batches
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM card_drafts AS draft
            JOIN collector_runs AS run ON run.run_id = draft.run_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = NEW.run_id
              AND draft.requested_date = NEW.requested_date
              AND draft.policy_version = NEW.policy_version
              AND draft.draft_checksum = NEW.draft_checksum
              AND run.requested_date = NEW.requested_date
        ) THEN RAISE(ABORT, 'publication batch draft evidence mismatch') END;
    END
    """,
    f"""
    CREATE TABLE publication_plays (
        play_id TEXT PRIMARY KEY CHECK (length(trim(play_id)) > 0),
        batch_id TEXT NOT NULL,
        policy_evaluation_id TEXT NOT NULL UNIQUE,
        prediction_id TEXT NOT NULL,
        event_id TEXT NOT NULL,
        market_key TEXT NOT NULL CHECK (market_key = 'h2h'),
        selection TEXT NOT NULL CHECK (length(trim(selection)) > 0),
        publication_price REAL NOT NULL CHECK (publication_price <> 0),
        bookmaker_key TEXT NOT NULL CHECK (length(trim(bookmaker_key)) > 0),
        market_no_vig_probability REAL NOT NULL
            CHECK (market_no_vig_probability BETWEEN 0 AND 1),
        break_even_probability REAL NOT NULL CHECK (break_even_probability BETWEEN 0 AND 1),
        edge_percentage_points REAL NOT NULL,
        expected_value_per_unit_risk REAL NOT NULL,
        confidence_grade TEXT NOT NULL CHECK (confidence_grade IN ('A', 'B', 'C', 'D')),
        prediction_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='prediction_checksum')}
        ),
        evidence_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='evidence_checksum')}
        ),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        gate_results_json TEXT NOT NULL CHECK (json_valid(gate_results_json)),
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        published_at TEXT NOT NULL CHECK (length(trim(published_at)) > 0),
        FOREIGN KEY(batch_id) REFERENCES publication_batches(batch_id) ON DELETE RESTRICT,
        FOREIGN KEY(policy_evaluation_id)
            REFERENCES policy_evaluations(policy_evaluation_id) ON DELETE RESTRICT,
        FOREIGN KEY(prediction_id) REFERENCES reviewed_predictions(prediction_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(event_id) REFERENCES games(event_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER publication_plays_require_approval
    BEFORE INSERT ON publication_plays
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM publication_batches AS batch
            JOIN review_decisions AS decision
              ON decision.draft_id = batch.draft_id
             AND decision.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN policy_evaluations AS policy
              ON policy.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN reviewed_predictions AS prediction
              ON prediction.prediction_id = NEW.prediction_id
            WHERE batch.batch_id = NEW.batch_id
              AND decision.decision = 'approved'
              AND policy.outcome = 'CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed = 1
              AND decision.policy_version = NEW.policy_version
              AND decision.prediction_checksum = NEW.prediction_checksum
              AND prediction.prediction_checksum = NEW.prediction_checksum
              AND prediction.evidence_checksum = NEW.evidence_checksum
        ) THEN RAISE(ABORT, 'publication play requires approved candidate evidence') END;
    END
    """,
    f"""
    CREATE TABLE settlement_events (
        settlement_event_id TEXT PRIMARY KEY
            CHECK (length(trim(settlement_event_id)) > 0),
        play_id TEXT NOT NULL,
        event_kind TEXT NOT NULL CHECK (event_kind IN ('original', 'correction')),
        result TEXT NOT NULL CHECK (result IN ('win', 'loss', 'push', 'void')),
        corrects_event_id TEXT,
        settled_at TEXT NOT NULL CHECK (length(trim(settled_at)) > 0),
        recorded_at TEXT NOT NULL CHECK (length(trim(recorded_at)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            {_CHECKSUM_SQL.format(column='source_checksum')}
        ),
        notes TEXT,
        payload_json TEXT NOT NULL CHECK (json_valid(payload_json)),
        CHECK (
            (event_kind = 'original' AND corrects_event_id IS NULL)
            OR (event_kind = 'correction' AND corrects_event_id IS NOT NULL)
        ),
        FOREIGN KEY(play_id) REFERENCES publication_plays(play_id) ON DELETE RESTRICT,
        FOREIGN KEY(corrects_event_id) REFERENCES settlement_events(settlement_event_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX idx_settlement_one_original_per_play
    ON settlement_events(play_id) WHERE event_kind = 'original'
    """,
    """
    CREATE TRIGGER settlement_correction_same_play
    BEFORE INSERT ON settlement_events
    WHEN NEW.event_kind = 'correction'
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM settlement_events AS prior
            WHERE prior.settlement_event_id = NEW.corrects_event_id
              AND prior.play_id = NEW.play_id
        ) THEN RAISE(ABORT, 'settlement correction must reference the same play') END;
    END
    """,
    *_immutable_triggers("reviewed_prediction_evidence"),
    *_immutable_triggers("reviewed_predictions"),
    *_immutable_triggers("market_evaluations"),
    *_immutable_triggers("policy_evaluations"),
    *_immutable_triggers("policy_gate_results"),
    *_immutable_triggers("card_drafts"),
    *_immutable_triggers("draft_policy_evaluations"),
    *_immutable_triggers("review_decisions"),
    *_immutable_triggers("publication_batches"),
    *_immutable_triggers("publication_plays"),
    *_immutable_triggers("settlement_events"),
)


FORMAL_SCHEMA_V4_STATEMENTS = (
    "DROP TRIGGER publication_batches_validate_draft",
    f"""
    CREATE TABLE _collector_runs_v4 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2, 3, 4)),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    INSERT INTO _collector_runs_v4(
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    )
    SELECT
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    FROM collector_runs
    """,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v4 RENAME TO collector_runs",
    """
    CREATE TRIGGER publication_batches_validate_draft
    BEFORE INSERT ON publication_batches
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM card_drafts AS draft
            JOIN collector_runs AS run ON run.run_id = draft.run_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = NEW.run_id
              AND draft.requested_date = NEW.requested_date
              AND draft.policy_version = NEW.policy_version
              AND draft.draft_checksum = NEW.draft_checksum
              AND draft.source_checksum = NEW.source_checksum
              AND run.requested_date = NEW.requested_date
        ) THEN RAISE(ABORT, 'publication batch draft evidence mismatch') END;
    END
    """,
    "DROP TRIGGER draft_policy_evaluations_validate_candidate",
    f"""
    CREATE TRIGGER draft_policy_evaluations_validate_candidate
    BEFORE INSERT ON draft_policy_evaluations
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM card_drafts AS draft
            JOIN policy_evaluations AS policy
              ON policy.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN market_evaluations AS evaluation
              ON evaluation.evaluation_id = policy.evaluation_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = evaluation.run_id
              AND draft.policy_version = 'DSE_MLB_ML_CANDIDATE_V1'
              AND policy.policy_version = draft.policy_version
              AND policy.outcome = 'CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed = 1
              AND (
                  SELECT COUNT(*) FROM policy_gate_results AS gate
                  WHERE gate.policy_evaluation_id = policy.policy_evaluation_id
                    AND gate.passed = 1
                    AND gate.gate_code IN ({_DSE_MLB_ML_CANDIDATE_V1_GATE_SQL})
              ) = {len(DSE_MLB_ML_CANDIDATE_V1_GATE_CODES)}
              AND NOT EXISTS (
                  SELECT 1 FROM policy_gate_results AS gate
                  WHERE gate.policy_evaluation_id = policy.policy_evaluation_id
                    AND (
                        gate.passed <> 1
                        OR gate.gate_code NOT IN ({_DSE_MLB_ML_CANDIDATE_V1_GATE_SQL})
                    )
              )
        ) THEN RAISE(ABORT, 'draft candidate requires the exact passing policy gate set') END;
    END
    """,
    "DROP TRIGGER publication_plays_require_approval",
    """
    CREATE TRIGGER publication_plays_require_approval
    BEFORE INSERT ON publication_plays
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM publication_batches AS batch
            JOIN review_decisions AS decision
              ON decision.draft_id = batch.draft_id
             AND decision.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN policy_evaluations AS policy
              ON policy.policy_evaluation_id = NEW.policy_evaluation_id
            JOIN market_evaluations AS evaluation
              ON evaluation.evaluation_id = policy.evaluation_id
            JOIN reviewed_predictions AS prediction
              ON prediction.prediction_id = policy.prediction_id
            JOIN reviewed_prediction_evidence AS evidence
              ON evidence.evidence_id = prediction.evidence_id
            WHERE batch.batch_id = NEW.batch_id
              AND decision.decision = 'approved'
              AND decision.reviewer_id = batch.reviewer_id
              AND policy.outcome = 'CANDIDATE_REQUIRES_REVIEW'
              AND policy.all_gates_passed = 1
              AND evaluation.event_id = NEW.event_id
              AND policy.prediction_id = NEW.prediction_id
              AND json_extract(policy.payload_json, '$.selected_team_key') = NEW.selection
              AND evaluation.best_price = NEW.publication_price
              AND EXISTS (
                  SELECT 1 FROM json_each(evaluation.best_price_books_json)
                  WHERE CAST(json_each.value AS TEXT) = NEW.bookmaker_key
              )
              AND evaluation.market_no_vig_probability = NEW.market_no_vig_probability
              AND evaluation.break_even_probability = NEW.break_even_probability
              AND evaluation.edge_percentage_points = NEW.edge_percentage_points
              AND evaluation.expected_value_per_unit_risk = NEW.expected_value_per_unit_risk
              AND json_extract(policy.payload_json, '$.confidence_grade') = NEW.confidence_grade
              AND policy.policy_version = NEW.policy_version
              AND decision.policy_version = NEW.policy_version
              AND batch.policy_version = NEW.policy_version
              AND decision.prediction_checksum = NEW.prediction_checksum
              AND prediction.prediction_checksum = NEW.prediction_checksum
              AND prediction.evidence_checksum = NEW.evidence_checksum
              AND json(json_extract(NEW.payload_json, '$.prediction_snapshot')) =
                  json(prediction.payload_json)
              AND json(json_extract(NEW.payload_json, '$.evidence_snapshot')) =
                  json(evidence.evidence_json)
              AND json_extract(NEW.payload_json, '$.event_id') = NEW.event_id
              AND json_extract(NEW.payload_json, '$.prediction_id') = NEW.prediction_id
              AND json_extract(NEW.payload_json, '$.selection') = NEW.selection
              AND json_extract(NEW.payload_json, '$.publication_price') =
                  NEW.publication_price
              AND json_extract(NEW.payload_json, '$.bookmaker_key') = NEW.bookmaker_key
              AND json_extract(NEW.payload_json, '$.market_no_vig_probability') =
                  NEW.market_no_vig_probability
              AND json_extract(NEW.payload_json, '$.break_even_probability') =
                  NEW.break_even_probability
              AND json_extract(NEW.payload_json, '$.edge_percentage_points') =
                  NEW.edge_percentage_points
              AND json_extract(NEW.payload_json, '$.expected_value_per_unit_risk') =
                  NEW.expected_value_per_unit_risk
              AND json_extract(NEW.payload_json, '$.confidence_grade') =
                  NEW.confidence_grade
              AND json_extract(NEW.payload_json, '$.policy_version') = NEW.policy_version
              AND json(json_extract(NEW.payload_json, '$.gate_results')) =
                  json(NEW.gate_results_json)
              AND decision.source_checksum = NEW.source_checksum
              AND json_array_length(NEW.gate_results_json) = (
                  SELECT COUNT(*) FROM policy_gate_results AS gate
                  WHERE gate.policy_evaluation_id = policy.policy_evaluation_id
              )
              AND NOT EXISTS (
                  SELECT 1 FROM policy_gate_results AS gate
                  WHERE gate.policy_evaluation_id = policy.policy_evaluation_id
                    AND NOT EXISTS (
                        SELECT 1 FROM json_each(NEW.gate_results_json) AS item
                        WHERE json_extract(item.value, '$.gate_code') = gate.gate_code
                          AND json_extract(item.value, '$.passed') = gate.passed
                          AND json_extract(item.value, '$.reason') = gate.reason
                          AND json_extract(item.value, '$.source_checksum') = gate.source_checksum
                          AND json_extract(item.value, '$.evaluated_at') = gate.evaluated_at
                          AND json_extract(item.value, '$.policy_version') = policy.policy_version
                          AND json_extract(item.value, '$.threshold') IS
                              json_extract(gate.threshold_json, '$')
                          AND json_extract(item.value, '$.observed') IS
                              json_extract(gate.observed_json, '$')
                    )
              )
        ) THEN RAISE(ABORT, 'publication play does not match approved authoritative evaluation') END;
    END
    """,
)


# The stats foundation is additive except for widening collector_runs.schema_version.
# Every observation/snapshot table is append-only; mutable run, checkpoint, identity,
# and watermark tables have explicit state or monotonicity constraints.
FORMAL_SCHEMA_V5_STATEMENTS = (
    "DROP TRIGGER publication_batches_validate_draft",
    f"""
    CREATE TABLE _collector_runs_v5 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version IN (1, 2, 3, 4, 5)),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    INSERT INTO _collector_runs_v5(
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    )
    SELECT
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    FROM collector_runs
    """,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v5 RENAME TO collector_runs",
    """
    CREATE TRIGGER publication_batches_validate_draft
    BEFORE INSERT ON publication_batches
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM card_drafts AS draft
            JOIN collector_runs AS run ON run.run_id = draft.run_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = NEW.run_id
              AND draft.requested_date = NEW.requested_date
              AND draft.policy_version = NEW.policy_version
              AND draft.draft_checksum = NEW.draft_checksum
              AND draft.source_checksum = NEW.source_checksum
              AND run.requested_date = NEW.requested_date
        ) THEN RAISE(ABORT, 'publication batch draft evidence mismatch') END;
    END
    """,
    """
    CREATE TABLE stats_ingestion_runs (
        stats_run_id TEXT PRIMARY KEY CHECK (length(trim(stats_run_id)) > 0),
        run_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        source_version TEXT NOT NULL CHECK (length(trim(source_version)) > 0),
        adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
        scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
        requested_through_date TEXT NOT NULL CHECK (
            requested_through_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        source_observed_at TEXT,
        latest_ingested_completed_game_date TEXT CHECK (
            latest_ingested_completed_game_date IS NULL OR
            latest_ingested_completed_game_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        contiguous_regular_season_complete_through_date TEXT CHECK (
            contiguous_regular_season_complete_through_date IS NULL OR
            contiguous_regular_season_complete_through_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        partial_date TEXT CHECK (
            partial_date IS NULL OR partial_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        partial_date_reason TEXT,
        status TEXT NOT NULL CHECK (
            status IN ('queued', 'running', 'completed', 'completed_with_warnings', 'failed')
        ),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
        failure_stage TEXT,
        error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
        configuration_checksum TEXT NOT NULL CHECK (
            length(configuration_checksum) = 64
            AND configuration_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL
                AND source_observed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL
                AND length(trim(failure_stage)) > 0)
        ),
        CHECK ((status = 'failed') OR failure_stage IS NULL),
        CHECK (
            (partial_date IS NULL AND partial_date_reason IS NULL)
            OR (partial_date IS NOT NULL AND length(trim(partial_date_reason)) > 0)
        ),
        CHECK (
            latest_ingested_completed_game_date IS NULL
            OR latest_ingested_completed_game_date <= requested_through_date
        ),
        CHECK (
            contiguous_regular_season_complete_through_date IS NULL
            OR contiguous_regular_season_complete_through_date <= requested_through_date
        ),
        CHECK (partial_date IS NULL OR partial_date <= requested_through_date),
        FOREIGN KEY(run_id) REFERENCES collector_runs(run_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_ingestion_runs_run
    ON stats_ingestion_runs(run_id, provider, created_at)
    """,
    """
    CREATE TRIGGER stats_ingestion_runs_validate_transition
    BEFORE UPDATE OF status ON stats_ingestion_runs
    WHEN NOT (
        OLD.status = NEW.status
        OR (OLD.status = 'queued' AND NEW.status IN ('running', 'failed'))
        OR (OLD.status = 'running'
            AND NEW.status IN ('completed', 'completed_with_warnings', 'failed'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid stats ingestion run transition');
    END
    """,
    """
    CREATE TRIGGER stats_ingestion_runs_preserve_provenance
    BEFORE UPDATE OF
        run_id, provider, source_version, adapter_version, scope_key,
        requested_through_date, configuration_checksum
    ON stats_ingestion_runs
    WHEN OLD.run_id <> NEW.run_id
      OR OLD.provider <> NEW.provider
      OR OLD.source_version <> NEW.source_version
      OR OLD.adapter_version <> NEW.adapter_version
      OR OLD.scope_key <> NEW.scope_key
      OR OLD.requested_through_date <> NEW.requested_through_date
      OR OLD.configuration_checksum <> NEW.configuration_checksum
    BEGIN
        SELECT RAISE(ABORT, 'stats ingestion provenance is immutable');
    END
    """,
    """
    CREATE TRIGGER stats_ingestion_runs_prevent_delete
    BEFORE DELETE ON stats_ingestion_runs
    BEGIN
        SELECT RAISE(ABORT, 'stats ingestion run evidence is immutable');
    END
    """,
    """
    CREATE TABLE stats_checkpoints (
        checkpoint_id TEXT PRIMARY KEY CHECK (length(trim(checkpoint_id)) > 0),
        stats_run_id TEXT NOT NULL,
        dataset_key TEXT NOT NULL CHECK (length(trim(dataset_key)) > 0),
        scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
        status TEXT NOT NULL CHECK (
            status IN ('pending', 'running', 'completed', 'partial', 'failed')
        ),
        cursor_before_json TEXT CHECK (cursor_before_json IS NULL OR json_valid(cursor_before_json)),
        cursor_after_json TEXT CHECK (cursor_after_json IS NULL OR json_valid(cursor_after_json)),
        observed_through TEXT,
        complete_through TEXT,
        records_seen INTEGER NOT NULL DEFAULT 0 CHECK (records_seen >= 0),
        records_persisted INTEGER NOT NULL DEFAULT 0 CHECK (records_persisted >= 0),
        records_rejected INTEGER NOT NULL DEFAULT 0 CHECK (records_rejected >= 0),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
        error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
        CHECK (
            (status = 'pending' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'partial', 'failed')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
        ),
        CHECK (complete_through IS NULL OR observed_through IS NOT NULL),
        CHECK (complete_through IS NULL OR complete_through <= observed_through),
        UNIQUE(stats_run_id, dataset_key, scope_key),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE CASCADE
    )
    """,
    """
    CREATE INDEX idx_stats_checkpoints_run_status
    ON stats_checkpoints(stats_run_id, status, dataset_key)
    """,
    """
    CREATE TRIGGER stats_checkpoints_validate_transition
    BEFORE UPDATE OF status ON stats_checkpoints
    WHEN NOT (
        OLD.status = NEW.status
        OR (OLD.status = 'pending' AND NEW.status IN ('running', 'failed'))
        OR (OLD.status = 'running' AND NEW.status IN ('completed', 'partial', 'failed'))
    )
    BEGIN
        SELECT RAISE(ABORT, 'invalid stats checkpoint transition');
    END
    """,
    """
    CREATE TRIGGER stats_checkpoints_prevent_delete
    BEFORE DELETE ON stats_checkpoints
    BEGIN
        SELECT RAISE(ABORT, 'stats checkpoint evidence is immutable');
    END
    """,
    """
    CREATE TABLE stats_raw_payload_metadata (
        raw_payload_id TEXT PRIMARY KEY CHECK (length(trim(raw_payload_id)) > 0),
        stats_run_id TEXT NOT NULL,
        checkpoint_id TEXT,
        phase1_raw_payload_id INTEGER,
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        endpoint_category TEXT NOT NULL CHECK (length(trim(endpoint_category)) > 0),
        source_capture_id TEXT NOT NULL CHECK (length(trim(source_capture_id)) > 0),
        provider_record_id TEXT,
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        content_type TEXT NOT NULL CHECK (length(trim(content_type)) > 0),
        checksum_sha256 TEXT NOT NULL CHECK (
            length(checksum_sha256) = 64
            AND checksum_sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        artifact_relpath TEXT NOT NULL CHECK (length(trim(artifact_relpath)) > 0),
        metadata_json TEXT NOT NULL CHECK (json_valid(metadata_json)),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(checkpoint_id) REFERENCES stats_checkpoints(checkpoint_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(phase1_raw_payload_id) REFERENCES raw_provider_payloads(id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_raw_payload_run
    ON stats_raw_payload_metadata(stats_run_id, provider, retrieved_at)
    """,
    """
    CREATE UNIQUE INDEX uq_stats_raw_payload_capture
    ON stats_raw_payload_metadata(
        stats_run_id, provider, endpoint_category, source_capture_id
    )
    """,
    """
    CREATE TRIGGER stats_raw_payload_metadata_validate_checkpoint_run
    BEFORE INSERT ON stats_raw_payload_metadata
    WHEN NEW.checkpoint_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_checkpoints AS checkpoint
            WHERE checkpoint.checkpoint_id = NEW.checkpoint_id
              AND checkpoint.stats_run_id = NEW.stats_run_id
        ) THEN RAISE(
            ABORT, 'stats raw payload checkpoint belongs to another run'
        ) END;
    END
    """,
    """
    CREATE TABLE stats_excluded_source_rows (
        excluded_row_id TEXT PRIMARY KEY CHECK (length(trim(excluded_row_id)) > 0),
        stats_run_id TEXT NOT NULL,
        raw_payload_id TEXT NOT NULL,
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        dataset_key TEXT NOT NULL CHECK (length(trim(dataset_key)) > 0),
        source_row_id TEXT NOT NULL CHECK (length(trim(source_row_id)) > 0),
        classification TEXT NOT NULL CHECK (
            classification IN (
                'non_regular_season', 'postseason', 'exhibition', 'all_star',
                'home_run_derby', 'futures_game', 'spring_training',
                'future', 'partial', 'in_progress', 'postponed', 'suspended',
                'cancelled', 'abandoned', 'source_incomplete',
                'validation_failed', 'malformed', 'unsupported'
            )
        ),
        reason_code TEXT NOT NULL CHECK (length(trim(reason_code)) > 0),
        source_effective_date TEXT CHECK (
            source_effective_date IS NULL OR source_effective_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        source_row_checksum TEXT NOT NULL CHECK (
            length(source_row_checksum) = 64
            AND source_row_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        UNIQUE(
            raw_payload_id, provider, dataset_key, source_row_id, source_row_checksum,
            classification, reason_code
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_excluded_source_lookup
    ON stats_excluded_source_rows(
        provider, dataset_key, classification, source_effective_date, source_row_id
    )
    """,
    """
    CREATE TRIGGER stats_excluded_source_rows_validate_raw_run
    BEFORE INSERT ON stats_excluded_source_rows
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_raw_payload_metadata AS raw
            WHERE raw.raw_payload_id = NEW.raw_payload_id
              AND raw.stats_run_id = NEW.stats_run_id
              AND raw.provider = NEW.provider
        ) THEN RAISE(ABORT, 'excluded source row raw payload belongs to another run') END;
    END
    """,
    """
    CREATE TABLE stats_team_identities (
        team_identity_id TEXT PRIMARY KEY CHECK (length(trim(team_identity_id)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        provider_team_id TEXT NOT NULL CHECK (length(trim(provider_team_id)) > 0),
        canonical_team_key TEXT,
        current_name TEXT NOT NULL CHECK (length(trim(current_name)) > 0),
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
        last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
        identity_checksum TEXT NOT NULL CHECK (
            length(identity_checksum) = 64
            AND identity_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (first_seen_at <= last_seen_at),
        UNIQUE(provider, provider_team_id)
    )
    """,
    """
    CREATE INDEX idx_stats_team_canonical
    ON stats_team_identities(canonical_team_key, provider)
    """,
    """
    CREATE TRIGGER stats_team_identities_preserve_facts
    BEFORE UPDATE ON stats_team_identities
    WHEN OLD.team_identity_id IS NOT NEW.team_identity_id
      OR OLD.provider IS NOT NEW.provider
      OR OLD.provider_team_id IS NOT NEW.provider_team_id
      OR OLD.canonical_team_key IS NOT NEW.canonical_team_key
      OR OLD.current_name IS NOT NEW.current_name
      OR OLD.active IS NOT NEW.active
      OR OLD.first_seen_at IS NOT NEW.first_seen_at
      OR OLD.identity_checksum IS NOT NEW.identity_checksum
      OR NEW.last_seen_at < OLD.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'team identity facts are immutable');
    END
    """,
    """
    CREATE TABLE stats_player_identities (
        player_identity_id TEXT PRIMARY KEY CHECK (length(trim(player_identity_id)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        provider_player_id TEXT NOT NULL CHECK (length(trim(provider_player_id)) > 0),
        full_name TEXT NOT NULL CHECK (length(trim(full_name)) > 0),
        primary_position TEXT,
        bats TEXT,
        throws TEXT,
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
        last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
        identity_checksum TEXT NOT NULL CHECK (
            length(identity_checksum) = 64
            AND identity_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (first_seen_at <= last_seen_at),
        UNIQUE(provider, provider_player_id)
    )
    """,
    """
    CREATE TRIGGER stats_player_identities_preserve_facts
    BEFORE UPDATE ON stats_player_identities
    WHEN OLD.player_identity_id IS NOT NEW.player_identity_id
      OR OLD.provider IS NOT NEW.provider
      OR OLD.provider_player_id IS NOT NEW.provider_player_id
      OR OLD.full_name IS NOT NEW.full_name
      OR OLD.primary_position IS NOT NEW.primary_position
      OR OLD.bats IS NOT NEW.bats
      OR OLD.throws IS NOT NEW.throws
      OR OLD.active IS NOT NEW.active
      OR OLD.first_seen_at IS NOT NEW.first_seen_at
      OR OLD.identity_checksum IS NOT NEW.identity_checksum
      OR NEW.last_seen_at < OLD.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'player identity facts are immutable');
    END
    """,
    """
    CREATE TABLE stats_canonical_players (
        canonical_player_id TEXT PRIMARY KEY CHECK (
            length(trim(canonical_player_id)) > 0
        ),
        created_stats_run_id TEXT NOT NULL,
        display_name TEXT NOT NULL CHECK (length(trim(display_name)) > 0),
        birth_date TEXT CHECK (
            birth_date IS NULL OR birth_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        canonical_checksum TEXT NOT NULL CHECK (
            length(canonical_checksum) = 64
            AND canonical_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(created_stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE stats_player_identifier_mappings (
        mapping_id TEXT PRIMARY KEY CHECK (length(trim(mapping_id)) > 0),
        stats_run_id TEXT NOT NULL,
        canonical_player_id TEXT NOT NULL,
        player_identity_id TEXT NOT NULL,
        mapping_method TEXT NOT NULL CHECK (
            mapping_method IN (
                'source_declared', 'exact_id', 'pybaseball_lookup', 'manual_review'
            )
        ),
        verification_status TEXT NOT NULL CHECK (
            verification_status IN ('verified', 'unverified', 'conflict')
        ),
        source_version TEXT NOT NULL CHECK (length(trim(source_version)) > 0),
        adapter_version TEXT NOT NULL CHECK (length(trim(adapter_version)) > 0),
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        UNIQUE(player_identity_id, canonical_player_id, source_checksum),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(canonical_player_id)
            REFERENCES stats_canonical_players(canonical_player_id) ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id)
            REFERENCES stats_player_identities(player_identity_id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_player_mapping_canonical
    ON stats_player_identifier_mappings(
        canonical_player_id, verification_status, player_identity_id
    )
    """,
    """
    CREATE INDEX idx_stats_player_mapping_identity
    ON stats_player_identifier_mappings(
        player_identity_id, verification_status, observed_at
    )
    """,
    """
    CREATE TABLE stats_game_identities (
        game_identity_id TEXT PRIMARY KEY CHECK (length(trim(game_identity_id)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        provider_game_id TEXT NOT NULL CHECK (length(trim(provider_game_id)) > 0),
        event_id TEXT,
        season INTEGER NOT NULL CHECK (season BETWEEN 1871 AND 9999),
        game_type TEXT NOT NULL CHECK (length(trim(game_type)) > 0),
        official_date TEXT NOT NULL CHECK (
            official_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        scheduled_start TEXT,
        home_team_identity_id TEXT NOT NULL,
        away_team_identity_id TEXT NOT NULL,
        venue_provider_id TEXT,
        venue_name TEXT,
        first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
        last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
        identity_checksum TEXT NOT NULL CHECK (
            length(identity_checksum) = 64
            AND identity_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (home_team_identity_id <> away_team_identity_id),
        CHECK (first_seen_at <= last_seen_at),
        UNIQUE(provider, provider_game_id),
        FOREIGN KEY(event_id) REFERENCES games(event_id) ON DELETE RESTRICT,
        FOREIGN KEY(home_team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(away_team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_game_date
    ON stats_game_identities(official_date, scheduled_start, provider_game_id)
    """,
    """
    CREATE TRIGGER stats_game_identities_preserve_facts
    BEFORE UPDATE ON stats_game_identities
    WHEN OLD.game_identity_id IS NOT NEW.game_identity_id
      OR OLD.provider IS NOT NEW.provider
      OR OLD.provider_game_id IS NOT NEW.provider_game_id
      OR OLD.event_id IS NOT NEW.event_id
      OR OLD.season IS NOT NEW.season
      OR OLD.game_type IS NOT NEW.game_type
      OR OLD.official_date IS NOT NEW.official_date
      OR OLD.scheduled_start IS NOT NEW.scheduled_start
      OR OLD.home_team_identity_id IS NOT NEW.home_team_identity_id
      OR OLD.away_team_identity_id IS NOT NEW.away_team_identity_id
      OR OLD.venue_provider_id IS NOT NEW.venue_provider_id
      OR OLD.venue_name IS NOT NEW.venue_name
      OR OLD.first_seen_at IS NOT NEW.first_seen_at
      OR OLD.identity_checksum IS NOT NEW.identity_checksum
      OR NEW.last_seen_at < OLD.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'game identity facts are immutable');
    END
    """,
    """
    CREATE TRIGGER stats_game_identities_validate_teams
    BEFORE INSERT ON stats_game_identities
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM stats_team_identities AS home
            JOIN stats_team_identities AS away
              ON away.team_identity_id = NEW.away_team_identity_id
            WHERE home.team_identity_id = NEW.home_team_identity_id
              AND home.provider = NEW.provider
              AND away.provider = NEW.provider
        ) THEN RAISE(ABORT, 'game teams must belong to the game provider') END;
    END
    """,
    """
    CREATE TABLE stats_game_status_observations (
        status_observation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stats_run_id TEXT NOT NULL,
        game_identity_id TEXT NOT NULL,
        raw_payload_id TEXT,
        abstract_state TEXT,
        detailed_state TEXT,
        status_code TEXT,
        scheduled_start TEXT,
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('initial', 'correction')
        ),
        status_json TEXT NOT NULL CHECK (json_valid(status_json)),
        normalized_checksum TEXT NOT NULL CHECK (
            length(normalized_checksum) = 64
            AND normalized_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        CHECK (
            (revision_number = 1 AND revision_kind = 'initial')
            OR (revision_number > 1 AND revision_kind = 'correction')
        ),
        UNIQUE(game_identity_id, revision_number),
        UNIQUE(game_identity_id, source_checksum)
    )
    """,
    """
    CREATE INDEX idx_stats_game_status_history
    ON stats_game_status_observations(
        game_identity_id, revision_number, retrieved_at, status_observation_id
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_game_status_normalized
    ON stats_game_status_observations(game_identity_id, normalized_checksum)
    """,
    """
    CREATE TABLE stats_game_team_snapshots (
        team_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stats_run_id TEXT NOT NULL,
        game_identity_id TEXT NOT NULL,
        team_identity_id TEXT NOT NULL,
        raw_payload_id TEXT,
        side TEXT NOT NULL CHECK (side IN ('home', 'away')),
        snapshot_kind TEXT NOT NULL CHECK (length(trim(snapshot_kind)) > 0),
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('initial', 'correction')
        ),
        stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
        normalized_checksum TEXT NOT NULL CHECK (
            length(normalized_checksum) = 64
            AND normalized_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        CHECK (
            (revision_number = 1 AND revision_kind = 'initial')
            OR (revision_number > 1 AND revision_kind = 'correction')
        ),
        UNIQUE(game_identity_id, team_identity_id, side, snapshot_kind, revision_number),
        UNIQUE(game_identity_id, team_identity_id, side, snapshot_kind, source_checksum)
    )
    """,
    """
    CREATE INDEX idx_stats_game_team_history
    ON stats_game_team_snapshots(
        team_identity_id, game_identity_id, snapshot_kind, revision_number
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_game_team_normalized
    ON stats_game_team_snapshots(
        game_identity_id, team_identity_id, side, snapshot_kind, normalized_checksum
    )
    """,
    """
    CREATE TRIGGER stats_game_team_snapshots_validate_side
    BEFORE INSERT ON stats_game_team_snapshots
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_game_identities AS game
            WHERE game.game_identity_id = NEW.game_identity_id
              AND (
                (NEW.side = 'home' AND game.home_team_identity_id = NEW.team_identity_id)
                OR (NEW.side = 'away' AND game.away_team_identity_id = NEW.team_identity_id)
              )
        ) THEN RAISE(ABORT, 'team snapshot side does not match game identity') END;
    END
    """,
    """
    CREATE TABLE stats_game_player_snapshots (
        player_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stats_run_id TEXT NOT NULL,
        game_identity_id TEXT NOT NULL,
        team_identity_id TEXT NOT NULL,
        player_identity_id TEXT NOT NULL,
        raw_payload_id TEXT,
        role TEXT NOT NULL CHECK (role IN ('batting', 'pitching', 'fielding', 'combined')),
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('initial', 'correction')
        ),
        stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
        normalized_checksum TEXT NOT NULL CHECK (
            length(normalized_checksum) = 64
            AND normalized_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        CHECK (
            (revision_number = 1 AND revision_kind = 'initial')
            OR (revision_number > 1 AND revision_kind = 'correction')
        ),
        UNIQUE(game_identity_id, team_identity_id, player_identity_id, role, revision_number),
        UNIQUE(game_identity_id, team_identity_id, player_identity_id, role, source_checksum)
    )
    """,
    """
    CREATE INDEX idx_stats_game_player_history
    ON stats_game_player_snapshots(
        player_identity_id, game_identity_id, role, revision_number
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_game_player_normalized
    ON stats_game_player_snapshots(
        game_identity_id, team_identity_id, player_identity_id, role,
        normalized_checksum
    )
    """,
    """
    CREATE TRIGGER stats_game_player_snapshots_validate_team
    BEFORE INSERT ON stats_game_player_snapshots
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_game_identities AS game
            WHERE game.game_identity_id = NEW.game_identity_id
              AND NEW.team_identity_id IN (
                game.home_team_identity_id, game.away_team_identity_id
              )
        ) THEN RAISE(ABORT, 'player snapshot team does not belong to game') END;
    END
    """,
    """
    CREATE TABLE stats_lineup_snapshots (
        lineup_snapshot_id TEXT PRIMARY KEY CHECK (length(trim(lineup_snapshot_id)) > 0),
        stats_run_id TEXT NOT NULL,
        game_identity_id TEXT NOT NULL,
        team_identity_id TEXT NOT NULL,
        raw_payload_id TEXT,
        lineup_state TEXT NOT NULL CHECK (
            lineup_state IN ('unknown', 'projected', 'confirmed', 'official')
        ),
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE stats_lineup_entries (
        lineup_entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
        lineup_snapshot_id TEXT NOT NULL,
        player_identity_id TEXT NOT NULL,
        batting_order INTEGER CHECK (batting_order IS NULL OR batting_order BETWEEN 1 AND 9),
        position_code TEXT,
        lineup_role TEXT NOT NULL CHECK (
            lineup_role IN ('starter', 'bench', 'pitcher', 'bullpen', 'unknown')
        ),
        entry_json TEXT NOT NULL CHECK (json_valid(entry_json)),
        FOREIGN KEY(lineup_snapshot_id) REFERENCES stats_lineup_snapshots(lineup_snapshot_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX idx_stats_lineup_entry_identity
    ON stats_lineup_entries(
        lineup_snapshot_id,
        player_identity_id,
        lineup_role,
        COALESCE(batting_order, 0)
    )
    """,
    """
    CREATE INDEX idx_stats_lineup_game_order
    ON stats_lineup_entries(lineup_snapshot_id, batting_order, lineup_entry_id)
    """,
    """
    CREATE TABLE stats_play_identities (
        play_identity_id TEXT PRIMARY KEY CHECK (length(trim(play_identity_id)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        game_identity_id TEXT NOT NULL,
        provider_play_id TEXT NOT NULL CHECK (length(trim(provider_play_id)) > 0),
        at_bat_index INTEGER CHECK (at_bat_index IS NULL OR at_bat_index >= 0),
        first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
        last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
        CHECK (first_seen_at <= last_seen_at),
        UNIQUE(provider, game_identity_id, provider_play_id),
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER stats_play_identities_preserve_facts
    BEFORE UPDATE ON stats_play_identities
    WHEN OLD.play_identity_id IS NOT NEW.play_identity_id
      OR OLD.provider IS NOT NEW.provider
      OR OLD.game_identity_id IS NOT NEW.game_identity_id
      OR OLD.provider_play_id IS NOT NEW.provider_play_id
      OR OLD.at_bat_index IS NOT NEW.at_bat_index
      OR OLD.first_seen_at IS NOT NEW.first_seen_at
      OR NEW.last_seen_at < OLD.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'play identity facts are immutable');
    END
    """,
    """
    CREATE TABLE stats_play_revisions (
        play_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        play_identity_id TEXT NOT NULL,
        stats_run_id TEXT NOT NULL,
        raw_payload_id TEXT,
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('initial', 'correction', 'tombstone')
        ),
        inning INTEGER CHECK (inning IS NULL OR inning >= 1),
        half_inning TEXT CHECK (half_inning IS NULL OR half_inning IN ('top', 'bottom')),
        event_type TEXT,
        batter_identity_id TEXT,
        pitcher_identity_id TEXT,
        started_at TEXT,
        ended_at TEXT,
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        play_json TEXT NOT NULL CHECK (json_valid(play_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            (revision_number = 1 AND revision_kind = 'initial')
            OR (revision_number > 1 AND revision_kind IN ('correction', 'tombstone'))
        ),
        UNIQUE(play_identity_id, revision_number),
        UNIQUE(play_identity_id, source_checksum),
        FOREIGN KEY(play_identity_id) REFERENCES stats_play_identities(play_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(batter_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(pitcher_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_play_revision_history
    ON stats_play_revisions(play_identity_id, revision_number)
    """,
    """
    CREATE TABLE statcast_pitch_identities (
        pitch_identity_id TEXT PRIMARY KEY CHECK (length(trim(pitch_identity_id)) > 0),
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        game_identity_id TEXT NOT NULL,
        play_identity_id TEXT,
        provider_pitch_id TEXT NOT NULL CHECK (length(trim(provider_pitch_id)) > 0),
        game_pk INTEGER NOT NULL CHECK (game_pk > 0),
        at_bat_number INTEGER NOT NULL CHECK (at_bat_number >= 0),
        pitch_number INTEGER NOT NULL CHECK (pitch_number >= 1),
        first_seen_at TEXT NOT NULL CHECK (length(trim(first_seen_at)) > 0),
        last_seen_at TEXT NOT NULL CHECK (length(trim(last_seen_at)) > 0),
        CHECK (provider = 'statcast'),
        CHECK (
            provider_pitch_id = CAST(game_pk AS TEXT) || ':'
                || CAST(at_bat_number AS TEXT) || ':' || CAST(pitch_number AS TEXT)
        ),
        CHECK (first_seen_at <= last_seen_at),
        UNIQUE(provider, game_identity_id, provider_pitch_id),
        UNIQUE(provider, game_pk, at_bat_number, pitch_number),
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(play_identity_id) REFERENCES stats_play_identities(play_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER statcast_pitch_identities_preserve_facts
    BEFORE UPDATE ON statcast_pitch_identities
    WHEN OLD.pitch_identity_id IS NOT NEW.pitch_identity_id
      OR OLD.provider IS NOT NEW.provider
      OR OLD.game_identity_id IS NOT NEW.game_identity_id
      OR OLD.play_identity_id IS NOT NEW.play_identity_id
      OR OLD.provider_pitch_id IS NOT NEW.provider_pitch_id
      OR OLD.game_pk IS NOT NEW.game_pk
      OR OLD.at_bat_number IS NOT NEW.at_bat_number
      OR OLD.pitch_number IS NOT NEW.pitch_number
      OR OLD.first_seen_at IS NOT NEW.first_seen_at
      OR NEW.last_seen_at < OLD.last_seen_at
    BEGIN
        SELECT RAISE(ABORT, 'Statcast pitch identity facts are immutable');
    END
    """,
    """
    CREATE TABLE statcast_pitch_revisions (
        statcast_revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
        pitch_identity_id TEXT NOT NULL,
        stats_run_id TEXT NOT NULL,
        raw_payload_id TEXT,
        revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
        revision_kind TEXT NOT NULL CHECK (
            revision_kind IN ('initial', 'correction', 'tombstone')
        ),
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        metrics_json TEXT NOT NULL CHECK (json_valid(metrics_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            (revision_number = 1 AND revision_kind = 'initial')
            OR (revision_number > 1 AND revision_kind IN ('correction', 'tombstone'))
        ),
        UNIQUE(pitch_identity_id, revision_number),
        UNIQUE(pitch_identity_id, source_checksum),
        FOREIGN KEY(pitch_identity_id) REFERENCES statcast_pitch_identities(pitch_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_statcast_revision_history
    ON statcast_pitch_revisions(pitch_identity_id, revision_number)
    """,
    """
    CREATE INDEX idx_statcast_game_tuple
    ON statcast_pitch_identities(
        game_identity_id, at_bat_number, pitch_number, pitch_identity_id
    )
    """,
    """
    CREATE TRIGGER statcast_pitch_identities_validate_game
    BEFORE INSERT ON statcast_pitch_identities
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_game_identities AS game
            WHERE game.game_identity_id = NEW.game_identity_id
              AND game.provider = 'statcast'
              AND game.provider_game_id = CAST(NEW.game_pk AS TEXT)
        ) THEN RAISE(ABORT, 'Statcast pitch tuple does not match game identity') END;
    END
    """,
    """
    CREATE TABLE stats_season_snapshots (
        season_snapshot_id TEXT PRIMARY KEY CHECK (length(trim(season_snapshot_id)) > 0),
        stats_run_id TEXT NOT NULL,
        raw_payload_id TEXT,
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        season INTEGER NOT NULL CHECK (season BETWEEN 1871 AND 9999),
        entity_kind TEXT NOT NULL CHECK (entity_kind IN ('team', 'player')),
        team_identity_id TEXT,
        player_identity_id TEXT,
        source_team_id TEXT NOT NULL CHECK (length(trim(source_team_id)) > 0),
        source_stint_key TEXT NOT NULL CHECK (length(trim(source_stint_key)) > 0),
        split_key TEXT NOT NULL CHECK (length(trim(split_key)) > 0),
        snapshot_as_of TEXT NOT NULL CHECK (length(trim(snapshot_as_of)) > 0),
        provider_updated_at TEXT,
        retrieved_at TEXT NOT NULL CHECK (length(trim(retrieved_at)) > 0),
        stats_json TEXT NOT NULL CHECK (json_valid(stats_json)),
        normalized_checksum TEXT NOT NULL CHECK (
            length(normalized_checksum) = 64
            AND normalized_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            (entity_kind = 'team' AND team_identity_id IS NOT NULL
                AND player_identity_id IS NULL)
            OR (entity_kind = 'player' AND player_identity_id IS NOT NULL
                AND team_identity_id IS NULL)
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_season_snapshot_lookup
    ON stats_season_snapshots(entity_kind, season, split_key, snapshot_as_of)
    """,
    """
    CREATE UNIQUE INDEX uq_stats_season_team_source
    ON stats_season_snapshots(
        provider, season, split_key, team_identity_id,
        source_team_id, source_stint_key, source_checksum
    ) WHERE entity_kind = 'team'
    """,
    """
    CREATE UNIQUE INDEX uq_stats_season_player_source
    ON stats_season_snapshots(
        provider, season, split_key, player_identity_id,
        source_team_id, source_stint_key, source_checksum
    ) WHERE entity_kind = 'player'
    """,
    """
    CREATE TABLE stats_feature_snapshots (
        feature_snapshot_id TEXT PRIMARY KEY CHECK (length(trim(feature_snapshot_id)) > 0),
        stats_run_id TEXT NOT NULL,
        feature_version TEXT NOT NULL CHECK (length(trim(feature_version)) > 0),
        entity_kind TEXT NOT NULL CHECK (entity_kind IN ('game', 'team', 'player')),
        game_identity_id TEXT,
        team_identity_id TEXT,
        player_identity_id TEXT,
        canonical_player_id TEXT,
        feature_as_of TEXT NOT NULL CHECK (length(trim(feature_as_of)) > 0),
        completeness_state TEXT NOT NULL CHECK (
            completeness_state IN ('complete', 'degraded', 'blocked')
        ),
        observed_through TEXT,
        complete_through TEXT,
        input_checksum TEXT NOT NULL CHECK (
            length(input_checksum) = 64 AND input_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        feature_checksum TEXT NOT NULL CHECK (
            length(feature_checksum) = 64 AND feature_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        features_json TEXT NOT NULL CHECK (json_valid(features_json)),
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        CHECK (complete_through IS NULL OR observed_through IS NOT NULL),
        CHECK (
            (entity_kind = 'game' AND game_identity_id IS NOT NULL
                AND team_identity_id IS NULL AND player_identity_id IS NULL
                AND canonical_player_id IS NULL)
            OR (entity_kind = 'team' AND team_identity_id IS NOT NULL
                AND game_identity_id IS NULL AND player_identity_id IS NULL
                AND canonical_player_id IS NULL)
            OR (entity_kind = 'player' AND canonical_player_id IS NOT NULL
                AND game_identity_id IS NULL AND team_identity_id IS NULL
                AND player_identity_id IS NULL)
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(canonical_player_id) REFERENCES stats_canonical_players(canonical_player_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_feature_game_input
    ON stats_feature_snapshots(
        feature_version, feature_as_of, input_checksum, game_identity_id
    ) WHERE entity_kind = 'game'
    """,
    """
    CREATE UNIQUE INDEX uq_stats_feature_team_input
    ON stats_feature_snapshots(
        feature_version, feature_as_of, input_checksum, team_identity_id
    ) WHERE entity_kind = 'team'
    """,
    """
    CREATE UNIQUE INDEX uq_stats_feature_player_input
    ON stats_feature_snapshots(
        feature_version, feature_as_of, input_checksum, canonical_player_id
    ) WHERE entity_kind = 'player'
    """,
    """
    CREATE TABLE stats_reconciliations (
        reconciliation_id TEXT PRIMARY KEY CHECK (length(trim(reconciliation_id)) > 0),
        stats_run_id TEXT NOT NULL,
        checkpoint_id TEXT,
        dataset_key TEXT NOT NULL CHECK (length(trim(dataset_key)) > 0),
        scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
        status TEXT NOT NULL CHECK (status IN ('passed', 'warnings', 'failed')),
        expected_count INTEGER CHECK (expected_count IS NULL OR expected_count >= 0),
        observed_count INTEGER CHECK (observed_count IS NULL OR observed_count >= 0),
        conflict_count INTEGER NOT NULL DEFAULT 0 CHECK (conflict_count >= 0),
        started_at TEXT NOT NULL CHECK (length(trim(started_at)) > 0),
        completed_at TEXT NOT NULL CHECK (length(trim(completed_at)) > 0),
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(checkpoint_id) REFERENCES stats_checkpoints(checkpoint_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE stats_reconciliation_items (
        reconciliation_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        reconciliation_id TEXT NOT NULL,
        severity TEXT NOT NULL CHECK (severity IN ('info', 'warning', 'error')),
        code TEXT NOT NULL CHECK (length(trim(code)) > 0),
        entity_kind TEXT,
        entity_key TEXT,
        message TEXT NOT NULL CHECK (length(trim(message)) > 0),
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(reconciliation_id) REFERENCES stats_reconciliations(reconciliation_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE INDEX idx_stats_reconciliation_items
    ON stats_reconciliation_items(reconciliation_id, severity, reconciliation_item_id)
    """,
    """
    CREATE TABLE stats_conflicts (
        conflict_id TEXT PRIMARY KEY CHECK (length(trim(conflict_id)) > 0),
        stats_run_id TEXT NOT NULL,
        checkpoint_id TEXT,
        conflict_code TEXT NOT NULL CHECK (length(trim(conflict_code)) > 0),
        entity_kind TEXT NOT NULL CHECK (length(trim(entity_kind)) > 0),
        entity_key TEXT NOT NULL CHECK (length(trim(entity_key)) > 0),
        existing_value_json TEXT CHECK (
            existing_value_json IS NULL OR json_valid(existing_value_json)
        ),
        observed_value_json TEXT CHECK (
            observed_value_json IS NULL OR json_valid(observed_value_json)
        ),
        detected_at TEXT NOT NULL CHECK (length(trim(detected_at)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(checkpoint_id) REFERENCES stats_checkpoints(checkpoint_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE stats_conflict_resolutions (
        conflict_resolution_id TEXT PRIMARY KEY CHECK (
            length(trim(conflict_resolution_id)) > 0
        ),
        conflict_id TEXT NOT NULL,
        reconciliation_id TEXT,
        resolution TEXT NOT NULL CHECK (
            resolution IN ('accepted_existing', 'accepted_observed', 'deferred')
        ),
        resolver_id TEXT NOT NULL CHECK (length(trim(resolver_id)) > 0),
        resolved_at TEXT NOT NULL CHECK (length(trim(resolved_at)) > 0),
        details_json TEXT NOT NULL CHECK (json_valid(details_json)),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        FOREIGN KEY(conflict_id) REFERENCES stats_conflicts(conflict_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(reconciliation_id) REFERENCES stats_reconciliations(reconciliation_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE stats_completeness_watermarks (
        provider TEXT NOT NULL CHECK (length(trim(provider)) > 0),
        dataset_key TEXT NOT NULL CHECK (length(trim(dataset_key)) > 0),
        scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
        requested_through_date TEXT NOT NULL CHECK (
            requested_through_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        source_observed_at TEXT NOT NULL CHECK (length(trim(source_observed_at)) > 0),
        latest_ingested_completed_game_date TEXT CHECK (
            latest_ingested_completed_game_date IS NULL OR
            latest_ingested_completed_game_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        contiguous_regular_season_complete_through_date TEXT CHECK (
            contiguous_regular_season_complete_through_date IS NULL OR
            contiguous_regular_season_complete_through_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        partial_date TEXT CHECK (
            partial_date IS NULL OR partial_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        partial_date_reason TEXT,
        cursor_json TEXT CHECK (cursor_json IS NULL OR json_valid(cursor_json)),
        source_stats_run_id TEXT NOT NULL,
        reconciliation_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        updated_at TEXT NOT NULL CHECK (length(trim(updated_at)) > 0),
        source_checksum TEXT NOT NULL CHECK (
            length(source_checksum) = 64
            AND source_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        CHECK (
            latest_ingested_completed_game_date IS NOT NULL
            OR contiguous_regular_season_complete_through_date IS NOT NULL
        ),
        CHECK (
            (partial_date IS NULL AND partial_date_reason IS NULL)
            OR (partial_date IS NOT NULL AND length(trim(partial_date_reason)) > 0)
        ),
        CHECK (
            latest_ingested_completed_game_date IS NULL
            OR latest_ingested_completed_game_date <= requested_through_date
        ),
        CHECK (
            contiguous_regular_season_complete_through_date IS NULL
            OR contiguous_regular_season_complete_through_date <= requested_through_date
        ),
        CHECK (partial_date IS NULL OR partial_date <= requested_through_date),
        PRIMARY KEY(provider, dataset_key, scope_key),
        FOREIGN KEY(source_stats_run_id) REFERENCES stats_ingestion_runs(stats_run_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(reconciliation_id) REFERENCES stats_reconciliations(reconciliation_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE TRIGGER stats_completeness_watermarks_no_regression
    BEFORE UPDATE ON stats_completeness_watermarks
    WHEN (
        NEW.requested_through_date < OLD.requested_through_date
    ) OR (
        NEW.source_observed_at < OLD.source_observed_at
    ) OR (
        OLD.latest_ingested_completed_game_date IS NOT NULL
        AND (
            NEW.latest_ingested_completed_game_date IS NULL
            OR NEW.latest_ingested_completed_game_date
                < OLD.latest_ingested_completed_game_date
        )
    ) OR (
        OLD.contiguous_regular_season_complete_through_date IS NOT NULL
        AND (
            NEW.contiguous_regular_season_complete_through_date IS NULL
            OR NEW.contiguous_regular_season_complete_through_date
                < OLD.contiguous_regular_season_complete_through_date
        )
    ) OR NEW.revision <= OLD.revision
    BEGIN
        SELECT RAISE(ABORT, 'stats completeness watermark cannot regress');
    END
    """,
    """
    CREATE TABLE stats_raw_entity_links (
        raw_entity_link_id INTEGER PRIMARY KEY AUTOINCREMENT,
        raw_payload_id TEXT NOT NULL,
        link_role TEXT NOT NULL CHECK (length(trim(link_role)) > 0),
        game_identity_id TEXT,
        team_identity_id TEXT,
        player_identity_id TEXT,
        play_identity_id TEXT,
        pitch_identity_id TEXT,
        CHECK (
            (game_identity_id IS NOT NULL)
            + (team_identity_id IS NOT NULL)
            + (player_identity_id IS NOT NULL)
            + (play_identity_id IS NOT NULL)
            + (pitch_identity_id IS NOT NULL) = 1
        ),
        FOREIGN KEY(raw_payload_id) REFERENCES stats_raw_payload_metadata(raw_payload_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(game_identity_id) REFERENCES stats_game_identities(game_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(team_identity_id) REFERENCES stats_team_identities(team_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(player_identity_id) REFERENCES stats_player_identities(player_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(play_identity_id) REFERENCES stats_play_identities(play_identity_id)
            ON DELETE RESTRICT,
        FOREIGN KEY(pitch_identity_id) REFERENCES statcast_pitch_identities(pitch_identity_id)
            ON DELETE RESTRICT
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_raw_entity_identity
    ON stats_raw_entity_links(
        raw_payload_id,
        link_role,
        COALESCE(game_identity_id, ''),
        COALESCE(team_identity_id, ''),
        COALESCE(player_identity_id, ''),
        COALESCE(play_identity_id, ''),
        COALESCE(pitch_identity_id, '')
    )
    """,
    _raw_payload_run_trigger("stats_game_status_observations"),
    _raw_payload_run_trigger("stats_game_team_snapshots"),
    _raw_payload_run_trigger("stats_game_player_snapshots"),
    _raw_payload_run_trigger("stats_lineup_snapshots"),
    _raw_payload_run_trigger("stats_play_revisions"),
    _raw_payload_run_trigger("statcast_pitch_revisions"),
    _raw_payload_run_trigger("stats_season_snapshots"),
    *_immutable_triggers("stats_raw_payload_metadata"),
    *_immutable_triggers("stats_excluded_source_rows"),
    *_immutable_triggers("stats_canonical_players"),
    *_immutable_triggers("stats_player_identifier_mappings"),
    *_immutable_triggers("stats_game_status_observations"),
    *_immutable_triggers("stats_game_team_snapshots"),
    *_immutable_triggers("stats_game_player_snapshots"),
    *_immutable_triggers("stats_lineup_snapshots"),
    *_immutable_triggers("stats_lineup_entries"),
    *_immutable_triggers("stats_play_revisions"),
    *_immutable_triggers("statcast_pitch_revisions"),
    *_immutable_triggers("stats_season_snapshots"),
    *_immutable_triggers("stats_feature_snapshots"),
    *_immutable_triggers("stats_reconciliations"),
    *_immutable_triggers("stats_reconciliation_items"),
    *_immutable_triggers("stats_conflicts"),
    *_immutable_triggers("stats_conflict_resolutions"),
    *_immutable_triggers("stats_raw_entity_links"),
)


FORMAL_SCHEMA_V7_STATEMENTS = (
    "DROP TRIGGER publication_batches_validate_draft",
    f"""
    CREATE TABLE _collector_runs_v7 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (status IN ({_RUN_STATUS_SQL})),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (failure_stage IN ({_FAILURE_STAGE_SQL})),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (
            schema_version IN (1, 2, 3, 4, 5, 6, 7)
        ),
        CHECK (
            (status = 'failed' AND failure_stage IS NOT NULL)
            OR (status <> 'failed' AND failure_stage IS NULL)
        ),
        CHECK (
            (status = 'queued' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN ('completed', 'completed_with_warnings')
                AND started_at IS NOT NULL AND completed_at IS NOT NULL)
            OR (status = 'failed' AND completed_at IS NOT NULL)
        )
    )
    """,
    """
    INSERT INTO _collector_runs_v7(
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    )
    SELECT
        run_id, requested_date, status, created_at, queued_at, started_at,
        completed_at, updated_at, failure_stage, error_message,
        artifact_relpath, app_version, schema_version
    FROM collector_runs
    """,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v7 RENAME TO collector_runs",
    """
    CREATE TRIGGER publication_batches_validate_draft
    BEFORE INSERT ON publication_batches
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM card_drafts AS draft
            JOIN collector_runs AS run ON run.run_id = draft.run_id
            WHERE draft.draft_id = NEW.draft_id
              AND draft.run_id = NEW.run_id
              AND draft.requested_date = NEW.requested_date
              AND draft.policy_version = NEW.policy_version
              AND draft.draft_checksum = NEW.draft_checksum
              AND draft.source_checksum = NEW.source_checksum
              AND run.requested_date = NEW.requested_date
        ) THEN RAISE(ABORT, 'publication batch draft evidence mismatch') END;
    END
    """,
    f"""
    CREATE TABLE pipeline_runs (
        run_id TEXT PRIMARY KEY,
        sport TEXT NOT NULL CHECK (sport = 'MLB'),
        run_type TEXT NOT NULL CHECK (run_type = 'manual_daily'),
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        as_of_time TEXT NOT NULL CHECK (length(trim(as_of_time)) > 0),
        timezone TEXT NOT NULL CHECK (length(trim(timezone)) > 0),
        pipeline_version TEXT NOT NULL CHECK (length(trim(pipeline_version)) > 0),
        configuration_version TEXT NOT NULL
            CHECK (length(trim(configuration_version)) > 0),
        configuration_fingerprint TEXT NOT NULL
            CHECK (
                length(configuration_fingerprint) = 64
                AND configuration_fingerprint NOT GLOB '*[^0-9a-f]*'
            ),
        configuration_metadata_json TEXT NOT NULL
            CHECK (json_valid(configuration_metadata_json)),
        code_revision TEXT NOT NULL CHECK (length(trim(code_revision)) > 0),
        database_schema_version INTEGER NOT NULL CHECK (database_schema_version = 7),
        force_refresh INTEGER NOT NULL CHECK (force_refresh IN (0, 1)),
        status TEXT NOT NULL CHECK (status IN ({_PIPELINE_RUN_STATUS_SQL})),
        failure_phase TEXT CHECK (
            failure_phase IS NULL OR failure_phase IN ({_PIPELINE_PHASE_KEY_SQL})
        ),
        error_message TEXT,
        final_summary_json TEXT CHECK (
            final_summary_json IS NULL OR json_valid(final_summary_json)
        ),
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        CHECK (
            (status = 'pending' AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (
                status IN (
                    'succeeded',
                    'succeeded_with_warnings',
                    'degraded',
                    'failed'
                )
                AND started_at IS NOT NULL
                AND completed_at IS NOT NULL
            )
        ),
        CHECK (
            (status = 'failed' AND failure_phase IS NOT NULL AND error_message IS NOT NULL)
            OR (status != 'failed' AND failure_phase IS NULL AND error_message IS NULL)
        )
    )
    """,
    """
    CREATE UNIQUE INDEX uq_pipeline_manual_run_guard
    ON pipeline_runs(sport, run_type, requested_date)
    WHERE force_refresh = 0 AND status != 'failed'
    """,
    """
    CREATE INDEX idx_pipeline_runs_requested_date
    ON pipeline_runs(requested_date, created_at, run_id)
    """,
    f"""
    CREATE TABLE pipeline_run_phases (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL CHECK (phase_key IN ({_PIPELINE_PHASE_KEY_SQL})),
        ordinal INTEGER NOT NULL CHECK (ordinal BETWEEN 1 AND 15),
        status TEXT NOT NULL CHECK (status IN ({_PIPELINE_PHASE_STATUS_SQL})),
        attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        input_checksum TEXT CHECK (
            input_checksum IS NULL OR (
                length(input_checksum) = 64
                AND input_checksum NOT GLOB '*[^0-9a-f]*'
            )
        ),
        output_checksum TEXT CHECK (
            output_checksum IS NULL OR (
                length(output_checksum) = 64
                AND output_checksum NOT GLOB '*[^0-9a-f]*'
            )
        ),
        artifact_relpath TEXT,
        warnings_json TEXT CHECK (warnings_json IS NULL OR json_valid(warnings_json)),
        error_json TEXT CHECK (error_json IS NULL OR json_valid(error_json)),
        reused_from_run_id TEXT,
        PRIMARY KEY(run_id, phase_key),
        UNIQUE(run_id, ordinal),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(reused_from_run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        CHECK (
            (status = 'pending' AND attempt_count = 0
                AND started_at IS NULL AND completed_at IS NULL)
            OR (status = 'running' AND attempt_count >= 1
                AND started_at IS NOT NULL AND completed_at IS NULL)
            OR (status IN (
                    'succeeded',
                    'succeeded_with_warnings',
                    'degraded',
                    'failed'
                )
                AND attempt_count >= 1
                AND started_at IS NOT NULL
                AND completed_at IS NOT NULL)
            OR (status IN ('skipped', 'reused') AND completed_at IS NOT NULL)
        ),
        CHECK (
            (status = 'reused' AND reused_from_run_id IS NOT NULL)
            OR (status != 'reused' AND reused_from_run_id IS NULL)
        ),
        CHECK (
            (status = 'failed' AND error_json IS NOT NULL)
            OR (status != 'failed' AND error_json IS NULL)
        )
    )
    """,
    """
    CREATE INDEX idx_pipeline_run_phases_status
    ON pipeline_run_phases(run_id, status, ordinal)
    """,
    """
    CREATE INDEX idx_pipeline_run_phases_reused_from
    ON pipeline_run_phases(reused_from_run_id, phase_key)
    WHERE reused_from_run_id IS NOT NULL
    """,
    f"""
    CREATE TABLE pipeline_run_transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        phase_key TEXT,
        from_status TEXT,
        to_status TEXT NOT NULL,
        transition_type TEXT NOT NULL
            CHECK (transition_type IN ({_PIPELINE_TRANSITION_TYPE_SQL})),
        reason TEXT,
        audit_metadata_json TEXT NOT NULL CHECK (json_valid(audit_metadata_json)),
        transitioned_at TEXT NOT NULL,
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
        FOREIGN KEY(run_id, phase_key)
            REFERENCES pipeline_run_phases(run_id, phase_key) ON DELETE CASCADE,
        CHECK (
            (phase_key IS NULL
                AND (from_status IS NULL OR from_status IN ({_PIPELINE_RUN_STATUS_SQL}))
                AND to_status IN ({_PIPELINE_RUN_STATUS_SQL}))
            OR
            (phase_key IN ({_PIPELINE_PHASE_KEY_SQL})
                AND (from_status IS NULL OR from_status IN ({_PIPELINE_PHASE_STATUS_SQL}))
                AND to_status IN ({_PIPELINE_PHASE_STATUS_SQL}))
        )
    )
    """,
    """
    CREATE INDEX idx_pipeline_run_transitions_audit
    ON pipeline_run_transitions(run_id, transitioned_at, id)
    """,
    """
    CREATE TRIGGER pipeline_run_transitions_reject_update
    BEFORE UPDATE ON pipeline_run_transitions
    BEGIN
        SELECT RAISE(ABORT, 'pipeline run transitions are append-only');
    END
    """,
    """
    CREATE TRIGGER pipeline_run_transitions_reject_delete
    BEFORE DELETE ON pipeline_run_transitions
    BEGIN
        SELECT RAISE(ABORT, 'pipeline run transitions are append-only');
    END
    """,
)


_COLLECTOR_RUNS_V8_CREATE = (
    FORMAL_SCHEMA_V7_STATEMENTS[1]
    .replace("_collector_runs_v7", "_collector_runs_v8")
    .replace(
        "schema_version IN (1, 2, 3, 4, 5, 6, 7)",
        "schema_version IN (1, 2, 3, 4, 5, 6, 7, 8)",
    )
)
_COLLECTOR_RUNS_V8_COPY = FORMAL_SCHEMA_V7_STATEMENTS[2].replace(
    "_collector_runs_v7", "_collector_runs_v8"
)
_PIPELINE_RUNS_V8_CREATE = (
    FORMAL_SCHEMA_V7_STATEMENTS[6]
    .replace("CREATE TABLE pipeline_runs", "CREATE TABLE _pipeline_runs_v8")
    .replace(
        "database_schema_version INTEGER NOT NULL CHECK (database_schema_version = 7)",
        "database_schema_version INTEGER NOT NULL CHECK ("
        "database_schema_version IN (7, 8))",
    )
)

FORMAL_SCHEMA_V8_STATEMENTS = (
    "DROP TRIGGER publication_batches_validate_draft",
    _COLLECTOR_RUNS_V8_CREATE,
    _COLLECTOR_RUNS_V8_COPY,
    "DROP TABLE collector_runs",
    "ALTER TABLE _collector_runs_v8 RENAME TO collector_runs",
    _PIPELINE_RUNS_V8_CREATE,
    """
    INSERT INTO _pipeline_runs_v8(
        run_id, sport, run_type, requested_date, as_of_time, timezone,
        pipeline_version, configuration_version, configuration_fingerprint,
        configuration_metadata_json, code_revision, database_schema_version,
        force_refresh, status, failure_phase, error_message,
        final_summary_json, created_at, started_at, completed_at, updated_at
    )
    SELECT
        run_id, sport, run_type, requested_date, as_of_time, timezone,
        pipeline_version, configuration_version, configuration_fingerprint,
        configuration_metadata_json, code_revision, database_schema_version,
        force_refresh, status, failure_phase, error_message,
        final_summary_json, created_at, started_at, completed_at, updated_at
    FROM pipeline_runs
    """,
    "DROP TABLE pipeline_runs",
    "ALTER TABLE _pipeline_runs_v8 RENAME TO pipeline_runs",
    FORMAL_SCHEMA_V7_STATEMENTS[7],
    FORMAL_SCHEMA_V7_STATEMENTS[8],
    """
    CREATE TABLE daily_slate_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (
            snapshot_id GLOB 'slate:[0-9a-f]*'
            AND length(snapshot_id) = 70
        ),
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'daily_slate' CHECK (
            phase_key = 'daily_slate'
        ),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt >= 1),
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        as_of_time TEXT NOT NULL CHECK (length(trim(as_of_time)) > 0),
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        sport TEXT NOT NULL CHECK (sport = 'MLB'),
        league TEXT NOT NULL CHECK (league = 'MLB'),
        source_authority TEXT NOT NULL CHECK (
            length(trim(source_authority)) > 0
        ),
        source_version TEXT,
        contract_version TEXT NOT NULL CHECK (
            contract_version = 'DSE_DAILY_SLATE_V1'
        ),
        snapshot_checksum TEXT NOT NULL CHECK (
            length(snapshot_checksum) = 64
            AND snapshot_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        artifact_relpath TEXT,
        artifact_checksum TEXT CHECK (
            artifact_checksum IS NULL OR (
                length(artifact_checksum) = 64
                AND artifact_checksum NOT GLOB '*[^0-9a-f]*'
            )
        ),
        provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        game_count INTEGER NOT NULL CHECK (game_count >= 0),
        sealed_at TEXT,
        created_at TEXT NOT NULL CHECK (length(trim(created_at)) > 0),
        UNIQUE(run_id, phase_attempt),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY(run_id, phase_key)
            REFERENCES pipeline_run_phases(run_id, phase_key) ON DELETE RESTRICT,
        CHECK (
            (artifact_relpath IS NULL AND artifact_checksum IS NULL)
            OR (artifact_relpath IS NOT NULL AND artifact_checksum IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX idx_daily_slate_snapshots_run
    ON daily_slate_snapshots(run_id, phase_attempt, created_at, snapshot_id)
    """,
    """
    CREATE INDEX idx_daily_slate_snapshots_date
    ON daily_slate_snapshots(requested_date, as_of_time, snapshot_id)
    """,
    """
    CREATE TABLE daily_slate_games (
        snapshot_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 1),
        edge_event_id TEXT NOT NULL CHECK (length(trim(edge_event_id)) > 0),
        daily_mlb_game_id TEXT NOT NULL CHECK (
            length(trim(daily_mlb_game_id)) > 0
        ),
        official_date TEXT NOT NULL CHECK (
            official_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        scheduled_start_time TEXT,
        away_team_id TEXT NOT NULL CHECK (length(trim(away_team_id)) > 0),
        home_team_id TEXT NOT NULL CHECK (length(trim(home_team_id)) > 0),
        venue_id TEXT,
        venue_mapping_status TEXT NOT NULL CHECK (
            venue_mapping_status IN ('resolved', 'unresolved')
        ),
        game_number INTEGER CHECK (game_number IS NULL OR game_number >= 1),
        doubleheader_status TEXT NOT NULL CHECK (
            doubleheader_status IN ('single', 'doubleheader', 'unknown')
        ),
        game_status TEXT NOT NULL CHECK (
            game_status IN (
                'scheduled', 'pregame', 'in_progress', 'delayed',
                'postponed', 'suspended', 'final', 'cancelled', 'unknown'
            )
        ),
        source_game_id TEXT NOT NULL CHECK (length(trim(source_game_id)) > 0),
        source_provider TEXT NOT NULL CHECK (length(trim(source_provider)) > 0),
        source_home_team_id TEXT,
        source_away_team_id TEXT,
        source_venue_id TEXT,
        source_venue_name TEXT,
        observed_at TEXT NOT NULL CHECK (length(trim(observed_at)) > 0),
        source_updated_at TEXT,
        away_probable_player_identity_id TEXT,
        away_probable_canonical_player_id TEXT,
        home_probable_player_identity_id TEXT,
        home_probable_canonical_player_id TEXT,
        provenance_json TEXT NOT NULL CHECK (json_valid(provenance_json)),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK (
            length(row_checksum) = 64
            AND row_checksum NOT GLOB '*[^0-9a-f]*'
        ),
        PRIMARY KEY(snapshot_id, edge_event_id),
        UNIQUE(snapshot_id, ordinal),
        UNIQUE(snapshot_id, daily_mlb_game_id),
        UNIQUE(snapshot_id, source_provider, source_game_id),
        FOREIGN KEY(snapshot_id)
            REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(away_probable_player_identity_id)
            REFERENCES stats_player_identities(player_identity_id)
                ON DELETE RESTRICT,
        FOREIGN KEY(away_probable_canonical_player_id)
            REFERENCES stats_canonical_players(canonical_player_id)
                ON DELETE RESTRICT,
        FOREIGN KEY(home_probable_player_identity_id)
            REFERENCES stats_player_identities(player_identity_id)
                ON DELETE RESTRICT,
        FOREIGN KEY(home_probable_canonical_player_id)
            REFERENCES stats_canonical_players(canonical_player_id)
                ON DELETE RESTRICT,
        CHECK (away_team_id <> home_team_id),
        CHECK (
            (venue_mapping_status = 'resolved' AND venue_id IS NOT NULL)
            OR (venue_mapping_status = 'unresolved' AND venue_id IS NULL)
        ),
        CHECK (
            (away_probable_player_identity_id IS NULL
                AND away_probable_canonical_player_id IS NULL)
            OR (away_probable_player_identity_id IS NOT NULL
                AND away_probable_canonical_player_id IS NOT NULL)
        ),
        CHECK (
            (home_probable_player_identity_id IS NULL
                AND home_probable_canonical_player_id IS NULL)
            OR (home_probable_player_identity_id IS NOT NULL
                AND home_probable_canonical_player_id IS NOT NULL)
        )
    )
    """,
    """
    CREATE INDEX idx_daily_slate_games_identity
    ON daily_slate_games(daily_mlb_game_id, observed_at, snapshot_id)
    """,
    """
    CREATE TRIGGER daily_slate_snapshots_validate_phase
    BEFORE INSERT ON daily_slate_snapshots
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM pipeline_runs AS run
            JOIN pipeline_run_phases AS phase
              ON phase.run_id=run.run_id
             AND phase.phase_key='daily_slate'
            WHERE run.run_id=NEW.run_id
              AND run.requested_date=NEW.requested_date
              AND phase.status='running'
              AND phase.attempt_count=NEW.phase_attempt
              AND NEW.sealed_at IS NULL
        ) THEN RAISE(
            ABORT,
            'daily slate snapshot requires matching active pipeline phase attempt'
        ) END;
    END
    """,
    """
    CREATE TRIGGER daily_slate_snapshots_reject_update
    BEFORE UPDATE OF
        snapshot_id, run_id, phase_key, phase_attempt, requested_date,
        as_of_time, observed_at, sport, league, source_authority,
        source_version, contract_version, snapshot_checksum,
        artifact_relpath, artifact_checksum, provenance_json, canonical_json,
        game_count, created_at
    ON daily_slate_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'daily slate snapshots are immutable');
    END
    """,
    """
    CREATE TRIGGER daily_slate_snapshots_validate_seal
    BEFORE UPDATE OF sealed_at ON daily_slate_snapshots
    BEGIN
        SELECT CASE WHEN
            OLD.sealed_at IS NOT NULL
            OR NEW.sealed_at IS NULL
            OR length(trim(NEW.sealed_at)) = 0
            OR (
                SELECT count(*)
                FROM daily_slate_games
                WHERE snapshot_id=OLD.snapshot_id
            ) <> OLD.game_count
            OR json_array_length(OLD.canonical_json, '$.games') <> OLD.game_count
        THEN RAISE(
            ABORT,
            'daily slate snapshot cannot seal incomplete game evidence'
        ) END;
    END
    """,
    """
    CREATE TRIGGER daily_slate_snapshots_reject_delete
    BEFORE DELETE ON daily_slate_snapshots
    BEGIN
        SELECT RAISE(ABORT, 'daily slate snapshots are retained evidence');
    END
    """,
    """
    CREATE TRIGGER daily_slate_games_reject_update
    BEFORE UPDATE ON daily_slate_games
    BEGIN
        SELECT RAISE(ABORT, 'daily slate game observations are immutable');
    END
    """,
    """
    CREATE TRIGGER daily_slate_games_validate_unsealed_snapshot
    BEFORE INSERT ON daily_slate_games
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1
            FROM daily_slate_snapshots AS snapshot
            WHERE snapshot.snapshot_id=NEW.snapshot_id
              AND snapshot.sealed_at IS NULL
              AND NEW.ordinal <= snapshot.game_count
        ) THEN RAISE(
            ABORT,
            'daily slate game requires an unsealed snapshot construction'
        ) END;
    END
    """,
    """
    CREATE TRIGGER daily_slate_games_reject_delete
    BEFORE DELETE ON daily_slate_games
    BEGIN
        SELECT RAISE(ABORT, 'daily slate game observations are retained evidence');
    END
    """,
    FORMAL_SCHEMA_V7_STATEMENTS[5],
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_sql(sql: str | None) -> str:
    return " ".join((sql or "").split())


def schema_fingerprint(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        """
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name, tbl_name
        """
    ).fetchall()
    objects = [
        {
            "type": str(row[0]),
            "name": str(row[1]),
            "table": str(row[2]),
            "sql": _canonical_sql(row[3]),
        }
        for row in rows
    ]
    encoded = json.dumps(objects, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _execute_statements(
    connection: sqlite3.Connection, statements: Iterable[str]
) -> None:
    for statement in statements:
        connection.execute(statement)


def _fingerprint_for_script(script: str) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(script)
        return schema_fingerprint(connection)
    finally:
        connection.close()


def _fingerprint_for_statements(statements: Iterable[str]) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        _execute_statements(connection, statements)
        return schema_fingerprint(connection)
    finally:
        connection.close()


def _fingerprint_for_migration_chain(
    *migration_statements: Iterable[str],
) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statements in migration_statements:
            _execute_statements(connection, statements)
        return schema_fingerprint(connection)
    finally:
        connection.close()


LEGACY_SCHEMA_FINGERPRINT = _fingerprint_for_script(LEGACY_SCHEMA_SQL)
FORMAL_SCHEMA_V1_FINGERPRINT = _fingerprint_for_statements(FORMAL_SCHEMA_V1_STATEMENTS)
MIGRATION_V1_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V1_NAME
        + "\nlegacy-transform-v1\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V1_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V2_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
)
MIGRATION_V2_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V2_NAME
        + "\nformal-v1-to-v2\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V2_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V3_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
)
MIGRATION_V3_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V3_NAME
        + "\nformal-v2-to-v3\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V3_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V4_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
)
MIGRATION_V4_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V4_NAME
        + "\nformal-v3-to-v4\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V4_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V5_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
)
MIGRATION_V5_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V5_NAME
        + "\nformal-v4-to-v5\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V5_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V6_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
)
MIGRATION_V6_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V6_NAME
        + "\nformal-v5-to-v6\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V6_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V7_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_STATEMENTS,
)
MIGRATION_V7_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V7_NAME
        + "\nformal-v6-to-v7\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V7_STATEMENTS)
    ).encode("utf-8")
).hexdigest()
FORMAL_SCHEMA_V8_FINGERPRINT = _fingerprint_for_migration_chain(
    FORMAL_SCHEMA_V1_STATEMENTS,
    FORMAL_SCHEMA_V2_STATEMENTS,
    FORMAL_SCHEMA_V3_STATEMENTS,
    FORMAL_SCHEMA_V4_STATEMENTS,
    FORMAL_SCHEMA_V5_STATEMENTS,
    FORMAL_SCHEMA_V6_STATEMENTS,
    FORMAL_SCHEMA_V7_STATEMENTS,
    FORMAL_SCHEMA_V8_STATEMENTS,
)
MIGRATION_V8_CHECKSUM = hashlib.sha256(
    (
        MIGRATION_V8_NAME
        + "\nformal-v7-to-v8\n"
        + "\n".join(_canonical_sql(statement) for statement in FORMAL_SCHEMA_V8_STATEMENTS)
    ).encode("utf-8")
).hexdigest()

MIGRATION_HISTORY = (
    (1, MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM),
    (2, MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM),
    (3, MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM),
    (4, MIGRATION_V4_NAME, MIGRATION_V4_CHECKSUM),
    (5, MIGRATION_V5_NAME, MIGRATION_V5_CHECKSUM),
    (6, MIGRATION_V6_NAME, MIGRATION_V6_CHECKSUM),
    (7, MIGRATION_V7_NAME, MIGRATION_V7_CHECKSUM),
    (8, MIGRATION_V8_NAME, MIGRATION_V8_CHECKSUM),
)


def _migration_directory(database_path: Path) -> Path:
    return database_path.with_name(f"{database_path.name}.migration-backups")


def _diagnostic_filename(started_at: str) -> str:
    safe_timestamp = started_at.replace(":", "").replace("+", "_")
    return f"migration-v1-{safe_timestamp}-{uuid4().hex[:8]}.json"


def _write_atomic_diagnostic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        observed = json.loads(path.read_text(encoding="utf-8"))
        if observed != payload:
            raise DiagnosticWriteError("Migration diagnostic verification failed")
    except Exception as exc:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        if isinstance(exc, DiagnosticWriteError):
            raise
        raise DiagnosticWriteError("Unable to write verified migration diagnostics") from exc


def _create_verified_backup(
    database_path: Path,
    backup_path: Path,
    expected_fingerprint: str,
) -> None:
    source: sqlite3.Connection | None = None
    destination: sqlite3.Connection | None = None
    backup_created = False
    backup_error: Exception | None = None
    try:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        if backup_path.exists():
            raise BackupVerificationError("Refusing to overwrite a migration backup")
        source = sqlite3.connect(database_path, timeout=5.0)
        destination = sqlite3.connect(backup_path, timeout=5.0)
        backup_created = True
        source.backup(destination)
        destination.commit()
    except Exception as exc:
        backup_error = exc
    finally:
        if destination is not None:
            destination.close()
        if source is not None:
            source.close()

    if backup_error is not None:
        if backup_created:
            backup_path.unlink(missing_ok=True)
        if isinstance(backup_error, BackupVerificationError):
            raise backup_error
        raise BackupVerificationError("SQLite migration backup failed") from backup_error

    verification = sqlite3.connect(backup_path, timeout=5.0)
    try:
        result = verification.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise BackupVerificationError("Migration backup integrity_check failed")
        if schema_fingerprint(verification) != expected_fingerprint:
            raise BackupVerificationError("Migration backup schema fingerprint mismatch")
    except Exception:
        verification.close()
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        if verification:
            verification.close()


def _assert_foreign_keys(connection: sqlite3.Connection) -> None:
    violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise SchemaVerificationError(
            f"Foreign-key verification found {len(violations)} violation(s)"
        )


def _assert_target_schema(connection: sqlite3.Connection, version: int) -> None:
    expected_fingerprint = {
        1: FORMAL_SCHEMA_V1_FINGERPRINT,
        2: FORMAL_SCHEMA_V2_FINGERPRINT,
        3: FORMAL_SCHEMA_V3_FINGERPRINT,
        4: FORMAL_SCHEMA_V4_FINGERPRINT,
        5: FORMAL_SCHEMA_V5_FINGERPRINT,
        6: FORMAL_SCHEMA_V6_FINGERPRINT,
        7: FORMAL_SCHEMA_V7_FINGERPRINT,
        8: FORMAL_SCHEMA_V8_FINGERPRINT,
    }.get(version)
    if expected_fingerprint is None:
        raise SchemaVerificationError(f"Unsupported target schema version {version}")
    observed = schema_fingerprint(connection)
    if observed != expected_fingerprint:
        raise SchemaVerificationError(
            f"Migrated schema fingerprint does not match formal schema v{version}"
        )
    _assert_foreign_keys(connection)


def _insert_migration_records(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    source_fingerprint: str,
    backup_path: Path | None,
    diagnostic_path: Path,
    started_at: str,
    completed_at: str,
    details: dict[str, Any],
) -> None:
    connection.execute(
        """
        INSERT INTO schema_migrations(version, name, checksum, applied_at)
        VALUES (?, ?, ?, ?)
        """,
        (1, MIGRATION_V1_NAME, MIGRATION_V1_CHECKSUM, completed_at),
    )
    connection.execute(
        """
        INSERT INTO migration_diagnostics(
            migration_version, source_kind, source_fingerprint, target_fingerprint,
            backup_path, diagnostic_filename, started_at, completed_at, status,
            details_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?)
        """,
        (
            1,
            source_kind,
            source_fingerprint,
            FORMAL_SCHEMA_V1_FINGERPRINT,
            str(backup_path) if backup_path else None,
            diagnostic_path.name,
            started_at,
            completed_at,
            json.dumps(details, sort_keys=True, separators=(",", ":")),
        ),
    )


def _base_diagnostic(
    *,
    status: str,
    database_path: Path,
    source_kind: str,
    source_fingerprint: str,
    started_at: str,
    backup_path: Path | None,
) -> dict[str, Any]:
    return {
        "backup_filename": backup_path.name if backup_path else None,
        "backup_path": str(backup_path.resolve()) if backup_path else None,
        "database_path": str(database_path.resolve()),
        "migration_checksum": MIGRATION_V1_CHECKSUM,
        "migration_name": MIGRATION_V1_NAME,
        "schema_version": 1,
        "source_fingerprint": source_fingerprint,
        "source_kind": source_kind,
        "started_at": started_at,
        "status": status,
        "target_fingerprint": FORMAL_SCHEMA_V1_FINGERPRINT,
    }


def _install_empty_schema(
    connection: sqlite3.Connection, database_path: Path
) -> MigrationResult:
    source_fingerprint = schema_fingerprint(connection)
    started_at = _utc_now()
    migration_dir = _migration_directory(database_path)
    diagnostic_path = migration_dir / _diagnostic_filename(started_at)
    diagnostic = _base_diagnostic(
        status="preflight_verified",
        database_path=database_path,
        source_kind="empty",
        source_fingerprint=source_fingerprint,
        started_at=started_at,
        backup_path=None,
    )
    _write_atomic_diagnostic(diagnostic_path, diagnostic)
    connection.execute("BEGIN IMMEDIATE")
    try:
        _execute_statements(connection, FORMAL_SCHEMA_V1_STATEMENTS)
        completed_at = _utc_now()
        connection.execute("PRAGMA user_version=1")
        _assert_target_schema(connection, 1)
        _insert_migration_records(
            connection,
            source_kind="empty",
            source_fingerprint=source_fingerprint,
            backup_path=None,
            diagnostic_path=diagnostic_path,
            started_at=started_at,
            completed_at=completed_at,
            details={"legacy_rows_preserved": None},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    diagnostic.update({"completed_at": completed_at, "status": "migration_verified"})
    _write_atomic_diagnostic(diagnostic_path, diagnostic)
    return MigrationResult(
        version=1,
        schema_fingerprint=FORMAL_SCHEMA_V1_FINGERPRINT,
        migrated=True,
        source_kind="empty",
        diagnostic_path=diagnostic_path,
    )


def _rename_legacy_tables(connection: sqlite3.Connection) -> None:
    for index_name in ("idx_odds_event_time", "idx_odds_run", "idx_weather_event_time"):
        connection.execute(f"DROP INDEX {index_name}")
    for table_name in (
        "collector_runs",
        "games",
        "odds_snapshots",
        "weather_snapshots",
        "collector_errors",
    ):
        connection.execute(
            f'ALTER TABLE "{table_name}" RENAME TO "_legacy_{table_name}"'
        )


def _copy_legacy_data(connection: sqlite3.Connection) -> dict[str, int]:
    invalid_statuses = connection.execute(
        """
        SELECT DISTINCT status FROM _legacy_collector_runs
        WHERE status NOT IN ('running', 'completed', 'completed_with_warnings', 'failed')
        """
    ).fetchall()
    if invalid_statuses:
        raise SchemaVerificationError("Legacy database contains unsupported run status values")

    connection.execute(
        """
        INSERT INTO collector_runs(
            run_id, requested_date, status, created_at, queued_at, started_at,
            completed_at, updated_at, failure_stage, error_message,
            artifact_relpath, app_version, schema_version
        )
        SELECT
            run_id,
            requested_date,
            status,
            started_at,
            started_at,
            started_at,
            CASE
                WHEN status IN ('completed', 'completed_with_warnings', 'failed')
                    THEN COALESCE(completed_at, started_at)
                ELSE NULL
            END,
            COALESCE(completed_at, started_at),
            CASE WHEN status = 'failed' THEN 'worker_execution' ELSE NULL END,
            error_message,
            NULL,
            'legacy-unknown',
            1
        FROM _legacy_collector_runs
        """
    )
    connection.execute(
        """
        INSERT INTO games(
            event_id, sport_key, commence_time, home_team, away_team,
            home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
        )
        SELECT
            event_id, sport_key, commence_time, home_team, away_team,
            home_team_key, away_team_key, raw_json, first_seen_at, last_seen_at
        FROM _legacy_games
        """
    )
    connection.execute(
        """
        INSERT INTO run_games(run_id, event_id, associated_at)
        SELECT associations.run_id, associations.event_id,
               COALESCE(r.started_at, g.first_seen_at)
        FROM (
            SELECT run_id, event_id FROM _legacy_odds_snapshots
            UNION
            SELECT run_id, event_id FROM _legacy_weather_snapshots
            UNION
            SELECT run_id, event_id FROM _legacy_collector_errors WHERE event_id IS NOT NULL
        ) AS associations
        JOIN _legacy_collector_runs AS r ON r.run_id = associations.run_id
        JOIN _legacy_games AS g ON g.event_id = associations.event_id
        """
    )
    connection.execute(
        """
        INSERT INTO odds_snapshots(
            id, run_id, event_id, bookmaker_key, bookmaker_title, market_key,
            market_last_update, outcome_name, price, point,
            bookmaker_last_update, retrieved_at, raw_json
        )
        SELECT
            id, run_id, event_id, bookmaker_key, bookmaker_title, market_key,
            NULL, outcome_name, price, point, bookmaker_last_update,
            retrieved_at, raw_json
        FROM _legacy_odds_snapshots
        """
    )
    connection.execute(
        """
        INSERT INTO weather_snapshots(
            id, run_id, event_id, provider, forecast_time, temperature_f,
            humidity_pct, precipitation_probability_pct, wind_speed_mph,
            wind_direction_deg, short_forecast, raw_json, retrieved_at
        )
        SELECT
            id, run_id, event_id, provider, forecast_time, temperature_f,
            humidity_pct, precipitation_probability_pct, wind_speed_mph,
            wind_direction_deg, short_forecast, raw_json, retrieved_at
        FROM _legacy_weather_snapshots
        """
    )
    connection.execute(
        """
        INSERT INTO collector_errors(
            id, run_id, stage, provider, event_id, message, details, created_at
        )
        SELECT id, run_id, stage, NULL, event_id, message, details, created_at
        FROM _legacy_collector_errors
        """
    )

    legacy_run_errors = connection.execute(
        """
        SELECT run_id, error_message FROM _legacy_collector_runs
        WHERE error_message IS NOT NULL
        """
    ).fetchall()
    for run_id, error_message in legacy_run_errors:
        connection.execute(
            "UPDATE collector_runs SET error_message=? WHERE run_id=?",
            (redact_text(error_message), run_id),
        )
    legacy_collector_errors = connection.execute(
        """
        SELECT id, message, details FROM _legacy_collector_errors ORDER BY id
        """
    ).fetchall()
    for error_id, message, details in legacy_collector_errors:
        connection.execute(
            "UPDATE collector_errors SET message=?, details=? WHERE id=?",
            (
                redact_text(message),
                redact_text(details) if details is not None else None,
                error_id,
            ),
        )

    runs = connection.execute(
        """
        SELECT run_id, status, started_at, completed_at, error_message
        FROM _legacy_collector_runs ORDER BY run_id
        """
    ).fetchall()
    for run_id, status, started_at, completed_at, error_message in runs:
        safe_error_message = (
            redact_text(error_message) if error_message is not None else None
        )
        connection.execute(
            """
            INSERT INTO collector_run_transitions(
                run_id, from_status, to_status, failure_stage, error_message,
                transitioned_at
            ) VALUES (?, NULL, 'queued', NULL, NULL, ?)
            """,
            (run_id, started_at),
        )
        connection.execute(
            """
            INSERT INTO collector_run_transitions(
                run_id, from_status, to_status, failure_stage, error_message,
                transitioned_at
            ) VALUES (?, 'queued', 'running', NULL, NULL, ?)
            """,
            (run_id, started_at),
        )
        if status != "running":
            connection.execute(
                """
                INSERT INTO collector_run_transitions(
                    run_id, from_status, to_status, failure_stage, error_message,
                    transitioned_at
                ) VALUES (?, 'running', ?, ?, ?, ?)
                """,
                (
                    run_id,
                    status,
                    "worker_execution" if status == "failed" else None,
                    safe_error_message,
                    completed_at or started_at,
                ),
            )

    counts: dict[str, int] = {}
    for table_name in (
        "collector_runs",
        "games",
        "run_games",
        "odds_snapshots",
        "weather_snapshots",
        "collector_errors",
    ):
        row = connection.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()
        counts[table_name] = int(row[0])
    artifact_row = connection.execute(
        """
        SELECT COUNT(*) FROM _legacy_collector_runs
        WHERE artifact_zip IS NOT NULL AND artifact_zip <> ''
        """
    ).fetchone()
    counts["legacy_artifact_paths_retained_in_backup_only"] = int(artifact_row[0])
    return counts


def _drop_legacy_tables(connection: sqlite3.Connection) -> None:
    for table_name in (
        "_legacy_odds_snapshots",
        "_legacy_weather_snapshots",
        "_legacy_collector_errors",
        "_legacy_games",
        "_legacy_collector_runs",
    ):
        connection.execute(f'DROP TABLE "{table_name}"')


def _upgrade_legacy_schema(
    connection: sqlite3.Connection, database_path: Path
) -> MigrationResult:
    started_at = _utc_now()
    source_fingerprint = schema_fingerprint(connection)
    migration_dir = _migration_directory(database_path)
    suffix = started_at.replace(":", "").replace("+", "_")
    backup_path = migration_dir / f"{database_path.name}.pre-v1-{suffix}.sqlite3"
    diagnostic_path = migration_dir / _diagnostic_filename(started_at)

    _create_verified_backup(database_path, backup_path, source_fingerprint)
    diagnostic = _base_diagnostic(
        status="backup_verified",
        database_path=database_path,
        source_kind="legacy_inline",
        source_fingerprint=source_fingerprint,
        started_at=started_at,
        backup_path=backup_path,
    )
    _write_atomic_diagnostic(diagnostic_path, diagnostic)

    connection.execute("BEGIN IMMEDIATE")
    try:
        if schema_fingerprint(connection) != LEGACY_SCHEMA_FINGERPRINT:
            raise UnknownSchemaError("Legacy schema changed before migration lock acquisition")
        _rename_legacy_tables(connection)
        _execute_statements(connection, FORMAL_SCHEMA_V1_STATEMENTS)
        counts = _copy_legacy_data(connection)
        _drop_legacy_tables(connection)
        completed_at = _utc_now()
        connection.execute("PRAGMA user_version=1")
        _assert_target_schema(connection, 1)
        _insert_migration_records(
            connection,
            source_kind="legacy_inline",
            source_fingerprint=source_fingerprint,
            backup_path=backup_path,
            diagnostic_path=diagnostic_path,
            started_at=started_at,
            completed_at=completed_at,
            details={"legacy_rows_preserved": counts},
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    diagnostic.update(
        {
            "completed_at": completed_at,
            "legacy_rows_preserved": counts,
            "status": "migration_verified",
        }
    )
    _write_atomic_diagnostic(diagnostic_path, diagnostic)

    return MigrationResult(
        version=1,
        schema_fingerprint=FORMAL_SCHEMA_V1_FINGERPRINT,
        migrated=True,
        source_kind="legacy_inline",
        backup_path=backup_path,
        diagnostic_path=diagnostic_path,
    )


def _upgrade_formal_v1_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 1:
        raise SchemaVerificationError("Formal v1 upgrade requires PRAGMA user_version=1")
    _assert_target_schema(connection, 1)

    # Rebuilding collector_runs requires temporarily disabling FK enforcement;
    # the complete migration remains atomic and is checked before commit.
    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v2 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V1_FINGERPRINT:
            raise SchemaVerificationError("Formal v1 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V2_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (2, ?, ?, ?)
            """,
            (MIGRATION_V2_NAME, MIGRATION_V2_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=2")
        _assert_target_schema(connection, 2)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v2 upgrade"
        )
    _assert_target_schema(connection, 2)
    return MigrationResult(
        version=2,
        schema_fingerprint=FORMAL_SCHEMA_V2_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v1",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v2_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 2:
        raise SchemaVerificationError("Formal v2 upgrade requires PRAGMA user_version=2")
    _assert_target_schema(connection, 2)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v3 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V2_FINGERPRINT:
            raise SchemaVerificationError("Formal v2 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V3_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (3, ?, ?, ?)
            """,
            (MIGRATION_V3_NAME, MIGRATION_V3_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=3")
        _assert_target_schema(connection, 3)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v3 upgrade"
        )
    _assert_target_schema(connection, 3)
    return MigrationResult(
        version=3,
        schema_fingerprint=FORMAL_SCHEMA_V3_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v2",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v3_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 3:
        raise SchemaVerificationError("Formal v3 upgrade requires PRAGMA user_version=3")
    _assert_target_schema(connection, 3)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v4 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V3_FINGERPRINT:
            raise SchemaVerificationError("Formal v3 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V4_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (4, ?, ?, ?)
            """,
            (MIGRATION_V4_NAME, MIGRATION_V4_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=4")
        _assert_target_schema(connection, 4)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v4 upgrade"
        )
    _assert_target_schema(connection, 4)
    return MigrationResult(
        version=4,
        schema_fingerprint=FORMAL_SCHEMA_V4_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v3",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v4_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 4:
        raise SchemaVerificationError("Formal v4 upgrade requires PRAGMA user_version=4")
    _assert_target_schema(connection, 4)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v5 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V4_FINGERPRINT:
            raise SchemaVerificationError("Formal v4 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V5_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (5, ?, ?, ?)
            """,
            (MIGRATION_V5_NAME, MIGRATION_V5_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=5")
        _assert_target_schema(connection, 5)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v5 upgrade"
        )
    _assert_target_schema(connection, 5)
    return MigrationResult(
        version=5,
        schema_fingerprint=FORMAL_SCHEMA_V5_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v4",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v5_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 5:
        raise SchemaVerificationError("Formal v5 upgrade requires PRAGMA user_version=5")
    _assert_target_schema(connection, 5)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v6 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V5_FINGERPRINT:
            raise SchemaVerificationError("Formal v5 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V6_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (6, ?, ?, ?)
            """,
            (MIGRATION_V6_NAME, MIGRATION_V6_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=6")
        _assert_target_schema(connection, 6)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v6 upgrade"
        )
    _assert_target_schema(connection, 6)
    return MigrationResult(
        version=6,
        schema_fingerprint=FORMAL_SCHEMA_V6_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v5",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v6_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 6:
        raise SchemaVerificationError("Formal v6 upgrade requires PRAGMA user_version=6")
    _assert_target_schema(connection, 6)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v7 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V6_FINGERPRINT:
            raise SchemaVerificationError("Formal v6 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V7_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (7, ?, ?, ?)
            """,
            (MIGRATION_V7_NAME, MIGRATION_V7_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=7")
        _assert_target_schema(connection, 7)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v7 upgrade"
        )
    _assert_target_schema(connection, 7)
    return MigrationResult(
        version=7,
        schema_fingerprint=FORMAL_SCHEMA_V7_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v6",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _upgrade_formal_v7_schema(
    connection: sqlite3.Connection,
    prior_result: MigrationResult | None = None,
) -> MigrationResult:
    if int(connection.execute("PRAGMA user_version").fetchone()[0]) != 7:
        raise SchemaVerificationError("Formal v7 upgrade requires PRAGMA user_version=7")
    _assert_target_schema(connection, 7)

    connection.execute("PRAGMA foreign_keys=OFF")
    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 0:
        raise SchemaVerificationError("Unable to prepare transactional schema v8 upgrade")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if schema_fingerprint(connection) != FORMAL_SCHEMA_V7_FINGERPRINT:
            raise SchemaVerificationError("Formal v7 schema changed before migration lock")
        _execute_statements(connection, FORMAL_SCHEMA_V8_STATEMENTS)
        applied_at = _utc_now()
        connection.execute(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at)
            VALUES (8, ?, ?, ?)
            """,
            (MIGRATION_V8_NAME, MIGRATION_V8_CHECKSUM, applied_at),
        )
        connection.execute("PRAGMA user_version=8")
        _assert_target_schema(connection, 8)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")

    if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
        raise SchemaVerificationError(
            "Foreign-key enforcement could not be restored after schema v8 upgrade"
        )
    _assert_target_schema(connection, 8)
    return MigrationResult(
        version=8,
        schema_fingerprint=FORMAL_SCHEMA_V8_FINGERPRINT,
        migrated=True,
        source_kind=prior_result.source_kind if prior_result else "formal_v7",
        backup_path=prior_result.backup_path if prior_result else None,
        diagnostic_path=prior_result.diagnostic_path if prior_result else None,
    )


def _validate_versioned_schema(connection: sqlite3.Connection) -> MigrationResult:
    rows = connection.execute(
        "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
    ).fetchall()
    if not rows:
        raise MigrationChecksumError("Versioned schema has no migration records")
    newest = int(rows[-1][0])
    if newest > CURRENT_SCHEMA_VERSION:
        raise NewerSchemaVersionError(
            f"Database schema version {newest} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    observed = [(int(row[0]), str(row[1]), str(row[2])) for row in rows]
    expected = list(MIGRATION_HISTORY[:newest])
    if observed != expected:
        raise MigrationChecksumError("Applied migration history or checksum is not recognized")
    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if user_version > CURRENT_SCHEMA_VERSION:
        raise NewerSchemaVersionError(
            f"Database user_version {user_version} is newer than supported version "
            f"{CURRENT_SCHEMA_VERSION}"
        )
    if user_version != newest:
        raise SchemaVerificationError("PRAGMA user_version does not match migration history")
    _assert_target_schema(connection, newest)
    fingerprints = {
        1: FORMAL_SCHEMA_V1_FINGERPRINT,
        2: FORMAL_SCHEMA_V2_FINGERPRINT,
        3: FORMAL_SCHEMA_V3_FINGERPRINT,
        4: FORMAL_SCHEMA_V4_FINGERPRINT,
        5: FORMAL_SCHEMA_V5_FINGERPRINT,
        6: FORMAL_SCHEMA_V6_FINGERPRINT,
        7: FORMAL_SCHEMA_V7_FINGERPRINT,
        8: FORMAL_SCHEMA_V8_FINGERPRINT,
    }
    return MigrationResult(
        version=newest,
        schema_fingerprint=fingerprints[newest],
        migrated=False,
        source_kind=f"formal_v{newest}",
    )


def ensure_schema(database_path: Path) -> MigrationResult:
    path = database_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=5.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if user_version > CURRENT_SCHEMA_VERSION:
            raise NewerSchemaVersionError(
                f"Database user_version {user_version} is newer than supported version "
                f"{CURRENT_SCHEMA_VERSION}"
            )

        object_names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        if not object_names:
            versioned_result = _install_empty_schema(connection, path)
        elif "schema_migrations" in object_names:
            versioned_result = _validate_versioned_schema(connection)
        elif schema_fingerprint(connection) == LEGACY_SCHEMA_FINGERPRINT:
            versioned_result = _upgrade_legacy_schema(connection, path)
        else:
            raise UnknownSchemaError(
                "Unversioned database does not match the exact supported legacy schema"
            )

        result = (
            _upgrade_formal_v1_schema(connection, versioned_result)
            if versioned_result.version == 1
            else versioned_result
        )
        if result.version == 2:
            result = _upgrade_formal_v2_schema(connection, result)
        if result.version == 3:
            result = _upgrade_formal_v3_schema(connection, result)
        if result.version == 4:
            result = _upgrade_formal_v4_schema(connection, result)
        if result.version == 5:
            result = _upgrade_formal_v5_schema(connection, result)
        if result.version == 6:
            result = _upgrade_formal_v6_schema(connection, result)
        if result.version == 7:
            result = _upgrade_formal_v7_schema(connection, result)

        journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0])
        if journal_mode.lower() != "wal":
            raise SchemaVerificationError("File-backed databases must use WAL journal mode")
        if int(connection.execute("PRAGMA foreign_keys").fetchone()[0]) != 1:
            raise SchemaVerificationError("SQLite foreign-key enforcement is unavailable")
        return result
    finally:
        connection.close()
