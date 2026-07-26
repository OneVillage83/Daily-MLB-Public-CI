# Phase 2 Live Weather Validation Checkpoint

**Checkpoint date:** 2026-07-12
**Report timezone:** America/Los_Angeles
**Status:** Paused, not accepted and not closed

## Preserved Evidence

- One live The Odds API request succeeded through the approved validation work.
- Zero NWS requests occurred.
- Zero OpenWeather requests occurred.
- No exact Odds API quota delta is claimed because the response summary needed to
  establish that delta was not persisted.
- No live weather-provider acceptance criterion has been satisfied by this checkpoint.

## Resume Condition

Validation may resume only when a naturally available future regular-season MLB club
game appears in the production path. Already-started games, completed games, and
special-case production logic are not acceptable substitutes. The July 14 All-Star
Game may provide supplemental evidence only if it naturally appears, but it cannot
clear the regular-season acceptance gate.

The resumed validation must preserve:

- the approved future-game eligibility gate;
- a maximum 60-minute offset between selected forecast period and scheduled first pitch;
- two separate live weather runs using the same canonical requested date;
- separate retrieval timestamps and run-owned weather snapshots, even when provider
  payloads are identical;
- NWS `updateTime` as preferred source-update provenance, with `generatedAt` retained
  separately and HTTP Date treated only as response metadata;
- validation of the existing OpenWeather One Call 3.0 integration only.

All weather-dependent release-candidate tests completed before resume are fixture or
contract validation, not live weather validation. The final Phase 2 report and its
audit addendum remain pending.

Only after every Phase 2 acceptance criterion passes may the canonical final report
contain the standalone line `PHASE2_RELEASE_GATE: ACCEPTED`. Release configuration
must use the SHA-256 of that report's exact bytes; an arbitrary hexadecimal value or
the paused checkpoint cannot clear the release gate.
