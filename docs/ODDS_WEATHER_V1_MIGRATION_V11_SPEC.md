# Odds + Weather V1 Schema-v11 Migration Specification

**Checkpoint:** `rc/odds-weather-schema-v11-20260731`

**Source schema:** formal schema v10

**Target schema:** formal schema v11

**Migration name:** `odds_weather_v1_temporal_persistence`

## 1. Scope and frozen boundaries

Schema v11 is the sole creation mechanism for the Phase 4 temporal persistence
surface. It persists the accepted `OddsWeatherV1` contract and the retained
provider evidence needed to reproduce its PIT selection. It does not implement
an `OddsWeatherRepository`, a production handler, provider orchestration,
controller registration, Data Quality, or any later phase.

Every v1-v10 migration statement, name, checksum, fingerprint, and semantic
behavior remains byte-for-byte unchanged. Only the schema-version constraints
on `collector_runs` and `pipeline_runs` are extended to accept version 11.

## 2. Migration identity and transaction

The migration checksum label is `formal-v10-to-v11`. The checksum is SHA-256
over that label, the migration name, and the canonical ordered v11 statements,
following the existing formal migration convention. The formal schema-v11
fingerprint is computed from the exact v1-v11 statement chain.

- Migration checksum:
  `a54865d8b5623e96c4f571d6c9d7f899e9ced0d1911128b5a874b41e29df75dd`
- Formal schema-v11 fingerprint:
  `5b9635e1aac05d98fd61dadaf2ac5d435e4aaae9214c79501b5dd642673c75b8`

An existing recognized v10 database receives a verified SQLite-backup-API
backup named:

`<database>.pre-v11-<timestamp>-<8hex>.sqlite3`

The corresponding credential-free diagnostic is named:

`migration-v11-<timestamp>-<8hex>.json`

The source and backup must each have the exact formal v10 fingerprint,
`integrity_check = ok`, zero `foreign_key_check` rows, and exact migration
history through v10 before any v11 DDL begins. The diagnostic is atomically
written, read back, and verified before DDL.

DDL executes under the established `PRAGMA foreign_keys=OFF` / `BEGIN
IMMEDIATE` transactional boundary. The migration row and `user_version=11`
are written only inside that transaction. The exact formal v11 fingerprint is
verified before commit. Every exception rolls back to a usable v10 database,
restores foreign-key enforcement, retains the verified backup, and records a
failed diagnostic without a false v11 success row. Fresh sequential installs
through v11 do not create a pre-v11 backup.

## 3. Table inventory and relationships

### `odds_weather_attempt_evidence`

One immutable row per `(run_id, phase_attempt)`. It binds the active
`odds_weather` attempt to the exact sealed DailySlate, GameState, and BIA chain,
the inherited run `as_of_time`, fixed Phase 4 `observed_at` cutoff, deterministic
phase input checksum, outcome, optional assembled snapshot checksum, immutable
attempt-manifest path/checksum/byte count, warnings JSON/count, and distinct
attempt `created_at` and `completed_at` timestamps.

Allowed outcomes are `assembled`, `acquisition_failed`,
`normalization_failed`, and `assembly_failed`. `assembled` requires a snapshot
checksum; failed outcomes prohibit one. A failed attempt can retain raw/provider
evidence without creating a canonical snapshot. Acquisition failure is never a
zero-game success.

### `odds_weather_snapshots`

One immutable canonical Phase 4 snapshot per `(run_id, phase_attempt)`. The
identity is exactly `odds-weather:` plus the `snapshot_checksum`. It retains
the exact run/date/time lineage, all three sealed upstream snapshot IDs and
checksums, phase input checksum, accepted contract constants, selected raw
capture checksum inventory, deterministic warnings, canonical snapshot JSON,
child counts, the required content-addressed artifact metadata group, and a
one-time `sealed_at` transition.

Artifact relative path, checksum, and byte count are all required. Application
verification in the later repository must require containment, canonical
bytes, SHA-256, and byte count. SQL independently requires the exact semantic
path `odds_weather/snapshots/<snapshot_checksum>/odds_weather_v1.json`, a
strictly positive artifact byte count, and equality between
`json_extract(canonical_json,'$.checksum')` and `snapshot_checksum`. The
snapshot checksum remains the contract's semantic content identity; the
artifact checksum remains the SHA-256 of exact serialized artifact bytes and
is intentionally not forced equal by SQL.

### `odds_weather_games`

One ordered row for every upstream game. It retains exact canonical and source
game identity, official/requested date, canonical and source team identity,
venue and GameState/BIA-derived game status lineage, scheduled first pitch,
all upstream game checksums, the selected odds summary dimensions, selected
weather status/context dimensions, canonical game JSON, and row checksum.

Rows reconcile by ordinal and identity with both DailySlate and BIA. The BIA
row supplies the exact GameState game checksum. A valid zero-game upstream pair
has zero rows.

### `odds_weather_warnings`

Ordered canonical `OddsWeatherWarningV1` rows belonging to one snapshot.
Code/domain/message and optional game/provider/event identity are retained with
canonical JSON and row checksum. Ordinals are contiguous and reproduce the
snapshot warning array exactly. Failed-attempt warnings remain separately in
immutable attempt evidence.

### `odds_weather_raw_captures`

Attempt-scoped immutable raw-capture metadata. Each row records provider,
endpoint category, optional source game/provider event identity, retrieval and
provider timestamps, contained raw relative path, raw SHA-256, byte count,
canonical metadata JSON, row checksum, and deterministic ordinal. It never
stores request URLs, queries, headers, quota metadata, or credentials.

The composite immutable identity is `(run_id, phase_attempt,
raw_capture_checksum)`. Identical bytes may legitimately be retained again in
a later attempt without mutating the earlier row.

### `odds_weather_snapshot_raw_captures`

Ordered link rows from a sealed canonical snapshot to the exact attempt-scoped
raw captures represented by `OddsWeatherV1.source_raw_capture_checksums`.
Set equality with canonical JSON is a mandatory repository seal/reconstruction
check; SQLite enforces parentage, uniqueness, counts, and contiguous ordinals.

### `odds_weather_provider_events`

Immutable retained `OddsProviderEventV1` revisions. The natural revision key is
`(run_id, phase_attempt, provider_event_id, retrieved_at)`, which prevents two
contradictory payloads at the same event retrieval instant. Each row retains
sport, commence time, canonical team resolution, raw capture checksum, event
checksum, contract version, canonical event JSON, row checksum, and a
deterministic ordinal per provider event.

Provider event IDs remain sportsbook identities and are never promoted to MLB
game IDs. Multiple temporal revisions remain distinct.

### `odds_weather_bookmakers`

Immutable bookmaker children of one exact provider-event revision. A bookmaker
key is a semantic provider identifier, not a credential. The row retains key,
title, optional bookmaker last-update time, canonical JSON, row checksum, and
ordinal. Keys and ordinals are unique within the event revision.

### `odds_weather_markets`

Immutable market children of one bookmaker revision. The schema accepts only
the frozen supported market keys `h2h`, `spreads`, and `totals`; it retains the
optional market last-update timestamp, canonical JSON, row checksum, and
ordinal. Keys and ordinals are unique within the bookmaker revision.

### `odds_weather_outcomes`

Immutable outcome children of one market. It retains outcome identity,
American price, optional point, canonical JSON, row checksum, and ordinal.
Outcome identities and ordinals are unique within the exact market revision.

### `odds_weather_odds_revisions`

Immutable PIT history rows belonging to one exact provider event. It retains
bookmaker key, market key, outcome identity, American price, optional point,
provider/bookmaker/market last-update timestamps, collector retrieval time,
canonical JSON, row checksum, and deterministic ordinal. The uniqueness key
prevents contradictory prices for the same semantic observation boundary while
allowing later observations to remain distinct. Rows after a future cutoff are
stored rather than collapsed; the future repository selector must filter them
at or before the fixed Phase 4 cutoff.

SQLite treats `NULL` values as distinct in ordinary unique constraints. The
original provisional v11 composite uniqueness declaration therefore did not
protect no-point moneyline revisions. The final migration replaces it with two
deterministic partial unique indexes:

- `uq_ow_odds_revisions_unpointed_identity` applies when `point IS NULL` and
  keys identity by run, attempt, provider event, event retrieval, bookmaker,
  market, outcome, and revision retrieval time;
- `uq_ow_odds_revisions_pointed_identity` applies when `point IS NOT NULL` and
  adds the exact point to that identity.

For canonical `h2h` history, point is absent and is not an identity dimension.
For spread and total history with a retained real point, point is an identity
dimension, so different valid line points may coexist at one retrieval instant
while duplicate or conflicting evidence at the same point cannot. A structurally
retained unpointed spread or total row remains covered by the unpointed index;
the migration does not silently strengthen or rewrite the accepted contract.
Different revision retrieval times remain distinct for every supported market.

### `odds_weather_weather_revisions`

Immutable retained `WeatherForecastEvidenceV1` revisions for one canonical MLB
source game and provider (`nws` or `openweather`). Each revision retains
collector retrieval time, forecast valid time, forecast-offset minutes,
contract version, forecast checksum, canonical evidence JSON, row checksum,
and a deterministic ordinal per game/provider. The natural revision key is
`(run_id, phase_attempt, source_game_id, provider, retrieved_at)`, preventing
contradictory evidence at the same observation instant while preserving later
revisions.

### `odds_weather_weather_raw_captures`

Ordered many-to-many links from one weather revision to its exact retained raw
captures. This supports NWS point plus hourly evidence and OpenWeather One Call
without flattening their distinct captures.

### `odds_weather_game_weather_selections`

Links a canonical snapshot game to the exact retained selected weather
revision(s). At most one selected revision exists per provider and exactly one
is primary when weather is available. Indoor-fixed-roof and unavailable weather
have no selected revisions. SQLite enforces parentage and uniqueness; the later
repository verifies exact canonical weather JSON and source-priority semantics.

## 4. Temporal distinctions

The schema keeps separate columns for:

- requested report date and upstream game official date;
- inherited immutable run `as_of_time`;
- fixed Phase 4 `observed_at` evidence cutoff;
- provider event and weather collector retrieval times;
- provider, bookmaker, and market last-update times;
- PIT odds-history retrieval/observation time;
- odds event commence time;
- authoritative scheduled first pitch;
- weather forecast valid time;
- raw-capture provider timestamp;
- attempt creation and completion time;
- snapshot creation and sealing time.

Canonical application construction requires timezone-aware values normalized to
UTC. DDL requires nonblank timestamps with an explicit `Z` or signed-offset
suffix; the later repository remains responsible for full datetime parsing and
UTC normalization. Local-time assumptions are prohibited.

## 5. Immutability and sealing

Every attempt, game, warning, raw capture, event revision, bookmaker, market,
outcome, odds-history revision, weather revision, and link row rejects all
updates and deletes. Snapshots reject every update except one valid
`sealed_at: NULL -> nonblank` transition and reject deletion.

Snapshot sealing requires:

- exact active assembled attempt and upstream chain;
- child game/warning/selected-raw counts equal declared counts;
- game, warning, and selected-raw ordinals are contiguous;
- canonical JSON game/warning/raw-checksum array lengths equal declared counts;
- DailySlate and BIA game counts equal the Phase 4 game count;
- every upstream game appears once with exact identity/order/checksums;
- selected odds references resolve to the same attempt's retained provider
  event and raw capture;
- available weather has retained selections and exactly one primary selection;
- indoor/nonapplicable/unavailable weather has zero selections;
- all selected children belong to the same run, attempt, and unsealed snapshot.

Exact canonical child JSON, raw-checksum set equality, odds-summary-to-provider
evidence reconciliation, weather-selection priority, artifact bytes, and full
offline reconstruction are intentionally mandatory later-repository checks
where SQLite JSON set comparison would overstate SQL guarantees.

## 6. Index inventory

The formal migration creates only repository-ready indexes for known access
patterns:

1. `idx_ow_attempt_lookup`
2. `idx_ow_attempt_cutoff`
3. `idx_ow_snapshots_run`
4. `idx_ow_snapshots_upstream_bia`
5. `idx_ow_snapshots_cutoff`
6. `idx_ow_snapshots_artifact`
7. `idx_ow_games_source`
8. `idx_ow_games_provider_event`
9. `idx_ow_warnings_lookup`
10. `idx_ow_raw_captures_provider`
11. `idx_ow_provider_events_cutoff`
12. `idx_ow_provider_events_match`
13. `idx_ow_bookmakers_key`
14. `idx_ow_markets_key_update`
15. `idx_ow_outcomes_identity`
16. `idx_ow_odds_revisions_cutoff`
17. `uq_ow_odds_revisions_unpointed_identity` (partial unique, `point IS NULL`)
18. `uq_ow_odds_revisions_pointed_identity` (partial unique, `point IS NOT NULL`)
19. `idx_ow_weather_revisions_cutoff`
20. `idx_ow_weather_selections_provider`

Primary and unique constraints provide parent-local identity and ordinal lookup;
redundant indexes are omitted.

The formal Phase 4 surface contains 14 tables, 20 explicit indexes, 10
validation/sealing triggers, and 28 immutability triggers. The formal v11
statement chain contains 138 statements.

## 7. Nullable-constraint audit

Every v11 primary-key and unique-index column is explicitly non-null except two
documented cases:

- `odds_weather_odds_revisions.point` is intentionally optional and is made
  null-safe by the paired partial unique indexes above (`corrected`);
- `odds_weather_games.selected_provider_event_id` is intentionally nullable so
  multiple unavailable games can coexist. Its uniqueness prevents one selected
  event from serving two games only when a value exists (`intentional`).

`odds_weather_snapshots.snapshot_id` is explicitly `NOT NULL`; this avoids
SQLite's legacy rowid-table behavior in which a non-integer primary key does not
alone provide a reliable not-null guarantee. All other composite primary-key and
unique-index columns are declared `NOT NULL` (`forbidden`).

Nullable foreign-key fields on `odds_weather_games` are intentional. The odds
availability cross-column check requires selected provider event, odds
retrieval time, raw checksum, match offset, summary JSON, and summary checksum
to be all present for `available` or all absent for `unavailable`; the composite
provider-event and raw-capture foreign keys therefore cannot be partially
bypassed. Multiple unavailable games with null selected-event identity are
valid. Optional venue identity, scheduled start, provider/update timestamps,
forecast offset, warning context, and weather context fields do not participate
in a primary or unique semantic identity. Their NULL behavior is explicit in
type/check clauses and remains application-verified where the accepted contract
delegates semantic reconstruction.

## 8. Security boundary

No v11 table contains a configured-secret list, API key, credential, bearer or
authorization value, request URL/query/header, quota metadata, password, client
secret, signature, or service token. `bookmaker_key` and `market_key` are
accepted only in their schema-owned semantic columns. Generic `key` is not a
column. Canonical and metadata JSON must pass the accepted path-aware and
configured-secret validation before later repository persistence.

Migration SQL, fingerprint, canonical object bytes, equality, and checksums do
not depend on configured secret inventories.

## 9. Required migration verification

Focused tests must prove fresh v11 installation, recognized v10 upgrade,
verified backup/diagnostic behavior, rollback under injected failure,
fresh/upgrade fingerprint equivalence, unchanged v1-v10 identities, exact
tables/indexes/triggers/FKs/constraints, representative one-game and
doubleheader relational evidence, multiple bookmaker/market/outcome and PIT
revision retention, multiple weather-provider/revision retention, warnings,
null-safe no-point and pointed revision identity, exact semantic artifact path,
positive artifact byte count, canonical JSON checksum identity, nullable-key
classification, artifact immutability, zero-game sealing, acquisition-failure distinction,
immutability, `integrity_check=ok`, zero foreign-key violations, and zero
application/provider network calls.

The controller must remain registered only for DailySlate, GameState, and BIA,
and must continue to block safely at pending `ODDS_WEATHER`.
