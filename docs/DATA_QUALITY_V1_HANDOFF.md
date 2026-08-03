# Data Quality V1 Handoff

## External integrity correction

Historical assessment reads resolve the exact four upstream snapshot
IDs/checksums stored on that assessment, never latest run state.
`assessment_failed` evidence uses already-resolved upstream/context identity
and does not invoke the failed assessment again. Standalone manifests and
snapshot artifacts reject configured secrets, noncanonical retained bytes,
symbolic links, and hard-linked final files. Status is deterministic: no issues
is `SUCCEEDED`, info-only issues is `SUCCEEDED_WITH_WARNINGS`, and any degraded
or insufficient game is `DEGRADED`. The immutable policy actively controls
supported-market assessment behavior.

Phase 5 is implemented end to end on the pre-model persistence sprint branch.
The production handler verifies the complete sealed Phase 1–4 chain, fixes one
UTC observation time, assesses without acquisition, persists through
`DataQualityRepository`, closes/reopens SQLite, and returns the exact sealed
artifact/checksum. The controller alone changes phase state.

Repository reads independently re-run the accepted assessment, compare all
relational games/issues and ordinals, verify the attempt manifest and artifact,
and reject unsealed or conflicting evidence. Exact replay is idempotent;
different input or canonical bytes conflict. Newly owned files are removed on
transaction rollback without deleting pre-existing immutable evidence.

Schema v12 migration identity is
`pre_model_pipeline_v1_temporal_persistence`. Phase 5 owns four tables:
`data_quality_attempt_evidence`, `data_quality_snapshots`,
`data_quality_games`, and `data_quality_issues`.

No provider, prediction, value, recommendation, ranking, report, or publication
logic exists here. The direct downstream consumer is Matchup Packet.
