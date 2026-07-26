# MLB Phase 1 Data Collector

A production-oriented starter repository for the first stage of a daily MLB research pipeline. Google Apps Script starts a Python job, the service collects current odds and stadium weather, stores snapshots in SQLite, exports normalized JSON, creates a ZIP artifact, and lets Apps Script save that ZIP to Google Drive.

> This repository intentionally uses **NWS + OpenWeather**, not Open-Meteo, because the commercial-report plan discussed for this project requires commercial-safe weather sources. NWS is primary for U.S. stadiums; OpenWeather is an optional comparison and fallback source.

## Included

- FastAPI job service with bearer-token authentication
- SQLite schema for runs, games, odds snapshots, weather snapshots, and errors
- Formal SQLite schema migrations, audited run-state transitions, and raw-response metadata
- The Odds API v4 MLB collector (`baseball_mlb`)
- All-book odds storage and basic consensus/no-vig calculations
- National Weather Service hourly forecast collector
- Optional OpenWeather One Call comparison collector
- Versioned 2026 stadium metadata with field-level provenance and verification states
- Roof-aware weather handling and baseball wind-component calculation
- JSON exports, manifest, error log, and ZIP artifact
- Dockerfile and Docker Compose configuration
- Google Apps Script starter, polling trigger, and Drive upload
- Unit tests for core calculations

## What Phase 1 does not yet include

- Official probable pitchers or confirmed lineups
- Injuries and transactions
- Statcast ingestion / pybaseball
- Bullpen workload reconstruction
- Sol reasoning or web research
- Prediction model

Those belong in later milestones. The current collector deliberately avoids inventing unavailable baseball facts.

## 1. Local setup

```bash
cp .env.example .env
# Edit .env
docker compose up --build
```

Health check:

```bash
curl http://localhost:8080/health
```

Start a collection:

```bash
curl -X POST http://localhost:8080/jobs/daily-collection \
  -H "Authorization: Bearer YOUR_SERVICE_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"requested_date":"2026-07-10"}'
```

`requested_date` is required and must be an exact calendar date in
`YYYY-MM-DD`. Accepted jobs return HTTP 202 with a service-generated ID in the
form `run_YYYYMMDD_<32 lowercase UUID hex characters>` and an initial `queued`
status.

Poll the returned run ID:

```bash
curl http://localhost:8080/jobs/RUN_ID \
  -H "Authorization: Bearer YOUR_SERVICE_AUTH_TOKEN"
```

Download the artifact:

```bash
curl -OJ http://localhost:8080/jobs/RUN_ID/artifact \
  -H "Authorization: Bearer YOUR_SERVICE_AUTH_TOKEN"
```

## 2. Run without Docker

Use Python 3.12. On Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.txt
```

On macOS/Linux, create the environment with a Python 3.12 executable and use
the equivalent activation command.

```bash
cp .env.example .env
python scripts/initialize_database.py
uvicorn app.main:app --reload --port 8080
```

Initialize a new database or upgrade a recognized starter database, then run a
one-off collection:

```bash
python scripts/initialize_database.py --check
python scripts/run_once.py --date 2026-07-10
```

Before an unversioned starter database is upgraded, the migration runner uses
SQLite's backup API and verifies the backup. Unknown or newer schemas are
refused.

## 3. Spreadsheet Config sheet

Add these labels in column A and values in column B:

| Setting | Purpose |
|---|---|
| `PYTHON_SERVICE_URL` | Public HTTPS URL for the deployed FastAPI service |
| `SERVICE_AUTH_TOKEN` | Must match the backend `.env` value |
| `DRIVE_FOLDER_ID` | Drive folder where ZIP artifacts will be saved |
| `REPORT_TIMEZONE` | Example: `America/Los_Angeles` |

The backend itself needs `ODDS_API_KEY`, `NWS_USER_AGENT`, and optionally `OPENWEATHER_API_KEY` in environment variables. Keep those server-side rather than exposing them in Apps Script.

## 4. Apps Script

Copy `apps_script/Main.gs` into the Apps Script project attached to the spreadsheet. Then:

1. Run `triggerMlbPhaseOneCollector()` manually once to authorize external requests and Drive access.
2. Confirm a ZIP appears in the selected Drive folder.
3. Run `installDailyCollectorTrigger()` once.
4. Set the Apps Script project timezone correctly under Project Settings.

The daily trigger starts the backend job. A short-lived follow-up trigger polls until the artifact is ready and then writes it to Drive.

## 5. Generated artifact

Each ZIP contains:

```text
games.json
odds_raw.json
odds_consensus.json
weather.json
collection_manifest.json
collection_errors.json
raw/
```

The run directory also contains `artifact.zip`; the ZIP excludes itself. Full
sanitized provider JSON lives under
`raw/PROVIDER/ENDPOINT_CATEGORY/SHA256.json`, while SQLite stores its provider,
run/event identity, timestamps, content type, checksum, and contained relative
path. SQLite preserves every odds and weather observation; snapshot row `id`
is the identity and repeated provider observations are valid.

## 6. Weather behavior

- A fixed-roof venue suppresses outdoor forecasts only when the roof type is verified.
- Retractable-roof outdoor conditions remain contextual while game-specific roof status is unknown.
- NWS is the primary source and requires an identifying User-Agent.
- OpenWeather is optional and compares temperature, precipitation probability, and wind.
- Unverified field bearings never produce field-relative wind components.

## 7. Deployment note

Phase 1 uses one in-process worker and executes collection runs serially. A
normal shutdown waits for active work, and startup reconciliation marks stale
`queued` or `running` rows failed. A forced process or host termination can
still interrupt work; this executor is suitable only for the documented local
or single-service Phase 1 deployment, not serverless or multi-replica use.

## 8. Security

- Rotate any API keys previously pasted into chat or screenshots.
- Never commit `.env`.
- Use a long random `SERVICE_AUTH_TOKEN`.
- Put the production service behind HTTPS.
- Restrict network access or use signed identity tokens when deploying to Google Cloud.

## 9. Tests

Install the resolved development dependency set in a Python 3.12 virtual
environment, then run all quality gates:

```powershell
python -m pip install --require-hashes -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m mypy
python -m pip check
python -m pip_audit --require-hashes -r requirements.txt --progress-spinner off
python -m pip install --dry-run --ignore-installed --require-hashes -r requirements-dev.txt
```

Runtime and development dependency inputs are separated in `requirements.in`
and `requirements-dev.in`. Regenerate their resolved files from Python 3.12:

```powershell
python -m piptools compile --resolver=backtracking --generate-hashes --strip-extras --allow-unsafe --output-file=requirements.txt requirements.in
python -m piptools compile --resolver=backtracking --generate-hashes --strip-extras --allow-unsafe --output-file=requirements-dev.txt requirements-dev.in
```

## 10. Local release-candidate workflow

The release-candidate path is local and human-review gated. The FastAPI service and
Apps Script cannot approve or publish a play. Phase 2 live weather acceptance must be
completed before a policy evaluation can become a candidate.

```powershell
.\.venv\Scripts\python.exe scripts\release_candidate.py assemble --run-id RUN_ID
.\.venv\Scripts\python.exe scripts\release_candidate.py prediction-template --run-id RUN_ID --event-id EVENT_ID --output .validation\reviewed-prediction.json
.\.venv\Scripts\python.exe scripts\release_candidate.py seal-prediction --run-id RUN_ID --input .validation\reviewed-prediction.json
.\.venv\Scripts\python.exe scripts\release_candidate.py evaluate --run-id RUN_ID --prediction-id PREDICTION_ID
.\.venv\Scripts\python.exe scripts\release_candidate.py draft --run-id RUN_ID
.\.venv\Scripts\python.exe scripts\release_candidate.py approve --run-id RUN_ID --reviewer-id REVIEWER_ID --decisions .validation\review-decisions.json
```

Prediction templates expose only the market-blind feature view. Structured analyst
evidence and probability are sealed before the separate evaluation command exposes
no-vig probability, price, edge, EV, or candidate status. Approval revalidates event
time, source checksums, eligible best price, and odds freshness.

See `docs/RELEASE_CANDIDATE_ARCHITECTURE.md`, `docs/THURSDAY_RELEASE_GATE.md`,
and the schemas under `schemas/`.
