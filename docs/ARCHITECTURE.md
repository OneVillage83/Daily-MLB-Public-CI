# Architecture

## Phase 1 Data Flow

```text
Google Apps Script time trigger
        |
        v
POST /jobs/daily-collection
        |
        v
queued run in SQLite
        |
        v
single in-process worker
        |
        v
FastAPI service
        |
        +--> The Odds API
        |
        +--> NWS
        |
        +--> OpenWeather (optional)
        |
        v
SQLite persistence
        |
        v
Normalized JSON exports
        |
        v
ZIP artifact
        |
        v
Apps Script downloads artifact
        |
        v
Google Drive
```

## Component Responsibilities

### Apps Script

Responsible for:

- scheduling;
- reading spreadsheet configuration;
- calling the Python service;
- polling job state;
- downloading final artifacts;
- writing files to Drive;
- surfacing errors.

Not responsible for:

- collecting dozens of upstream endpoints;
- heavy transformations;
- long-running analysis;
- AI inference.

### FastAPI Service

Responsible for:

- authentication;
- job creation;
- job status;
- collector orchestration;
- artifact serving.

The job-start endpoint validates an explicit report date, creates a `queued`
run, and submits its service-generated run ID to a single-worker executor. The
worker owns the `queued -> running -> terminal` lifecycle. On startup, work
left `queued` or `running` by a prior process is reconciled to `failed`; work is
not resumed.

### Collectors

Each collector must:

- validate upstream payloads;
- preserve raw data;
- normalize fields;
- return provider metadata;
- raise typed errors;
- support retries.

### Processors

Processors must be pure where possible.

Examples:

- odds conversion;
- no-vig normalization;
- provider comparison;
- wind-vector math.

### Database

The database is the system of record for Phase 1 runs.

- Formal schema version 1 is installed by an ordered in-repository migration
  runner.
- Recognized legacy databases are backed up with the SQLite backup API before
  upgrade; unknown and newer schemas are refused.
- Every connection enables foreign keys, a 5-second busy timeout, and WAL.
- Connections live for one read or write transaction and are never shared
  across worker threads.
- Run/game associations enforce child-row ownership, while repeated odds and
  weather observations remain valid independent snapshot rows.

### Exporter

The exporter writes stable machine-readable artifacts.

All run paths are constructed by one contained path layer from a typed date and
validated run ID. JSON, raw payload, and ZIP writes use temporary files and
atomic replacement. Raw response metadata and SHA-256 values live in SQLite;
sanitized full JSON lives in the run directory.

## Phase 1 Runtime Limit

The executor is intentionally process-local and serial. Graceful shutdown waits
for active work, but forced termination can interrupt a run. It is not a
distributed or durable queue and must not be deployed as multiple replicas.

## Future Architecture

Later phases may add:

- PostgreSQL;
- worker queue;
- Cloud Run Jobs;
- per-game research workers;
- OpenAI Responses API;
- GPT-5.6 Sol analysis;
- critic/audit pass;
- Google Docs/PDF generation;
- Whop delivery.

These should not be implemented during Phase 1.

## Manual Run Controller Production Boundary

The canonical manual pipeline has production handlers for exactly all 15 initial
phases: `DAILY_SLATE`, `GAME_STATE`, `BASEBALL_INTELLIGENCE_ASSEMBLY`,
`ODDS_WEATHER`, `DATA_QUALITY`, `MATCHUP_PACKET`, `MODEL_FEATURE_SET`,
`PREDICTIONS`, `VALUE_ENGINE`, `RECOMMENDATION_GATE`, `RANKINGS`, `PDF_REPORT`,
`INFOGRAPHIC`, `FINAL_QC`, and `HUMAN_REVIEW`. Each handler verifies
sealed upstream repository evidence and returns an immutable phase result; the
controller service alone owns phase and run status transitions. After a
successful Phase 7 commit, execution enters the schema-v13 prediction/decision
chain: market-blind reviewed-analyst `PREDICTIONS`, factual `VALUE_ENGINE`,
deterministic `RECOMMENDATION_GATE`, then lexicographic `RANKINGS`. Prediction,
value, decision, and rank remain separate immutable snapshots. PASS and AVOID
retain all upstream evidence. After a successful Phase 11 commit, execution
encounters unregistered `PDF_REPORT` and
raises the safe blocked condition while the run remains resumable. Repeated
resume does not rerun completed upstream phases. Phase 12 reuses the accepted
ReportLab presentation system with exact v13 semantics; Phase 13 selectively
ports the deterministic SVG presentation system. Phase 14 reconciles exact
artifacts and analytics without modifying them. Before an explicit operator
decision exists, Phase 15 pauses without starting or failing an attempt. An
immutable APPROVE or REJECT completes Human Review, but no publication follows.

Phase 4 uses retained-evidence acquisition: The Odds API is required for a
nonempty slate, NWS is primary weather evidence, and optional OpenWeather is a
comparison/fallback source. A positively established zero-game slate bypasses
all provider calls. Controller configuration metadata records only contract and
provider-policy versions and boolean capability modes; paths, headers, request
metadata, API keys, and configured-secret inventories are excluded.

The common immutable persistence lifecycle is defined in
`PIPELINE_PERSISTENCE_STANDARD.md`; local validation profiles are defined in
`VALIDATION_TIERS.md`. These documents set invariants without introducing a
generic cross-phase schema or repository implementation.
