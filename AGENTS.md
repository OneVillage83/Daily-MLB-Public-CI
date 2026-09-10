# Daily MLB Public CI Mirror — Agent Instructions

**Status:** CURRENT PUBLIC-MIRROR CONTROL INSTRUCTIONS
**Last updated:** 2026-08-22T17:52:00-07:00 (America/Los_Angeles)

## Purpose

This repository is a **sanitized public CI mirror** for selected Daily-MLB code that is safe for public disclosure.

It is **not** the canonical Daily-MLB product repository, architecture authority, model authority, historical-data authority, or release authority.

The private canonical repository is:

`OneVillage83/Daily-MLB`

Do not infer current product scope from this repository's `main` branch, README history, branch names, or old Phase 1 material.

## Non-negotiable rules

1. Never treat this repository as the source of truth for current Daily-MLB architecture.
2. Never move secrets, credentials, `.env` contents, private datasets, databases, proprietary artifacts, licensed/raw provider data, or sensitive implementation material here merely to obtain public GitHub Actions capacity.
3. Every future mirror used as evidence for a private commit must preserve an explicit private-source-SHA -> public-mirror-SHA mapping.
4. Record mirrored paths, exclusions/redactions, sanitization differences, workflow/run identity, and the validation conclusion.
5. A green public workflow without an explicit mapping proves only that the public commit passed.
6. Public CI is supplemental engineering evidence. Canonical release validation belongs to the exact private head when private Actions execution is available.
7. Do not promote models, change production permissions, or make architecture decisions from stale public-mirror code.
8. Do not rewrite or delete historical mirror branches merely because they are old; they may be useful CI/history evidence.

## Current known state

At the 2026-08-22 P0 audit:

- public `main` was still based on the earlier Phase 1 / manual-pipeline era;
- the latest public `main` commit found was `ba37f77483b84c180f839e3e3231d531a661699d` (`Mirror DailySlateV1 DS1-A`, 2026-07-27);
- many historical `debug/**`, `live/**`, `mirror/**`, and `rc/**` branches remain;
- V4-V8-era RC branches are present;
- no `gate2/**` branch was found;
- no `mq*` branch was found.

Therefore absence of newer private work from this repository does not mean that work does not exist.

## Documentation discipline

Any meaningful change to this public mirror must be documented with a timezone-aware ISO-8601 timestamp and must state:

- what was mirrored or changed;
- the private source branch/SHA if applicable;
- the public target branch/SHA;
- what was excluded or sanitized;
- what CI ran and whether all declared steps executed;
- remaining differences/limitations;
- the next handoff.

Do not leave mirror provenance only in a chat or commit message.

## Recommended branch naming

For a future private-head mirror, prefer an identity-bearing branch such as:

`mirror/private-<short-private-sha>-<yyyyMMdd>`

rather than reusing a generic branch for unrelated private heads.
