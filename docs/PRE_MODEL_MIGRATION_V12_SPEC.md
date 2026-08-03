# Schema v12 — Pre-Model Pipeline Temporal Persistence

Migration name: `pre_model_pipeline_v1_temporal_persistence`
Target version: 12
Formal statement count: 148
Migration checksum: `1408c940cea49c84c68c87a9d350a852d038671e9867acec534440435d60d5ce`
Schema fingerprint: `15e30a24bb578ce691ef5b9063ce61fe8d3e2a09708236de55390ccef9ed8581`

All schema v1–v11 statement bytes, migration names, checksums, order, and
fingerprints remain frozen. V12 rebuilds only the two existing tables whose
schema-version constraints must admit 12, then appends phase-specific objects.

## Object inventory

Twelve tables:

- Data Quality: `data_quality_attempt_evidence`, `data_quality_snapshots`,
  `data_quality_games`, `data_quality_issues`;
- Matchup Packet: `matchup_packet_attempt_evidence`,
  `matchup_packet_snapshots`, `matchup_packet_games`;
- Model Feature Set: `model_feature_set_attempt_evidence`,
  `model_feature_set_snapshots`, `model_feature_set_games`,
  `model_feature_set_market_contexts`, `model_feature_set_source_features`.

Eleven explicit indexes provide run/attempt, requested-date, upstream lineage,
source-game, artifact-checksum, quality-state, and selected-feature access.
Fifteen validation/sealing triggers enforce active phase ownership, exact
upstream IDs/checksums, unsealed child insertion, contiguous ordinals, counts,
and the one-time seal. Twenty-four immutability triggers reject retained child,
attempt, snapshot update/delete operations after their permitted insert/seal
lifecycle.

Every attempt has a non-null run/attempt identity and phase-specific outcome.
Only `assembled` attempts may carry a snapshot checksum. Snapshot IDs and
relative artifact paths are exact checksum-derived identities. Parent/child
foreign keys include run and attempt ownership to prevent cross-attempt mixing.

Data Quality stores ordered per-game assessments and per-game ordered issues.
Matchup Packet stores one ordered canonical packet per upstream slate game.
Model Feature Set stores one ordered predictive vector per packet game, one
separate factual market-context row per game, and ordered exact retained V3
feature lineage. No EAV scalar-feature table or universal phase payload exists.

Fresh installation and exact frozen-v11 upgrade produce the same fingerprint.
Upgrade uses the established verified backup, diagnostic, transactional DDL,
precommit verification, rollback, integrity, and foreign-key-check path.
