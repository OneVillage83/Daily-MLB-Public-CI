# Schema v12 — Pre-Model Pipeline Temporal Persistence

Migration name: `pre_model_pipeline_v1_temporal_persistence`
Target version: 12
Formal statement count: 148
Migration checksum: `9409445202fc362377f112dc546bacf087820b0f1fb608b08b1a542afa4950d0`
Schema fingerprint: `f597210e59f941e3e0bcdd5583dea597ee9cbbcb598a45fc23abe18144f52a22`

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
Its attempt and snapshot rows also retain canonical `policy_json`, explicit
`policy_version`, and `policy_checksum`; insert/seal validation binds the
attempt, relational snapshot, and canonical snapshot policy identities.
Matchup Packet stores one ordered canonical packet per upstream slate game.
Model Feature Set stores one ordered predictive vector per packet game, one
separate factual market-context row per game, and ordered exact retained V3
feature lineage. Every source lineage row binds `canonical_player_id`,
`feature_snapshot_id`, and `feature_checksum`; its insertion and first seal
verify the exact player, V3 version/date/completeness/PIT evidence, canonical
game payload, contiguous ordinal, and global selected inventory. No EAV
scalar-feature table or universal phase payload exists.

The three snapshot immutability triggers allow exactly one pre-seal update:
`sealed_at` from NULL to a non-NULL aware timestamp. All semantic columns must
remain byte-identical during that update. The seal validators read NEW values,
reconcile game/issue/market/source counts and ordinals, and reject incomplete or
extra source-feature lineage. After sealing, all updates and deletes fail.

Phase-specific attempt manifests enforce their exact phase key, contract,
outcome set, ordered upstream phase inventory, positive non-Boolean attempt,
canonical UTC timestamps, completion ordering, and assembled/snapshot checksum
rule. Publication and retained-byte verification reject configured secrets,
symbolic links, and files whose exposed link count is not exactly one.
Model Feature Set attempt evidence additionally records whether its exact
packet-derived selected inventory is `unverified` (`input_failed`) or
`validated` (all later outcomes), preventing failed input from being relabeled
as transformation-ready evidence.

Fresh installation and exact frozen-v11 upgrade produce the same fingerprint.
Upgrade uses the established verified backup, diagnostic, transactional DDL,
precommit verification, rollback, integrity, and foreign-key-check path.
