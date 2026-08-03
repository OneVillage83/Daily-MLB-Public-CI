# ModelFeatureSet V1 Design

**Status:** production persisted in schema v12
**Controller phase:** `MODEL_FEATURE_SET` (phase 7 of 15)  
**Upstream:** `MatchupPacketV1`  
**Downstream:** `PREDICTIONS`

## Purpose

`ModelFeatureSetV1` answers:

> What deterministic, versioned, point-in-time numeric inputs may the initial Daily MLB prediction models consume for each canonical game?

This is the first layer allowed to transform the lossless `MatchupPacketV1` into a model-oriented representation.

It is not a prediction engine.

## Hard boundary

```text
MatchupPacketV1
      ↓
ModelFeatureSetV1
      ↓
PREDICTIONS
```

ModelFeatureSet may:

- select explicitly approved fields from MatchupPacket
- aggregate already-retained player features into lineup/bullpen features
- calculate deterministic rates from retained canonical counts
- encode bounded categorical state as fixed numeric indicators
- represent unavailable evidence as explicit null/missing values
- preserve feature schema/version/checksum lineage

ModelFeatureSet may not:

- call providers
- query raw provider JSON
- mutate MatchupPacket
- invent league-average defaults
- silently replace missing values with zero
- train a model
- choose a model
- make a probability prediction
- calculate value/EV
- recommend BET/LEAN/PASS/AVOID
- rank/report/publish/place bets

## Frozen candidate schema

V1 uses one ordered feature-name tuple:

`DSE_MODEL_FEATURE_SCHEMA_V1`

Current candidate constants:

- feature count: **613**
- schema checksum: `726edd4dddcd89ed93f15b7e1152810ae22cce34bd4af9be9fe07976b00c78a1`

The module recomputes both width and checksum at import and fails immediately if they differ from the frozen constants. Changing feature names, ordering, aggregation semantics, categorical encoding, or missingness semantics after freeze requires a new schema version.

Every game has exactly one value slot per schema feature. Each slot is either:

- a finite numeric value; or
- `null` because the required upstream evidence is unavailable.

`missing_feature_names` is derived exactly from null positions.

## Canonical game identity

Each `ModelFeatureGameV1` must carry the same authoritative MLB identity already frozen upstream:

- positive decimal `source_game_id` / MLB gamePk
- `edge_event_id == edge:mlb:<gamePk>`
- `daily_mlb_game_id == game:mlb:<gamePk>`
- canonical away/home team IDs

Phase 7 does not invent or rematch game identities.

## Predictive and market-context isolation

Sportsbook prices, implied probabilities, no-vig probabilities, consensus probabilities, and line movement are **not included in the V1 predictive vector**.

Factual market context is retained in a separate canonical mapping and separate
checksum. `market_reference_checksum` must equal that market-context checksum.
It is never part of the ordered 613-slot predictive vector. Tests reject
bookmaker names/prices, American or decimal odds, implied/consensus market
probabilities, best prices, and line-movement expectations in predictive
feature names.

Schema v12 persists predictive game JSON/checksums separately from
`model_feature_set_market_contexts` and exact selected V3 lineage in
`model_feature_set_source_features`. Baseball Intelligence is authoritative for
the snapshot IDs: the repository validates each referenced retained V3 row,
checksum, completeness state, requested date, and PIT creation boundary. It
never substitutes a newer global feature row.

This preserves:

> Market probabilities are comparison baselines, not model predictions.

A future market-informed calibration/meta-model requires a new explicit feature schema/version and separate evaluation.

## V1 feature families

### Schedule/context

- game number
- doubleheader status one-hot: single / doubleheader / unknown

### GameState categorical state

For away and home teams:

- starter certainty one-hot: unavailable / probable / announced / confirmed
- lineup availability one-hot: unavailable / partial / posted

### Baseball Intelligence coverage

For away and home teams:

- gameday player count
- resolved player count
- player feature count
- lineup player/feature counts
- bullpen player/feature counts
- bench player/feature counts
- starter feature availability

These values describe represented evidence. They do not upgrade Data Quality.

### Starting pitcher — current form and pitch shape

For season-to-date and rolling 7/14/30-day windows when available:

Pitching:

- ERA
- WHIP
- K rate
- BB rate
- K-BB rate
- K/9
- BB/9
- HR/9

Pitch traits:

- strike rate
- whiff rate
- chase rate
- contact rate

Pitch physics:

- effective speed
- release position X/Z
- arm angle
- arm-side break
- vertical break with gravity

### Starting pitcher — workload/fatigue

- days since previous appearance
- days since previous start
- previous 1/3/7-day appearance counts
- previous 1/3/7-day start counts
- previous 1/3/7-day relief appearance counts
- previous 1/3/7-day pitch counts

### Starting pitcher — recent-start history

From frozen V3 `previous_start` and `previous_three_starts` evidence:

Previous start:

- pitches
- batters faced
- hits
- home runs
- walks
- strikeouts
- outs recorded

Previous three starts:

- retained start count
- sum and mean for each field above

When V3 explicitly marks pitcher appearance history unavailable, these fields remain null rather than being fabricated from another source.

### Expected lineup offense

For GameState batting-order players with retained V3 features, for season-to-date and rolling 7/14/30-day windows:

- available player count
- PA
- AVG
- OBP
- SLG
- OPS
- ISO
- BABIP
- K rate
- BB rate

Rates are recalculated from summed canonical player counts rather than averaging player rates blindly.

### Expected lineup contact / Statcast

For season-to-date and rolling 7/14/30-day windows:

- batted-ball player count
- batted-ball observation count
- exit velocity
- max exit velocity
- hard-hit rate
- barrel rate
- launch angle
- xBA
- xSLG
- xwOBA
- swing-metric player count
- bat speed
- swing length
- attack angle
- attack direction
- swing-path tilt
- miss distance
- hyper speed

#### Batted-ball aggregation precision

Frozen V3 currently exposes one `batted_ball_count` per player/window, but not metric-specific sample counts for every batted-ball mean.

Therefore V1 intentionally uses:

- summed `batted_ball_count`
- maximum of player `max_exit_velocity`
- `batted_ball_count`-weighted player V3 means for exit velocity, hard-hit rate, barrel rate, launch angle, xBA, xSLG, and xwOBA

These weighted player aggregates are deterministic and auditable, but they are **not represented as exact event-level recomputations**. If future Baseball Intelligence retains metric-specific batted-ball numerators/sample counts, a later ModelFeatureSet schema may improve these aggregations explicitly.

Swing metrics are stronger: V3 retains metric-specific `*_samples`, so each swing mean uses its corresponding retained sample count.

### Bullpen performance

For GameState bullpen player IDs with retained V3 features, for season-to-date and rolling 7/14/30-day windows:

- available player count
- batters faced
- ERA
- WHIP
- K rate
- BB rate
- K-BB rate
- K/9
- BB/9
- HR/9

Rates are recalculated from summed canonical pitching counts.

### Bullpen workload/fatigue

Only bullpen players whose retained V3 `pitcher_workload.source` is not `unavailable` contribute workload evidence.

Per team:

- available workload-history player count
- minimum/mean days since previous appearance
- minimum/mean days since previous start
- previous 1/3/7-day summed appearances
- previous 1/3/7-day summed starts
- previous 1/3/7-day summed relief appearances
- previous 1/3/7-day summed pitch counts

If no bullpen workload histories are available, available-player count is zero and the fatigue numerics remain null. Missing history is not interpreted as rest.

### Weather / venue context

When game-relevant weather is available:

- temperature
- humidity
- precipitation probability
- wind speed
- wind gust
- cloud cover
- pressure
- outfield wind component
- crosswind component

Fixed one-hot indicators also capture:

- weather status
- weather relevance
- roof type

For verified fixed-roof indoor games, outdoor weather numerics remain missing because phase 4 explicitly suppresses them.

## Quality behavior

`DataQualityDisposition` is retained as game metadata, not injected into the V1 predictive vector.

Exact Data Quality issue codes are also retained as metadata. This prevents the model from treating the quality gate itself as a shortcut while preserving auditability.

READY, DEGRADED, and INSUFFICIENT games all receive a ModelFeatureSet row.

## Missingness

Missing evidence stays missing.

Examples:

- starter unavailable → starter numeric features remain null
- 7-day sample absent → rolling-7 values remain null
- unresolved lineup player → aggregation uses only available canonical players and exposes available-player counts
- pitcher history explicitly unavailable → recent-start/workload fields remain null
- bullpen history missing → no false “rested” value
- weather unavailable → weather numerics remain null
- fixed indoor roof → outdoor weather numerics remain null by design

V1 does not substitute:

- zero for unknown evidence
- league average
- season average for missing rolling windows
- sportsbook probabilities
- fabricated starter/lineup/workload values

Explicit observed counts such as zero available workload-history players are legitimate numeric evidence and are distinct from a missing fatigue measurement.

Model-specific imputation/preprocessing, if later required, must be versioned with the model/training artifact and never mutate this canonical feature set.

## Determinism

The same MatchupPacket checksum and the same feature schema version must produce byte-identical ModelFeatureSet output.

Top-level lineage retains:

- upstream MatchupPacket checksum
- schema version
- schema checksum
- ordered feature names
- ordered game rows

Each game retains:

- exact upstream MatchupPacket game checksum
- canonical game identity
- Data Quality metadata
- optional non-predictive market reference checksum

## Zero-game behavior

A valid zero-game MatchupPacket produces a valid zero-game ModelFeatureSet with:

- zero game feature rows
- full schema metadata
- exact upstream MatchupPacket checksum
- deterministic checksum/artifact
- no provider/network calls

## Artifact

Canonical path:

```text
model_feature_set/snapshots/<feature_set_checksum>/model_feature_set_v1.json
```

## Intentionally excluded until canonical upstream evidence exists

V1 does not invent features for information not yet represented by explicit retained upstream contracts. Examples include:

- park-factor features beyond current venue/weather context
- travel/rest schedule engineering beyond retained pitcher workload
- injury severity scores
- handedness-specific matchup aggregates not yet assembled into the packet
- arbitrary external rankings

Those may enter a future schema only after the relevant acquisition/canonical contracts exist and PIT lineage can be proven.

## MFS1-A — safe now

Implement fixture-first:

1. frozen feature schema/name order
2. canonical contracts
3. deterministic MatchupPacket → feature transformation
4. lineup/bullpen aggregation helpers
5. starter performance, workload, recent-start extraction
6. bullpen performance + workload extraction
7. weather/context extraction
8. explicit missingness
9. market-feature exclusion tests
10. canonical identity tests
11. real frozen-V3 integration test
12. zero-game behavior
13. deterministic content-addressed artifact
14. focused public CI validation

## MFS1-B — later persistence integration

After phase 2–6 persistence is complete:

1. temporal ModelFeatureSet migration/repository
2. exact sealed MatchupPacket loading
3. production `MODEL_FEATURE_SET` handler
4. canonical artifact persistence/read-back verification
5. controller advances to `PREDICTIONS`
6. stop safely if prediction handler is not yet registered

Do not choose, train, or invoke the initial prediction models inside phase 7.
