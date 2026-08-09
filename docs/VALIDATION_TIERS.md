# Daily MLB Validation Tiers

Use the smallest explicit profile that matches the checkpoint boundary. The
runner never infers a broader profile from changed files and never promotes one
profile into another.

## Commands

```powershell
# Implementation loop: explicit tests and affected Python surface.
python scripts/validate_checkpoint.py task `
  --test tests/test_odds_weather_handler.py `
  --python-target app/odds_weather/handler.py `
  --python-target tests/test_odds_weather_handler.py

# Final-output focused task example.
python scripts/validate_checkpoint.py task `
  --test tests/test_final_output_pipeline.py `
  --python-target app/pdf_report `
  --python-target app/infographic `
  --python-target app/final_qc `
  --python-target app/human_review

# Explain without executing anything.
python scripts/validate_checkpoint.py sprint --explain

# Exhaustive release gate; intentionally not used by ordinary tasks or sprints.
python scripts/validate_checkpoint.py release
```

Paths are passed directly to subprocesses without a shell. A failed required
gate stops the profile immediately and returns a nonzero exit code.

## `task`

The task profile requires explicit `--test` and `--python-target` arguments. It
runs only:

- the supplied relevant tests;
- Ruff on supplied Python targets;
- mypy `--no-incremental` on the same affected surface.

It skips full suites, stats environments, audits, lock rehearsals, Docker,
migration matrices, and public workflows. Those omissions are printed.

## `sprint`

The sprint profile runs once after a grouped sprint. For the prediction-to-ranking sprint it runs:

- Predictions, Value Engine, Recommendation Gate, and Rankings contract,
  repository, artifact, handler, controller, replay, and integrity tests;
- the focused v13 fresh-install, frozen-v12 upgrade, identity, and rollback
  regression surface;
- Phase 4 handler/controller/repository/selector/manifest and credential tests;
- Phase 1-11 integration and controller resume tests;
- one complete development suite;
- a focused stats contract regression;
- full Ruff once;
- full mypy `--no-incremental` once;
- a fresh SQLite v13 integrity and foreign-key check;
- the private repository secret scan;
- an optional generated-evidence configured-secret scan.

For the final-output sprint, the accepted Phase 1-11 base already has exhaustive
cross-environment evidence. Local work therefore uses explicit focused task gates
for PDF, Infographic, Final QC, Human Review, controller integration, and v14
migration/SQLite checks. Public exact-head workflows provide the exhaustive
development, stats, security, locked-install, Linux, and Docker evidence. This is
an intentional accelerated checkpoint, not a silent promotion of `task` into a
broader profile.

## `release`

The release profile preserves exhaustive capabilities for migrations,
dependency/security changes, Docker/runtime changes, first complete pipeline
rehearsals, and production releases. It includes full development and stats
suites, Ruff, mypy, dependency consistency, locked vulnerability audits,
hash-locked dry runs, offline pybaseball compatibility, SQLite checks, secret
scans, and Docker release verification. Migration checkpoints additionally run
their fresh/upgrade/backup/diagnostic/failure-injection tests explicitly.

Release validation is expensive and must not be run merely because a task or
sprint profile was requested.
