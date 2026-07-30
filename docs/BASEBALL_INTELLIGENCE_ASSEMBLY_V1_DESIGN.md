# Baseball Intelligence Assembly V1 Design

**Status:** BIA1-A fixture-first foundation reconciled on `rc/baseball-intelligence-foundation-reconciliation-20260729`; schema-v10 persistence is the next checkpoint.
**Controller phase:** `BASEBALL_INTELLIGENCE_ASSEMBLY` (phase 3 of 15)  
**Upstream contracts:** frozen `DailySlateV1`; sealed GameStateV1 schema-v9 persistence/handler; frozen Baseball Intelligence V3 player features
**Downstream:** `ODDS_WEATHER` → `DATA_QUALITY` → `MATCHUP_PACKET`

## 1. Purpose

`BaseballIntelligenceAssemblyV1` answers:

> For every canonical DailySlate/GameState game, which already-retained point-in-time baseball intelligence is applicable to the teams and players currently expected to participate?

This phase is an **assembly/join layer**, not a second statistics engine.

It must:

- preserve DailySlate event/game/team identity exactly
- preserve GameState starter/lineup/personnel state exactly
- select only accepted Baseball Intelligence V3 feature snapshots whose point-in-time boundary is valid for the requested game date/run
- attach those feature snapshots to the correct canonical players/teams
- measure explicit coverage/missingness
- preserve immutable input lineage/checksums
- produce deterministic baseball-only game assemblies

It must not call external providers.

## 2. Frozen pipeline boundary

The relevant controller order is:

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

Therefore Baseball Intelligence Assembly **must not contain**:

- sportsbook odds
- implied/no-vig probabilities
- market movement
- weather observations
- baseball weather effects
- data-quality pass/fail decisions for the final combined matchup
- `MatchupPacketV1`
- `ModelFeatureSetV1`
- model predictions
- value/EV
- Recommendation Gate outcomes

Those are later phases.

## 3. Relationship to Baseball Intelligence V3

Baseball Intelligence V3 remains the owner of statistical derivation.

Examples already derived upstream include:

- season-to-date player hitting/pitching windows
- rolling player windows
- contact quality
- swing metrics
- pitch physics / pitch traits
- pitcher workload / prior appearances
- prior-start information
- team aggregates where materialized
- bullpen workload where materialized

The assembly must **not recalculate** those metrics from raw Statcast or game logs.

The assembly may derive only structural metadata required to join and audit the inputs, such as:

- which feature snapshot was selected
- which GameState player it belongs to
- whether the player is starter / lineup / bench / bullpen
- feature coverage counts
- duplicate/conflict detection
- deterministic checksums

## 4. Point-in-time rule

For requested game date `D`:

- Baseball Intelligence V3 feature snapshots must use `feature_as_of = D`
- the frozen V3 feature contract itself enforces a strict source-data boundary `< D`
- feature `knowledge_cutoff` must not exceed the immutable run `as_of_time`
- selection uses a fixed, timezone-aware `selection_observed_at` chosen before
  feature candidates are filtered. Fixture calls may omit it, in which case it
  is the later of the sealed DailySlate/GameState observation times; a future
  production handler will supply it explicitly.
- feature `created_at` must not exceed `selection_observed_at`; candidates
  created later are excluded with deterministic warnings and cannot expand
  their own selection boundary.
- assembly `observed_at` is the fixed selection observation time and must not
  precede DailySlate/GameState observation or any selected feature creation.
- same-day game results must never leak into the pregame assembly

This makes the assembly a **PIT selector**, not a re-computation service.

## 5. Canonical source priority

Phase 3 inputs are accepted only from retained canonical/internal evidence:

1. `DailySlateV1` — authoritative game/team/venue identity
2. `GameStateV1` — mutable game-local starter/lineup/personnel state
3. Baseball Intelligence V3 feature snapshots — point-in-time statistical intelligence
4. future retained MLB-specific canonical context snapshots explicitly frozen for phase 3

Provider JSON must never be consumed directly by phase 3.

## 6. Proposed contract

### `BaseballIntelligenceAssemblyV1`

Required concepts:

- contract/version
- sport/league
- requested date
- `as_of_time`
- `observed_at`
- upstream DailySlate checksum
- upstream GameState checksum
- Baseball Intelligence feature version
- selected source stats run / feature inventory lineage when available
- immutable ordered game assemblies
- deterministic checksum

Game ordering must exactly equal DailySlate/GameState ordering.

### `BaseballIntelligenceGameV1`

Required concepts:

- `edge_event_id`
- `daily_mlb_game_id`
- authoritative source game ID
- away/home canonical team IDs
- venue identity inherited from DailySlate
- game status inherited from GameState
- away/home `TeamBaseballIntelligenceV1`
- deterministic row checksum

### `TeamBaseballIntelligenceV1`

Required concepts:

- canonical team ID
- authoritative source team ID
- starter intelligence
- lineup intelligence
- bullpen intelligence inventory
- bench intelligence inventory
- additional gameday pitcher/batter intelligence inventory
- explicit coverage summary

The assembly does not infer a player role that GameState did not provide.

**Team-level V3 feature decision:** deferred. The production V3 materializer
records `entity_kind='player'` snapshots only. The older aggregate-team builder
is not a `DSE_MLB_STATS_FEATURES_V3` materialization path, so BIA1-A must not
advertise or fabricate team V3 intelligence. Team candidates remain a distinct
entity kind and are never joined to players; a future version needs an explicit
versioned team materializer and selector contract.

### `PlayerIntelligenceV1`

Required concepts:

- authoritative MLB source player ID
- canonical player ID when verified
- player identity ID when verified
- full name from GameState
- role(s) supplied by GameState
- selected V3 player feature snapshot reference
- selected V3 feature checksum
- canonical V3 feature payload when available
- explicit availability state

No name-only feature matching.

## 7. Feature-selection identity rule

Preferred join:

```text
GameState canonical_player_id
        ↓ exact equality
stats_feature_snapshots.canonical_player_id
```

The feature snapshot must also satisfy:

```text
entity_kind = player
feature_version = DSE_MLB_STATS_FEATURES_V3
feature_as_of calendar date = requested_date
```

If GameState has only an MLB source player ID but no verified canonical mapping, phase 3 must not guess a feature match by name.

Result: unavailable player intelligence + warning.

## 8. Multiple matching feature snapshots

Multiple rows may exist because materialization reruns may reuse identical outputs.

Selection rules:

1. all accepted candidates for one canonical player/date/version must agree on `feature_checksum`
2. if checksums disagree, fail closed as conflicting retained feature evidence
3. if checksums agree, choose one deterministic representative and retain the
   sorted equivalent feature-snapshot IDs and stats-run IDs with that player
   intelligence record
4. never silently select a newer/different payload merely because it was inserted later

## 9. Coverage policy

Coverage is explicit, never fabricated.

Per team/game track at minimum:

- starter feature available / unavailable
- posted lineup players with feature coverage
- posted lineup players without feature coverage
- bullpen players with/without feature coverage
- bench players with/without feature coverage
- all gameday personnel with/without feature coverage

### Warning examples

- starter is probable/confirmed but lacks verified canonical player ID
- verified starter has no accepted V3 feature snapshot
- posted lineup has one or more players without V3 feature snapshots
- bullpen player feature coverage is incomplete
- blocked feature evidence excluded from usable selection
- feature evidence created after the fixed selection boundary excluded

### Hard failure examples

- DailySlate and GameState game sets disagree
- GameState upstream DailySlate checksum does not match selected DailySlate
- game/team identity disagreement
- V3 feature snapshot date violates requested date
- V3 feature knowledge cutoff exceeds assembly `as_of_time`
- same canonical player/date/version has conflicting retained feature checksums
- selected feature payload checksum does not match stored feature checksum
- feature payload claims a different canonical player/date/version
- credential-bearing material enters the assembly

Initial V1 should prefer `SUCCEEDED_WITH_WARNINGS` for missing optional intelligence and fail closed for contradictory identity/PIT evidence.

## 10. Zero-game behavior

A legitimate zero-game DailySlate + zero-game GameState produces a legitimate zero-game Baseball Intelligence Assembly.

It must perform zero feature-provider requests. Database feature lookup may be skipped entirely.

## 11. Park/rest/travel/injury scope

The long-term Daily MLB architecture includes park factors, rest/travel/context, injuries/transactions, and bullpen intelligence.

For BIA V1:

- **bullpen workload already represented by frozen Baseball Intelligence feature logic may be attached**
- **starter workload already represented by V3 player features may be attached**
- park/rest/travel/injury context may enter only through explicit retained canonical component contracts
- phase 3 must not scrape/infer those ad hoc
- absent components are represented as unavailable with lineage/warnings, not invented defaults

This allows the assembly contract to grow without breaking the RAW → CANONICAL → FEATURE discipline.

## 12. Artifact

Content-addressed path:

`baseball_intelligence/snapshots/<assembly_checksum>/baseball_intelligence_assembly_v1.json`

Artifact bytes must exactly equal canonical assembly JSON bytes.

## 13. Persistence

Formal temporal persistence is intentionally deferred until the BIA1-A contract
is persisted in schema v10. GameState schema v9 persistence and its production
handler are accepted upstream.

Expected later migration (likely schema v10 after GameState v9):

- `baseball_intelligence_snapshots`
- `baseball_intelligence_games`

The persisted assembly must reference the exact sealed GameState snapshot and selected upstream feature inventory.

No production phase-3 handler is registered until:

1. a formal BIA schema-v10 migration/repository exists
2. exact selected V3 feature lineage is persisted
3. a production phase-3 handler is registered and verifies sealed upstream artifacts

## 14. BIA1-A — reconciled fixture-first foundation

1. freeze assembly boundaries
2. implement canonical contract
3. implement deterministic artifact writer
4. implement fixture-first in-memory assembler from `DailySlateV1` + `GameStateV1` + supplied V3 feature snapshot inventory
5. implement strict PIT/identity/checksum validation
6. implement coverage/warning calculation
7. add zero-network focused tests
8. validate Ruff/mypy/full tests on isolated branch

## 15. BIA1-B — schema-v10 persistence and handler

1. implement feature-store selector against retained `stats_feature_snapshots`
2. add formal assembly persistence migration/repository
3. require exact sealed GameState snapshot lineage
4. register production `BASEBALL_INTELLIGENCE_ASSEMBLY` handler
5. prove controller advances to `ODDS_WEATHER`
6. stop safely if phase 4 handler is unavailable

Do not fabricate downstream phase success.
