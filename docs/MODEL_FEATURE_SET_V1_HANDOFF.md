# ModelFeatureSet V1 — Pre-Persistence Handoff

## Current persisted integrity correction

Selected V3 lineage includes canonical player identity in contracts,
manifests, canonical artifacts, relational children, indexes, and seal
verification. The handler resolves and verifies exact player feature references
before calculating the input checksum, then enters one transformation boundary.
A transformation failure retains immutable evidence from that selection without
rebuilding. Historical reads use exact stored Matchup Packet and Data Quality
snapshot IDs; wrong-player substitution fails closed.

Phase input now has an explicit two-step boundary: derive the selected player
inventory solely from the sealed packet, compute the input checksum, then
validate each reference against retained V3 stats rows. Missing, wrong-player,
wrong-checksum/version/kind/date/completeness, or late PIT evidence persists
`input_failed` with the exact packet-derived inventory marked `unverified` and
never enters transformation. `transformation_failed` and `persistence_failed`
require the exact validated inventory. Failed writers do not rerun selection,
validation, or transformation.

**Private branch:** `rc/model-feature-set-v1-direct-20260727`  
**Private draft PR:** #17  
**Base:** `rc/matchup-packet-v1-direct-20260727`  
**Controller phase:** 7 — `MODEL_FEATURE_SET`

## Current checkpoint

MFS1-A is implemented fixture-first as a deterministic, zero-network transformation from canonical `MatchupPacketV1` into a fixed model-input schema.

Implemented surface:

- `docs/MODEL_FEATURE_SET_V1_DESIGN.md`
- `docs/MODEL_FEATURE_SET_V1_PERSISTENCE_SPEC.md`
- `docs/MODEL_FEATURE_SET_V1_HANDOFF.md`
- `app/model_feature_set/schema.py`
- `app/model_feature_set/contracts.py`
- `app/model_feature_set/builder.py`
- `app/model_feature_set/artifact.py`
- `app/model_feature_set/__init__.py`
- `tests/test_model_feature_set.py`
- `tests/test_model_feature_set_artifact.py`
- `tests/test_model_feature_set_v3_integration.py`
- `tests/test_model_feature_set_identity.py`

## Frozen candidate schema

Contract:

`DSE_MODEL_FEATURE_SET_V1`

Feature schema:

`DSE_MODEL_FEATURE_SCHEMA_V1`

Candidate width:

**613 ordered numeric/null slots**

Candidate schema checksum:

`726edd4dddcd89ed93f15b7e1152810ae22cce34bd4af9be9fe07976b00c78a1`

`schema.py` computes the name list/checksum and fails import if the width or checksum differs from these constants. After freeze, any semantic feature change requires a new feature-schema version.

## Predictive feature boundary

The V1 vector includes only explicitly approved baseball/context evidence already present in MatchupPacket:

- schedule/doubleheader context
- starter certainty and lineup availability
- Baseball Intelligence coverage metadata
- starter pitching windows
- starter pitch traits / pitch physics
- starter workload
- starter previous-start and previous-three-start history
- expected-lineup hitting
- expected-lineup batted-ball metrics
- expected-lineup swing metrics
- bullpen performance
- bullpen workload/fatigue
- weather / wind / roof context

Sportsbook prices, implied probabilities, no-vig probabilities, consensus probabilities, and line movement are **not predictive slots**.

`market_reference_checksum` is retained only as non-predictive lineage metadata for later Value Engine comparison.

## Missingness behavior

Every game receives the same 613 positions.

Each value is either:

- finite numeric evidence; or
- canonical null.

`missing_feature_names` is derived exactly from null slots.

No silent zero, league-average, or season-for-rolling-window imputation occurs.

Explicit observed counts may legitimately be zero; for example, zero bullpen players with retained workload history is different from claiming their fatigue metrics equal zero.

## Game retention / Data Quality

READY, DEGRADED, and INSUFFICIENT MatchupPacket games all receive ModelFeatureSet rows.

Data Quality disposition and exact issue codes are retained as metadata but are not injected into the predictive vector.

Phase 7 does not filter games.

## Canonical identity

`ModelFeatureGameV1` now independently validates:

- authoritative positive-decimal MLB `source_game_id`
- exact derived `edge_event_id`
- exact derived `daily_mlb_game_id`
- canonical away/home teams
- exact upstream MatchupPacket game checksum

No team/date rematching occurs in phase 7.

## Aggregation behavior

### Lineup hitting

Canonical player counts are summed first, then AVG/OBP/SLG/OPS/ISO/BABIP/K-rate/BB-rate are recalculated from the totals.

Player rates are not blindly averaged.

### Bullpen pitching

Canonical bullpen pitcher counts are summed first, then pitching rates are recalculated from totals.

### Batted-ball metrics

V3 currently supplies a common `batted_ball_count` per player/window, not metric-specific sample counts for all batted-ball means.

Therefore V1 uses:

- summed batted-ball count
- maximum player max-exit-velocity
- batted-ball-count-weighted player V3 means for other batted-ball metrics

These are deterministic engineered player aggregates, not claimed to be exact event-level recomputations.

### Swing metrics

V3 supplies metric-specific `*_samples`; V1 uses those sample counts for weighted means.

### Starter history

When V3 appearance-history source is available, V1 includes previous-start values and previous-three-start sums/means. When the source is explicitly unavailable, those values remain null.

### Bullpen workload

Only bullpen players with retained workload source != `unavailable` contribute fatigue evidence. V1 records available-history player count plus recent appearance/start/relief/pitch-count sums and days-since-appearance/start min/mean metrics.

Missing workload history is not interpreted as rest.

## Artifact

Canonical path:

```text
model_feature_set/snapshots/<feature_set_checksum>/model_feature_set_v1.json
```

The writer is:

- content-addressed
- path-contained
- atomic
- deterministic
- idempotent for identical content
- secret-aware

## Focused validation evidence

Sanitized public branch:

`rc/model-feature-set-v1-direct-20260727`

Public draft PR:

#24

Final focused diagnostic:

- temporary PR #29, closed unmerged after validation
- workflow run `30328078924`
- job `90177507930`
- Python 3.12.10
- **22 tests passed in 2.38s**
- **Ruff passed**
- **mypy passed with zero issues across 202 source files**

The final focused suite includes:

- schema width/checksum drift guard
- market-feature exclusion
- zero-game determinism
- explicit missingness
- INSUFFICIENT retention
- point-in-time observed-at rule
- canonical hitting/pitching aggregation math
- weighted sample math
- starter recent-start extraction
- unavailable starter-history behavior
- bullpen workload aggregation
- vector-width/non-finite rejection
- content-addressed artifact behavior
- real frozen-V3 → BIA → OddsWeather → DataQuality → MatchupPacket → ModelFeatureSet integration
- canonical game identity mismatch rejection

## Full repository matrix

The normal public full-quality workflow for the exact final production/test branch head must still be polled before claiming the complete Windows/stats/Linux/Docker matrix green.

Focused MFS1-A is green; full-repository acceptance remains a separate gate.

## Production persistence completion

The schema-v12 repository, immutable attempt manifest, production handler, and
controller registration are implemented. Phase 7 owns
`model_feature_set_attempt_evidence`, `model_feature_set_snapshots`,
`model_feature_set_games`, `model_feature_set_market_contexts`, and
`model_feature_set_source_features`.

Canonical paths are:

- `model_feature_set/attempts/<run_id>/attempt_<NNNN>.json`
- `model_feature_set/snapshots/<checksum>/model_feature_set_v1.json`

The input checksum binds the feature-set contract, 613-slot schema/checksum,
transformation/missing/encoding policies, frozen V3 feature version, fixed
observation time, sealed Matchup Packet and Data Quality identities, and exact
selected feature inventory. Missing values remain `null` with explicit derived
missingness; no statistical imputation or invented zero is performed.

## Next architecture phase

After this sprint, phase 8 is `PREDICTIONS`; it remains unregistered and is the
controller's safe block.

Prediction design should consume only versioned `ModelFeatureSetV1` inputs and should separately version:

- model family/target
- training dataset cutoff
- preprocessing/imputation policy
- training code/configuration
- trained artifact/checksum
- calibration method
- inference contract

Do not put model-specific imputation/training behavior back into canonical ModelFeatureSet.
