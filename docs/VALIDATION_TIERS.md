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

# One grouped sprint boundary. Optional evidence root is scanned for configured
# secret values after the tests have produced sanitized evidence.
python scripts/validate_checkpoint.py sprint `
  --generated-evidence-root .validation/pre-model-persistence-sprint

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

The sprint profile runs once after a grouped sprint. For the pre-model sprint it runs:

- Data Quality, Matchup Packet, and Model Feature Set contract, repository,
  artifact, handler, and controller tests;
- the focused v12 fresh-install, frozen-v11 upgrade, identity, and rollback
  regression surface;
- Phase 4 handler/controller/repository/selector/manifest and credential tests;
- Phase 1-7 integration and controller resume tests;
- one complete development suite;
- a focused stats contract regression;
- full Ruff once;
- full mypy `--no-incremental` once;
- a fresh SQLite v12 integrity and foreign-key check;
- the private repository secret scan;
- an optional generated-evidence configured-secret scan.

This sprint changes the schema but not shared stats dependencies, lock files, or
Docker inputs. Therefore it runs focused migration verification and explicitly
skips the complete local stats suite, audits, hash-locked install rehearsals,
Docker, and the exhaustive release-only failure-injection matrix.
The exact-head public workflow supplies final cross-environment and release-image
evidence.

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
