# TDL-03E sanitized public CI handoff

Updated: 2026-09-09T23:48:26-07:00 (America/Los_Angeles)

This branch is a supplemental public CI execution surface for private Daily-MLB
authoritative head `c4d8575401114a4f5f5ec7923a48e56b96fa4097` on
`codex/ddc6-mlb-consumer-20260909`. The private implementation is
`61944cc2d4c1974c6b3a92a6d790cf9b5b128e80`; the later private commits contain
only receipts, an empty validation trigger, and documentation. The private
repository remains the source, architecture, scientific, evidence, and release
authority.

The approved mirror surface contains 17 previously disclosed dependency,
artifact-containment, scan, compatibility, and Docker-verifier contract paths.
Every path has the same Git blob ID in the private authoritative head and inherited
public commit `875ae18830c24bc1e699f26024fcdac3daf1ff25`. The canonical 17-entry
manifest SHA-256 is
`941f78a42081b00933cd2d07499e659ca1cf2f18ef949d230223361f24b44f9f`.
No private source file needed copying because the sanctioned blobs were already
identical.

The current private Dockerfile differs and is excluded. Private A1/A2/V17/DDC
consumer implementation, migrations, tests, configuration, retained/provider
evidence, fixtures, databases, logs, credentials, generated producer packages,
scientific documents, and all other private paths were not copied. The public
application baseline remains the previously disclosed pre-A1 tree and is not
equivalent to the private application tree.

The established `quality` and `tdl01-sanitized-exact-content` workflows will run
against the exact public branch head. The Docker job is a real build/runtime/schema
check only for the sanitized public baseline. It does not exercise or certify the
excluded private Dockerfile, schema V17, DDC consumer, forecast-window/evaluation
persistence, manual runtime, or publication pipeline.

The exact tested public SHA is
`e8110eb076eea3feb091831661e5888544899053`. Both pull-request workflows completed
successfully and every declared step executed:

| Workflow/run | Job ID | Result |
| --- | --- | --- |
| `quality` / `34447058915` | `102774044277` Python | 1,844 passed, 2 skipped, 1 warning; secret scan, Ruff, mypy 375 files, consistency, runtime audit, and locked dry run passed |
| `quality` / `34447058915` | `102774044334` Stats | 280 focused passed; 1,846 full passed, 1 warning; offline compatibility with zero network requests, consistency, audit, and locked dry run passed |
| `quality` / `34447058915` | `102774044053` Linux security | 6 passed |
| `quality` / `34447058915` | `102774044269` Docker runtime | Build/runtime/schema/integrity verification passed for the sanitized public baseline |
| `tdl01-sanitized-exact-content` / `34447058918` | `102774044038` mapped surface | 77 passed; secret scan, Ruff, mypy 7 files, consistency, runtime audit, and locked dry run passed |
| `tdl01-sanitized-exact-content` / `34447058918` | `102774044363` exact Stats toolchain | Locked install, offline compatibility with zero network requests, consistency, audit, and locked dry run passed |

Docker artifact `10140060748`,
`docker-release-verification-e8110eb076eea3feb091831661e5888544899053`,
contains `verification.json` with SHA-256
`ebcdaff4b762cd9b6fb76d8b99657690865fa181bab572c2e3a6380a6d9e27a0`.
It records matching checkout/commit/image revision, healthy UID 999 execution,
read-only application code, persistent database and artifacts, schema V14 checksum
and fingerprint agreement, SQLite integrity `ok`, zero foreign-key violations,
zero collection runs, no Stats packages in the collection image, and a passing
synthetic credential-log scan.

These runs are mapped Level-B supplemental evidence for the 17 exact blobs and
the already-public pre-A1 baseline. They do not certify private TDL-03E source,
private schema V17, the changed private Dockerfile, or the private DDC consumer and
evaluation/persistence path. Private exact-head hosted execution remains required
for release certification. Scientific permissions remain
`0 AVAILABLE / 5 BLOCKED / 22 MISSING`; PIT, model promotion, registry,
Recommendation Gate, and production architecture are unchanged.
