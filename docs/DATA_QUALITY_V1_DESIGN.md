# Data Quality V1 Design

**Status:** production persisted in schema v12
**Phase:** 5 — `DATA_QUALITY`
**Contract:** `DSE_DATA_QUALITY_V1`
**Policy:** `DSE_DATA_QUALITY_POLICY_V1`

Data Quality is a zero-network, point-in-time assessment of the exact sealed
DailySlate, GameState, Baseball Intelligence, and Odds + Weather chain. It
creates one ordered assessment for every slate game. Structural identity,
checksum, temporal, artifact, or relational conflicts raise and fail the phase;
ordinary incomplete evidence remains an explicit issue and disposition.

The accepted dispositions are `ready`, `degraded`, and `insufficient`.
`insufficient` is not a deletion instruction: the game remains persisted and
the handler returns `DEGRADED` with `continue_pipeline=True`. A valid snapshot
with noncritical issues returns `SUCCEEDED_WITH_WARNINGS`; a clear snapshot
returns `SUCCEEDED`.

Stable issue codes are:

- schedule: `scheduled_start_time_missing`, `game_cancelled`,
  `game_postponed`, `game_suspended`, `game_already_in_progress`,
  `game_already_final`, `game_delayed`, `game_status_unknown`;
- GameState: `starter_unavailable`, `starter_probable_not_confirmed`,
  `starter_announced_not_confirmed`, `lineup_unavailable`, `lineup_partial`,
  `gameday_personnel_unavailable`;
- Baseball Intelligence: `team_player_intelligence_unavailable`,
  `team_player_features_unavailable`, `player_identity_coverage_incomplete`,
  `player_feature_coverage_incomplete`, `starter_feature_missing`,
  `lineup_features_unavailable`, `lineup_feature_coverage_incomplete`,
  `bullpen_features_unavailable`, `bullpen_feature_coverage_incomplete`,
  `feature_snapshot_completeness_limited`;
- odds: `odds_unavailable`, `odds_supported_markets_unavailable`,
  `stale_odds_markets_present`;
- weather/venue: `weather_unavailable`, `nws_primary_unavailable`,
  `weather_provider_comparison_unavailable`,
  `weather_provider_agreement_moderate`, `weather_provider_agreement_weak`,
  `field_relative_wind_unavailable`, `roof_type_unverified`, and
  `retractable_roof_status_unknown`.

Input checksum `DSE_DATA_QUALITY_PHASE_INPUT_V1` binds the policy, requested
date, as-of time, fixed assessment observation time, and all four upstream
snapshot IDs/checksums. Secrets, paths, URLs, and runtime identities are
excluded.

Persistence uses phase-specific attempt, snapshot, game, and issue tables.
Artifacts use
`data_quality/snapshots/<checksum>/data_quality_v1.json`; manifests use
`data_quality/attempts/<run_id>/attempt_<NNNN>.json`. Both are canonical,
atomic, content verified, immutable, and credential-free. A confirmed
zero-game upstream chain produces a valid empty sealed snapshot.
