from __future__ import annotations

"""Append-only schema v14 persistence for the final output and review phases."""


def _immutable(table: str) -> tuple[str, str]:
    return (
        f"""
        CREATE TRIGGER {table}_reject_update
        BEFORE UPDATE ON {table}
        BEGIN
          SELECT RAISE(ABORT, '{table} records are immutable');
        END
        """,
        f"""
        CREATE TRIGGER {table}_reject_delete
        BEFORE DELETE ON {table}
        BEGIN
          SELECT RAISE(ABORT, '{table} records are immutable');
        END
        """,
    )


_SHA = "length({0})=64 AND {0} NOT GLOB '*[^0-9a-f]*'"


FINAL_OUTPUT_SCHEMA_V14_TABLE_STATEMENTS = (
    f"""
    CREATE TABLE pdf_report_attempt_evidence (
      run_id TEXT NOT NULL,
      phase_key TEXT NOT NULL DEFAULT 'pdf_report' CHECK(phase_key='pdf_report'),
      phase_attempt INTEGER NOT NULL CHECK(typeof(phase_attempt)='integer' AND phase_attempt>=1),
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      upstream_rankings_snapshot_id TEXT NOT NULL,
      upstream_rankings_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_rankings_checksum")}),
      upstream_recommendation_gate_snapshot_id TEXT NOT NULL,
      upstream_recommendation_gate_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_recommendation_gate_checksum")}),
      upstream_value_engine_snapshot_id TEXT NOT NULL,
      upstream_value_engine_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_value_engine_checksum")}),
      upstream_predictions_snapshot_id TEXT NOT NULL,
      upstream_predictions_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_predictions_checksum")}),
      upstream_matchup_packet_snapshot_id TEXT NOT NULL,
      upstream_matchup_packet_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_matchup_packet_checksum")}),
      upstream_data_quality_snapshot_id TEXT NOT NULL,
      upstream_data_quality_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_data_quality_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      outcome TEXT NOT NULL CHECK(outcome IN ('assembled','input_failed','assembly_failed','rendering_failed','verification_failed','persistence_failed')),
      snapshot_checksum TEXT CHECK(snapshot_checksum IS NULL OR ({_SHA.format("snapshot_checksum")})),
      evidence_manifest_relpath TEXT NOT NULL,
      evidence_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("evidence_manifest_checksum")}),
      evidence_manifest_byte_count INTEGER NOT NULL CHECK(evidence_manifest_byte_count>0),
      warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json) AND json_type(warnings_json)='array'),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      created_at TEXT NOT NULL,
      completed_at TEXT NOT NULL,
      PRIMARY KEY(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key),
      CHECK((outcome='assembled')=(snapshot_checksum IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE pdf_report_snapshots (
      snapshot_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      contract_version TEXT NOT NULL,
      render_version TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      upstream_rankings_snapshot_id TEXT NOT NULL,
      upstream_rankings_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_rankings_checksum")}),
      upstream_recommendation_gate_snapshot_id TEXT NOT NULL,
      upstream_recommendation_gate_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_recommendation_gate_checksum")}),
      upstream_value_engine_snapshot_id TEXT NOT NULL,
      upstream_value_engine_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_value_engine_checksum")}),
      upstream_predictions_snapshot_id TEXT NOT NULL,
      upstream_predictions_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_predictions_checksum")}),
      upstream_matchup_packet_snapshot_id TEXT NOT NULL,
      upstream_matchup_packet_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_matchup_packet_checksum")}),
      upstream_data_quality_snapshot_id TEXT NOT NULL,
      upstream_data_quality_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_data_quality_checksum")}),
      semantic_checksum TEXT NOT NULL UNIQUE CHECK({_SHA.format("semantic_checksum")}),
      game_count INTEGER NOT NULL CHECK(game_count>=0),
      recommendation_count INTEGER NOT NULL CHECK(recommendation_count>=0 AND recommendation_count<=game_count),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      document_relpath TEXT NOT NULL CHECK(document_relpath='pdf_report/snapshots/'||semantic_checksum||'/pdf_report_document_v1.json'),
      document_checksum TEXT NOT NULL CHECK({_SHA.format("document_checksum")}),
      document_byte_count INTEGER NOT NULL CHECK(document_byte_count>0),
      pdf_relpath TEXT NOT NULL CHECK(pdf_relpath='pdf_report/snapshots/'||semantic_checksum||'/daily_mlb_report_v1.pdf'),
      pdf_checksum TEXT NOT NULL CHECK({_SHA.format("pdf_checksum")}),
      pdf_byte_count INTEGER NOT NULL CHECK(pdf_byte_count>0),
      pdf_page_count INTEGER NOT NULL CHECK(pdf_page_count>0),
      render_manifest_relpath TEXT NOT NULL CHECK(render_manifest_relpath='pdf_report/snapshots/'||semantic_checksum||'/render_manifest_v1.json'),
      render_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("render_manifest_checksum")}),
      render_manifest_byte_count INTEGER NOT NULL CHECK(render_manifest_byte_count>0),
      report_status TEXT NOT NULL CHECK(report_status='pre_review'),
      sealed_at TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_attempt) REFERENCES pdf_report_attempt_evidence(run_id,phase_attempt)
    )
    """,
    f"""
    CREATE TABLE pdf_report_games (
      snapshot_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=1),
      source_game_id TEXT NOT NULL,
      decision TEXT NOT NULL CHECK(decision IN ('recommend','pass','avoid')),
      recommendation_rank INTEGER CHECK(recommendation_rank IS NULL OR (typeof(recommendation_rank)='integer' AND recommendation_rank>=1)),
      selected_side TEXT CHECK(selected_side IN ('home','away')),
      selected_team_id TEXT,
      game_checksum TEXT NOT NULL CHECK({_SHA.format("game_checksum")}),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      PRIMARY KEY(snapshot_id,ordinal),
      UNIQUE(snapshot_id,source_game_id),
      UNIQUE(snapshot_id,recommendation_rank),
      FOREIGN KEY(snapshot_id) REFERENCES pdf_report_snapshots(snapshot_id),
      FOREIGN KEY(run_id,phase_attempt) REFERENCES pdf_report_attempt_evidence(run_id,phase_attempt),
      CHECK((decision='recommend')=(recommendation_rank IS NOT NULL AND selected_side IS NOT NULL AND selected_team_id IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE infographic_attempt_evidence (
      run_id TEXT NOT NULL,
      phase_key TEXT NOT NULL DEFAULT 'infographic' CHECK(phase_key='infographic'),
      phase_attempt INTEGER NOT NULL CHECK(typeof(phase_attempt)='integer' AND phase_attempt>=1),
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      upstream_pdf_report_snapshot_id TEXT NOT NULL,
      upstream_pdf_report_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_pdf_report_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      outcome TEXT NOT NULL CHECK(outcome IN ('assembled','input_failed','assembly_failed','rendering_failed','verification_failed','persistence_failed')),
      snapshot_checksum TEXT CHECK(snapshot_checksum IS NULL OR ({_SHA.format("snapshot_checksum")})),
      evidence_manifest_relpath TEXT NOT NULL,
      evidence_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("evidence_manifest_checksum")}),
      evidence_manifest_byte_count INTEGER NOT NULL CHECK(evidence_manifest_byte_count>0),
      warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json) AND json_type(warnings_json)='array'),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      created_at TEXT NOT NULL,
      completed_at TEXT NOT NULL,
      PRIMARY KEY(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key),
      CHECK((outcome='assembled')=(snapshot_checksum IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE infographic_snapshots (
      snapshot_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      generated_at TEXT NOT NULL,
      contract_version TEXT NOT NULL,
      render_version TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      upstream_pdf_report_snapshot_id TEXT NOT NULL,
      upstream_pdf_report_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_pdf_report_checksum")}),
      infographic_checksum TEXT NOT NULL UNIQUE CHECK({_SHA.format("infographic_checksum")}),
      recommendation_count INTEGER NOT NULL CHECK(recommendation_count>=0),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      document_relpath TEXT NOT NULL CHECK(document_relpath='infographic/snapshots/'||infographic_checksum||'/infographic_document_v1.json'),
      document_checksum TEXT NOT NULL CHECK({_SHA.format("document_checksum")}),
      document_byte_count INTEGER NOT NULL CHECK(document_byte_count>0),
      feed_relpath TEXT NOT NULL CHECK(feed_relpath='infographic/snapshots/'||infographic_checksum||'/daily_mlb_feed_4x5.svg'),
      feed_checksum TEXT NOT NULL CHECK({_SHA.format("feed_checksum")}),
      feed_byte_count INTEGER NOT NULL CHECK(feed_byte_count>0),
      story_relpath TEXT NOT NULL CHECK(story_relpath='infographic/snapshots/'||infographic_checksum||'/daily_mlb_story_9x16.svg'),
      story_checksum TEXT NOT NULL CHECK({_SHA.format("story_checksum")}),
      story_byte_count INTEGER NOT NULL CHECK(story_byte_count>0),
      render_manifest_relpath TEXT NOT NULL CHECK(render_manifest_relpath='infographic/snapshots/'||infographic_checksum||'/render_manifest_v1.json'),
      render_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("render_manifest_checksum")}),
      render_manifest_byte_count INTEGER NOT NULL CHECK(render_manifest_byte_count>0),
      report_status TEXT NOT NULL CHECK(report_status='pre_review'),
      sealed_at TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_attempt) REFERENCES infographic_attempt_evidence(run_id,phase_attempt)
    )
    """,
    f"""
    CREATE TABLE infographic_variants (
      snapshot_id TEXT NOT NULL,
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      ordinal INTEGER NOT NULL CHECK(ordinal IN (1,2)),
      variant TEXT NOT NULL CHECK(variant IN ('feed_4x5','story_9x16')),
      width INTEGER NOT NULL,
      height INTEGER NOT NULL,
      variant_checksum TEXT NOT NULL CHECK({_SHA.format("variant_checksum")}),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      PRIMARY KEY(snapshot_id,ordinal),
      UNIQUE(snapshot_id,variant),
      FOREIGN KEY(snapshot_id) REFERENCES infographic_snapshots(snapshot_id),
      CHECK((variant='feed_4x5' AND width=1080 AND height=1350) OR (variant='story_9x16' AND width=1080 AND height=1920))
    )
    """,
    f"""
    CREATE TABLE final_qc_attempt_evidence (
      run_id TEXT NOT NULL,
      phase_key TEXT NOT NULL DEFAULT 'final_qc' CHECK(phase_key='final_qc'),
      phase_attempt INTEGER NOT NULL CHECK(typeof(phase_attempt)='integer' AND phase_attempt>=1),
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      evaluated_at TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      upstream_pdf_report_snapshot_id TEXT NOT NULL,
      upstream_pdf_report_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_pdf_report_checksum")}),
      upstream_infographic_snapshot_id TEXT NOT NULL,
      upstream_infographic_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_infographic_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      outcome TEXT NOT NULL CHECK(outcome IN ('assembled','input_failed','validation_failed','persistence_failed')),
      snapshot_checksum TEXT CHECK(snapshot_checksum IS NULL OR ({_SHA.format("snapshot_checksum")})),
      evidence_manifest_relpath TEXT NOT NULL,
      evidence_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("evidence_manifest_checksum")}),
      evidence_manifest_byte_count INTEGER NOT NULL CHECK(evidence_manifest_byte_count>0),
      warnings_json TEXT NOT NULL CHECK(json_valid(warnings_json) AND json_type(warnings_json)='array'),
      warning_count INTEGER NOT NULL CHECK(warning_count>=0),
      created_at TEXT NOT NULL,
      completed_at TEXT NOT NULL,
      PRIMARY KEY(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key),
      CHECK((outcome='assembled')=(snapshot_checksum IS NOT NULL))
    )
    """,
    f"""
    CREATE TABLE final_qc_snapshots (
      snapshot_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      evaluated_at TEXT NOT NULL,
      contract_version TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      policy_json TEXT NOT NULL CHECK(json_valid(policy_json) AND json_type(policy_json)='object'),
      policy_checksum TEXT NOT NULL CHECK({_SHA.format("policy_checksum")}),
      upstream_pdf_report_snapshot_id TEXT NOT NULL,
      upstream_pdf_report_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_pdf_report_checksum")}),
      upstream_infographic_snapshot_id TEXT NOT NULL,
      upstream_infographic_checksum TEXT NOT NULL CHECK({_SHA.format("upstream_infographic_checksum")}),
      qc_checksum TEXT NOT NULL UNIQUE CHECK({_SHA.format("qc_checksum")}),
      overall_result TEXT NOT NULL CHECK(overall_result='pass'),
      check_count INTEGER NOT NULL CHECK(check_count>0),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      artifact_relpath TEXT NOT NULL CHECK(artifact_relpath='final_qc/snapshots/'||qc_checksum||'/final_qc_v1.json'),
      artifact_checksum TEXT NOT NULL CHECK({_SHA.format("artifact_checksum")}),
      artifact_byte_count INTEGER NOT NULL CHECK(artifact_byte_count>0),
      sealed_at TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(run_id,phase_attempt),
      FOREIGN KEY(run_id,phase_attempt) REFERENCES final_qc_attempt_evidence(run_id,phase_attempt)
    )
    """,
    f"""
    CREATE TABLE final_qc_checks (
      run_id TEXT NOT NULL,
      phase_attempt INTEGER NOT NULL,
      ordinal INTEGER NOT NULL CHECK(typeof(ordinal)='integer' AND ordinal>=1),
      code TEXT NOT NULL,
      passed INTEGER NOT NULL CHECK(passed IN (0,1)),
      observed_json TEXT NOT NULL CHECK(json_valid(observed_json)),
      source_checksum TEXT NOT NULL CHECK({_SHA.format("source_checksum")}),
      check_checksum TEXT NOT NULL CHECK({_SHA.format("check_checksum")}),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      PRIMARY KEY(run_id,phase_attempt,ordinal),
      UNIQUE(run_id,phase_attempt,code),
      FOREIGN KEY(run_id,phase_attempt) REFERENCES final_qc_attempt_evidence(run_id,phase_attempt)
    )
    """,
    f"""
    CREATE TABLE human_review_records (
      review_id TEXT PRIMARY KEY,
      run_id TEXT NOT NULL,
      review_ordinal INTEGER NOT NULL CHECK(typeof(review_ordinal)='integer' AND review_ordinal>=1),
      requested_date TEXT NOT NULL,
      final_qc_snapshot_id TEXT NOT NULL,
      final_qc_checksum TEXT NOT NULL CHECK({_SHA.format("final_qc_checksum")}),
      pdf_report_snapshot_id TEXT NOT NULL,
      pdf_report_checksum TEXT NOT NULL CHECK({_SHA.format("pdf_report_checksum")}),
      pdf_artifact_checksum TEXT NOT NULL CHECK({_SHA.format("pdf_artifact_checksum")}),
      infographic_snapshot_id TEXT NOT NULL,
      infographic_checksum TEXT NOT NULL CHECK({_SHA.format("infographic_checksum")}),
      infographic_artifact_checksums_json TEXT NOT NULL CHECK(json_valid(infographic_artifact_checksums_json) AND json_type(infographic_artifact_checksums_json)='object'),
      reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id))>0),
      decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
      reviewed_at TEXT NOT NULL,
      notes TEXT,
      review_checksum TEXT NOT NULL UNIQUE CHECK({_SHA.format("review_checksum")}),
      canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json) AND json_type(canonical_json)='object'),
      created_at TEXT NOT NULL,
      UNIQUE(run_id,review_ordinal),
      FOREIGN KEY(final_qc_snapshot_id) REFERENCES final_qc_snapshots(snapshot_id),
      FOREIGN KEY(pdf_report_snapshot_id) REFERENCES pdf_report_snapshots(snapshot_id),
      FOREIGN KEY(infographic_snapshot_id) REFERENCES infographic_snapshots(snapshot_id)
    )
    """,
    f"""
    CREATE TABLE human_review_attempt_evidence (
      run_id TEXT NOT NULL,
      phase_key TEXT NOT NULL DEFAULT 'human_review' CHECK(phase_key='human_review'),
      phase_attempt INTEGER NOT NULL CHECK(typeof(phase_attempt)='integer' AND phase_attempt>=1),
      requested_date TEXT NOT NULL,
      as_of_time TEXT NOT NULL,
      completed_at TEXT NOT NULL,
      phase_input_checksum TEXT NOT NULL CHECK({_SHA.format("phase_input_checksum")}),
      final_qc_snapshot_id TEXT NOT NULL,
      final_qc_checksum TEXT NOT NULL CHECK({_SHA.format("final_qc_checksum")}),
      review_id TEXT NOT NULL,
      review_checksum TEXT NOT NULL CHECK({_SHA.format("review_checksum")}),
      decision TEXT NOT NULL CHECK(decision IN ('approve','reject')),
      outcome TEXT NOT NULL CHECK(outcome='assembled'),
      evidence_manifest_relpath TEXT NOT NULL,
      evidence_manifest_checksum TEXT NOT NULL CHECK({_SHA.format("evidence_manifest_checksum")}),
      evidence_manifest_byte_count INTEGER NOT NULL CHECK(evidence_manifest_byte_count>0),
      created_at TEXT NOT NULL,
      PRIMARY KEY(run_id,phase_attempt),
      UNIQUE(review_id),
      FOREIGN KEY(run_id,phase_key) REFERENCES pipeline_run_phases(run_id,phase_key),
      FOREIGN KEY(review_id) REFERENCES human_review_records(review_id),
      FOREIGN KEY(final_qc_snapshot_id) REFERENCES final_qc_snapshots(snapshot_id)
    )
    """,
)


FINAL_OUTPUT_SCHEMA_V14_INDEX_STATEMENTS = (
    "CREATE INDEX idx_pdf_report_attempt_run ON pdf_report_attempt_evidence(run_id,phase_attempt)",
    "CREATE INDEX idx_pdf_report_snapshot_run ON pdf_report_snapshots(run_id,phase_attempt)",
    "CREATE INDEX idx_pdf_report_game_source ON pdf_report_games(run_id,source_game_id)",
    "CREATE INDEX idx_infographic_attempt_run ON infographic_attempt_evidence(run_id,phase_attempt)",
    "CREATE INDEX idx_infographic_snapshot_run ON infographic_snapshots(run_id,phase_attempt)",
    "CREATE INDEX idx_final_qc_attempt_run ON final_qc_attempt_evidence(run_id,phase_attempt)",
    "CREATE INDEX idx_final_qc_snapshot_run ON final_qc_snapshots(run_id,phase_attempt)",
    "CREATE INDEX idx_human_review_run ON human_review_records(run_id,review_ordinal)",
    "CREATE INDEX idx_human_review_attempt_run ON human_review_attempt_evidence(run_id,phase_attempt)",
)


FINAL_OUTPUT_SCHEMA_V14_VALIDATION_TRIGGER_STATEMENTS = (
    """
    CREATE TRIGGER pdf_report_attempt_validate_phase_and_upstream
    BEFORE INSERT ON pdf_report_attempt_evidence
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pipeline_run_phases p WHERE p.run_id=NEW.run_id AND p.phase_key='pdf_report' AND p.status='running' AND p.attempt_count=NEW.phase_attempt) THEN RAISE(ABORT,'PDF attempt is not active') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM ranking_snapshots s WHERE s.snapshot_id=NEW.upstream_rankings_snapshot_id AND s.snapshot_checksum=NEW.upstream_rankings_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Rankings upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM recommendation_gate_snapshots s WHERE s.snapshot_id=NEW.upstream_recommendation_gate_snapshot_id AND s.snapshot_checksum=NEW.upstream_recommendation_gate_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Gate upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM value_engine_snapshots s WHERE s.snapshot_id=NEW.upstream_value_engine_snapshot_id AND s.snapshot_checksum=NEW.upstream_value_engine_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Value upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM prediction_snapshots s WHERE s.snapshot_id=NEW.upstream_predictions_snapshot_id AND s.snapshot_checksum=NEW.upstream_predictions_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Predictions upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM matchup_packet_snapshots s WHERE s.snapshot_id=NEW.upstream_matchup_packet_snapshot_id AND s.packet_checksum=NEW.upstream_matchup_packet_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Matchup Packet upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM data_quality_snapshots s WHERE s.snapshot_id=NEW.upstream_data_quality_snapshot_id AND s.snapshot_checksum=NEW.upstream_data_quality_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'PDF Data Quality upstream mismatch') END;
    END
    """,
    """
    CREATE TRIGGER pdf_report_snapshot_validate_attempt
    BEFORE INSERT ON pdf_report_snapshots
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pdf_report_attempt_evidence a WHERE a.run_id=NEW.run_id AND a.phase_attempt=NEW.phase_attempt AND a.outcome='assembled' AND a.snapshot_checksum=NEW.semantic_checksum AND a.phase_input_checksum=NEW.phase_input_checksum AND a.policy_json=NEW.policy_json AND a.policy_checksum=NEW.policy_checksum AND a.upstream_rankings_snapshot_id=NEW.upstream_rankings_snapshot_id AND a.upstream_rankings_checksum=NEW.upstream_rankings_checksum AND a.upstream_recommendation_gate_snapshot_id=NEW.upstream_recommendation_gate_snapshot_id AND a.upstream_recommendation_gate_checksum=NEW.upstream_recommendation_gate_checksum AND a.upstream_value_engine_snapshot_id=NEW.upstream_value_engine_snapshot_id AND a.upstream_value_engine_checksum=NEW.upstream_value_engine_checksum AND a.upstream_predictions_snapshot_id=NEW.upstream_predictions_snapshot_id AND a.upstream_predictions_checksum=NEW.upstream_predictions_checksum AND a.upstream_matchup_packet_snapshot_id=NEW.upstream_matchup_packet_snapshot_id AND a.upstream_matchup_packet_checksum=NEW.upstream_matchup_packet_checksum AND a.upstream_data_quality_snapshot_id=NEW.upstream_data_quality_snapshot_id AND a.upstream_data_quality_checksum=NEW.upstream_data_quality_checksum) THEN RAISE(ABORT,'PDF snapshot disagrees with attempt evidence') END;
    END
    """,
    """
    CREATE TRIGGER pdf_report_games_validate_unsealed
    BEFORE INSERT ON pdf_report_games
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pdf_report_snapshots s WHERE s.snapshot_id=NEW.snapshot_id AND s.run_id=NEW.run_id AND s.phase_attempt=NEW.phase_attempt AND s.sealed_at IS NULL) THEN RAISE(ABORT,'PDF game requires its unsealed snapshot') END;
    END
    """,
    """
    CREATE TRIGGER pdf_report_snapshots_validate_seal
    BEFORE UPDATE OF sealed_at ON pdf_report_snapshots
    BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL THEN RAISE(ABORT,'PDF snapshot may be sealed once') END;
      SELECT CASE WHEN OLD.snapshot_id IS NOT NEW.snapshot_id OR OLD.run_id IS NOT NEW.run_id OR OLD.phase_attempt IS NOT NEW.phase_attempt OR OLD.requested_date IS NOT NEW.requested_date OR OLD.as_of_time IS NOT NEW.as_of_time OR OLD.generated_at IS NOT NEW.generated_at OR OLD.contract_version IS NOT NEW.contract_version OR OLD.render_version IS NOT NEW.render_version OR OLD.phase_input_checksum IS NOT NEW.phase_input_checksum OR OLD.policy_json IS NOT NEW.policy_json OR OLD.policy_checksum IS NOT NEW.policy_checksum OR OLD.upstream_rankings_snapshot_id IS NOT NEW.upstream_rankings_snapshot_id OR OLD.upstream_rankings_checksum IS NOT NEW.upstream_rankings_checksum OR OLD.upstream_recommendation_gate_snapshot_id IS NOT NEW.upstream_recommendation_gate_snapshot_id OR OLD.upstream_recommendation_gate_checksum IS NOT NEW.upstream_recommendation_gate_checksum OR OLD.upstream_value_engine_snapshot_id IS NOT NEW.upstream_value_engine_snapshot_id OR OLD.upstream_value_engine_checksum IS NOT NEW.upstream_value_engine_checksum OR OLD.upstream_predictions_snapshot_id IS NOT NEW.upstream_predictions_snapshot_id OR OLD.upstream_predictions_checksum IS NOT NEW.upstream_predictions_checksum OR OLD.upstream_matchup_packet_snapshot_id IS NOT NEW.upstream_matchup_packet_snapshot_id OR OLD.upstream_matchup_packet_checksum IS NOT NEW.upstream_matchup_packet_checksum OR OLD.upstream_data_quality_snapshot_id IS NOT NEW.upstream_data_quality_snapshot_id OR OLD.upstream_data_quality_checksum IS NOT NEW.upstream_data_quality_checksum OR OLD.semantic_checksum IS NOT NEW.semantic_checksum OR OLD.game_count IS NOT NEW.game_count OR OLD.recommendation_count IS NOT NEW.recommendation_count OR OLD.warning_count IS NOT NEW.warning_count OR OLD.canonical_json IS NOT NEW.canonical_json OR OLD.document_relpath IS NOT NEW.document_relpath OR OLD.document_checksum IS NOT NEW.document_checksum OR OLD.document_byte_count IS NOT NEW.document_byte_count OR OLD.pdf_relpath IS NOT NEW.pdf_relpath OR OLD.pdf_checksum IS NOT NEW.pdf_checksum OR OLD.pdf_byte_count IS NOT NEW.pdf_byte_count OR OLD.pdf_page_count IS NOT NEW.pdf_page_count OR OLD.render_manifest_relpath IS NOT NEW.render_manifest_relpath OR OLD.render_manifest_checksum IS NOT NEW.render_manifest_checksum OR OLD.render_manifest_byte_count IS NOT NEW.render_manifest_byte_count OR OLD.report_status IS NOT NEW.report_status OR OLD.created_at IS NOT NEW.created_at THEN RAISE(ABORT,'PDF snapshot semantics cannot change during seal') END;
      SELECT CASE WHEN (SELECT count(*) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>NEW.game_count THEN RAISE(ABORT,'PDF game count mismatch') END;
      SELECT CASE WHEN NEW.game_count>0 AND ((SELECT min(ordinal) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(ordinal) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>NEW.game_count) THEN RAISE(ABORT,'PDF game ordinals are not contiguous') END;
      SELECT CASE WHEN (SELECT count(recommendation_rank) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>NEW.recommendation_count THEN RAISE(ABORT,'PDF recommendation count mismatch') END;
      SELECT CASE WHEN NEW.recommendation_count>0 AND ((SELECT min(recommendation_rank) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>1 OR (SELECT max(recommendation_rank) FROM pdf_report_games g WHERE g.snapshot_id=NEW.snapshot_id)<>NEW.recommendation_count) THEN RAISE(ABORT,'PDF recommendation ranks are not contiguous') END;
    END
    """,
    """
    CREATE TRIGGER infographic_variants_validate_unsealed
    BEFORE INSERT ON infographic_variants
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM infographic_snapshots s WHERE s.snapshot_id=NEW.snapshot_id AND s.run_id=NEW.run_id AND s.phase_attempt=NEW.phase_attempt AND s.sealed_at IS NULL) THEN RAISE(ABORT,'Infographic variant requires its unsealed snapshot') END;
    END
    """,
    """
    CREATE TRIGGER infographic_attempt_validate_phase_and_upstream
    BEFORE INSERT ON infographic_attempt_evidence
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pipeline_run_phases p WHERE p.run_id=NEW.run_id AND p.phase_key='infographic' AND p.status='running' AND p.attempt_count=NEW.phase_attempt) THEN RAISE(ABORT,'Infographic attempt is not active') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pdf_report_snapshots s WHERE s.snapshot_id=NEW.upstream_pdf_report_snapshot_id AND s.semantic_checksum=NEW.upstream_pdf_report_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Infographic PDF upstream mismatch') END;
    END
    """,
    """
    CREATE TRIGGER infographic_snapshot_validate_attempt
    BEFORE INSERT ON infographic_snapshots
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM infographic_attempt_evidence a WHERE a.run_id=NEW.run_id AND a.phase_attempt=NEW.phase_attempt AND a.outcome='assembled' AND a.snapshot_checksum=NEW.infographic_checksum AND a.phase_input_checksum=NEW.phase_input_checksum AND a.policy_json=NEW.policy_json AND a.policy_checksum=NEW.policy_checksum AND a.upstream_pdf_report_snapshot_id=NEW.upstream_pdf_report_snapshot_id AND a.upstream_pdf_report_checksum=NEW.upstream_pdf_report_checksum) THEN RAISE(ABORT,'Infographic snapshot disagrees with attempt evidence') END;
    END
    """,
    """
    CREATE TRIGGER infographic_snapshots_validate_seal
    BEFORE UPDATE OF sealed_at ON infographic_snapshots
    BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL THEN RAISE(ABORT,'Infographic snapshot may be sealed once') END;
      SELECT CASE WHEN OLD.snapshot_id IS NOT NEW.snapshot_id OR OLD.run_id IS NOT NEW.run_id OR OLD.phase_attempt IS NOT NEW.phase_attempt OR OLD.requested_date IS NOT NEW.requested_date OR OLD.as_of_time IS NOT NEW.as_of_time OR OLD.generated_at IS NOT NEW.generated_at OR OLD.contract_version IS NOT NEW.contract_version OR OLD.render_version IS NOT NEW.render_version OR OLD.phase_input_checksum IS NOT NEW.phase_input_checksum OR OLD.policy_json IS NOT NEW.policy_json OR OLD.policy_checksum IS NOT NEW.policy_checksum OR OLD.upstream_pdf_report_snapshot_id IS NOT NEW.upstream_pdf_report_snapshot_id OR OLD.upstream_pdf_report_checksum IS NOT NEW.upstream_pdf_report_checksum OR OLD.infographic_checksum IS NOT NEW.infographic_checksum OR OLD.recommendation_count IS NOT NEW.recommendation_count OR OLD.warning_count IS NOT NEW.warning_count OR OLD.canonical_json IS NOT NEW.canonical_json OR OLD.document_relpath IS NOT NEW.document_relpath OR OLD.document_checksum IS NOT NEW.document_checksum OR OLD.document_byte_count IS NOT NEW.document_byte_count OR OLD.feed_relpath IS NOT NEW.feed_relpath OR OLD.feed_checksum IS NOT NEW.feed_checksum OR OLD.feed_byte_count IS NOT NEW.feed_byte_count OR OLD.story_relpath IS NOT NEW.story_relpath OR OLD.story_checksum IS NOT NEW.story_checksum OR OLD.story_byte_count IS NOT NEW.story_byte_count OR OLD.render_manifest_relpath IS NOT NEW.render_manifest_relpath OR OLD.render_manifest_checksum IS NOT NEW.render_manifest_checksum OR OLD.render_manifest_byte_count IS NOT NEW.render_manifest_byte_count OR OLD.report_status IS NOT NEW.report_status OR OLD.created_at IS NOT NEW.created_at THEN RAISE(ABORT,'Infographic snapshot semantics cannot change during seal') END;
      SELECT CASE WHEN (SELECT count(*) FROM infographic_variants v WHERE v.snapshot_id=NEW.snapshot_id)<>2 THEN RAISE(ABORT,'Infographic requires two variants') END;
    END
    """,
    """
    CREATE TRIGGER final_qc_checks_validate_unsealed
    BEFORE INSERT ON final_qc_checks
    BEGIN
      SELECT CASE WHEN EXISTS (SELECT 1 FROM final_qc_snapshots s WHERE s.run_id=NEW.run_id AND s.phase_attempt=NEW.phase_attempt AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Final QC checks cannot change after seal') END;
    END
    """,
    """
    CREATE TRIGGER final_qc_attempt_validate_phase_and_upstream
    BEFORE INSERT ON final_qc_attempt_evidence
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pipeline_run_phases p WHERE p.run_id=NEW.run_id AND p.phase_key='final_qc' AND p.status='running' AND p.attempt_count=NEW.phase_attempt) THEN RAISE(ABORT,'Final QC attempt is not active') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pdf_report_snapshots s WHERE s.snapshot_id=NEW.upstream_pdf_report_snapshot_id AND s.semantic_checksum=NEW.upstream_pdf_report_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Final QC PDF upstream mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM infographic_snapshots s WHERE s.snapshot_id=NEW.upstream_infographic_snapshot_id AND s.infographic_checksum=NEW.upstream_infographic_checksum AND s.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Final QC Infographic upstream mismatch') END;
    END
    """,
    """
    CREATE TRIGGER final_qc_snapshot_validate_attempt
    BEFORE INSERT ON final_qc_snapshots
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM final_qc_attempt_evidence a WHERE a.run_id=NEW.run_id AND a.phase_attempt=NEW.phase_attempt AND a.outcome='assembled' AND a.snapshot_checksum=NEW.qc_checksum AND a.phase_input_checksum=NEW.phase_input_checksum AND a.policy_json=NEW.policy_json AND a.policy_checksum=NEW.policy_checksum AND a.upstream_pdf_report_snapshot_id=NEW.upstream_pdf_report_snapshot_id AND a.upstream_pdf_report_checksum=NEW.upstream_pdf_report_checksum AND a.upstream_infographic_snapshot_id=NEW.upstream_infographic_snapshot_id AND a.upstream_infographic_checksum=NEW.upstream_infographic_checksum) THEN RAISE(ABORT,'Final QC snapshot disagrees with attempt evidence') END;
    END
    """,
    """
    CREATE TRIGGER final_qc_snapshots_validate_seal
    BEFORE UPDATE OF sealed_at ON final_qc_snapshots
    BEGIN
      SELECT CASE WHEN OLD.sealed_at IS NOT NULL OR NEW.sealed_at IS NULL THEN RAISE(ABORT,'Final QC snapshot may be sealed once') END;
      SELECT CASE WHEN OLD.snapshot_id IS NOT NEW.snapshot_id OR OLD.run_id IS NOT NEW.run_id OR OLD.phase_attempt IS NOT NEW.phase_attempt OR OLD.requested_date IS NOT NEW.requested_date OR OLD.as_of_time IS NOT NEW.as_of_time OR OLD.evaluated_at IS NOT NEW.evaluated_at OR OLD.contract_version IS NOT NEW.contract_version OR OLD.phase_input_checksum IS NOT NEW.phase_input_checksum OR OLD.policy_json IS NOT NEW.policy_json OR OLD.policy_checksum IS NOT NEW.policy_checksum OR OLD.upstream_pdf_report_snapshot_id IS NOT NEW.upstream_pdf_report_snapshot_id OR OLD.upstream_pdf_report_checksum IS NOT NEW.upstream_pdf_report_checksum OR OLD.upstream_infographic_snapshot_id IS NOT NEW.upstream_infographic_snapshot_id OR OLD.upstream_infographic_checksum IS NOT NEW.upstream_infographic_checksum OR OLD.qc_checksum IS NOT NEW.qc_checksum OR OLD.overall_result IS NOT NEW.overall_result OR OLD.check_count IS NOT NEW.check_count OR OLD.canonical_json IS NOT NEW.canonical_json OR OLD.artifact_relpath IS NOT NEW.artifact_relpath OR OLD.artifact_checksum IS NOT NEW.artifact_checksum OR OLD.artifact_byte_count IS NOT NEW.artifact_byte_count OR OLD.created_at IS NOT NEW.created_at THEN RAISE(ABORT,'Final QC snapshot semantics cannot change during seal') END;
      SELECT CASE WHEN (SELECT count(*) FROM final_qc_checks q WHERE q.run_id=NEW.run_id AND q.phase_attempt=NEW.phase_attempt)<>NEW.check_count THEN RAISE(ABORT,'Final QC check count mismatch') END;
      SELECT CASE WHEN (SELECT min(ordinal) FROM final_qc_checks q WHERE q.run_id=NEW.run_id AND q.phase_attempt=NEW.phase_attempt)<>1 OR (SELECT max(ordinal) FROM final_qc_checks q WHERE q.run_id=NEW.run_id AND q.phase_attempt=NEW.phase_attempt)<>NEW.check_count THEN RAISE(ABORT,'Final QC check ordinals are not contiguous') END;
      SELECT CASE WHEN EXISTS (SELECT 1 FROM final_qc_checks q WHERE q.run_id=NEW.run_id AND q.phase_attempt=NEW.phase_attempt AND q.passed<>1) THEN RAISE(ABORT,'Final QC cannot seal failed checks') END;
    END
    """,
    *(
        f"""
        CREATE TRIGGER {table}_reject_unsealed_update
        BEFORE UPDATE ON {table}
        WHEN OLD.sealed_at IS NULL AND NEW.sealed_at IS NULL
        BEGIN SELECT RAISE(ABORT,'{table} may only transition from unsealed to sealed'); END
        """
        for table in ("pdf_report_snapshots", "infographic_snapshots", "final_qc_snapshots")
    ),
    """
    CREATE TRIGGER human_review_records_validate_target
    BEFORE INSERT ON human_review_records
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM final_qc_snapshots q WHERE q.snapshot_id=NEW.final_qc_snapshot_id AND q.run_id=NEW.run_id AND q.requested_date=NEW.requested_date AND q.qc_checksum=NEW.final_qc_checksum AND q.overall_result='pass' AND q.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Human Review requires exact sealed PASS Final QC') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pdf_report_snapshots p WHERE p.snapshot_id=NEW.pdf_report_snapshot_id AND p.run_id=NEW.run_id AND p.semantic_checksum=NEW.pdf_report_checksum AND p.pdf_checksum=NEW.pdf_artifact_checksum AND p.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Human Review PDF identity mismatch') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM infographic_snapshots i WHERE i.snapshot_id=NEW.infographic_snapshot_id AND i.run_id=NEW.run_id AND i.infographic_checksum=NEW.infographic_checksum AND i.sealed_at IS NOT NULL) THEN RAISE(ABORT,'Human Review Infographic identity mismatch') END;
    END
    """,
    """
    CREATE TRIGGER human_review_attempt_validate_phase_and_review
    BEFORE INSERT ON human_review_attempt_evidence
    BEGIN
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM pipeline_run_phases p WHERE p.run_id=NEW.run_id AND p.phase_key='human_review' AND p.status='running' AND p.attempt_count=NEW.phase_attempt) THEN RAISE(ABORT,'Human Review attempt is not active') END;
      SELECT CASE WHEN NOT EXISTS (SELECT 1 FROM human_review_records r WHERE r.review_id=NEW.review_id AND r.run_id=NEW.run_id AND r.final_qc_snapshot_id=NEW.final_qc_snapshot_id AND r.final_qc_checksum=NEW.final_qc_checksum AND r.review_checksum=NEW.review_checksum AND r.decision=NEW.decision) THEN RAISE(ABORT,'Human Review attempt identity mismatch') END;
    END
    """,
)


_SNAPSHOT_IMMUTABILITY = (
    "pdf_report_snapshots",
    "infographic_snapshots",
    "final_qc_snapshots",
)
_FULL_IMMUTABILITY = (
    "pdf_report_attempt_evidence",
    "pdf_report_games",
    "infographic_attempt_evidence",
    "infographic_variants",
    "final_qc_attempt_evidence",
    "final_qc_checks",
    "human_review_records",
    "human_review_attempt_evidence",
)


FINAL_OUTPUT_SCHEMA_V14_IMMUTABILITY_TRIGGER_STATEMENTS = (
    *(statement for table in _FULL_IMMUTABILITY for statement in _immutable(table)),
    *(
        statement
        for table in _SNAPSHOT_IMMUTABILITY
        for statement in (
            f"""
            CREATE TRIGGER {table}_reject_update_after_seal
            BEFORE UPDATE ON {table} WHEN OLD.sealed_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, '{table} records are immutable after seal'); END
            """,
            f"""
            CREATE TRIGGER {table}_reject_delete
            BEFORE DELETE ON {table}
            BEGIN SELECT RAISE(ABORT, '{table} records are immutable'); END
            """,
        )
    ),
)


FINAL_OUTPUT_SCHEMA_V14_STATEMENTS = (
    *FINAL_OUTPUT_SCHEMA_V14_TABLE_STATEMENTS,
    *FINAL_OUTPUT_SCHEMA_V14_INDEX_STATEMENTS,
    *FINAL_OUTPUT_SCHEMA_V14_VALIDATION_TRIGGER_STATEMENTS,
    *FINAL_OUTPUT_SCHEMA_V14_IMMUTABILITY_TRIGGER_STATEMENTS,
)
