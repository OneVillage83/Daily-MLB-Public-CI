# Daily MLB Pipeline Persistence Standard

This standard defines invariants for every remaining pipeline phase. It is not a
universal schema and does not authorize a generic repository base class. Each
phase keeps its own versioned contract, tables, selectors, repository, and
phase-specific integrity rules.

## Canonical lifecycle

Every persisted phase follows this order:

1. Define a canonical, versioned input contract.
2. Compute a deterministic phase-input checksum over every nonsecret input that
   can change behavior.
3. Retain immutable attempt evidence.
4. Give every failed attempt an explicit phase-specific outcome.
5. Produce a canonical output snapshot.
6. Bind the exact upstream snapshot IDs and checksums.
7. Persist phase-specific relational children with deterministic ordinals.
8. Publish a content-addressed artifact.
9. Publish an immutable, credential-free attempt manifest.
10. Reconstruct relational evidence before sealing.
11. Permit exactly one unsealed-to-sealed transition.
12. Commit the transaction.
13. Close and reopen storage and verify the complete evidence chain offline.
14. Treat exact replay as idempotent.
15. Reject conflicting replay without rewriting retained evidence.
16. Preserve settlement and evaluation linkage where the phase requires it.

Application verification supplements SQLite constraints; neither is a substitute
for the other. Canonical JSON alone is never sufficient reconstruction proof.
Artifacts and manifests are published atomically and verified by path, bytes,
checksum, byte count, canonical identity, containment, and credential boundary.

## Phase flow

The pipeline retains evidence at every boundary:

```text
Acquire -> Persist
Normalize -> Persist
Validate -> Persist
Transform -> Persist
Packetize -> Persist
Feature Engineer -> Persist
Predict -> Persist
Value -> Persist
Gate -> Persist
Rank -> Persist
Report -> Persist
Infographic -> Persist
Settle -> Persist
Evaluate -> Persist
```

The following are distinct contracts and must never be collapsed:

```text
PREDICTION
!= VALUE
!= RECOMMENDATION
!= RANK
!= REPORT DISPLAY
!= APPROVED/PUBLISHED BET
```

Every eligible game and market eventually receives a stored prediction even if
the Recommendation Gate later returns `PASS` or `AVOID`.

## Required integrity boundaries

- Use exact canonical IDs; never promote provider IDs or fuzzy names.
- Keep requested date, as-of time, observation/cutoff time, provider update
  time, retrieval time, creation time, and completion time distinct.
- Retain all eligible, excluded, future, degraded, and failed evidence required
  for replay. Selected evidence is a verified subset of retained evidence.
- Store deterministic warnings as canonical evidence without secrets, request
  URLs, query strings, headers, quota metadata, or environment values.
- A positively established zero-game input may succeed; acquisition failure may
  never masquerade as zero games.
- The controller owns phase state transitions. Handlers orchestrate acquisition,
  normalization, assembly, and repositories but do not update phase status.
- A later phase may execute only after every preceding phase is sealed and
  independently verified.

## Phase-specific design requirement

Before a new persistence migration or repository is implemented, document its
canonical object, natural identities, temporal boundaries, parent/child graph,
PIT policy, immutable replay behavior, warning behavior, zero-item behavior,
and application-versus-SQL verification split. Do not add speculative columns,
indexes, or generic abstractions for future phases.
