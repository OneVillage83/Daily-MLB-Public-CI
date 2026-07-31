# Baseball Intelligence Assembly V1 — Repository/Selector Handoff

**Historical foundation branch/PR:** `rc/baseball-intelligence-assembly-v1-direct-20260727` / private draft PR #11 (historical evidence only)
**Current repository branch:** `rc/baseball-intelligence-repository-selector-20260730`
**Base:** accepted GameState handler checkpoint `24da1c9a3912272f8b5733a5f66ac41c32c68485`
**Controller phase:** 3 — `BASEBALL_INTELLIGENCE_ASSEMBLY`

## Current checkpoint

BIA1-A is implemented fixture-first as a zero-network baseball-only assembly layer.

Implemented:

- `docs/BASEBALL_INTELLIGENCE_ASSEMBLY_V1_DESIGN.md`
- `app/baseball_intelligence/contracts.py`
- `app/baseball_intelligence/assembly.py`
- `app/baseball_intelligence/artifact.py`
- `app/baseball_intelligence/__init__.py`
- focused assembly and artifact tests
- schema-v10 immutable temporal persistence surface
- DB-backed retained-V3 candidate selector
- immutable Phase 3 attempt-manifest writer/verifier
- `BaseballIntelligenceRepository` persistence, sealing, retrieval, and offline verification

The assembly consumes only:

1. `DailySlateV1`
2. `GameStateV1`
3. retained Baseball Intelligence V3 player feature snapshots

It does not call external providers and does not consume provider JSON directly.

Schema v10 is frozen. The repository creates no tables lazily and does not
change migration identities. The controller remains registered only for
`DAILY_SLATE` and `GAME_STATE`.

## Frozen boundary carried forward

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

BIA V1 does not own:

- odds
- weather
- market movement
- final Data Quality acceptance
- MatchupPacket
- ModelFeatureSet
- predictions
- value/EV
- Recommendation Gate
- rankings
- reports/publishing

## Implemented contract behavior

### Exact upstream identity

Every assembled game preserves and verifies:

- `edge_event_id`
- `daily_mlb_game_id`
- MLB source game ID
- away/home canonical team IDs
- away/home authoritative source team IDs
- DailySlate game checksum
- GameState game checksum
- DailySlate snapshot checksum
- GameState snapshot checksum

Game ordering must match DailySlate/GameState exactly.

### Player intelligence join

Join only by verified canonical player identity:

```text
GameState canonical_player_id
        ↓ exact equality
Baseball Intelligence V3 player feature entity_id
```

No player-name matching or fuzzy identity resolution is allowed.

### Player roles

One player-intelligence record is retained per GameState source player ID. Roles are merged rather than duplicating the same feature payload:

- starter
- lineup
- bullpen
- bench
- batter
- pitcher

Lineup order and GameState personnel buckets remain separate structural indexes.

### V3 feature integrity

`BaseballFeatureSnapshotV1` reproduces the frozen V3 checksum domain exactly:

1. remove `feature_checksum` from the retained feature payload
2. JSON serialize with the same frozen V3 options
3. SHA-256 the bytes
4. require equality with the retained `feature_checksum`

It also validates:

- contract version = `DSE_MLB_STATS_FEATURES_V3`
- `feature_as_of`
- player entity identity
- knowledge cutoff format
- input/feature checksum format
- deeply immutable retained payload

### PIT selection

For assembly requested date `D`:

- historical V3 snapshots from other dates are ignored
- candidate `feature_as_of` must equal `D`
- selected feature `knowledge_cutoff` may not exceed immutable assembly `as_of_time`
- a fixed selection observation boundary is chosen before candidate filtering;
  selected feature creation time may not exceed it
- candidates created after that boundary are excluded with deterministic
  warnings; a candidate cannot expand the boundary used to select itself
- multiple same-player/date candidates must agree on `feature_checksum`
- checksum-equivalent reruns are selected deterministically
- conflicting retained feature evidence fails closed

### Coverage and warnings

Per team the assembly tracks:

- total GameState players
- resolved canonical players
- players with V3 features
- lineup feature coverage
- bullpen feature coverage
- bench feature coverage
- starter feature availability

Warnings include:

- unresolved player identity
- missing player feature
- missing starter feature
- incomplete lineup feature coverage
- incomplete bullpen feature coverage
- incomplete bench feature coverage

Missing intelligence is never replaced with fabricated defaults.

### Completeness state

The retained V3 feature snapshot permits only `complete`, `degraded`, or
`blocked`. Complete is usable. Degraded is usable but retains its original
state. Blocked is never attached as available intelligence: it is excluded,
represented through coverage/missingness, and emits deterministic warning
evidence. Unknown state strings fail at the contract boundary.

### Team V3 feature boundary

Production V3 materialization records player snapshots only. The historical
team aggregate builder is not a V3 production materializer, so team-level V3
intelligence is explicitly deferred from BIA1-A. Team entity rows cannot join
to players.

### Lineage inventory

Top-level assembly records the unique selected:

- `source_stats_run_ids`
- `source_feature_checksums`

Every selected player additionally records its deterministic representative
feature snapshot/run plus all checksum-equivalent feature snapshot IDs and
stats-run IDs. Only selected, usable player intelligence contributes to the
top-level inventory.

## Artifact

Content-addressed path:

```text
baseball_intelligence/
└── snapshots/
    └── <assembly_checksum>/
        └── baseball_intelligence_assembly_v1.json
```

Artifact bytes exactly equal canonical assembly JSON bytes.

The repository verifies this content-addressed artifact before persistence and
again on every offline read. Missing, altered, unsafe, or semantically wrong
paths fail closed.

## Candidate selector and repository

`BaseballIntelligenceFeatureSelector` is read-only. It loads every retained V3
player candidate for the exact requested date and exact resolved GameState
canonical-player set, in deterministic canonical-player/snapshot/run/input
order. It deliberately retains complete, degraded, blocked, and late-created
candidates; the frozen assembler remains the sole owner of completeness, PIT,
warnings, and representative-selection policy.

`BaseballIntelligenceRepository` verifies the sealed DailySlate → GameState
chain and both artifacts, independently reconstructs the exact GameState player
registry and merged role set, and derives the relevant canonical-player set.
Before either successful or failed attempt evidence is written, it reloads the
selector inventory at the same stable database boundary and requires exact
candidate, snapshot/run/checksum, requested-player, and inventory-checksum
equality. Successful persistence reruns the frozen assembler at the supplied
fixed observation boundary and requires byte-identical assembly output and
warnings.

Before sealing, the repository re-queries the snapshot plus every game, player,
and equivalent-feature row. It verifies upstream game/player lineage,
relational columns and canonical JSON, ordinals, representative snapshot/run
pairing, per-game and top-level counts, and exact selected inventories. Every
offline read repeats those checks against the independently reconstructed
sealed GameState—not merely against the BIA snapshot's own child JSON. Resolved
identities remain preserved where feature intelligence is unavailable.

Attempt manifests use:

```text
baseball_intelligence/attempts/<run_id>/attempt_<NNNN>.json
```

They are canonical, credential-free, immutable evidence containing exact
upstream lineage, selection boundary, outcome, warnings, requested canonical
players, and candidate inventory identities/checksums—but never feature payload
duplication. Failed
`selection_failed` and `assembly_failed` attempts retain one manifest and one
attempt-evidence row without a BIA snapshot. Exact replay verifies existing
immutable evidence; conflicting replay fails closed.

The manifest and content-addressed artifact publishers write and fsync complete
bytes to a short same-directory temporary file, then atomically create the
immutable final name with a same-filesystem hard link. Publication never
replaces conflicting evidence. A database rollback removes only files created
by that call; pre-existing verified idempotent evidence is never deleted.
Temporary files are removed after both successful and failed publication.

## Validation evidence

Sanitized public validation branch:

`rc/baseball-intelligence-repository-selector-20260730`

Public draft PR #50 validates the schema-v10 repository/selector stack. The
private/public approved source files are byte-equivalent.

Current validation checkpoint:

- repository integrity coverage includes independent GameState player lineage,
  exact equivalent-row reconstruction, selector-inventory binding, atomic
  publication, rollback cleanup, immutable replay, six-category DB reopen, and
  zero-game reconstruction
- 80 focused BIA tests passed
- 490 cross-phase focused tests passed with 5 Windows symlink skips
- development full suite: 1,463 passed, 8 skipped, plus only the 8 accepted
  Phase 4 credential-boundary failures
- stats full suite: 1,465 passed, 6 skipped, plus the same 8 accepted Phase 4
  failures; stats-only: 267 passed
- Ruff passed; full non-incremental mypy passed across 255 source files
- the two former Windows temporary-path artifact failures remain resolved
- the only accepted integrated full-suite failures are the separately tracked
  Phase 4 Odds+Weather credential-boundary tests


## Current upstream and intentional blockers

DailySlate V1 and GameState V1 production persistence/handlers are accepted;
the controller executes phases 1 and 2 then blocks safely at phase 3.

No production BIA handler is registered in this checkpoint.

Do not register the production phase-3 handler yet.

BIA1-B remaining work is limited to:

1. production `BASEBALL_INTELLIGENCE_ASSEMBLY` handler
2. Phase 3 controller registration
3. proof that successful Phase 3 advances only to a safe block at `ODDS_WEATHER`

Do not fabricate phase 3 or phase 4 success before those persistence/handler gates exist.

No Phase 3 production handler is registered in this checkpoint. Do not claim
Phase 3 execution success until the subsequent handler/controller checkpoint.
