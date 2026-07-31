# Odds + Weather V1 Design

**Status:** foundation reconciliation on `rc/odds-weather-foundation-reconciliation-20260731`
**Controller phase:** `ODDS_WEATHER` (phase 4 of 15)
**Upstream:** accepted production `DailySlateV1`, `GameStateV1`, and `BaseballIntelligenceAssemblyV1` through private commit `e03780fa00df5af0a1e093d6a34e5f1dd5eb146f`
**Downstream:** `DATA_QUALITY` → `MATCHUP_PACKET`

## 1. Purpose

`OddsWeatherV1` answers:

> For every canonical DailySlate/Baseball Intelligence game, what market evidence and game-time weather evidence were known as of this phase execution, with exact canonical game linkage, freshness, provider lineage, stadium context, and explicit missingness?

Phase 4 is an enrichment/assembly layer around already-hardened provider collectors and processors. It does not reimplement the frozen odds or weather math.

## 2. Reuse instead of rewrite

The existing code remains authoritative for provider-specific behavior:

### Odds

- `OddsCollector` — The Odds API request + structural validation
- `process_game` — normalization, bookmaker freshness, implied probability, no-vig probability, hold, consensus, primary/alternate spread/total lines, disagreement, best prices
- `calculate_line_movement` — retained first-observed/latest-observed movement traceability
- frozen processor contracts:
  - `odds-consensus-v2`
  - `odds-v3-provider-snapshots`

### Weather

- `NwsWeatherCollector` — NWS primary source
- `OpenWeatherCollector` — OpenWeather One Call 3.0 secondary source
- `compare` — bounded cross-provider comparison
- `wind_impact` — field-relative wind only when outfield bearing is explicitly verified
- stadium catalog policy/verification logic in `app.stadiums`

Phase 4 wraps those outputs in canonical Daily Edge contracts and lineage.

The historical public Phase 4 PR #13 and branch
`rc/odds-weather-v1-direct-20260727` remain fixture-foundation evidence only.
They are not the base for the integrated reconciliation checkpoint.

## 3. Pipeline boundary

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

Phase 4 must not own:

- model features
- predictions
- model probabilities
- edge/value/EV
- recommendation decisions
- Recommendation Gate
- ranking
- reports/publishing

Phase 4 may calculate only market-normalization values already owned by the frozen odds processor and weather context already owned by the frozen weather processor.

## 4. Why DailySlate is an explicit phase-4 input

`BaseballIntelligenceAssemblyV1` currently preserves canonical game/team/venue identity but does not duplicate DailySlate scheduled start time.

Odds-event matching and game-time weather validation require the authoritative scheduled start time.

Therefore V1 accepts both:

1. the exact sealed `DailySlateV1`
2. the exact `BaseballIntelligenceAssemblyV1` whose `upstream_daily_slate_checksum` equals that DailySlate checksum

This is not a provider reach-around. DailySlate is a prior canonical contract and BIA must prove it references the same snapshot.

A future BIA contract revision may embed the required start-time field and allow phase 4 to consume only BIA.

Phase 4 also requires each BIA game to retain the exact DailySlate row checksum,
venue identity, canonical game/team identities, and ordering. Game status is
GameState-derived and may legitimately be newer than DailySlate status; Phase 4
binds it through the exact upstream BIA game checksum rather than incorrectly
requiring cross-time status equality.

## 5. Odds provider event matching

The Odds API event ID is not MLB `gamePk` and must never become the Daily Edge canonical game identity.

Matching requires:

- exact canonical home team
- exact canonical away team
- provider `sport_key = baseball_mlb`
- timezone-aware provider commence time
- commence time within a configured maximum offset from the DailySlate scheduled start

Default fixture-first maximum offset: **180 minutes**.

### Assignment rule

Each valid provider odds event chooses its unique nearest canonical game among games with the exact team pair inside the tolerance.

- equal nearest-game distance → fail closed as ambiguous
- multiple provider events choosing one canonical game → choose the unique nearest event; equal distance → fail closed
- provider events outside the slate/tolerance → warning and exclusion
- canonical game with no matched event → explicit odds unavailable warning

This permits one available odds event in a same-day doubleheader to match only its nearest canonical game instead of being duplicated into both games.

## 6. Odds evidence contract

Fixture-first `OddsProviderEventV1` retains:

- provider event ID
- provider event payload
- retrieval timestamp
- raw capture SHA-256
- optional retained historical market rows for line movement

The selected event is processed through frozen `process_game`.

`OddsSnapshotV1` retains:

- provider event ID
- canonical match offset minutes
- provider retrieval time
- raw capture checksum
- processor contract/calculation versions
- frozen canonical odds summary
- summary checksum
- bookmaker/market/freshness inventory
- warnings already produced by the frozen odds processor

No odds warning is silently discarded.

## 7. Weather evidence contract

Weather observations are keyed to canonical MLB source game ID, not sportsbook event ID.

`WeatherForecastEvidenceV1` retains:

- canonical source game ID
- provider (`nws` or `openweather`)
- normalized forecast output from the existing collector
- retrieval timestamp
- one or more raw capture SHA-256 values

`WeatherForecastV1` validates normalized values including:

- forecast time
- forecast offset from first pitch
- temperature
- humidity
- precipitation probability
- wind speed/direction/gust
- cloud percentage
- pressure
- forecast text

Forecast time must be within 60 minutes of authoritative scheduled first pitch, matching the existing collector contract.

## 8. Stadium / roof / wind policy

Phase 4 uses the retained stadium catalog and its verification metadata.

### Fixed indoor

Weather is suppressed only when:

- current team/venue association is valid
- roof type is verified `fixed`
- operational status is `closed`

Result: `indoor_fixed_roof`.

### Open / retractable / uncertain roof

Weather collection remains relevant when verified coordinates/timezone are available.

- retractable roof with no game-specific operational status → weather remains contextual
- unverified roof type → weather remains contextual
- invalid/unverified coordinates or date-association metadata → weather unavailable

### Field-relative wind

`wind_impact` is called only with the existing verified-bearing helper. Unverified or unknown bearing remains explicitly `unknown`; no bearing is inferred.

## 9. Source priority

Weather source priority remains:

1. NWS primary
2. OpenWeather 3.0 secondary

When both are present:

- retain both
- compute existing provider comparison
- use NWS as the primary weather row for field-relative wind

When only OpenWeather is available, it becomes the primary row and the comparison remains `single_source`.

## 10. Point-in-time rules

For phase observation time `T`:

- odds retrieval time must be `<= T`
- weather retrieval time must be `<= T`
- selected provider market timestamps are evaluated by the frozen odds freshness processor
- no future provider evidence may be silently accepted
- raw capture checksums are immutable lineage inputs

Phase 4 records what was known at `T`; it does not use later market/weather revisions in an earlier snapshot.

## 11. Missingness vs failure

### Warning / unavailable

Examples:

- canonical game has no matching odds event
- canonical game has no weather forecast
- only one weather provider succeeded
- stadium coordinates unavailable/unverified
- odds processor emits stale/insufficient-bookmaker/incomplete-market warning
- extra odds provider event does not match this DailySlate
- extra weather evidence references no DailySlate game

### Hard failure

Examples:

- DailySlate and BIA requested dates differ
- BIA does not reference supplied DailySlate checksum
- DailySlate/BIA canonical game order or identity differs
- equal-distance odds event match is ambiguous
- one canonical game has equal-distance competing provider events
- selected provider evidence retrieval time exceeds phase observation time
- normalized weather forecast violates the ±60 minute collector contract
- raw checksum malformed
- credential-bearing material enters a canonical phase-4 artifact

Missing odds/weather does not itself fabricate a failed controller phase. Phase 5 `DATA_QUALITY` decides whether the assembled evidence is sufficient for downstream modeling/recommendations.

## 12. Zero-game behavior

Zero-game DailySlate + zero-game BIA produces a valid zero-game OddsWeather snapshot.

Fixture-first assembly performs no network requests.

Production phase 4 should perform zero odds/weather provider requests for a legitimate zero-game slate.

## 13. Artifact

Content-addressed path:

`odds_weather/snapshots/<snapshot_checksum>/odds_weather_v1.json`

Artifact bytes must equal canonical contract JSON bytes exactly. Publication
uses the shared short same-directory atomic-create primitive, preserves
content-addressed immutability, and verifies exact idempotent replay. Missing,
tampered, traversing, or unsafe-linked artifacts fail closed.

## 14. Credential boundary

The Odds API uses the field name `key` for two required semantic identities:

- `event.bookmakers[*].key`
- `event.bookmakers[*].markets[*].key`

Generic redaction intentionally treats `key` as sensitive. Phase 4 therefore
uses exact path-aware validation rather than a field-name-wide exception. Only
those two paths may preserve `key`, and only after the enclosing event,
bookmaker, market, outcome, team, and timestamp structure validates. A `key`
at any other path remains credential-bearing and fails the canonical contract.

All other sensitive names—including API keys, tokens, authorization/bearer
fields, passwords, client secrets, credentials, signatures, and service auth
tokens—fail at every depth and capitalization. Request URLs, query/parameter
objects, headers, and quota metadata are transport evidence and cannot enter a
canonical provider event or normalized forecast.

Configured secret values are non-persisted constructor/adapter inputs. They are
checked across event strings, bookmaker/market/outcome content, history rows,
normalized forecasts, and collector warnings. They do not participate in
contract equality, `as_dict()`, canonical bytes, or checksums.

Raw Odds capture sanitization uses the same exact two path patterns with a
leading event-list position. Unknown sensitive paths are redacted in retained
raw bytes rather than preserved globally. Generic redaction behavior outside
this provider boundary remains strict.

## 15. Persistence and production wiring

The upstream temporal chain is now available through schema v10, but formal
Phase 4 persistence remains deliberately deferred to the next checkpoint.
Schema v10 and every historical migration identity remain unchanged here.

Remaining order:

1. schema-v11 temporal persistence design and migration
2. Odds + Weather repository and retained-evidence selectors
3. production phase-4 handler
4. controller registration
5. proof of safe block at `DATA_QUALITY`

The production handler will call the existing live provider collectors, retain raw captures first, convert those retained results to `OddsProviderEventV1` / `WeatherForecastEvidenceV1`, then assemble the canonical phase-4 snapshot.

## 16. OW1-A — foundation reconciliation

1. canonical contracts
2. odds-event matcher
3. frozen odds processor adapter
4. weather forecast contracts
5. stadium-context adapter
6. NWS/OpenWeather source selection + comparison
7. field-relative wind integration
8. deterministic warnings/missingness
9. deterministic artifact
10. fixture-first tests
11. path-aware credential validation
12. public CI validation

## 17. OW1-B — remaining production work

1. schema-v11 migration specification and implementation
2. DB-backed retained odds/weather selectors and repository
3. production `ODDS_WEATHER` handler
4. Phase 4 controller registration
5. zero-game provider-call proof through the production handler
6. manual controller proof advancing only to `DATA_QUALITY`
