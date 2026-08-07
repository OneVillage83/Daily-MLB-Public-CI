# Prediction Decision Migration v13 Specification

Migration `prediction_decision_v1_temporal_persistence` is the single additive
schema v13 migration for Phases 8-11. It leaves every v1-v12 statement tuple and
identity unchanged. The final migration checksum is
`9606657f9497cd54444ecb35680f05d7003a0db535d2e9a1fcc594c9bbf63089`;
the formal schema fingerprint is
`d33d27ba07d21aa35584e0afe3c39334deba30170d761897acb13e9e04a65fee`.
The formal chain contains 155 statements.

## Object inventory

The 17 tables are:

- Predictions: `prediction_authoring_inputs`, `predictions_attempt_evidence`,
  `prediction_snapshots`, `prediction_games`.
- Value Engine: `value_engine_attempt_evidence`, `value_engine_snapshots`,
  `value_engine_games`, `value_engine_outcomes`, `value_engine_book_pairs`.
- Recommendation Gate: `recommendation_gate_attempt_evidence`,
  `recommendation_gate_snapshots`, `recommendation_gate_games`,
  `recommendation_gate_sides`, `recommendation_gate_results`.
- Rankings: `rankings_attempt_evidence`, `ranking_snapshots`,
  `ranking_entries`.

The 18 explicit indexes cover authoring lookup, attempt outcome lookup, exact
upstream snapshot lookup, game identity, outcome side, bookmaker retrieval,
gate result code, and ranking identity. Partial unique indexes enforce at most
one recommended side per game and unique non-null recommendation rank.

There are 16 active-attempt, child-insertion, and seal-validation triggers and
34 immutability triggers. First seal may change only `sealed_at`; every semantic
column must equal its old value. Seal validation proves parent counts, game and
child ordinals, exactly two moneyline outcomes/sides, pair counts, gate result
counts, and contiguous recommendation ranks. Sealed snapshots and children are
immutable and undeletable.

The corrected seal validators also prove reviewed market-independence
attestation, exact authoring-to-Model-Feature-Set identity, Gate reason/result
equality and side/game decision consistency, and ranking completeness without
requiring ranks to ascend in slate order. Gate rows require the exact ordered
17-code inventory and sole-selection evidence. Ranking policies require the
frozen V1 comparator JSON, exact policy version, and canonical policy checksum.
Gate sealing rejects every failed structural code, so structural corruption
cannot be persisted as PASS or AVOID. First seal still permits only `sealed_at: NULL` to a
non-null value; semantic mutation during that update remains prohibited.

## Evidence identity

Attempts bind the exact controller attempt, upstream IDs/checksums, full policy
or provider identity, phase-input checksum, outcome, warning inventory, and
attempt manifest. Successful attempts alone have a snapshot checksum. Snapshot
paths are constrained to each phase's exact content-addressed semantic path.
Canonical child JSON checksums must agree with relational checksum columns.

Prediction authoring is anchored to one exact Model Feature Set snapshot and
game, and retains explicit market-independence attestation. Invalid retained
authoring inventory is stored as durable failed-attempt evidence. Value outcomes
retain home and away rows plus same-book pair children. Gate retains two sides
and ordered gate results whose failures exactly match reason codes. Rankings
retains every Gate game while only recommendations receive a rank. All histories are append-only; no mutable
latest-state row is introduced.

Fresh installation and exact v12-to-v13 upgrade produce the same fingerprint.
Migration application is transactional, offline, and validated with SQLite
integrity and foreign-key checks.
