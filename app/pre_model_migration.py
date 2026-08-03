from __future__ import annotations

"""Formal schema-v12 persistence for phases 5 through 7.

The statements in this module are intentionally phase-specific.  Helpers only
format repeated SQLite predicates; they do not define a universal phase schema.
"""

_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def _sha(name: str) -> str:
    return f"length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*'"


def _aware(name: str) -> str:
    return (
        f"length(trim({name}))>0 AND "
        f"(substr({name},-1)='Z' OR substr({name},-6,1) IN ('+','-'))"
    )


PRE_MODEL_SCHEMA_V12_TABLE_STATEMENTS = (
    f"""
    CREATE TABLE data_quality_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'data_quality' CHECK (phase_key='data_quality'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        upstream_odds_weather_snapshot_id TEXT NOT NULL,
        upstream_odds_weather_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_checksum')}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','assessment_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha('snapshot_checksum')})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (
            evidence_manifest_relpath='data_quality/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'
        ),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha('evidence_manifest_checksum')}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        completed_at TEXT NOT NULL CHECK ({_aware('completed_at')}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_daily_slate_snapshot_id) REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_game_state_snapshot_id) REFERENCES game_state_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_baseball_intelligence_snapshot_id) REFERENCES baseball_intelligence_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_odds_weather_snapshot_id) REFERENCES odds_weather_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL))
    )
    """,
    f"""
    CREATE TABLE data_quality_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='data-quality:' || snapshot_checksum),
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'data_quality' CHECK (phase_key='data_quality'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_DATA_QUALITY_V1'),
        policy_version TEXT NOT NULL CHECK (length(trim(policy_version))>0),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        upstream_odds_weather_snapshot_id TEXT NOT NULL,
        upstream_odds_weather_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_checksum')}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha('snapshot_checksum')}),
        quality_state TEXT NOT NULL CHECK (quality_state IN ('clear','warning','degraded')),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=snapshot_checksum),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        issue_count INTEGER NOT NULL CHECK (issue_count>=0),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='data_quality/snapshots/' || snapshot_checksum || '/data_quality_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha('artifact_checksum')}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware('sealed_at')})),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES data_quality_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_daily_slate_snapshot_id) REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_game_state_snapshot_id) REFERENCES game_state_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_baseball_intelligence_snapshot_id) REFERENCES baseball_intelligence_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_odds_weather_snapshot_id) REFERENCES odds_weather_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE data_quality_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        edge_event_id TEXT NOT NULL,
        daily_mlb_game_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        away_team_id TEXT NOT NULL,
        home_team_id TEXT NOT NULL CHECK (home_team_id<>away_team_id),
        disposition TEXT NOT NULL CHECK (disposition IN ('ready','degraded','insufficient')),
        model_ready INTEGER NOT NULL CHECK (model_ready IN (0,1)),
        upstream_daily_slate_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_game_checksum')}),
        upstream_game_state_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_game_checksum')}),
        upstream_baseball_intelligence_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_game_checksum')}),
        upstream_odds_weather_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_game_checksum')}),
        issue_count INTEGER NOT NULL CHECK (issue_count>=0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        UNIQUE(snapshot_id,edge_event_id),
        UNIQUE(snapshot_id,daily_mlb_game_id),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES data_quality_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE data_quality_issues (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        issue_code TEXT NOT NULL CHECK (length(trim(issue_code))>0),
        severity TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
        domain TEXT NOT NULL CHECK (length(trim(domain))>0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK (length(row_checksum)=64 AND row_checksum NOT GLOB '*[^0-9a-f]*'),
        PRIMARY KEY(snapshot_id,source_game_id,ordinal),
        UNIQUE(snapshot_id,source_game_id,issue_code,canonical_json),
        FOREIGN KEY(snapshot_id,source_game_id) REFERENCES data_quality_games(snapshot_id,source_game_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE matchup_packet_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'matchup_packet' CHECK (phase_key='matchup_packet'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        upstream_odds_weather_snapshot_id TEXT NOT NULL,
        upstream_odds_weather_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_checksum')}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha('upstream_data_quality_checksum')}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','assembly_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha('snapshot_checksum')})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='matchup_packet/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha('evidence_manifest_checksum')}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        completed_at TEXT NOT NULL CHECK ({_aware('completed_at')}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_daily_slate_snapshot_id) REFERENCES daily_slate_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_game_state_snapshot_id) REFERENCES game_state_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_baseball_intelligence_snapshot_id) REFERENCES baseball_intelligence_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_odds_weather_snapshot_id) REFERENCES odds_weather_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL))
    )
    """,
    f"""
    CREATE TABLE matchup_packet_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='matchup-packet:' || packet_checksum),
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'matchup_packet' CHECK (phase_key='matchup_packet'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MATCHUP_PACKET_V1'),
        assembly_policy_version TEXT NOT NULL CHECK (length(trim(assembly_policy_version))>0),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_daily_slate_snapshot_id TEXT NOT NULL,
        upstream_daily_slate_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_checksum')}),
        upstream_game_state_snapshot_id TEXT NOT NULL,
        upstream_game_state_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_checksum')}),
        upstream_baseball_intelligence_snapshot_id TEXT NOT NULL,
        upstream_baseball_intelligence_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_checksum')}),
        upstream_odds_weather_snapshot_id TEXT NOT NULL,
        upstream_odds_weather_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_checksum')}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha('upstream_data_quality_checksum')}),
        packet_checksum TEXT NOT NULL CHECK ({_sha('packet_checksum')}),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=packet_checksum),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='matchup_packet/snapshots/' || packet_checksum || '/matchup_packet_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha('artifact_checksum')}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware('sealed_at')})),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES matchup_packet_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE matchup_packet_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        edge_event_id TEXT NOT NULL,
        daily_mlb_game_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        away_team_id TEXT NOT NULL,
        home_team_id TEXT NOT NULL CHECK (home_team_id<>away_team_id),
        quality_disposition TEXT NOT NULL CHECK (quality_disposition IN ('ready','degraded','insufficient')),
        upstream_daily_slate_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_daily_slate_game_checksum')}),
        upstream_game_state_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_game_state_game_checksum')}),
        upstream_baseball_intelligence_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_baseball_intelligence_game_checksum')}),
        upstream_odds_weather_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_odds_weather_game_checksum')}),
        upstream_data_quality_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_data_quality_game_checksum')}),
        section_checksums_json TEXT NOT NULL CHECK (json_valid(section_checksums_json) AND json_type(section_checksums_json)='object'),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        UNIQUE(snapshot_id,edge_event_id),
        UNIQUE(snapshot_id,daily_mlb_game_id),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES matchup_packet_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE model_feature_set_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'model_feature_set' CHECK (phase_key='model_feature_set'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha('upstream_data_quality_checksum')}),
        upstream_matchup_packet_snapshot_id TEXT NOT NULL,
        upstream_matchup_packet_checksum TEXT NOT NULL CHECK ({_sha('upstream_matchup_packet_checksum')}),
        selected_feature_inventory_json TEXT NOT NULL CHECK (json_valid(selected_feature_inventory_json) AND json_type(selected_feature_inventory_json)='array'),
        selected_feature_inventory_checksum TEXT NOT NULL CHECK ({_sha('selected_feature_inventory_checksum')}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','transformation_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha('snapshot_checksum')})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='model_feature_set/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha('evidence_manifest_checksum')}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        completed_at TEXT NOT NULL CHECK ({_aware('completed_at')}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_matchup_packet_snapshot_id) REFERENCES matchup_packet_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL))
    )
    """,
    f"""
    CREATE TABLE model_feature_set_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='model-feature-set:' || feature_set_checksum),
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'model_feature_set' CHECK (phase_key='model_feature_set'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware('as_of_time')}),
        observed_at TEXT NOT NULL CHECK ({_aware('observed_at')}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MODEL_FEATURE_SET_V1'),
        schema_version TEXT NOT NULL CHECK (schema_version='DSE_MODEL_FEATURE_SCHEMA_V1'),
        schema_checksum TEXT NOT NULL CHECK ({_sha('schema_checksum')}),
        transformation_policy_version TEXT NOT NULL,
        missing_value_policy_version TEXT NOT NULL,
        encoding_policy_version TEXT NOT NULL,
        feature_version TEXT NOT NULL CHECK (feature_version='DSE_MLB_STATS_FEATURES_V3'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha('phase_input_checksum')}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha('upstream_data_quality_checksum')}),
        upstream_matchup_packet_snapshot_id TEXT NOT NULL,
        upstream_matchup_packet_checksum TEXT NOT NULL CHECK ({_sha('upstream_matchup_packet_checksum')}),
        selected_feature_inventory_json TEXT NOT NULL CHECK (json_valid(selected_feature_inventory_json) AND json_type(selected_feature_inventory_json)='array'),
        selected_feature_inventory_checksum TEXT NOT NULL CHECK ({_sha('selected_feature_inventory_checksum')}),
        feature_set_checksum TEXT NOT NULL CHECK ({_sha('feature_set_checksum')}),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=feature_set_checksum),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='model_feature_set/snapshots/' || feature_set_checksum || '/model_feature_set_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha('artifact_checksum')}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware('sealed_at')})),
        created_at TEXT NOT NULL CHECK ({_aware('created_at')}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES model_feature_set_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_matchup_packet_snapshot_id) REFERENCES matchup_packet_snapshots(snapshot_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE model_feature_set_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        edge_event_id TEXT NOT NULL,
        daily_mlb_game_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        away_team_id TEXT NOT NULL,
        home_team_id TEXT NOT NULL CHECK (home_team_id<>away_team_id),
        upstream_matchup_packet_game_checksum TEXT NOT NULL CHECK ({_sha('upstream_matchup_packet_game_checksum')}),
        quality_disposition TEXT NOT NULL CHECK (quality_disposition IN ('ready','degraded','insufficient')),
        quality_issue_codes_json TEXT NOT NULL CHECK (json_valid(quality_issue_codes_json) AND json_type(quality_issue_codes_json)='array'),
        predictive_features_json TEXT NOT NULL CHECK (json_valid(predictive_features_json) AND json_type(predictive_features_json)='array' AND json_array_length(predictive_features_json)=613),
        missing_feature_names_json TEXT NOT NULL CHECK (json_valid(missing_feature_names_json) AND json_type(missing_feature_names_json)='array'),
        available_feature_count INTEGER NOT NULL CHECK (available_feature_count BETWEEN 0 AND 613),
        predictive_feature_checksum TEXT NOT NULL CHECK ({_sha('predictive_feature_checksum')}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json)),
        row_checksum TEXT NOT NULL CHECK ({_sha('row_checksum')}),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        UNIQUE(snapshot_id,edge_event_id),
        UNIQUE(snapshot_id,daily_mlb_game_id),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES model_feature_set_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE model_feature_set_market_contexts (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        market_context_json TEXT NOT NULL CHECK (json_valid(market_context_json) AND json_type(market_context_json)='object'),
        market_context_checksum TEXT NOT NULL CHECK ({_sha('market_context_checksum')}),
        PRIMARY KEY(snapshot_id,source_game_id),
        FOREIGN KEY(snapshot_id,source_game_id) REFERENCES model_feature_set_games(snapshot_id,source_game_id) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE model_feature_set_source_features (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        feature_snapshot_id TEXT NOT NULL,
        feature_checksum TEXT NOT NULL CHECK ({_sha('feature_checksum')}),
        PRIMARY KEY(snapshot_id,source_game_id,ordinal),
        UNIQUE(snapshot_id,source_game_id,feature_snapshot_id),
        FOREIGN KEY(snapshot_id,source_game_id) REFERENCES model_feature_set_games(snapshot_id,source_game_id) ON DELETE RESTRICT,
        FOREIGN KEY(feature_snapshot_id) REFERENCES stats_feature_snapshots(feature_snapshot_id) ON DELETE RESTRICT
    )
    """,
)


PRE_MODEL_SCHEMA_V12_INDEX_STATEMENTS = (
    "CREATE INDEX idx_dq_attempt_run_date ON data_quality_attempt_evidence(run_id,requested_date,outcome)",
    "CREATE INDEX idx_dq_snapshot_lineage ON data_quality_snapshots(upstream_odds_weather_snapshot_id,upstream_odds_weather_checksum)",
    "CREATE INDEX idx_dq_game_identity ON data_quality_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_dq_issue_code ON data_quality_issues(issue_code,severity,snapshot_id)",
    "CREATE INDEX idx_mp_attempt_run_date ON matchup_packet_attempt_evidence(run_id,requested_date,outcome)",
    "CREATE INDEX idx_mp_snapshot_lineage ON matchup_packet_snapshots(upstream_data_quality_snapshot_id,upstream_data_quality_checksum)",
    "CREATE INDEX idx_mp_game_identity ON matchup_packet_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_mfs_attempt_run_date ON model_feature_set_attempt_evidence(run_id,requested_date,outcome)",
    "CREATE INDEX idx_mfs_snapshot_lineage ON model_feature_set_snapshots(upstream_matchup_packet_snapshot_id,upstream_matchup_packet_checksum)",
    "CREATE INDEX idx_mfs_game_identity ON model_feature_set_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_mfs_source_feature_lookup ON model_feature_set_source_features(feature_snapshot_id,feature_checksum,snapshot_id)",
)


PRE_MODEL_SCHEMA_V12_VALIDATION_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER data_quality_attempt_validate_phase BEFORE INSERT ON data_quality_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_runs run
        JOIN pipeline_run_phases phase ON phase.run_id=run.run_id AND phase.phase_key='data_quality'
        JOIN daily_slate_snapshots slate ON slate.snapshot_id=NEW.upstream_daily_slate_snapshot_id
        JOIN game_state_snapshots state ON state.snapshot_id=NEW.upstream_game_state_snapshot_id
        JOIN baseball_intelligence_snapshots bia ON bia.snapshot_id=NEW.upstream_baseball_intelligence_snapshot_id
        JOIN odds_weather_snapshots ow ON ow.snapshot_id=NEW.upstream_odds_weather_snapshot_id
        WHERE run.run_id=NEW.run_id AND run.requested_date=NEW.requested_date AND run.as_of_time=NEW.as_of_time
          AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND slate.run_id=NEW.run_id AND slate.sealed_at IS NOT NULL AND slate.snapshot_checksum=NEW.upstream_daily_slate_checksum
          AND state.run_id=NEW.run_id AND state.sealed_at IS NOT NULL AND state.snapshot_checksum=NEW.upstream_game_state_checksum
          AND bia.run_id=NEW.run_id AND bia.sealed_at IS NOT NULL AND bia.assembly_checksum=NEW.upstream_baseball_intelligence_checksum
          AND ow.run_id=NEW.run_id AND ow.sealed_at IS NOT NULL AND ow.snapshot_checksum=NEW.upstream_odds_weather_checksum
          AND state.upstream_daily_slate_snapshot_id=slate.snapshot_id
          AND bia.upstream_game_state_snapshot_id=state.snapshot_id
          AND ow.upstream_baseball_intelligence_snapshot_id=bia.snapshot_id
      ) THEN RAISE(ABORT,'Data Quality attempt requires active phase and exact sealed upstream chain') END;
    END
    """,
    """
    CREATE TRIGGER data_quality_snapshot_validate_insert BEFORE INSERT ON data_quality_snapshots BEGIN
      SELECT CASE WHEN NEW.sealed_at IS NOT NULL THEN RAISE(ABORT,'Data Quality snapshot must begin unsealed') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM data_quality_attempt_evidence attempt
        WHERE attempt.run_id=NEW.run_id AND attempt.phase_attempt=NEW.phase_attempt
          AND attempt.outcome='assembled' AND attempt.snapshot_checksum=NEW.snapshot_checksum
          AND attempt.phase_input_checksum=NEW.phase_input_checksum
          AND attempt.observed_at=NEW.observed_at
          AND attempt.upstream_odds_weather_snapshot_id=NEW.upstream_odds_weather_snapshot_id
      ) THEN RAISE(ABORT,'Data Quality snapshot requires matching assembled attempt') END;
    END
    """,
    """
    CREATE TRIGGER data_quality_game_validate_insert BEFORE INSERT ON data_quality_games BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM data_quality_snapshots snapshot
        JOIN daily_slate_games slate ON slate.snapshot_id=snapshot.upstream_daily_slate_snapshot_id AND slate.ordinal=NEW.ordinal
        JOIN game_state_games state ON state.snapshot_id=snapshot.upstream_game_state_snapshot_id AND state.ordinal=NEW.ordinal
        JOIN baseball_intelligence_games bia ON bia.snapshot_id=snapshot.upstream_baseball_intelligence_snapshot_id AND bia.ordinal=NEW.ordinal
        JOIN odds_weather_games ow ON ow.snapshot_id=snapshot.upstream_odds_weather_snapshot_id AND ow.ordinal=NEW.ordinal
        WHERE snapshot.snapshot_id=NEW.snapshot_id AND snapshot.run_id=NEW.run_id AND snapshot.phase_attempt=NEW.phase_attempt AND snapshot.sealed_at IS NULL
          AND slate.source_game_id=NEW.source_game_id AND state.source_game_id=NEW.source_game_id AND bia.source_game_id=NEW.source_game_id AND ow.source_game_id=NEW.source_game_id
          AND slate.row_checksum=NEW.upstream_daily_slate_game_checksum AND state.row_checksum=NEW.upstream_game_state_game_checksum
          AND bia.row_checksum=NEW.upstream_baseball_intelligence_game_checksum AND ow.row_checksum=NEW.upstream_odds_weather_game_checksum
      ) THEN RAISE(ABORT,'Data Quality game requires exact ordered upstream lineage') END;
    END
    """,
    """
    CREATE TRIGGER data_quality_issue_validate_insert BEFORE INSERT ON data_quality_issues BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM data_quality_games game JOIN data_quality_snapshots snapshot ON snapshot.snapshot_id=game.snapshot_id
        WHERE game.snapshot_id=NEW.snapshot_id AND game.source_game_id=NEW.source_game_id AND snapshot.sealed_at IS NULL
      ) THEN RAISE(ABORT,'Data Quality issue requires an unsealed game parent') END;
    END
    """,
    """
    CREATE TRIGGER data_quality_snapshot_validate_seal BEFORE UPDATE OF sealed_at ON data_quality_snapshots BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR length(trim(NEW.sealed_at))=0 THEN RAISE(ABORT,'Data Quality seal is one-time') END;
      SELECT CASE WHEN (SELECT count(*) FROM data_quality_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
        OR (SELECT count(*) FROM data_quality_issues WHERE snapshot_id=OLD.snapshot_id)<>OLD.issue_count
        OR (OLD.game_count>0 AND ((SELECT min(ordinal) FROM data_quality_games WHERE snapshot_id=OLD.snapshot_id)<>1 OR (SELECT max(ordinal) FROM data_quality_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count))
        OR EXISTS (SELECT 1 FROM data_quality_games game WHERE game.snapshot_id=OLD.snapshot_id AND (SELECT count(*) FROM data_quality_issues issue WHERE issue.snapshot_id=game.snapshot_id AND issue.source_game_id=game.source_game_id)<>game.issue_count)
        THEN RAISE(ABORT,'Data Quality snapshot children are incomplete') END;
    END
    """,
    """
    CREATE TRIGGER matchup_packet_attempt_validate_phase BEFORE INSERT ON matchup_packet_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_run_phases phase
        JOIN data_quality_snapshots dq ON dq.snapshot_id=NEW.upstream_data_quality_snapshot_id
        WHERE phase.run_id=NEW.run_id AND phase.phase_key='matchup_packet' AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND dq.run_id=NEW.run_id AND dq.sealed_at IS NOT NULL AND dq.snapshot_checksum=NEW.upstream_data_quality_checksum
          AND dq.upstream_daily_slate_snapshot_id=NEW.upstream_daily_slate_snapshot_id
          AND dq.upstream_game_state_snapshot_id=NEW.upstream_game_state_snapshot_id
          AND dq.upstream_baseball_intelligence_snapshot_id=NEW.upstream_baseball_intelligence_snapshot_id
          AND dq.upstream_odds_weather_snapshot_id=NEW.upstream_odds_weather_snapshot_id
      ) THEN RAISE(ABORT,'Matchup Packet attempt requires active phase and exact sealed lineage') END;
    END
    """,
    """
    CREATE TRIGGER matchup_packet_snapshot_validate_insert BEFORE INSERT ON matchup_packet_snapshots BEGIN
      SELECT CASE WHEN NEW.sealed_at IS NOT NULL THEN RAISE(ABORT,'Matchup Packet snapshot must begin unsealed') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM matchup_packet_attempt_evidence attempt WHERE attempt.run_id=NEW.run_id AND attempt.phase_attempt=NEW.phase_attempt AND attempt.outcome='assembled' AND attempt.snapshot_checksum=NEW.packet_checksum AND attempt.phase_input_checksum=NEW.phase_input_checksum AND attempt.upstream_data_quality_snapshot_id=NEW.upstream_data_quality_snapshot_id) THEN RAISE(ABORT,'Matchup Packet snapshot requires matching assembled attempt') END;
    END
    """,
    """
    CREATE TRIGGER matchup_packet_game_validate_insert BEFORE INSERT ON matchup_packet_games BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM matchup_packet_snapshots snapshot
        JOIN data_quality_games dq ON dq.snapshot_id=snapshot.upstream_data_quality_snapshot_id AND dq.ordinal=NEW.ordinal
        WHERE snapshot.snapshot_id=NEW.snapshot_id AND snapshot.run_id=NEW.run_id AND snapshot.phase_attempt=NEW.phase_attempt AND snapshot.sealed_at IS NULL
          AND dq.source_game_id=NEW.source_game_id AND dq.row_checksum=NEW.upstream_data_quality_game_checksum
      ) THEN RAISE(ABORT,'Matchup Packet game requires exact Data Quality lineage') END;
    END
    """,
    """
    CREATE TRIGGER matchup_packet_snapshot_validate_seal BEFORE UPDATE OF sealed_at ON matchup_packet_snapshots BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR length(trim(NEW.sealed_at))=0 THEN RAISE(ABORT,'Matchup Packet seal is one-time') END;
      SELECT CASE WHEN (SELECT count(*) FROM matchup_packet_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
        OR (OLD.game_count>0 AND ((SELECT min(ordinal) FROM matchup_packet_games WHERE snapshot_id=OLD.snapshot_id)<>1 OR (SELECT max(ordinal) FROM matchup_packet_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count))
        THEN RAISE(ABORT,'Matchup Packet children are incomplete') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_attempt_validate_phase BEFORE INSERT ON model_feature_set_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_run_phases phase
        JOIN matchup_packet_snapshots packet ON packet.snapshot_id=NEW.upstream_matchup_packet_snapshot_id
        JOIN data_quality_snapshots dq ON dq.snapshot_id=NEW.upstream_data_quality_snapshot_id
        WHERE phase.run_id=NEW.run_id AND phase.phase_key='model_feature_set' AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND packet.run_id=NEW.run_id AND packet.sealed_at IS NOT NULL AND packet.packet_checksum=NEW.upstream_matchup_packet_checksum
          AND dq.run_id=NEW.run_id AND dq.sealed_at IS NOT NULL AND dq.snapshot_checksum=NEW.upstream_data_quality_checksum
          AND packet.upstream_data_quality_snapshot_id=dq.snapshot_id
      ) THEN RAISE(ABORT,'Model Feature Set attempt requires active phase and exact sealed lineage') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_snapshot_validate_insert BEFORE INSERT ON model_feature_set_snapshots BEGIN
      SELECT CASE WHEN NEW.sealed_at IS NOT NULL THEN RAISE(ABORT,'Model Feature Set snapshot must begin unsealed') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM model_feature_set_attempt_evidence attempt WHERE attempt.run_id=NEW.run_id AND attempt.phase_attempt=NEW.phase_attempt AND attempt.outcome='assembled' AND attempt.snapshot_checksum=NEW.feature_set_checksum AND attempt.phase_input_checksum=NEW.phase_input_checksum AND attempt.selected_feature_inventory_checksum=NEW.selected_feature_inventory_checksum) THEN RAISE(ABORT,'Model Feature Set snapshot requires matching assembled attempt') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_game_validate_insert BEFORE INSERT ON model_feature_set_games BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM model_feature_set_snapshots snapshot
        JOIN matchup_packet_games packet ON packet.snapshot_id=snapshot.upstream_matchup_packet_snapshot_id AND packet.ordinal=NEW.ordinal
        WHERE snapshot.snapshot_id=NEW.snapshot_id AND snapshot.run_id=NEW.run_id AND snapshot.phase_attempt=NEW.phase_attempt AND snapshot.sealed_at IS NULL
          AND packet.source_game_id=NEW.source_game_id AND packet.row_checksum=NEW.upstream_matchup_packet_game_checksum
      ) THEN RAISE(ABORT,'Model Feature Set game requires exact Matchup Packet lineage') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_market_validate_insert BEFORE INSERT ON model_feature_set_market_contexts BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM model_feature_set_games game JOIN model_feature_set_snapshots snapshot ON snapshot.snapshot_id=game.snapshot_id WHERE game.snapshot_id=NEW.snapshot_id AND game.source_game_id=NEW.source_game_id AND snapshot.sealed_at IS NULL) THEN RAISE(ABORT,'Market context requires unsealed game parent') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_source_validate_insert BEFORE INSERT ON model_feature_set_source_features BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM model_feature_set_games game JOIN model_feature_set_snapshots snapshot ON snapshot.snapshot_id=game.snapshot_id WHERE game.snapshot_id=NEW.snapshot_id AND game.source_game_id=NEW.source_game_id AND snapshot.sealed_at IS NULL) THEN RAISE(ABORT,'Feature source requires unsealed game parent') END;
    END
    """,
    """
    CREATE TRIGGER model_feature_set_snapshot_validate_seal BEFORE UPDATE OF sealed_at ON model_feature_set_snapshots BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL OR length(trim(NEW.sealed_at))=0 THEN RAISE(ABORT,'Model Feature Set seal is one-time') END;
      SELECT CASE WHEN (SELECT count(*) FROM model_feature_set_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
        OR (SELECT count(*) FROM model_feature_set_market_contexts WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count
        OR (OLD.game_count>0 AND ((SELECT min(ordinal) FROM model_feature_set_games WHERE snapshot_id=OLD.snapshot_id)<>1 OR (SELECT max(ordinal) FROM model_feature_set_games WHERE snapshot_id=OLD.snapshot_id)<>OLD.game_count))
        THEN RAISE(ABORT,'Model Feature Set children are incomplete') END;
    END
    """,
)


_IMMUTABLE_TABLES = (
    "data_quality_attempt_evidence",
    "data_quality_games",
    "data_quality_issues",
    "matchup_packet_attempt_evidence",
    "matchup_packet_games",
    "model_feature_set_attempt_evidence",
    "model_feature_set_games",
    "model_feature_set_market_contexts",
    "model_feature_set_source_features",
)


PRE_MODEL_SCHEMA_V12_IMMUTABILITY_TRIGGER_STATEMENTS = tuple(
    statement
    for table in _IMMUTABLE_TABLES
    for statement in (
        f"CREATE TRIGGER {table}_reject_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'Pre-model retained evidence is immutable'); END",
        f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Pre-model retained evidence cannot be deleted'); END",
    )
) + tuple(
    statement
    for table in (
        "data_quality_snapshots",
        "matchup_packet_snapshots",
        "model_feature_set_snapshots",
    )
    for statement in (
        f"CREATE TRIGGER {table}_reject_semantic_update BEFORE UPDATE ON {table} WHEN NEW.sealed_at IS OLD.sealed_at OR OLD.sealed_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'Pre-model snapshot evidence is immutable'); END",
        f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'Pre-model snapshots cannot be deleted'); END",
    )
)


PRE_MODEL_SCHEMA_V12_STATEMENTS = (
    *PRE_MODEL_SCHEMA_V12_TABLE_STATEMENTS,
    *PRE_MODEL_SCHEMA_V12_INDEX_STATEMENTS,
    *PRE_MODEL_SCHEMA_V12_VALIDATION_TRIGGER_STATEMENTS,
    *PRE_MODEL_SCHEMA_V12_IMMUTABILITY_TRIGGER_STATEMENTS,
)
