# MatchupPacket V1 — Pre-Persistence Handoff

**Private branch:** `rc/matchup-packet-v1-direct-20260727`  
**Private draft PR:** #15  
**Base:** `rc/data-quality-v1-direct-20260727`  
**Controller phase:** 6 — `MATCHUP_PACKET`

## Current checkpoint

MP1-A is implemented fixture-first as a zero-network, no-recalculation packaging layer.

Implemented files:

- `docs/MATCHUP_PACKET_V1_DESIGN.md`
- `docs/MATCHUP_PACKET_V1_PERSISTENCE_SPEC.md`
- `app/matchup_packet/contracts.py`
- `app/matchup_packet/assembly.py`
- `app/matchup_packet/artifact.py`
- `app/matchup_packet/__init__.py`
- `tests/test_matchup_packet.py`
- `tests/test_matchup_packet_artifact.py`

## Contract

Canonical version:

`DSE_MATCHUP_PACKET_V1`

A `MatchupPacketGameV1` embeds the exact canonical phase rows:

1. `DailySlateGameV1`
2. `GameStateGameV1`
3. `BaseballIntelligenceGameV1`
4. `OddsWeatherGameV1`
5. `DataQualityGameV1`

It does not flatten those rows into a competing feature schema.

## Lineage behavior

Assembly requires exact phase 1–5 agreement for:

- requested date
- `as_of_time`
- MLB sport/league
- ordered game set
- canonical event/game/source IDs
- away/home teams
- all existing top-level upstream snapshot checksums
- all existing per-game upstream row checksums
- scheduled first-pitch identity where represented upstream

A mixed row from another run/observation cannot package successfully merely because teams happen to match.

## Data Quality behavior

The packet preserves the entire canonical `DataQualityGameV1`, including issue codes, domains, severities, expected/actual evidence, provider/team context, and disposition.

All dispositions are retained:

- `READY`
- `DEGRADED`
- `INSUFFICIENT`

No game is filtered by MatchupPacket.

MatchupPacket does not recompute quality or create another weighted score.

## Point-in-time behavior

All upstream snapshots must share the same `as_of_time`.

Packet `observed_at` defaults to the latest upstream phase 1–5 observation and may not be earlier than any upstream observation.

No provider/network calls occur.

## Zero-game behavior

A valid zero-game phase 1–5 chain produces a valid zero-game MatchupPacket with:

- zero game rows
- all five upstream snapshot checksums retained
- deterministic canonical JSON/checksum
- zero network activity

## Artifact

Canonical path:

```text
matchup_packet/snapshots/<packet_checksum>/matchup_packet_v1.json
```

The writer is:

- content-addressed
- path-contained
- atomic
- deterministic
- idempotent for identical packet content
- secret-aware

## Validation evidence

Sanitized public branch:

`rc/matchup-packet-v1-direct-20260727`

Public draft PR #22 validates the same production source surface.

Focused Windows/Python 3.12 checkpoint:

- 17 MatchupPacket tests passed
- Ruff passed
- mypy passed with zero issues across 193 source files

The normal public repository matrix also had Linux security and Docker runtime green at the last recorded checkpoint; long Python/stats jobs were still running and must be re-polled before claiming the entire matrix green.

## Production persistence completion

The schema-v12 repository, immutable attempt manifest, production handler, and
controller registration are implemented. The handler performs no provider
calls, persists every Data Quality disposition, reconstructs after closing and
reopening SQLite, and continues to `MODEL_FEATURE_SET`.

Canonical paths are:

- `matchup_packet/attempts/<run_id>/attempt_<NNNN>.json`
- `matchup_packet/snapshots/<checksum>/matchup_packet_v1.json`

## Production handler recipe

Once repositories exist, phase 6 should be very small:

```text
resolve exact sealed phase 1–5 snapshots
        ↓
assemble_matchup_packet(...)
        ↓
persist packet + canonical artifact
        ↓
read back and verify exact checksum/bytes
        ↓
mark MATCHUP_PACKET succeeded
        ↓
advance to MODEL_FEATURE_SET
```

No provider call, statistical calculation, odds calculation, weather calculation, quality reassessment, model feature transformation, prediction, value, recommendation, ranking, reporting, publication, or betting belongs in this handler.

## Next phase

Phase 7 `MODEL_FEATURE_SET` consumes the sealed packet as its canonical input
boundary. Matchup Packet remains free of prediction, value, recommendation,
ranking, and report-display output.
