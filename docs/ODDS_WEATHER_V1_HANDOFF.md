# Odds + Weather V1 — Pre-Persistence Handoff

**Current private branch:** `rc/odds-weather-foundation-reconciliation-20260731`
**Accepted base:** `rc/baseball-intelligence-handler-controller-20260730` at `e03780fa00df5af0a1e093d6a34e5f1dd5eb146f`
**Historical public evidence:** PR #13 / `rc/odds-weather-v1-direct-20260727` (not the integrated base)
**Controller phase:** 4 — `ODDS_WEATHER`

## Purpose

This checkpoint completes everything that is safe to build for controller phase 4 before the temporal persistence chain and production handler are available.

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

## Work intentionally left for the next checkpoints

1. schema-v11 temporal persistence design and migration
2. Odds + Weather repository and retained-evidence selectors
3. production Phase 4 handler
4. controller registration
5. proof of safe block at `DATA_QUALITY`

Do not fabricate production phase success before the sealed persistence/handler chain exists.

## Current checkpoint boundary

Schema remains v10. No Phase 4 persistence table, repository, production
handler, or controller registration is included. The controller still executes
only phases 1–3 and blocks safely at pending `ODDS_WEATHER`.

Local reconciliation validation is green:

- exact formerly failing credential-boundary tests: 8 passed in development
  and 8 passed in stats
- focused Phase 4/collector/processor/stadium/security: 254 passed
- Phase 1–3/schema/controller/Docker cross-phase regression: 347 passed
- full development suite: 1,527 passed, 8 documented environment skips
- full stats suite: 1,529 passed, 6 documented Windows symlink skips
- stats-only suite: 267 passed
- Ruff: passed
- mypy `--no-incremental`: passed across 259 source files

The new stacked public PR and exact Actions evidence are recorded after the
sanitized public branch is pushed. Application/provider network requests for
this fixture-first checkpoint remain zero.
