from __future__ import annotations

"""Formal schema-v13 persistence for canonical pipeline phases 8 through 11."""

_DATE = "GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'"


def _sha(name: str) -> str:
    return f"length({name})=64 AND {name} NOT GLOB '*[^0-9a-f]*'"


def _aware(name: str) -> str:
    return f"length(trim({name}))>0 AND (substr({name},-1)='Z' OR substr({name},-6,1) IN ('+','-'))"


PREDICTION_DECISION_SCHEMA_V13_TABLE_STATEMENTS = (
    f"""
    CREATE TABLE prediction_authoring_inputs (
        input_id TEXT PRIMARY KEY CHECK (input_id='reviewed-analyst:' || input_checksum),
        run_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        upstream_model_feature_set_snapshot_id TEXT NOT NULL,
        upstream_model_feature_set_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_set_checksum")}),
        upstream_model_feature_game_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_game_checksum")}),
        predictive_feature_checksum TEXT NOT NULL CHECK ({_sha("predictive_feature_checksum")}),
        provider_policy_json TEXT NOT NULL CHECK (json_valid(provider_policy_json) AND json_type(provider_policy_json)='object'),
        provider_policy_checksum TEXT NOT NULL CHECK ({_sha("provider_policy_checksum")}),
        home_probability REAL NOT NULL CHECK (home_probability BETWEEN 0.0 AND 1.0),
        home_lower REAL NOT NULL CHECK (home_lower BETWEEN 0.0 AND home_probability),
        home_upper REAL NOT NULL CHECK (home_upper BETWEEN home_probability AND 1.0),
        generated_at TEXT NOT NULL CHECK ({_aware("generated_at")}),
        sealed_at TEXT NOT NULL CHECK ({_aware("sealed_at")}),
        market_independence_attested INTEGER NOT NULL CHECK (typeof(market_independence_attested)='integer' AND market_independence_attested=1),
        authoring_evidence_json TEXT NOT NULL CHECK (json_valid(authoring_evidence_json) AND json_type(authoring_evidence_json)='object'),
        evidence_checksum TEXT NOT NULL CHECK ({_sha("evidence_checksum")}),
        input_checksum TEXT NOT NULL CHECK ({_sha("input_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=input_checksum),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        UNIQUE(run_id,source_game_id,upstream_model_feature_set_snapshot_id),
        FOREIGN KEY(run_id) REFERENCES pipeline_runs(run_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_model_feature_set_snapshot_id) REFERENCES model_feature_set_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (provider_policy_checksum=json_extract(provider_policy_json,'$.checksum')),
        CHECK (json_extract(canonical_json,'$.market_independence_attested')=market_independence_attested),
        CHECK (julianday(sealed_at)>=julianday(generated_at))
    )
    """,
    f"""
    CREATE TABLE predictions_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'predictions' CHECK (phase_key='predictions'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        observed_at TEXT NOT NULL CHECK ({_aware("observed_at")}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        provider_policy_json TEXT NOT NULL CHECK (json_valid(provider_policy_json) AND json_type(provider_policy_json)='object'),
        provider_policy_checksum TEXT NOT NULL CHECK ({_sha("provider_policy_checksum")}),
        upstream_model_feature_set_snapshot_id TEXT NOT NULL,
        upstream_model_feature_set_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_set_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        expected_game_ids_json TEXT NOT NULL CHECK (json_valid(expected_game_ids_json) AND json_type(expected_game_ids_json)='array'),
        present_input_checksums_json TEXT NOT NULL CHECK (json_valid(present_input_checksums_json) AND json_type(present_input_checksums_json)='array'),
        missing_game_ids_json TEXT NOT NULL CHECK (json_valid(missing_game_ids_json) AND json_type(missing_game_ids_json)='array'),
        invalid_inputs_json TEXT NOT NULL CHECK (json_valid(invalid_inputs_json) AND json_type(invalid_inputs_json)='array'),
        invalid_input_count INTEGER NOT NULL CHECK (invalid_input_count>=0 AND invalid_input_count=json_array_length(invalid_inputs_json)),
        input_inventory_checksum TEXT NOT NULL CHECK ({_sha("input_inventory_checksum")}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','validation_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha("snapshot_checksum")})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='predictions/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha("evidence_manifest_checksum")}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        completed_at TEXT NOT NULL CHECK ({_aware("completed_at")}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_model_feature_set_snapshot_id) REFERENCES model_feature_set_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (provider_policy_checksum=json_extract(provider_policy_json,'$.checksum')),
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL)),
        CHECK (julianday(completed_at)>=julianday(created_at))
    )
    """,
    f"""
    CREATE TABLE prediction_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='predictions:' || snapshot_checksum),
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        observed_at TEXT NOT NULL CHECK ({_aware("observed_at")}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MLB_ML_PREDICTIONS_V1'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        provider_policy_json TEXT NOT NULL CHECK (json_valid(provider_policy_json) AND json_type(provider_policy_json)='object'),
        provider_policy_checksum TEXT NOT NULL CHECK ({_sha("provider_policy_checksum")}),
        upstream_model_feature_set_snapshot_id TEXT NOT NULL,
        upstream_model_feature_set_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_set_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        input_inventory_checksum TEXT NOT NULL CHECK ({_sha("input_inventory_checksum")}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha("snapshot_checksum")}),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        warning_count INTEGER NOT NULL CHECK (warning_count>=0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array' AND json_array_length(warnings_json)=warning_count),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=snapshot_checksum),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='predictions/snapshots/' || snapshot_checksum || '/predictions_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha("artifact_checksum")}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware("sealed_at")})),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES predictions_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_model_feature_set_snapshot_id) REFERENCES model_feature_set_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (provider_policy_checksum=json_extract(provider_policy_json,'$.checksum'))
    )
    """,
    f"""
    CREATE TABLE prediction_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        source_game_id TEXT NOT NULL,
        away_team_id TEXT NOT NULL,
        home_team_id TEXT NOT NULL CHECK (away_team_id<>home_team_id),
        scheduled_start_time TEXT NOT NULL CHECK ({_aware("scheduled_start_time")}),
        predictive_feature_checksum TEXT NOT NULL CHECK ({_sha("predictive_feature_checksum")}),
        upstream_model_feature_game_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_game_checksum")}),
        provider_identity_json TEXT NOT NULL CHECK (json_valid(provider_identity_json) AND json_type(provider_identity_json)='object'),
        provider_identity_checksum TEXT NOT NULL CHECK ({_sha("provider_identity_checksum")}),
        home_probability REAL NOT NULL CHECK (home_probability BETWEEN 0.0 AND 1.0),
        away_probability REAL NOT NULL CHECK (away_probability BETWEEN 0.0 AND 1.0),
        home_lower REAL NOT NULL CHECK (home_lower BETWEEN 0.0 AND home_probability),
        home_upper REAL NOT NULL CHECK (home_upper BETWEEN home_probability AND 1.0),
        away_lower REAL NOT NULL CHECK (away_lower BETWEEN 0.0 AND away_probability),
        away_upper REAL NOT NULL CHECK (away_upper BETWEEN away_probability AND 1.0),
        generated_at TEXT NOT NULL CHECK ({_aware("generated_at")}),
        prediction_sealed_at TEXT NOT NULL CHECK ({_aware("prediction_sealed_at")}),
        evidence_checksum TEXT NOT NULL CHECK ({_sha("evidence_checksum")}),
        market_independence_attested INTEGER NOT NULL CHECK (typeof(market_independence_attested)='integer' AND market_independence_attested=1),
        prediction_checksum TEXT NOT NULL CHECK ({_sha("prediction_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=prediction_checksum),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        UNIQUE(snapshot_id,prediction_checksum),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES prediction_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK (abs((home_probability+away_probability)-1.0)<=0.000000000001),
        CHECK (abs((home_lower+away_upper)-1.0)<=0.000000000001),
        CHECK (abs((home_upper+away_lower)-1.0)<=0.000000000001),
        CHECK ((home_upper-home_lower)>=0.10),
        CHECK (json_extract(canonical_json,'$.market_independence_attestation')=market_independence_attested),
        CHECK (julianday(prediction_sealed_at)>=julianday(generated_at)),
        CHECK (julianday(prediction_sealed_at)<julianday(scheduled_start_time))
    )
    """,
    f"""
    CREATE TABLE value_engine_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'value_engine' CHECK (phase_key='value_engine'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        evaluated_at TEXT NOT NULL CHECK ({_aware("evaluated_at")}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_predictions_snapshot_id TEXT NOT NULL,
        upstream_predictions_checksum TEXT NOT NULL CHECK ({_sha("upstream_predictions_checksum")}),
        upstream_model_feature_set_snapshot_id TEXT NOT NULL,
        upstream_model_feature_set_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_set_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        market_inventory_checksum TEXT NOT NULL CHECK ({_sha("market_inventory_checksum")}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','calculation_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha("snapshot_checksum")})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='value_engine/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha("evidence_manifest_checksum")}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        completed_at TEXT NOT NULL CHECK ({_aware("completed_at")}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_predictions_snapshot_id) REFERENCES prediction_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_model_feature_set_snapshot_id) REFERENCES model_feature_set_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum')),
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL)),
        CHECK (julianday(completed_at)>=julianday(created_at))
    )
    """,
    f"""
    CREATE TABLE value_engine_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='value-engine:' || snapshot_checksum),
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        evaluated_at TEXT NOT NULL CHECK ({_aware("evaluated_at")}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MLB_ML_VALUE_ENGINE_V1'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_predictions_snapshot_id TEXT NOT NULL,
        upstream_predictions_checksum TEXT NOT NULL CHECK ({_sha("upstream_predictions_checksum")}),
        upstream_model_feature_set_snapshot_id TEXT NOT NULL,
        upstream_model_feature_set_checksum TEXT NOT NULL CHECK ({_sha("upstream_model_feature_set_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        market_inventory_checksum TEXT NOT NULL CHECK ({_sha("market_inventory_checksum")}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha("snapshot_checksum")}),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        outcome_count INTEGER NOT NULL CHECK (outcome_count=game_count*2),
        warning_count INTEGER NOT NULL CHECK (warning_count>=0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_array_length(warnings_json)=warning_count),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=snapshot_checksum),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='value_engine/snapshots/' || snapshot_checksum || '/value_engine_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha("artifact_checksum")}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware("sealed_at")})),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES value_engine_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum'))
    )
    """,
    f"""
    CREATE TABLE value_engine_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        source_game_id TEXT NOT NULL,
        upstream_prediction_checksum TEXT NOT NULL CHECK ({_sha("upstream_prediction_checksum")}),
        market_context_checksum TEXT NOT NULL CHECK ({_sha("market_context_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=row_checksum),
        row_checksum TEXT NOT NULL CHECK ({_sha("row_checksum")}),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES value_engine_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE value_engine_outcomes (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal IN (1,2)),
        side TEXT NOT NULL CHECK (side IN ('home','away')),
        outcome_team_id TEXT NOT NULL CHECK (length(trim(outcome_team_id))>0),
        availability TEXT NOT NULL CHECK (availability IN ('available','unavailable')),
        prediction_probability REAL NOT NULL CHECK (prediction_probability BETWEEN 0.0 AND 1.0),
        probability_lower REAL NOT NULL CHECK (probability_lower BETWEEN 0.0 AND prediction_probability),
        probability_upper REAL NOT NULL CHECK (probability_upper BETWEEN prediction_probability AND 1.0),
        eligible_bookmaker_count INTEGER NOT NULL CHECK (eligible_bookmaker_count>=0),
        best_price REAL,
        consensus_no_vig_probability REAL CHECK (consensus_no_vig_probability IS NULL OR consensus_no_vig_probability BETWEEN 0.0 AND 1.0),
        break_even_probability REAL CHECK (break_even_probability IS NULL OR break_even_probability BETWEEN 0.0 AND 1.0),
        edge REAL,
        expected_value_per_unit REAL,
        lower_bound_clearance REAL,
        freshness_state TEXT NOT NULL CHECK (freshness_state IN ('fresh','unavailable')),
        market_context_checksum TEXT NOT NULL CHECK ({_sha("market_context_checksum")}),
        prediction_checksum TEXT NOT NULL CHECK ({_sha("prediction_checksum")}),
        exclusion_json TEXT NOT NULL CHECK (json_valid(exclusion_json) AND json_type(exclusion_json)='object'),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=row_checksum),
        row_checksum TEXT NOT NULL CHECK ({_sha("row_checksum")}),
        PRIMARY KEY(snapshot_id,source_game_id,side),
        UNIQUE(snapshot_id,source_game_id,ordinal),
        FOREIGN KEY(snapshot_id,source_game_id) REFERENCES value_engine_games(snapshot_id,source_game_id) ON DELETE RESTRICT,
        CHECK ((ordinal=1 AND side='home') OR (ordinal=2 AND side='away')),
        CHECK ((availability='available' AND best_price IS NOT NULL AND consensus_no_vig_probability IS NOT NULL AND break_even_probability IS NOT NULL AND edge IS NOT NULL AND expected_value_per_unit IS NOT NULL AND lower_bound_clearance IS NOT NULL) OR (availability='unavailable' AND best_price IS NULL AND consensus_no_vig_probability IS NULL AND break_even_probability IS NULL AND edge IS NULL AND expected_value_per_unit IS NULL AND lower_bound_clearance IS NULL))
    )
    """,
    f"""
    CREATE TABLE value_engine_book_pairs (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('home','away')),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        bookmaker_key TEXT NOT NULL CHECK (length(trim(bookmaker_key))>0),
        home_price REAL NOT NULL,
        away_price REAL NOT NULL,
        home_implied_probability REAL NOT NULL CHECK (home_implied_probability BETWEEN 0.0 AND 1.0),
        away_implied_probability REAL NOT NULL CHECK (away_implied_probability BETWEEN 0.0 AND 1.0),
        home_no_vig_probability REAL NOT NULL CHECK (home_no_vig_probability BETWEEN 0.0 AND 1.0),
        away_no_vig_probability REAL NOT NULL CHECK (away_no_vig_probability BETWEEN 0.0 AND 1.0),
        effective_timestamp TEXT NOT NULL CHECK ({_aware("effective_timestamp")}),
        retrieval_timestamp TEXT NOT NULL CHECK ({_aware("retrieval_timestamp")}),
        pair_checksum TEXT NOT NULL CHECK ({_sha("pair_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=pair_checksum),
        PRIMARY KEY(snapshot_id,source_game_id,side,ordinal),
        UNIQUE(snapshot_id,source_game_id,side,bookmaker_key,retrieval_timestamp),
        FOREIGN KEY(snapshot_id,source_game_id,side) REFERENCES value_engine_outcomes(snapshot_id,source_game_id,side) ON DELETE RESTRICT
    )
    """,
    f"""
    CREATE TABLE recommendation_gate_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'recommendation_gate' CHECK (phase_key='recommendation_gate'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        evaluated_at TEXT NOT NULL CHECK ({_aware("evaluated_at")}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_value_engine_snapshot_id TEXT NOT NULL,
        upstream_value_engine_checksum TEXT NOT NULL CHECK ({_sha("upstream_value_engine_checksum")}),
        upstream_predictions_snapshot_id TEXT NOT NULL,
        upstream_predictions_checksum TEXT NOT NULL CHECK ({_sha("upstream_predictions_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','evaluation_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha("snapshot_checksum")})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='recommendation_gate/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha("evidence_manifest_checksum")}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        completed_at TEXT NOT NULL CHECK ({_aware("completed_at")}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_value_engine_snapshot_id) REFERENCES value_engine_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_predictions_snapshot_id) REFERENCES prediction_snapshots(snapshot_id) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_data_quality_snapshot_id) REFERENCES data_quality_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum')),
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL)),
        CHECK (julianday(completed_at)>=julianday(created_at))
    )
    """,
    f"""
    CREATE TABLE recommendation_gate_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='recommendation-gate:' || snapshot_checksum),
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        evaluated_at TEXT NOT NULL CHECK ({_aware("evaluated_at")}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MLB_ML_RECOMMENDATION_GATE_V1'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_value_engine_snapshot_id TEXT NOT NULL,
        upstream_value_engine_checksum TEXT NOT NULL CHECK ({_sha("upstream_value_engine_checksum")}),
        upstream_predictions_snapshot_id TEXT NOT NULL,
        upstream_predictions_checksum TEXT NOT NULL CHECK ({_sha("upstream_predictions_checksum")}),
        upstream_data_quality_snapshot_id TEXT NOT NULL,
        upstream_data_quality_checksum TEXT NOT NULL CHECK ({_sha("upstream_data_quality_checksum")}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha("snapshot_checksum")}),
        game_count INTEGER NOT NULL CHECK (game_count>=0),
        recommended_game_count INTEGER NOT NULL CHECK (recommended_game_count BETWEEN 0 AND game_count),
        warning_count INTEGER NOT NULL CHECK (warning_count>=0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_array_length(warnings_json)=warning_count),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=snapshot_checksum),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='recommendation_gate/snapshots/' || snapshot_checksum || '/recommendation_gate_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha("artifact_checksum")}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware("sealed_at")})),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES recommendation_gate_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum'))
    )
    """,
    f"""
    CREATE TABLE recommendation_gate_games (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        source_game_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('recommend','pass','avoid')),
        selected_side TEXT CHECK (selected_side IS NULL OR selected_side IN ('home','away')),
        selected_team_id TEXT,
        upstream_value_game_checksum TEXT NOT NULL CHECK ({_sha("upstream_value_game_checksum")}),
        upstream_prediction_checksum TEXT NOT NULL CHECK ({_sha("upstream_prediction_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=row_checksum),
        row_checksum TEXT NOT NULL CHECK ({_sha("row_checksum")}),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES recommendation_gate_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK ((decision='recommend' AND selected_side IS NOT NULL AND selected_team_id IS NOT NULL) OR (decision<>'recommend' AND selected_side IS NULL AND selected_team_id IS NULL))
    )
    """,
    f"""
    CREATE TABLE recommendation_gate_sides (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal IN (1,2)),
        side TEXT NOT NULL CHECK (side IN ('home','away')),
        outcome_team_id TEXT NOT NULL CHECK (length(trim(outcome_team_id))>0),
        decision TEXT NOT NULL CHECK (decision IN ('recommend','pass','avoid')),
        upstream_outcome_checksum TEXT NOT NULL CHECK ({_sha("upstream_outcome_checksum")}),
        gate_count INTEGER NOT NULL CHECK (gate_count>=1),
        reason_codes_json TEXT NOT NULL CHECK (json_valid(reason_codes_json) AND json_type(reason_codes_json)='array'),
        side_checksum TEXT NOT NULL CHECK ({_sha("side_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=side_checksum),
        PRIMARY KEY(snapshot_id,source_game_id,side),
        UNIQUE(snapshot_id,source_game_id,ordinal),
        FOREIGN KEY(snapshot_id,source_game_id) REFERENCES recommendation_gate_games(snapshot_id,source_game_id) ON DELETE RESTRICT,
        CHECK ((ordinal=1 AND side='home') OR (ordinal=2 AND side='away'))
    )
    """,
    f"""
    CREATE TABLE recommendation_gate_results (
        snapshot_id TEXT NOT NULL,
        source_game_id TEXT NOT NULL,
        side TEXT NOT NULL CHECK (side IN ('home','away')),
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        gate_code TEXT NOT NULL CHECK (gate_code IN ('prediction_valid','prediction_market_independent','prediction_identity_valid','feature_lineage_valid','market_supported','minimum_bookmaker_count','odds_fresh','best_price_available','data_quality_model_ready','minimum_edge','minimum_ev','lower_bound_clears_market','uncertainty_acceptable','lineup_and_starter_risk','weather_evidence_acceptable','event_pregame','opposing_side_not_selected')),
        passed INTEGER NOT NULL CHECK (passed IN (0,1)),
        threshold_json TEXT NOT NULL CHECK (json_valid(threshold_json)),
        observed_json TEXT NOT NULL CHECK (json_valid(observed_json)),
        reason TEXT NOT NULL CHECK (length(trim(reason))>0),
        evaluated_at TEXT NOT NULL CHECK ({_aware("evaluated_at")}),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        source_checksum TEXT NOT NULL CHECK ({_sha("source_checksum")}),
        result_checksum TEXT NOT NULL CHECK ({_sha("result_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=result_checksum),
        PRIMARY KEY(snapshot_id,source_game_id,side,ordinal),
        UNIQUE(snapshot_id,source_game_id,side,gate_code),
        FOREIGN KEY(snapshot_id,source_game_id,side) REFERENCES recommendation_gate_sides(snapshot_id,source_game_id,side) ON DELETE RESTRICT,
        CHECK (ordinal=CASE gate_code WHEN 'prediction_valid' THEN 1 WHEN 'prediction_market_independent' THEN 2 WHEN 'prediction_identity_valid' THEN 3 WHEN 'feature_lineage_valid' THEN 4 WHEN 'market_supported' THEN 5 WHEN 'minimum_bookmaker_count' THEN 6 WHEN 'odds_fresh' THEN 7 WHEN 'best_price_available' THEN 8 WHEN 'data_quality_model_ready' THEN 9 WHEN 'minimum_edge' THEN 10 WHEN 'minimum_ev' THEN 11 WHEN 'lower_bound_clears_market' THEN 12 WHEN 'uncertainty_acceptable' THEN 13 WHEN 'lineup_and_starter_risk' THEN 14 WHEN 'weather_evidence_acceptable' THEN 15 WHEN 'event_pregame' THEN 16 WHEN 'opposing_side_not_selected' THEN 17 END)
    )
    """,
    f"""
    CREATE TABLE rankings_attempt_evidence (
        run_id TEXT NOT NULL,
        phase_key TEXT NOT NULL DEFAULT 'rankings' CHECK (phase_key='rankings'),
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        ranked_at TEXT NOT NULL CHECK ({_aware("ranked_at")}),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_recommendation_gate_snapshot_id TEXT NOT NULL,
        upstream_recommendation_gate_checksum TEXT NOT NULL CHECK ({_sha("upstream_recommendation_gate_checksum")}),
        outcome TEXT NOT NULL CHECK (outcome IN ('assembled','input_failed','ranking_failed','persistence_failed')),
        snapshot_checksum TEXT CHECK (snapshot_checksum IS NULL OR ({_sha("snapshot_checksum")})),
        evidence_manifest_relpath TEXT NOT NULL CHECK (evidence_manifest_relpath='rankings/attempts/' || run_id || '/attempt_' || printf('%04d',phase_attempt) || '.json'),
        evidence_manifest_checksum TEXT NOT NULL CHECK ({_sha("evidence_manifest_checksum")}),
        evidence_manifest_byte_count INTEGER NOT NULL CHECK (evidence_manifest_byte_count>0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_type(warnings_json)='array'),
        warning_count INTEGER NOT NULL CHECK (warning_count=json_array_length(warnings_json) AND warning_count>=0),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        completed_at TEXT NOT NULL CHECK ({_aware("completed_at")}),
        PRIMARY KEY(run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key) ON DELETE RESTRICT,
        FOREIGN KEY(upstream_recommendation_gate_snapshot_id) REFERENCES recommendation_gate_snapshots(snapshot_id) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum')),
        CHECK (json_extract(policy_json,'$.comparator')='["decision_eligibility","ev_desc","edge_desc","lower_bound_clearance_desc","interval_width_asc","data_quality_disposition","bookmaker_count_desc","scheduled_start_time","source_game_id","selected_team_id"]'),
        CHECK ((outcome='assembled' AND snapshot_checksum IS NOT NULL) OR (outcome<>'assembled' AND snapshot_checksum IS NULL)),
        CHECK (julianday(completed_at)>=julianday(created_at))
    )
    """,
    f"""
    CREATE TABLE ranking_snapshots (
        snapshot_id TEXT PRIMARY KEY CHECK (snapshot_id='rankings:' || snapshot_checksum),
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL CHECK (phase_attempt>=1),
        requested_date TEXT NOT NULL CHECK (requested_date {_DATE}),
        as_of_time TEXT NOT NULL CHECK ({_aware("as_of_time")}),
        ranked_at TEXT NOT NULL CHECK ({_aware("ranked_at")}),
        contract_version TEXT NOT NULL CHECK (contract_version='DSE_MLB_ML_RANKINGS_V1'),
        phase_input_checksum TEXT NOT NULL CHECK ({_sha("phase_input_checksum")}),
        policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND json_type(policy_json)='object'),
        policy_checksum TEXT NOT NULL CHECK ({_sha("policy_checksum")}),
        upstream_recommendation_gate_snapshot_id TEXT NOT NULL,
        upstream_recommendation_gate_checksum TEXT NOT NULL CHECK ({_sha("upstream_recommendation_gate_checksum")}),
        snapshot_checksum TEXT NOT NULL CHECK ({_sha("snapshot_checksum")}),
        entry_count INTEGER NOT NULL CHECK (entry_count>=0),
        eligible_count INTEGER NOT NULL CHECK (eligible_count BETWEEN 0 AND entry_count),
        warning_count INTEGER NOT NULL CHECK (warning_count>=0),
        warnings_json TEXT NOT NULL CHECK (json_valid(warnings_json) AND json_array_length(warnings_json)=warning_count),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=snapshot_checksum),
        artifact_relpath TEXT NOT NULL CHECK (artifact_relpath='rankings/snapshots/' || snapshot_checksum || '/rankings_v1.json'),
        artifact_checksum TEXT NOT NULL CHECK ({_sha("artifact_checksum")}),
        artifact_byte_count INTEGER NOT NULL CHECK (artifact_byte_count>0),
        sealed_at TEXT CHECK (sealed_at IS NULL OR ({_aware("sealed_at")})),
        created_at TEXT NOT NULL CHECK ({_aware("created_at")}),
        UNIQUE(run_id,phase_attempt),
        UNIQUE(snapshot_id,run_id,phase_attempt),
        FOREIGN KEY(run_id,phase_attempt) REFERENCES rankings_attempt_evidence(run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK (policy_checksum=json_extract(policy_json,'$.checksum')),
        CHECK (json_extract(policy_json,'$.comparator')='["decision_eligibility","ev_desc","edge_desc","lower_bound_clearance_desc","interval_width_asc","data_quality_disposition","bookmaker_count_desc","scheduled_start_time","source_game_id","selected_team_id"]')
    )
    """,
    f"""
    CREATE TABLE ranking_entries (
        snapshot_id TEXT NOT NULL,
        run_id TEXT NOT NULL,
        phase_attempt INTEGER NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal>=1),
        source_game_id TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('recommend','pass','avoid')),
        selected_side TEXT CHECK (selected_side IS NULL OR selected_side IN ('home','away')),
        selected_team_id TEXT,
        rank_eligible INTEGER NOT NULL CHECK (rank_eligible IN (0,1)),
        recommendation_rank INTEGER CHECK (recommendation_rank IS NULL OR (typeof(recommendation_rank)='integer' AND recommendation_rank>=1)),
        upstream_gate_game_checksum TEXT NOT NULL CHECK ({_sha("upstream_gate_game_checksum")}),
        entry_checksum TEXT NOT NULL CHECK ({_sha("entry_checksum")}),
        canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND json_extract(canonical_json,'$.checksum')=entry_checksum),
        PRIMARY KEY(snapshot_id,source_game_id),
        UNIQUE(snapshot_id,ordinal),
        FOREIGN KEY(snapshot_id,run_id,phase_attempt) REFERENCES ranking_snapshots(snapshot_id,run_id,phase_attempt) ON DELETE RESTRICT,
        CHECK ((rank_eligible=1 AND decision='recommend' AND recommendation_rank IS NOT NULL AND selected_side IS NOT NULL AND selected_team_id IS NOT NULL) OR (rank_eligible=0 AND decision IN ('pass','avoid') AND recommendation_rank IS NULL AND selected_side IS NULL AND selected_team_id IS NULL))
    )
    """,
)


PREDICTION_DECISION_SCHEMA_V13_INDEX_STATEMENTS = (
    "CREATE INDEX idx_prediction_inputs_run_game ON prediction_authoring_inputs(run_id,source_game_id,upstream_model_feature_set_snapshot_id,sealed_at)",
    "CREATE INDEX idx_predictions_attempt_run_outcome ON predictions_attempt_evidence(run_id,outcome,phase_attempt)",
    "CREATE INDEX idx_prediction_snapshot_upstream ON prediction_snapshots(upstream_model_feature_set_snapshot_id,upstream_data_quality_snapshot_id)",
    "CREATE INDEX idx_prediction_game_source ON prediction_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_value_attempt_run_outcome ON value_engine_attempt_evidence(run_id,outcome,phase_attempt)",
    "CREATE INDEX idx_value_snapshot_upstream ON value_engine_snapshots(upstream_predictions_snapshot_id,upstream_model_feature_set_snapshot_id)",
    "CREATE INDEX idx_value_game_source ON value_engine_games(source_game_id,snapshot_id)",
    "CREATE INDEX idx_value_outcome_side ON value_engine_outcomes(source_game_id,side,snapshot_id)",
    "CREATE INDEX idx_value_pair_book ON value_engine_book_pairs(bookmaker_key,retrieval_timestamp,snapshot_id)",
    "CREATE INDEX idx_gate_attempt_run_outcome ON recommendation_gate_attempt_evidence(run_id,outcome,phase_attempt)",
    "CREATE INDEX idx_gate_snapshot_upstream ON recommendation_gate_snapshots(upstream_value_engine_snapshot_id,upstream_predictions_snapshot_id)",
    "CREATE INDEX idx_gate_game_source ON recommendation_gate_games(source_game_id,snapshot_id)",
    "CREATE UNIQUE INDEX uq_gate_one_recommend_per_game ON recommendation_gate_sides(snapshot_id,source_game_id) WHERE decision='recommend'",
    "CREATE INDEX idx_gate_result_code ON recommendation_gate_results(gate_code,passed,snapshot_id)",
    "CREATE INDEX idx_rankings_attempt_run_outcome ON rankings_attempt_evidence(run_id,outcome,phase_attempt)",
    "CREATE INDEX idx_rankings_snapshot_upstream ON ranking_snapshots(upstream_recommendation_gate_snapshot_id,upstream_recommendation_gate_checksum)",
    "CREATE UNIQUE INDEX uq_ranking_recommendation_rank ON ranking_entries(snapshot_id,recommendation_rank) WHERE recommendation_rank IS NOT NULL",
    "CREATE INDEX idx_ranking_entry_source ON ranking_entries(source_game_id,snapshot_id)",
)


PREDICTION_DECISION_SCHEMA_V13_VALIDATION_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER predictions_attempt_validate_active BEFORE INSERT ON predictions_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_runs run
        JOIN pipeline_run_phases phase ON phase.run_id=run.run_id AND phase.phase_key='predictions'
        JOIN model_feature_set_snapshots mfs ON mfs.snapshot_id=NEW.upstream_model_feature_set_snapshot_id
        JOIN data_quality_snapshots dq ON dq.snapshot_id=NEW.upstream_data_quality_snapshot_id
        WHERE run.run_id=NEW.run_id AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND mfs.run_id=NEW.run_id AND mfs.sealed_at IS NOT NULL AND mfs.feature_set_checksum=NEW.upstream_model_feature_set_checksum
          AND dq.run_id=NEW.run_id AND dq.sealed_at IS NOT NULL AND dq.snapshot_checksum=NEW.upstream_data_quality_checksum
          AND mfs.upstream_data_quality_snapshot_id=dq.snapshot_id
      ) THEN RAISE(ABORT,'Predictions attempt requires active phase and exact sealed upstream') END;
    END
    """,
    """
    CREATE TRIGGER value_engine_attempt_validate_active BEFORE INSERT ON value_engine_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_run_phases phase
        JOIN prediction_snapshots p ON p.snapshot_id=NEW.upstream_predictions_snapshot_id
        JOIN model_feature_set_snapshots mfs ON mfs.snapshot_id=NEW.upstream_model_feature_set_snapshot_id
        JOIN data_quality_snapshots dq ON dq.snapshot_id=NEW.upstream_data_quality_snapshot_id
        WHERE phase.run_id=NEW.run_id AND phase.phase_key='value_engine' AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND p.run_id=NEW.run_id AND p.sealed_at IS NOT NULL AND p.snapshot_checksum=NEW.upstream_predictions_checksum
          AND mfs.snapshot_id=p.upstream_model_feature_set_snapshot_id AND mfs.feature_set_checksum=NEW.upstream_model_feature_set_checksum AND mfs.sealed_at IS NOT NULL
          AND dq.snapshot_id=p.upstream_data_quality_snapshot_id AND dq.snapshot_checksum=NEW.upstream_data_quality_checksum AND dq.sealed_at IS NOT NULL
      ) THEN RAISE(ABORT,'Value Engine attempt requires active phase and exact sealed upstream') END;
    END
    """,
    """
    CREATE TRIGGER recommendation_gate_attempt_validate_active BEFORE INSERT ON recommendation_gate_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_run_phases phase
        JOIN value_engine_snapshots v ON v.snapshot_id=NEW.upstream_value_engine_snapshot_id
        JOIN prediction_snapshots p ON p.snapshot_id=NEW.upstream_predictions_snapshot_id
        JOIN data_quality_snapshots dq ON dq.snapshot_id=NEW.upstream_data_quality_snapshot_id
        WHERE phase.run_id=NEW.run_id AND phase.phase_key='recommendation_gate' AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND v.run_id=NEW.run_id AND v.sealed_at IS NOT NULL AND v.snapshot_checksum=NEW.upstream_value_engine_checksum
          AND p.snapshot_id=v.upstream_predictions_snapshot_id AND p.snapshot_checksum=NEW.upstream_predictions_checksum AND p.sealed_at IS NOT NULL
          AND dq.snapshot_id=v.upstream_data_quality_snapshot_id AND dq.snapshot_checksum=NEW.upstream_data_quality_checksum AND dq.sealed_at IS NOT NULL
      ) THEN RAISE(ABORT,'Recommendation Gate attempt requires active phase and exact sealed upstream') END;
    END
    """,
    """
    CREATE TRIGGER rankings_attempt_validate_active BEFORE INSERT ON rankings_attempt_evidence BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM pipeline_run_phases phase
        JOIN recommendation_gate_snapshots g ON g.snapshot_id=NEW.upstream_recommendation_gate_snapshot_id
        WHERE phase.run_id=NEW.run_id AND phase.phase_key='rankings' AND phase.status='running' AND phase.attempt_count=NEW.phase_attempt
          AND g.run_id=NEW.run_id AND g.sealed_at IS NOT NULL AND g.snapshot_checksum=NEW.upstream_recommendation_gate_checksum
      ) THEN RAISE(ABORT,'Rankings attempt requires active phase and exact sealed upstream') END;
    END
    """,
    *tuple(
        f"""
        CREATE TRIGGER {table}_require_unsealed BEFORE INSERT ON {table} BEGIN
          SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM {parent} WHERE snapshot_id=NEW.snapshot_id AND sealed_at IS NULL)
            THEN RAISE(ABORT,'sealed snapshot children are immutable') END;
        END
        """
        for table, parent in (
            ("prediction_games", "prediction_snapshots"),
            ("value_engine_games", "value_engine_snapshots"),
            ("value_engine_outcomes", "value_engine_snapshots"),
            ("value_engine_book_pairs", "value_engine_snapshots"),
            ("recommendation_gate_games", "recommendation_gate_snapshots"),
            ("recommendation_gate_sides", "recommendation_gate_snapshots"),
            ("recommendation_gate_results", "recommendation_gate_snapshots"),
            ("ranking_entries", "ranking_snapshots"),
        )
    ),
    """
    CREATE TRIGGER prediction_snapshots_validate_seal BEFORE UPDATE OF sealed_at ON prediction_snapshots
    WHEN OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL BEGIN
      SELECT CASE WHEN (SELECT count(*) FROM prediction_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count
        THEN RAISE(ABORT,'Prediction game count mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM predictions_attempt_evidence a WHERE a.run_id=NEW.run_id AND a.phase_attempt=NEW.phase_attempt AND (a.invalid_input_count<>0 OR json_array_length(a.missing_game_ids_json)<>0)) THEN RAISE(ABORT,'Assembled Predictions attempt contains missing or invalid input') END;
      SELECT CASE WHEN NEW.game_count>0 AND ((SELECT min(ordinal) FROM prediction_games WHERE snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(ordinal) FROM prediction_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count)
        THEN RAISE(ABORT,'Prediction ordinals are not contiguous') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM prediction_games WHERE snapshot_id=NEW.snapshot_id AND (market_independence_attested<>1 OR json_extract(canonical_json,'$.market_independence_attestation')<>1)) THEN RAISE(ABORT,'Prediction market-independence attestation mismatch') END;
    END
    """,
    """
    CREATE TRIGGER value_engine_snapshots_validate_seal BEFORE UPDATE OF sealed_at ON value_engine_snapshots
    WHEN OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL BEGIN
      SELECT CASE WHEN (SELECT count(*) FROM value_engine_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count THEN RAISE(ABORT,'Value game count mismatch') END;
      SELECT CASE WHEN (SELECT count(*) FROM value_engine_outcomes WHERE snapshot_id=NEW.snapshot_id)<>NEW.outcome_count THEN RAISE(ABORT,'Value outcome count mismatch') END;
      SELECT CASE WHEN NEW.game_count>0 AND ((SELECT min(ordinal) FROM value_engine_games WHERE snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(ordinal) FROM value_engine_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count) THEN RAISE(ABORT,'Value game ordinals are not contiguous') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM value_engine_games g WHERE g.snapshot_id=NEW.snapshot_id AND (SELECT count(*) FROM value_engine_outcomes o WHERE o.snapshot_id=g.snapshot_id AND o.source_game_id=g.source_game_id)<>2) THEN RAISE(ABORT,'Value game requires both moneyline outcomes') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM value_engine_outcomes o WHERE o.snapshot_id=NEW.snapshot_id AND (SELECT count(*) FROM value_engine_book_pairs p WHERE p.snapshot_id=o.snapshot_id AND p.source_game_id=o.source_game_id AND p.side=o.side)<>o.eligible_bookmaker_count) THEN RAISE(ABORT,'Value eligible bookmaker count mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM value_engine_outcomes o WHERE o.snapshot_id=NEW.snapshot_id AND o.eligible_bookmaker_count>0 AND ((SELECT min(ordinal) FROM value_engine_book_pairs p WHERE p.snapshot_id=o.snapshot_id AND p.source_game_id=o.source_game_id AND p.side=o.side)<>1 OR (SELECT max(ordinal) FROM value_engine_book_pairs p WHERE p.snapshot_id=o.snapshot_id AND p.source_game_id=o.source_game_id AND p.side=o.side)<>o.eligible_bookmaker_count)) THEN RAISE(ABORT,'Value pair ordinals are not contiguous') END;
    END
    """,
    """
    CREATE TRIGGER recommendation_gate_snapshots_validate_seal BEFORE UPDATE OF sealed_at ON recommendation_gate_snapshots
    WHEN OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL BEGIN
      SELECT CASE WHEN (SELECT count(*) FROM recommendation_gate_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count THEN RAISE(ABORT,'Gate game count mismatch') END;
      SELECT CASE WHEN NEW.game_count>0 AND ((SELECT min(ordinal) FROM recommendation_gate_games WHERE snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(ordinal) FROM recommendation_gate_games WHERE snapshot_id=NEW.snapshot_id)<>NEW.game_count) THEN RAISE(ABORT,'Gate game ordinals are not contiguous') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_games g WHERE g.snapshot_id=NEW.snapshot_id AND (SELECT count(*) FROM recommendation_gate_sides s WHERE s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id)<>2) THEN RAISE(ABORT,'Gate game requires both side evaluations') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND (SELECT count(*) FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side)<>s.gate_count) THEN RAISE(ABORT,'Gate result count mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND ((SELECT min(ordinal) FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side)<>1 OR (SELECT max(ordinal) FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side)<>s.gate_count)) THEN RAISE(ABORT,'Gate result ordinals are not contiguous') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND json(s.reason_codes_json)<>json((SELECT json_group_array(gate_code) FROM (SELECT gate_code FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.passed=0 ORDER BY r.ordinal)))) THEN RAISE(ABORT,'Gate reason codes do not equal failed results') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND (SELECT count(*) FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.gate_code='opposing_side_not_selected')<>1) THEN RAISE(ABORT,'Gate selection result inventory mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND ((s.decision='recommend' AND EXISTS (SELECT 1 FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.passed=0)) OR (s.decision='pass' AND ((SELECT count(*) FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.passed=0)=0 OR EXISTS (SELECT 1 FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.passed=0 AND r.gate_code IN ('data_quality_model_ready','uncertainty_acceptable','lineup_and_starter_risk','weather_evidence_acceptable','event_pregame')))) OR (s.decision='avoid' AND NOT EXISTS (SELECT 1 FROM recommendation_gate_results r WHERE r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side AND r.passed=0 AND r.gate_code IN ('data_quality_model_ready','uncertainty_acceptable','lineup_and_starter_risk','weather_evidence_acceptable','event_pregame'))))) THEN RAISE(ABORT,'Gate side decision disagrees with failed-result taxonomy') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_games g WHERE g.snapshot_id=NEW.snapshot_id AND ((g.decision='recommend' AND ((SELECT count(*) FROM recommendation_gate_sides s WHERE s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id AND s.decision='recommend')<>1 OR NOT EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id AND s.decision='recommend' AND s.side=g.selected_side AND s.outcome_team_id=g.selected_team_id))) OR (g.decision='pass' AND EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id AND s.decision<>'pass')) OR (g.decision='avoid' AND NOT EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id AND s.decision='avoid')))) THEN RAISE(ABORT,'Gate game decision disagrees with side decisions') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_games g JOIN recommendation_gate_sides s ON s.snapshot_id=g.snapshot_id AND s.source_game_id=g.source_game_id JOIN recommendation_gate_results r ON r.snapshot_id=s.snapshot_id AND r.source_game_id=s.source_game_id AND r.side=s.side WHERE g.snapshot_id=NEW.snapshot_id AND g.decision='recommend' AND r.gate_code='opposing_side_not_selected' AND ((s.decision='recommend' AND r.passed<>1) OR (s.decision<>'recommend' AND r.passed<>0))) THEN RAISE(ABORT,'Gate selected-side results are inconsistent') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_games g JOIN recommendation_gate_results r ON r.snapshot_id=g.snapshot_id AND r.source_game_id=g.source_game_id WHERE g.snapshot_id=NEW.snapshot_id AND g.decision<>'recommend' AND r.gate_code='opposing_side_not_selected' AND r.passed<>0) THEN RAISE(ABORT,'Unrecommended game cannot claim a selected side') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_games g WHERE g.snapshot_id=NEW.snapshot_id AND (json_extract(g.canonical_json,'$.decision')<>g.decision OR json_extract(g.canonical_json,'$.selected_side') IS NOT g.selected_side OR json_extract(g.canonical_json,'$.selected_team_id') IS NOT g.selected_team_id)) THEN RAISE(ABORT,'Gate game canonical identity mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_sides s WHERE s.snapshot_id=NEW.snapshot_id AND (json_extract(s.canonical_json,'$.decision')<>s.decision OR json(json_extract(s.canonical_json,'$.reason_codes'))<>json(s.reason_codes_json))) THEN RAISE(ABORT,'Gate side canonical evidence mismatch') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM recommendation_gate_results r WHERE r.snapshot_id=NEW.snapshot_id AND (json_extract(r.canonical_json,'$.code')<>r.gate_code OR json_extract(r.canonical_json,'$.passed')<>r.passed)) THEN RAISE(ABORT,'Gate result canonical evidence mismatch') END;
      SELECT CASE WHEN (SELECT count(*) FROM recommendation_gate_games WHERE snapshot_id=NEW.snapshot_id AND decision='recommend')<>NEW.recommended_game_count THEN RAISE(ABORT,'Recommended game count mismatch') END;
    END
    """,
    """
    CREATE TRIGGER ranking_snapshots_validate_seal BEFORE UPDATE OF sealed_at ON ranking_snapshots
    WHEN OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL BEGIN
      SELECT CASE WHEN (SELECT count(*) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id)<>NEW.entry_count THEN RAISE(ABORT,'Ranking entry count mismatch') END;
      SELECT CASE WHEN (SELECT count(*) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id AND rank_eligible=1)<>NEW.eligible_count THEN RAISE(ABORT,'Ranking eligible count mismatch') END;
      SELECT CASE WHEN (SELECT count(recommendation_rank) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id)<>NEW.eligible_count THEN RAISE(ABORT,'Ranking recommendation rank count mismatch') END;
      SELECT CASE WHEN NEW.entry_count>0 AND ((SELECT min(ordinal) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(ordinal) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id)<>NEW.entry_count) THEN RAISE(ABORT,'Ranking ordinals are not contiguous') END;
      SELECT CASE WHEN NEW.eligible_count>0 AND ((SELECT min(recommendation_rank) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id AND rank_eligible=1)<>1 OR (SELECT max(recommendation_rank) FROM ranking_entries WHERE snapshot_id=NEW.snapshot_id AND rank_eligible=1)<>NEW.eligible_count) THEN RAISE(ABORT,'Recommendation ranks are not contiguous') END;
    END
    """,
)


_SNAPSHOT_COLUMNS: dict[str, tuple[str, ...]] = {
    "prediction_snapshots": (
        "snapshot_id",
        "run_id",
        "phase_attempt",
        "requested_date",
        "as_of_time",
        "observed_at",
        "contract_version",
        "phase_input_checksum",
        "provider_policy_json",
        "provider_policy_checksum",
        "upstream_model_feature_set_snapshot_id",
        "upstream_model_feature_set_checksum",
        "upstream_data_quality_snapshot_id",
        "upstream_data_quality_checksum",
        "input_inventory_checksum",
        "snapshot_checksum",
        "game_count",
        "warning_count",
        "warnings_json",
        "canonical_json",
        "artifact_relpath",
        "artifact_checksum",
        "artifact_byte_count",
        "created_at",
    ),
    "value_engine_snapshots": (
        "snapshot_id",
        "run_id",
        "phase_attempt",
        "requested_date",
        "as_of_time",
        "evaluated_at",
        "contract_version",
        "phase_input_checksum",
        "policy_json",
        "policy_checksum",
        "upstream_predictions_snapshot_id",
        "upstream_predictions_checksum",
        "upstream_model_feature_set_snapshot_id",
        "upstream_model_feature_set_checksum",
        "upstream_data_quality_snapshot_id",
        "upstream_data_quality_checksum",
        "market_inventory_checksum",
        "snapshot_checksum",
        "game_count",
        "outcome_count",
        "warning_count",
        "warnings_json",
        "canonical_json",
        "artifact_relpath",
        "artifact_checksum",
        "artifact_byte_count",
        "created_at",
    ),
    "recommendation_gate_snapshots": (
        "snapshot_id",
        "run_id",
        "phase_attempt",
        "requested_date",
        "as_of_time",
        "evaluated_at",
        "contract_version",
        "phase_input_checksum",
        "policy_json",
        "policy_checksum",
        "upstream_value_engine_snapshot_id",
        "upstream_value_engine_checksum",
        "upstream_predictions_snapshot_id",
        "upstream_predictions_checksum",
        "upstream_data_quality_snapshot_id",
        "upstream_data_quality_checksum",
        "snapshot_checksum",
        "game_count",
        "recommended_game_count",
        "warning_count",
        "warnings_json",
        "canonical_json",
        "artifact_relpath",
        "artifact_checksum",
        "artifact_byte_count",
        "created_at",
    ),
    "ranking_snapshots": (
        "snapshot_id",
        "run_id",
        "phase_attempt",
        "requested_date",
        "as_of_time",
        "ranked_at",
        "contract_version",
        "phase_input_checksum",
        "policy_json",
        "policy_checksum",
        "upstream_recommendation_gate_snapshot_id",
        "upstream_recommendation_gate_checksum",
        "snapshot_checksum",
        "entry_count",
        "eligible_count",
        "warning_count",
        "warnings_json",
        "canonical_json",
        "artifact_relpath",
        "artifact_checksum",
        "artifact_byte_count",
        "created_at",
    ),
}

_ALWAYS_IMMUTABLE_TABLES = (
    "prediction_authoring_inputs",
    "predictions_attempt_evidence",
    "value_engine_attempt_evidence",
    "recommendation_gate_attempt_evidence",
    "rankings_attempt_evidence",
)

_CHILD_TABLES = (
    ("prediction_games", "prediction_snapshots"),
    ("value_engine_games", "value_engine_snapshots"),
    ("value_engine_outcomes", "value_engine_snapshots"),
    ("value_engine_book_pairs", "value_engine_snapshots"),
    ("recommendation_gate_games", "recommendation_gate_snapshots"),
    ("recommendation_gate_sides", "recommendation_gate_snapshots"),
    ("recommendation_gate_results", "recommendation_gate_snapshots"),
    ("ranking_entries", "ranking_snapshots"),
)


PREDICTION_DECISION_SCHEMA_V13_IMMUTABILITY_TRIGGER_STATEMENTS = (
    *tuple(
        statement
        for table in _ALWAYS_IMMUTABLE_TABLES
        for statement in (
            f"CREATE TRIGGER {table}_reject_update BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence cannot be updated'); END",
            f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence cannot be deleted'); END",
        )
    ),
    *tuple(
        statement
        for table, columns in _SNAPSHOT_COLUMNS.items()
        for statement in (
            f"CREATE TRIGGER {table}_seal_only BEFORE UPDATE ON {table} WHEN NOT (OLD.sealed_at IS NULL AND NEW.sealed_at IS NOT NULL AND {' AND '.join(f'NEW.{column}=OLD.{column}' for column in columns)}) BEGIN SELECT RAISE(ABORT,'snapshot update may only set sealed_at'); END",
            f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} BEGIN SELECT RAISE(ABORT,'snapshot cannot be deleted'); END",
        )
    ),
    *tuple(
        statement
        for table, parent in _CHILD_TABLES
        for statement in (
            f"CREATE TRIGGER {table}_reject_update BEFORE UPDATE ON {table} WHEN EXISTS (SELECT 1 FROM {parent} p WHERE p.snapshot_id=OLD.snapshot_id AND p.sealed_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'sealed child cannot be updated'); END",
            f"CREATE TRIGGER {table}_reject_delete BEFORE DELETE ON {table} WHEN EXISTS (SELECT 1 FROM {parent} p WHERE p.snapshot_id=OLD.snapshot_id AND p.sealed_at IS NOT NULL) BEGIN SELECT RAISE(ABORT,'sealed child cannot be deleted'); END",
        )
    ),
)


PREDICTION_DECISION_SCHEMA_V13_STATEMENTS = (
    *PREDICTION_DECISION_SCHEMA_V13_TABLE_STATEMENTS,
    *PREDICTION_DECISION_SCHEMA_V13_INDEX_STATEMENTS,
    *PREDICTION_DECISION_SCHEMA_V13_VALIDATION_TRIGGER_STATEMENTS,
    *PREDICTION_DECISION_SCHEMA_V13_IMMUTABILITY_TRIGGER_STATEMENTS,
)
