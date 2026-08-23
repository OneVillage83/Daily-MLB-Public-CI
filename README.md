# Daily-MLB Public CI Mirror

**Status:** SANITIZED CI MIRROR — NOT PRODUCT AUTHORITY  
**Last updated:** 2026-08-22T17:52:00-07:00 (America/Los_Angeles)

This public repository exists to run selected GitHub Actions checks for Daily-MLB code that is safe for public disclosure when private-repository Actions capacity is constrained.

It is **not** the canonical Daily-MLB product repository, current architecture source, model authority, historical-data authority, or release authority.

The canonical project repository is private:

`OneVillage83/Daily-MLB`

## Important

The public `main` branch contains historical mirror material from an earlier implementation stage. Do not infer the current Daily-MLB product scope from the old branch history, older Phase 1 code, or stale mirror branches.

At the 2026-08-22 P0 audit:

- the latest public `main` commit found before this control-plane update was `ba37f77483b84c180f839e3e3231d531a661699d` (`Mirror DailySlateV1 DS1-A`, 2026-07-27);
- many historical `debug/**`, `live/**`, `mirror/**`, and `rc/**` branches remained;
- some later RC market-work branches were present;
- no public `gate2/**` branch was found;
- no public `mq*` branch was found.

That is expected for a historical/sanitized CI mirror and does **not** indicate that newer private work is absent from the canonical project.

## CI evidence rule

A public green check is evidence only for the exact public commit that ran.

To use a public CI run as supplemental evidence for a private Daily-MLB commit, preserve an explicit mapping containing at minimum:

- private repository/branch/commit SHA;
- public mirror branch/commit SHA;
- mirrored paths;
- excluded/redacted/sanitized paths or content;
- deterministic content/tree checksum when practical;
- workflow name and run ID;
- whether all declared workflow steps actually executed;
- workflow conclusion;
- known differences between the private and public trees.

A similar branch name or commit message is not enough to prove equivalence.

## Public-safety rule

Do not copy material here merely to obtain free public Actions capacity if that material should remain private.

Do not publish:

- secrets, tokens, API keys, cookies, passwords, or `.env` contents;
- private databases or datasets;
- customer/user information;
- proprietary or licensed raw data that is not approved for redistribution;
- private reports/artifacts;
- sensitive logs or local environment dumps;
- any code or evidence whose public disclosure has not been evaluated.

Run the repository secret scan on sanitized mirror content before treating it as safe.

## Agent instructions

Read [`AGENTS.md`](AGENTS.md) before making changes in this repository.

The key rule is simple:

> This repository may execute CI; it does not define Daily-MLB product truth.

Canonical exact-head private CI should be rerun when private GitHub Actions execution is available again before a private release head is treated as independently CI-green.
