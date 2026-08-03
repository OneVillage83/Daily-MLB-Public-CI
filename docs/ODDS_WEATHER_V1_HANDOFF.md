# Odds + Weather V1 — Production Phase Handoff

**Current private branch:** `rc/phase4-production-acceleration-20260801`
**Accepted base:** `rc/odds-weather-repository-selectors-20260731` at `13507df70823c428f62ef91b2189731b74cd238c`
**Historical public evidence:** PR #13 / `rc/odds-weather-v1-direct-20260727` (not the integrated base)
**Controller phase:** 4 — `ODDS_WEATHER`

## Purpose

The durable Phase 4 repository and selectors are accepted. This sprint adds the
production `OddsWeatherPhaseHandler`, registers Phase 4 in the Manual Run
Controller, and advances the safe execution boundary to pending `DATA_QUALITY`.
It also establishes the repository-wide persistence and validation protocols in
`PIPELINE_PERSISTENCE_STANDARD.md` and `VALIDATION_TIERS.md`.

The phase boundary remains:

```text
DAILY_SLATE
    ↓
GAME_STATE
    ↓
BASEBALL_INTELLIGENCE_ASSEMBLY
    ↓
ODDS_WEATHER
    ↓
DATA_QUALITY
    ↓
MATCHUP_PACKET
    ↓
MODEL_FEATURE_SET
    ↓
PREDICTIONS
```

Phase 4 owns market and environmental evidence assembly. It does **not** own model predictions, EV/value, Recommendation Gate decisions, rankings, reports, or publishing.

## Implemented files

Core:

- `app/odds_weather/contracts.py`
- `app/odds_weather/assembly.py`
- `app/odds_weather/adapters.py`
- `app/odds_weather/history.py`
- `app/odds_weather/artifact.py`
- `app/odds_weather/attempt_manifest.py`
- `app/odds_weather/selector.py`
- `app/odds_weather/repository.py`
- `app/odds_weather/__init__.py`

Design/reference:

- `docs/ODDS_WEATHER_V1_DESIGN.md`
- `docs/ODDS_WEATHER_V1_HANDOFF.md`

Focused tests:

- `tests/test_odds_weather_assembly.py`
- `tests/test_odds_weather_adapters.py`
- `tests/test_odds_weather_contract_edges.py`
- `tests/test_odds_weather_artifact.py`
- `tests/test_odds_weather_history.py`
- `tests/test_odds_weather_history_adapter.py`

## Existing frozen logic reused

Phase 4 deliberately does not rewrite the validated Phase 1 provider math.

### Odds

Existing collector:

- `app/collectors/odds_collector.py`
- The Odds API
- collector contract/version already validated upstream
- malformed events/books/markets/outcomes are excluded with structured warnings
- raw payload capture is sanitized and checksum-addressable

Existing processor:

- `app/processors/odds_processor.py`
- `ODDS_CONSENSUS_CONTRACT_VERSION = odds-consensus-v2`
- `CALCULATION_VERSION = odds-v3-provider-snapshots`

Reused outputs include:

- moneyline / h2h
- spreads
- totals
- alternate lines
- per-book offers
- best available prices
- implied probabilities
- no-vig probabilities
- hold / overround
- consensus disagreement
- market freshness
- stale/future-skew warnings
- line movement

### Weather

Existing collectors:

- `app/collectors/nws_weather_collector.py` — primary
- `app/collectors/openweather_collector.py` — OpenWeather One Call 3.0 secondary

Existing processor:

- `app/processors/weather_processor.py`
- cross-provider comparison
- verified field-relative wind impact

Existing stadium authority:

- `app/stadiums.py`
- physical venue identity
- active club association
- coordinates/timezone
- roof type and verification
- operational roof state
- verified outfield bearing
- historical/current venue aliases

## Canonical contracts

### `OddsProviderEventV1`

Retained provider event evidence containing:

- sportsbook provider event ID
- retrieval timestamp
- sanitized raw-capture checksum
- immutable validated provider event payload
- optional point-in-time line-history rows

The provider event ID is **never** treated as an MLB game ID.

The prior contract passed the complete event through generic redaction. Because
generic redaction correctly treats every field named `key` as sensitive, it
also rejected the valid Odds API bookmaker and market identifiers. The
reconciled boundary preserves `key` only at the two closed, structurally
validated paths `bookmakers[*].key` and
`bookmakers[*].markets[*].key`. Unrelated `key` fields and every API key,
token, authorization/bearer, password, client secret, credential, signature,
or service-auth field remain rejected at every nesting depth.

Adapters additionally pass configured secret values into non-persisted
contract validation. Secret lists never enter `as_dict()`, equality, canonical
bytes, or checksums. Event/history/forecast strings and collector warnings are
checked, while raw request URLs, queries, headers, and quota metadata cannot
enter canonical Phase 4 evidence. Raw Odds capture sanitization uses the same
exact path policy rather than preserving every field named `key`.

### `WeatherForecastEvidenceV1`

Canonical retained forecast evidence containing:

- authoritative MLB `source_game_id`
- provider (`nws` or `openweather`)
- retrieval timestamp
- raw-capture checksum inventory
- normalized first-pitch forecast

Forecast evidence is keyed to the canonical MLB game, not a sportsbook event ID.

### `VenueWeatherContextV1`

Preserves:

- canonical home team
- physical venue key
- venue display name
- coordinates
- timezone
- roof type
- operational roof status
- roof verification state
- outfield bearing + verification state
- stadium metadata/catalog versions
- explicit association/coordinate errors

### `OddsSnapshotV1`

Preserves the complete frozen odds processor summary plus:

- availability
- selected sportsbook event ID
- retrieval timestamp
- raw capture checksum
- canonical-event match offset
- market/snapshot counts
- freshness counts
- frozen odds calculation/consensus versions
- deterministic summary checksum

### `WeatherSnapshotV1`

Preserves:

- availability/status
- game relevance
- venue context
- NWS evidence
- OpenWeather evidence
- selected primary source
- cross-provider comparison
- baseball wind impact

### `OddsWeatherGameV1`

Per canonical DailySlate game:

- `edge_event_id`
- `daily_mlb_game_id`
- authoritative MLB source game ID
- canonical away/home team IDs
- scheduled start time, including explicit `None` when DailySlate has no known start
- exact upstream DailySlate game checksum
- exact upstream Baseball Intelligence game checksum
- odds snapshot
- weather snapshot
- deterministic game checksum

### `OddsWeatherV1`

Top-level phase snapshot:

- requested date / as-of / observed-at
- exact DailySlate snapshot checksum
- exact Baseball Intelligence snapshot checksum
- only the raw-capture checksums actually selected into phase 4
- canonical ordered games
- deterministic structured warnings
- deterministic top-level checksum / canonical JSON

## Odds event matching

Sportsbook event identity is resolved to canonical DailySlate games by:

1. exact canonical away team
2. exact canonical home team
3. scheduled start inside a bounded tolerance
4. unique nearest scheduled start

Default tolerance:

`180 minutes`

This is specifically designed for doubleheaders.

Rules:

- one provider event nearest to one game → select it
- provider event outside tolerance → exclude + warning
- provider event equally near two DailySlate games → fail closed
- two provider events equally near one DailySlate game → fail closed
- second provider event farther from the same game → exclude deterministically + warning
- missing DailySlate start time → no event guessing; odds unavailable + explicit warning

## Point-in-time revision rules

### Odds event revisions

For one provider event ID:

- only revisions with `retrieved_at <= phase observed_at` are eligible when an explicit phase cutoff is supplied
- later revisions are ignored with a warning
- choose the latest eligible retrieval time
- contradictory payloads at the same latest retrieval time fail closed

### Weather revisions

For `(MLB source_game_id, provider)`:

- only revisions known by the phase cutoff are eligible
- later revisions are ignored with a warning
- choose latest eligible retrieval time
- contradictory same-time revisions fail closed

### Legacy odds line history

`app/odds_weather/history.py` wraps the existing legacy:

`Database.get_odds_history(provider_event_id)`

with a strict PIT selector.

`select_odds_history_at(...)`:

- requires a timezone-aware cutoff
- requires valid timezone-aware row `retrieved_at`
- rejects cross-event rows
- excludes every row retrieved after the cutoff
- sorts deterministically by retrieval time + row ID

Therefore a historical/manual replay cannot accidentally use line movement observed later in the day.

## Live collector output adapters

### `odds_collection_to_phase4(...)`

Converts the existing validated `OddsCollectionResult` into phase-4 provider events.

It:

- validates the raw capture is `the_odds_api / mlb_odds`
- uses the existing `sanitized_checksum()` as raw lineage
- converts only collector-accepted games
- converts collector structural warnings into canonical phase-4 warnings
- accepts already-selected history rows OR an `OddsHistorySource`
- when given a history source, applies the PIT history selector automatically

The eventual production handler can therefore pass the existing `Database` directly as `history_source`.

### `nws_forecast_to_phase4(...)`

Converts existing NWS outputs.

It requires both retained captures:

- `nws / point_lookup`
- `nws / hourly_forecast`

Both sanitized checksums are preserved; the evidence retrieval time is the later capture timestamp.

### `openweather_forecast_to_phase4(...)`

Converts existing OpenWeather One Call 3.0 output and retains its sanitized raw checksum.

## Weather policy

### First-pitch integrity

The forecast contract allows only provider-normalized offsets within 60 minutes.

The assembler independently recomputes:

`abs(forecast_time - authoritative DailySlate scheduled_start_time)`

and requires it to be within 60 minutes.

It also checks the provider-reported offset against the recomputed value. A fake/incorrect offset cannot make an out-of-window forecast look valid.

### NWS / OpenWeather priority

- NWS available → NWS is primary
- NWS unavailable and OpenWeather available → OpenWeather is primary
- both available → preserve both + run comparison
- one source only → still available, but explicit missing-secondary warning
- neither → weather unavailable

### Roof behavior

Verified fixed closed roof:

- outside weather is suppressed
- supplied weather evidence is ignored with an audit warning
- no weather is invented

Retractable roof:

- outside weather remains available as context
- relevance is `contextual_roof_status_unknown` when no authoritative roof-state evidence exists

Unverified roof type:

- outside weather remains contextual
- no indoor suppression is inferred

### Stadium metadata failures

Association or coordinate failures do not trigger venue guessing. Weather becomes unavailable with explicit stadium warnings.

DailySlate venue identity/name is cross-checked against the canonical stadium authority when available.

## Missingness behavior

Phase 4 assembles evidence; phase 5 `DATA_QUALITY` decides sufficiency.

Therefore:

- no matching odds → odds unavailable + warning
- no weather → weather unavailable + warning
- missing scheduled start → game remains in phase snapshot; odds/weather unavailable + warnings
- missing secondary weather source → primary source remains usable + warning
- invalid stadium metadata → weather unavailable + warning

No fabricated market line, probability, temperature, wind, roof state, or game time is inserted.

## Source warnings

`assemble_odds_weather(..., source_warnings=...)` preserves warnings produced before canonical assembly, especially structural exclusion warnings from `OddsCollector`.

This prevents rejected provider rows from disappearing from the audit trail simply because they were correctly excluded from the accepted event inventory.

## Artifact

Content-addressed path:

```text
odds_weather/
└── snapshots/
    └── <snapshot_checksum>/
        └── odds_weather_v1.json
```

Artifact bytes exactly equal `OddsWeatherV1.canonical_json_bytes()`.

Writes use the shared short same-directory atomic-create primitive. Exact replay
is idempotent, conflicting immutable bytes are never overwritten, and artifact
path traversal, unsafe links, missing bytes, checksum/byte-count mismatches, and
tampering are rejected.

## Validation surface

Historical public branch/PR #13 remains evidence for the original fixture
foundation only. The integrated reconciliation is stacked on the accepted
Phase 3 production checkpoint and will use a new public branch/PR.

Focused validation exercises:

- complete odds + dual weather
- zero-game determinism
- doubleheader nearest matching
- ambiguous doubleheader failure
- competing sportsbook event failure
- unmatched provider events
- odds revision PIT cutoff
- weather revision PIT cutoff
- stale market propagation
- single-source weather
- fixed-roof suppression
- retractable-roof contextual weather
- venue mismatch
- first-pitch forecast-time integrity
- upstream checksum mismatch
- collector warning preservation
- missing start time degradation
- live OddsCollector adapter
- live NWS adapter
- live OpenWeather adapter
- raw capture provider/endpoint validation
- PIT legacy odds history selection
- PIT history source integration through the odds adapter
- content-addressed artifact behavior
- configured secret rejection

## Repository and retained-evidence boundary

`OddsWeatherRetainedEvidenceSelector` reconstructs, in explicit ordinal order,
the exact immutable attempt inventory from schema v11. It verifies raw artifact
bytes and metadata, provider-event revisions and their bookmaker/market/outcome
children, every PIT odds-history row, every weather revision, and ordered
weather/raw links. It returns all retained evidence—including future and
excluded revisions—rather than making a new PIT decision. The frozen assembler
remains the only selection owner.

`OddsWeatherRetainedEvidenceInventoryV1` binds run ID, phase attempt, requested
date, normalized UTC `as_of_time`, normalized UTC `observed_at` cutoff, phase
input checksum, the exact three-snapshot upstream chain, ordered raw captures,
provider-event revisions, odds-history revisions, weather revisions, source
warnings, final warnings, and the selected raw-capture set. Its checksum is the
canonical SHA-256 of that complete identity projection. `as_of_time` remains
the immutable upstream run reference while `observed_at` remains the Phase 4
selection boundary; both are directly and independently hashed.

The checksum binds nested evidence transitively: raw metadata is represented by
its full canonical projection, from which its row checksum is reproduced;
provider-event identity carries the event checksum and full event-row checksum;
odds-history identity lists every bookmaker/market/outcome, point,
event/revision retrieval time, ordinal, and row checksum, with that checksum
transitively binding price and provider/bookmaker/market update times; weather
identity carries forecast and full row checksums. Payload JSON stays authoritative in the relational rows and verified
raw artifacts rather than being duplicated in the identity projection.

`OddsWeatherRepository` exposes:

- `build_inventory(...)` and `assemble_inventory(...)`
- `persist_assembly(...)` and `persist_failed_attempt(...)`
- `get_attempt_evidence(...)`, `get_attempt_manifest(...)`, and
  `list_attempt_evidence(...)`
- `load_retained_inventory(...)`, including an existing-connection selector
  boundary
- `get_by_snapshot_id(...)`, `get_for_run_attempt(...)`, and
  `get_latest_for_run(...)`

Successful persistence verifies sealed DailySlate, GameState, and BIA objects;
validates the active Phase 4 attempt; verifies every raw artifact; atomically
publishes the manifest and snapshot artifact; inserts all 14-table relational
evidence in one write transaction; reloads the inventory in that transaction;
reassembles it; verifies every snapshot/game/warning/selection/count/checksum;
and performs the one-time seal. After commit it opens a fresh normal SQLite
connection, reconstructs from relational children, verifies upstream artifacts,
manifest, snapshot artifact, selected and retained raw artifacts, then repeats
the frozen assembly offline.

Attempt manifests use contract
`DSE_ODDS_WEATHER_ATTEMPT_MANIFEST_V1` and path
`odds_weather/attempts/<run_id>/attempt_<NNNN>.json`. Atomic immutable creation
uses a flushed/fsynced short same-directory temporary file and link publication.
Exact replay verifies existing bytes; conflicting replay never overwrites them.
Rollback cleanup removes only newly created files whose checksum and byte count
still match repository ownership. Pre-existing verified files, raw provider
artifacts, and unrelated files are never removed.

The standalone manifest constructor, `from_inventory`, publisher, writer, and
verifier all accept configured secret values as nonstored validation inputs.
They scan all manifest string leaves both before publication and after parsing
retained bytes. The repository passes its configured-secret inventory through
assembled and failed construction, publication, idempotent replay, row
reconstruction, attempt reads, snapshot verification, and close/reopen
reconstruction. The inventory is never a dataclass field and never enters
equality, `as_dict()`, canonical bytes, artifact metadata, retained-inventory
checksums, SQLite, logs, or exceptions. Legitimate `bookmaker_key` and
`market_key` identities remain semantic evidence; configured secret values in
those or any other string value still fail closed.

The repository distinguishes every retained raw capture from the exact selected
raw checksum inventory stored on the sealed snapshot. Future odds/weather
revisions remain queryable and auditable but cannot enter historical replay.
Source warnings remain distinct in the attempt manifest; the canonical snapshot
and warning table retain the frozen assembler's final ordered warnings.

Failed outcomes are exactly `acquisition_failed`, `normalization_failed`, and
`assembly_failed`. They retain an immutable manifest and available retained
evidence but create no snapshot or snapshot artifact. A positively established
zero-game chain instead creates and seals a canonical zero-game snapshot with
zero child/selection rows and reconstructs exactly after database reopen.

## Production handler and controller boundary

`OddsWeatherPhaseHandler(context) -> PhaseExecutionResult` accepts only an
active `ODDS_WEATHER` attempt. It independently reloads and verifies the sealed
DailySlate, GameState, and Baseball Intelligence chain before any acquisition.
The constructor supports deterministic injection of the clock, odds collector,
NWS collector, optional OpenWeather collector, repository, stadium authority,
and acquisition planner; production defaults reuse the validated collectors.

The handler fixes one UTC observation boundary for the attempt after acquisition
timestamps are known and before PIT selection. Its
`DSE_ODDS_WEATHER_PHASE_INPUT_V1` checksum binds requested date, upstream as-of
time, the fixed observation cutoff, all three upstream checksums, accepted
snapshot/event/weather contract versions, provider policy versions and modes,
configured nonsecret odds regions/markets/format, OpenWeather enablement and
comparison mode, and stadium catalog/policy versions. Credentials, request
metadata, and secret inventories never enter the checksum.

For nonempty slates, validated odds evidence is required, NWS is primary, and
OpenWeather is comparison/fallback evidence under the accepted policy. A failed
NWS request may fall back to OpenWeather with an explicit warning; failure of
both required paths retains `acquisition_failed`. Optional comparison failure
does not discard valid NWS evidence. Adapter failures retain
`normalization_failed`; deterministic assembly or persistence/reconstruction
failures retain `assembly_failed`. Each failure keeps safely available raw and
normalized evidence in its immutable attempt manifest and creates no snapshot.
Failure-evidence errors are attached to, but never mask, the original error.

Raw publication validates the complete descriptor, timestamps, checksum, byte
count, containment-safe path, and relative path before atomically exposing final
bytes. A newly created file is removed after a post-create failure only when its
bytes, checksum, and size still prove ownership by that call; pre-existing exact,
conflicting, changed, and unrelated files are never removed. Multi-capture NWS
acquisition owns the point descriptor immediately before attempting the hourly
capture. Thus an hourly publication failure retains the Odds and NWS point raw
evidence with contiguous ordinals, records `acquisition_failed`, and cannot
orphan the point artifact from its attempt manifest.

A positively established zero-game chain skips every provider and collector,
builds an empty inventory, persists and reconstructs a sealed zero-game
snapshot, and returns success. A nonempty acquisition failure cannot use this
path.

On success the handler builds and assembles the accepted retained inventory,
persists through `OddsWeatherRepository`, reloads attempt evidence, manifest,
inventory, artifact and relational snapshot, and requires exact canonical
equivalence. No warnings returns `SUCCEEDED`; canonical warnings return
`SUCCEEDED_WITH_WARNINGS`. The handler returns both phase-input and output
checksums, the content-addressed artifact path, exact warnings, and
`continue_pipeline=True`; only the controller service changes phase state.

Production controller construction now registers exactly:

1. `DAILY_SLATE`
2. `GAME_STATE`
3. `BASEBALL_INTELLIGENCE_ASSEMBLY`
4. `ODDS_WEATHER`
5. `DATA_QUALITY`
6. `MATCHUP_PACKET`
7. `MODEL_FEATURE_SET`

After Phase 4 commits, the zero-network pre-model chain continues through
Phases 5–7. The same resume then blocks safely at unregistered pending
`PREDICTIONS`. A later resume does not rerun any completed Phase 1–7 attempt or
alter its checksums, manifest, or snapshot.

## Validation protocol

During implementation, run focused checks with:

```text
python scripts/validate_checkpoint.py task --test <path> --python-target <path>
```

At the single sprint boundary, run:

```text
python scripts/validate_checkpoint.py sprint
```

Use `--explain` to print commands and documented skips without execution. This
sprint intentionally does not run the release profile, the complete local stats
environment, dependency audits, hash-lock rehearsals, Docker, or the migration
failure-injection matrix because schema, dependencies, stats contracts, lock
files, and Docker inputs are unchanged. Public CI supplies final cross-platform,
stats-environment, security, and Docker evidence.

## Frozen schema and current checkpoint boundary

Schema v11 now formally creates the immutable Phase 4 temporal persistence
surface documented in
`ODDS_WEATHER_V1_MIGRATION_V11_SPEC.md`. Migration identity is
`odds_weather_v1_temporal_persistence`, checksum
`a54865d8b5623e96c4f571d6c9d7f899e9ced0d1911128b5a874b41e29df75dd`,
and formal fingerprint
`5b9635e1aac05d98fd61dadaf2ac5d435e4aaae9214c79501b5dd642673c75b8`.
Fresh installs and verified v10 upgrades produce the same schema; existing v10
databases receive a verified pre-v11 backup and atomic diagnostic.

External review reproduced and closed two provisional-v11 integrity gaps:

- SQLite considered `NULL` point values distinct inside the original composite
  unique constraint. Paired partial unique indexes now make unpointed revision
  identity null-safe and retain exact point as an identity dimension for
  pointed spread/total history.
- Snapshot artifact metadata previously accepted any nonblank relative path.
  SQL now requires the exact checksum-derived Phase 4 semantic path, positive
  artifact bytes, and agreement between the canonical JSON checksum field and
  `snapshot_checksum`.

The nullable audit also made `snapshot_id` explicitly `NOT NULL`, documented
the intentional null uniqueness of unavailable games' selected provider event,
and confirmed all remaining optional venue, weather, provider-update, and
warning-context fields are outside unique identities or protected by explicit
availability groups. The final surface has 14 tables, 20 explicit indexes, 10
validation/sealing triggers, 28 immutability triggers, and 138 formal schema
statements. Every v1-v10 migration identity and SQL byte remains frozen.

Schema v11 is byte-for-byte frozen. This checkpoint changes no migration,
table, index, trigger, checksum, or fingerprint. The controller executes phases
1–4 and blocks safely at pending `DATA_QUALITY`; repository and handler methods
do not transition controller state. Exact local and public validation counts are
recorded in the checkpoint completion report after the final heads are pushed.
Application/provider network requests during tests remain zero.

Repository external review subsequently found and corrected two identity-layer
gaps without changing schema v11: `as_of_time` is an explicit retained-inventory
checksum member alongside `observed_at`, and every standalone attempt-manifest
API enforces configured-secret values through validation-only arguments. Those
accepted boundaries are unchanged by the production handler.
