from __future__ import annotations


# Migration v6 changes only the durable player-game snapshot grain. Migration v5
# remains immutable and checksum-pinned. Existing fielding revision chains cannot
# prove which entries were later provider corrections because v5 omitted source-row
# identity. The conservative transformation therefore preserves every row and its
# checksums while assigning each historical fielding chain member its own source-row
# key and resetting it to an initial observation. Future corrections revision only
# within the same explicit source-row key.
FORMAL_SCHEMA_V6_STATEMENTS = (
    "DROP TRIGGER publication_batches_validate_draft",
    """
    CREATE TABLE _collector_runs_v6 (
        run_id TEXT PRIMARY KEY,
        requested_date TEXT NOT NULL CHECK (
            requested_date GLOB
            '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
        ),
        status TEXT NOT NULL CHECK (
            status IN ('queued', 'running', 'completed', 'completed_with_warnings', 'failed')
        ),
        created_at TEXT NOT NULL,
        queued_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        updated_at TEXT NOT NULL,
        failure_stage TEXT CHECK (
            failure_stage IN (
                'before_worker_start', 'startup_reconciliation', 'worker_execution',
                'collector', 'persistence', 'artifact_generation'
            )
        ),
        error_message TEXT,
        artifact_relpath TEXT,
        app_version TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (
            schema_version IN (1, 2, 3, 4, 5, 6)
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
    INSERT INTO _collector_runs_v6(
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
    "ALTER TABLE _collector_runs_v6 RENAME TO collector_runs",
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
    CREATE TABLE _stats_fielding_grain_v6_guard (
        valid INTEGER NOT NULL CHECK (valid = 1)
    )
    """,
    """
    INSERT INTO _stats_fielding_grain_v6_guard(valid)
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM stats_game_player_snapshots
        WHERE role = 'fielding'
          AND (
              json_type(stats_json, '$.values.d_pos') NOT IN ('integer', 'real', 'text')
              OR length(trim(CAST(json_extract(stats_json, '$.values.d_pos') AS TEXT))) = 0
          )
    ) THEN 0 ELSE 1 END
    """,
    "DROP TABLE _stats_fielding_grain_v6_guard",
    "DROP TRIGGER stats_game_player_snapshots_validate_team",
    "DROP TRIGGER stats_game_player_snapshots_validate_raw_run",
    "DROP TRIGGER stats_game_player_snapshots_reject_update",
    "DROP TRIGGER stats_game_player_snapshots_reject_delete",
    "DROP INDEX idx_stats_game_player_history",
    "DROP INDEX uq_stats_game_player_normalized",
    "ALTER TABLE stats_game_player_snapshots RENAME TO _stats_game_player_snapshots_v5",
    """
    CREATE TABLE stats_game_player_snapshots (
        player_snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
        stats_run_id TEXT NOT NULL,
        game_identity_id TEXT NOT NULL,
        team_identity_id TEXT NOT NULL,
        player_identity_id TEXT NOT NULL,
        raw_payload_id TEXT,
        role TEXT NOT NULL CHECK (
            role IN ('batting', 'pitching', 'fielding', 'baserunning')
        ),
        source_row_key TEXT NOT NULL CHECK (length(trim(source_row_key)) > 0),
        position_code TEXT CHECK (
            position_code IS NULL OR length(trim(position_code)) > 0
        ),
        source_stint_key TEXT NOT NULL CHECK (length(trim(source_stint_key)) > 0),
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
        CHECK (
            (role = 'fielding' AND position_code IS NOT NULL)
            OR (role <> 'fielding' AND position_code IS NULL)
        ),
        CHECK (
            source_row_key = role || ':' || COALESCE(position_code, '_') || ':'
                || source_stint_key
        ),
        UNIQUE(
            game_identity_id, team_identity_id, player_identity_id, role,
            source_row_key, revision_number
        ),
        UNIQUE(
            game_identity_id, team_identity_id, player_identity_id, role,
            source_row_key, source_checksum
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
            ON DELETE RESTRICT
    )
    """,
    """
    INSERT INTO stats_game_player_snapshots(
        player_snapshot_id, stats_run_id, game_identity_id, team_identity_id,
        player_identity_id, raw_payload_id, role, source_row_key, position_code,
        source_stint_key, provider_updated_at, retrieved_at, revision_number,
        revision_kind, stats_json, normalized_checksum, source_checksum
    )
    WITH ranked AS (
        SELECT
            snapshot.*,
            CASE
                WHEN role = 'fielding' THEN
                    trim(CAST(json_extract(stats_json, '$.values.d_pos') AS TEXT))
                ELSE NULL
            END AS migrated_position_code,
            CASE
                WHEN role = 'fielding' THEN
                    ROW_NUMBER() OVER (
                        PARTITION BY
                            game_identity_id,
                            team_identity_id,
                            player_identity_id,
                            role,
                            trim(CAST(
                                json_extract(stats_json, '$.values.d_pos') AS TEXT
                            ))
                        ORDER BY revision_number, player_snapshot_id
                    )
                ELSE 1
            END AS migrated_stint_number
        FROM _stats_game_player_snapshots_v5 AS snapshot
    )
    SELECT
        player_snapshot_id,
        stats_run_id,
        game_identity_id,
        team_identity_id,
        player_identity_id,
        raw_payload_id,
        role,
        CASE
            WHEN role = 'fielding' THEN
                role || ':' || migrated_position_code || ':'
                    || printf('%06d', migrated_stint_number)
            ELSE role || ':_:000001'
        END,
        migrated_position_code,
        CASE
            WHEN role = 'fielding' THEN printf('%06d', migrated_stint_number)
            ELSE '000001'
        END,
        provider_updated_at,
        retrieved_at,
        CASE WHEN role = 'fielding' THEN 1 ELSE revision_number END,
        CASE WHEN role = 'fielding' THEN 'initial' ELSE revision_kind END,
        stats_json,
        normalized_checksum,
        source_checksum
    FROM ranked
    ORDER BY player_snapshot_id
    """,
    "DROP TABLE _stats_game_player_snapshots_v5",
    """
    CREATE INDEX idx_stats_game_player_history
    ON stats_game_player_snapshots(
        player_identity_id, game_identity_id, role, source_row_key, revision_number
    )
    """,
    """
    CREATE UNIQUE INDEX uq_stats_game_player_normalized
    ON stats_game_player_snapshots(
        game_identity_id, team_identity_id, player_identity_id, role,
        source_row_key, normalized_checksum
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
        ) THEN RAISE(ABORT, 'player snapshot team is not part of the game') END;
    END
    """,
    """
    CREATE TRIGGER stats_game_player_snapshots_validate_raw_run
    BEFORE INSERT ON stats_game_player_snapshots
    WHEN NEW.raw_payload_id IS NOT NULL
    BEGIN
        SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM stats_raw_payload_metadata AS raw
            WHERE raw.raw_payload_id = NEW.raw_payload_id
              AND raw.stats_run_id = NEW.stats_run_id
        ) THEN RAISE(
            ABORT, 'stats_game_player_snapshots raw payload belongs to another run'
        ) END;
    END
    """,
    """
    CREATE TRIGGER stats_game_player_snapshots_reject_update
    BEFORE UPDATE ON stats_game_player_snapshots
    BEGIN
        SELECT RAISE(
            ABORT, 'stats_game_player_snapshots records are immutable'
        );
    END
    """,
    """
    CREATE TRIGGER stats_game_player_snapshots_reject_delete
    BEFORE DELETE ON stats_game_player_snapshots
    BEGIN
        SELECT RAISE(
            ABORT, 'stats_game_player_snapshots records are immutable'
        );
    END
    """,
)
