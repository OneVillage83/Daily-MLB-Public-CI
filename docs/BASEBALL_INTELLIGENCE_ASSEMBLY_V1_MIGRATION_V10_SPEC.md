# Baseball Intelligence Assembly V1 — Schema v10 Migration Specification

**Status:** authoritative migration-only specification.  No BIA repository,
selector, handler, or controller registration is part of this checkpoint.

## Identity and transaction boundary

- Source: formal schema v9; target: v10.
- Migration name: `baseball_intelligence_assembly_v1_temporal_persistence`.
- Checksum label: `formal-v9-to-v10`.
- The central migration executes under `BEGIN IMMEDIATE` with foreign keys
  temporarily disabled only for the established constrained-table rebuild,
  then restored and verified before commit returns.
- Historical v1–v9 statements, names, checksums, fingerprints, and behavior
  are immutable. Fresh databases install sequentially through v10.

## DDL order

1. Drop only triggers which reference rebuilt `collector_runs` or
   `pipeline_runs`.
2. Rebuild those two version-constrained tables solely to accept schema v10;
   copy every row and recreate their existing triggers/indexes.
3. Create `baseball_intelligence_attempt_evidence`,
   `baseball_intelligence_snapshots`, `baseball_intelligence_games`,
   `baseball_intelligence_players`, and
   `baseball_intelligence_feature_equivalents`.
4. Create the focused lookup indexes and insert/seal/immutability triggers.
5. Recreate the temporarily dropped v9 GameState triggers and validate the
   full v10 fingerprint before recording migration history and user version.

All retained evidence uses `ON DELETE RESTRICT`; no BIA evidence cascades.

## Temporal lineage and evidence

Attempt evidence is one immutable row per `(run_id, phase_attempt)`, fixed to
`baseball_intelligence_assembly`. It retains the exact sealed DailySlate and
GameState snapshot IDs/checksums, fixed `selection_observed_at`, deterministic
warnings, and the evidence-manifest path/checksum/byte-count group. Outcomes
are `assembled`, `selection_failed`, or `assembly_failed`; only `assembled`
may carry an assembly checksum. Failed attempts intentionally have no BIA
snapshot but remain reconstructable from their immutable manifest.

An assembled snapshot is content addressed as `bia:<assembly_checksum>`; SQL
enforces exact `snapshot_id = 'bia:' || assembly_checksum`, rather than only a
prefix/length shape. It
retains both upstream chains, contract/feature versions, artifact metadata,
canonical JSON, counts, source run/checksum arrays, warnings, and a one-way
seal. Artifact containment and exact file-byte verification are mandatory
repository responsibilities in the next checkpoint; this migration preserves
the atomic path/checksum/byte-count metadata group.

## Child rows and selection evidence

Games reconcile ordinal, IDs, teams, and DailySlate/GameState row checksums.
Players reconcile exact GameState away/home canonical and source-team identity
through the sealed GameState game canonical JSON. `player_identity_id` and
`canonical_player_id` are SQL-enforced as a both-or-neither pair. Available
players require that resolved pair plus a `complete` or `degraded` V3 player
feature representative; unavailable players have no feature lineage but may
retain a resolved identity pair when no usable feature exists.
`blocked` never appears in selected rows. Equivalent rows retain all accepted
checksum-equivalent feature snapshot/run IDs without duplicating payloads.
They must match the representative checksum and immutable V3 player row.
`player_identity_id` remains application-verified because GameState's source
identity domain is not a safe direct foreign-key target.

At sealing, the database verifies counts, contiguous game/player/equivalent
ordinals, upstream game coverage, per-game player/available-feature counts,
one representative per available player matching its parent representative
snapshot/run/checksum, and no equivalents for unavailable players. Full
GameState player-record checksum reconstruction remains a mandatory repository
verification because v9 intentionally has no relational player child table.
Exact top-level set
equality for source-run/checksum inventories is additionally a mandatory
repository seal/reconstruction verification because SQLite cannot safely
express that JSON-set comparison as a durable simple constraint.

## Backup, diagnostic, and preservation

Recognized v9 databases are checked for exact history/fingerprint, integrity,
foreign keys, and enabled FK enforcement before DDL. SQLite's backup API writes
`<database>.pre-v10-<timestamp>-<8hex>.sqlite3`; the verified diagnostic is
`migration-v10-<timestamp>-<8hex>.json`. It records source/target identity,
paths, checks, timestamps, and outcome, is atomically written/read before DDL,
and is atomically completed after success or failure. Fresh installs need no
pre-v10 backup. Any backup, diagnostic, DDL, or pre-commit fingerprint failure
rolls back to readable v9 and leaves the verified backup available.

Populated v9 preservation tests cover controller transitions, DailySlate and
GameState evidence, canonical JSON/checksums, stats runs/player identities,
and complete/degraded/blocked/equivalent V3 feature rows. Required tests also
cover every insert trigger, immutability trigger, valid multi-game and
zero-game seals, rollback injection, idempotence, and integrity/FK checks.
