# MatchupPacket V1 Design

## Historical and failed-attempt integrity

Snapshot reconstruction is anchored to the exact stored Data Quality and
Phase 1–4 snapshot IDs/checksums. It does not traverse a latest Data Quality
snapshot. An `assembly_failed` manifest is built from already-resolved context
and upstream identities and never invokes packet assembly a second time.
Attempt manifests enforce the exact Matchup Packet profile and upstream order;
artifact and manifest reads reject configured secrets and linked/substituted
files.

**Status:** production persisted in schema v12
**Controller phase:** `MATCHUP_PACKET` (phase 6 of 15)  
**Upstream:** `DailySlateV1` → `GameStateV1` → `BaseballIntelligenceAssemblyV1` → `OddsWeatherV1` → `DataQualityV1`  
**Downstream:** `MODEL_FEATURE_SET`

## Purpose

`MatchupPacketV1` answers:

> What exact, canonical, point-in-time evidence package belongs to each Daily MLB game after phases 1–5 have completed?

MatchupPacket is a **lossless packaging and lineage boundary**. It is not another calculation engine.

It packages the already-canonical per-game evidence from:

1. schedule / canonical game identity
2. mutable GameState
3. Baseball Intelligence V3 assembly
4. odds + weather
5. Data Quality disposition/issues

The packet is the object downstream feature construction may consume. `ModelFeatureSetV1` must not reach around MatchupPacket to provider JSON or mutable source tables.

## Non-negotiable pipeline boundary

```text
RAW
  ↓
CANONICAL
  ↓
FEATURE
  ↓
MATCHUP PACKET       ← phase 6
  ↓
MODEL FEATURE SET    ← phase 7
  ↓
PREDICTION
```

MatchupPacket does **not**:

- acquire provider data
- normalize provider payloads
- calculate baseball statistics
- calculate odds/no-vig/line movement
- calculate weather effects
- recompute Data Quality
- impute missing evidence
- scale/encode/vectorize model features
- make predictions
- calculate value/EV
- recommend BET / LEAN / PASS / AVOID
- rank, report, publish, or place bets

## Every upstream game remains represented

Data Quality has three dispositions:

- `READY`
- `DEGRADED`
- `INSUFFICIENT`

All three receive a MatchupPacket game row.

`INSUFFICIENT` is **not** a deletion instruction. The later model/prediction/recommendation policies may decide how an insufficient packet is handled, but the canonical evidence, eventual prediction lineage, settlement, and learning/audit trail must remain representable.

This preserves the Daily MLB invariant that predictions are not silently erased because a recommendation or quality gate later says PASS/AVOID/INSUFFICIENT.

## Canonical contract

### `MatchupPacketV1`

Required concepts:

- contract version `DSE_MATCHUP_PACKET_V1`
- MLB sport/league
- requested date
- point-in-time `as_of_time`
- packet `observed_at`
- exact upstream snapshot checksums for phases 1–5
- immutable ordered game packets
- deterministic checksum

### `MatchupPacketGameV1`

Each game contains the **exact canonical upstream row objects**:

- `DailySlateGameV1`
- `GameStateGameV1`
- `BaseballIntelligenceGameV1`
- `OddsWeatherGameV1`
- `DataQualityGameV1`

The packet does not flatten those objects into a competing parallel schema. Their canonical `as_dict()` representations are embedded directly into the packet artifact.

The packet also exposes canonical game identity and Data Quality disposition as derived properties for convenient downstream routing.

## Snapshot lineage validation

Before assembly, require exact equality across all five upstream snapshots for:

- `requested_date`
- `as_of_time`
- MLB sport/league
- game ordering and game set

Require the existing top-level lineage chain to be exact:

```text
GameState.upstream_daily_slate_checksum == DailySlate.checksum

BIA.upstream_daily_slate_checksum == DailySlate.checksum
BIA.upstream_game_state_checksum == GameState.checksum

OddsWeather.upstream_daily_slate_checksum == DailySlate.checksum
OddsWeather.upstream_baseball_intelligence_checksum == BIA.checksum

DataQuality.upstream_daily_slate_checksum == DailySlate.checksum
DataQuality.upstream_game_state_checksum == GameState.checksum
DataQuality.upstream_baseball_intelligence_checksum == BIA.checksum
DataQuality.upstream_odds_weather_checksum == OddsWeather.checksum
```

## Per-game lineage validation

For each ordinal, all phase rows must agree exactly on:

- `edge_event_id`
- `daily_mlb_game_id`
- authoritative MLB `source_game_id`
- away canonical team
- home canonical team

Also require all existing row-level checksum links:

```text
BIA.upstream_daily_slate_game_checksum == DailySlateGame.checksum
BIA.upstream_game_state_game_checksum == GameStateGame.checksum

OddsWeather.upstream_daily_slate_game_checksum == DailySlateGame.checksum
OddsWeather.upstream_baseball_intelligence_game_checksum == BIAGame.checksum

DataQuality.upstream_daily_slate_game_checksum == DailySlateGame.checksum
DataQuality.upstream_game_state_game_checksum == GameStateGame.checksum
DataQuality.upstream_baseball_intelligence_game_checksum == BIAGame.checksum
DataQuality.upstream_odds_weather_game_checksum == OddsWeatherGame.checksum
```

Data Quality scheduled first-pitch identity must agree with DailySlate. OddsWeather scheduled first pitch must also agree with DailySlate wherever the current upstream contract supplies one.

A row from a different observation/run therefore cannot be mixed into a packet merely because the teams happen to match.

## Quality preservation

MatchupPacket copies the canonical `DataQualityGameV1` object unchanged.

It does not:

- add/remove/reclassify quality issues
- change issue severity
- upgrade `DEGRADED` to `READY`
- downgrade/upgrade `INSUFFICIENT`
- calculate a second quality score

Downstream code may read `packet_game.quality_disposition`, but that property is derived directly from `packet_game.data_quality.disposition`.

## Point-in-time behavior

The packet `as_of_time` is the common upstream cutoff.

The packet `observed_at` must be timezone-aware and may not precede any of the five upstream snapshot observation times. The fixture-first assembler defaults it to the latest upstream `observed_at`.

No provider/network requests occur in phase 6.

## Zero-game behavior

A legitimate zero-game chain across phases 1–5 produces a legitimate zero-game MatchupPacket.

Requirements:

- no provider calls
- zero packet games
- exact upstream snapshot checksums retained
- deterministic canonical bytes/checksum

## Artifact

Content-addressed path:

```text
matchup_packet/snapshots/<packet_checksum>/matchup_packet_v1.json
```

Artifact bytes must equal `MatchupPacketV1.canonical_json_bytes()` exactly.

## Production persistence

Schema v12 owns `matchup_packet_attempt_evidence`,
`matchup_packet_snapshots`, and `matchup_packet_games`. The repository reloads
and verifies the exact sealed Phase 1–5 chain, reassembles the packet, writes an
immutable attempt manifest and content-addressed artifact, inserts ordered game
rows, reconstructs before the one-time seal, and verifies again after reopening
SQLite.

The input contract `DSE_MATCHUP_PACKET_PHASE_INPUT_V1` binds requested date,
as-of time, fixed packet observation time, assembly policy
`DSE_MATCHUP_PACKET_ASSEMBLY_POLICY_V1`, and every Phase 1–5 snapshot ID and
checksum. Exact replay is idempotent and conflicting replay fails closed.

## Historical fixture checkpoint

Implement now:

1. canonical packet/game contracts
2. strict phase 1–5 snapshot lineage validation
3. strict per-game identity + row-checksum validation
4. READY/DEGRADED/INSUFFICIENT preservation
5. zero-game behavior
6. deterministic canonical JSON/checksums
7. content-addressed artifact
8. fixture-first focused tests
9. sanitized public CI validation

## MP1-B — after upstream persistence chain is complete

Defer until Codex/local integration:

1. formal temporal MatchupPacket persistence migration/repository
2. load exact sealed phase 1–5 snapshots for one controller run/attempt
3. register production `MATCHUP_PACKET` phase handler
4. persist packet before phase success
5. controller advances to `MODEL_FEATURE_SET`
6. if phase 7 handler is unavailable, stop safely without fabricating downstream success

Do not redesign the packet during MP1-B. Persistence should store/reconstruct the frozen canonical MP1-A contract exactly.
