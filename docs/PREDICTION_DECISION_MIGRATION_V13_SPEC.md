# Prediction Decision Migration v13 Specification

Migration `prediction_decision_v1_temporal_persistence` is the single additive
schema v13 migration for Phases 8-11. It leaves every v1-v12 statement tuple and
identity unchanged. The final migration checksum is
`1c17d467535a6531b920378611881634e5ef5349ac7fa3152d51597ae37f8b84`;
the formal schema fingerprint is
`9813312fe416dc0aa69c155a3da36e0d98d8ef6cb8caac46ba2bb752eab8b856`.
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

## Evidence identity

Attempts bind the exact controller attempt, upstream IDs/checksums, full policy
or provider identity, phase-input checksum, outcome, warning inventory, and
attempt manifest. Successful attempts alone have a snapshot checksum. Snapshot
paths are constrained to each phase's exact content-addressed semantic path.
Canonical child JSON checksums must agree with relational checksum columns.

Prediction authoring is anchored to one Model Feature Set game. Value outcomes
retain home and away rows plus same-book pair children. Gate retains two sides
and ordered gate results. Rankings retains every Gate game while only
recommendations receive a rank. All histories are append-only; no mutable
latest-state row is introduced.

Fresh installation and exact v12-to-v13 upgrade produce the same fingerprint.
Migration application is transactional, offline, and validated with SQLite
integrity and foreign-key checks.
