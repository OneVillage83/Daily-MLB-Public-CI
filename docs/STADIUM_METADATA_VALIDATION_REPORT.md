# MLB Stadium Metadata Validation

**Policy:** `DSE_MLB_STADIUM_METADATA_V1`

**Active catalog:** `app/data/mlb_stadiums.v4.json`, version 4

**Prior catalogs retained:** `app/data/mlb_stadiums.v3.json`, version 3, and
`app/data/mlb_stadiums.v2.json`, version 2

**Evidence contract:** `DSE_STADIUM_FIELD_EVIDENCE_V2`

**Validation date:** 2026-07-13

**Catalog v4 evidence timestamps:** official MLB article retrieved
`2026-07-13T23:22:03-07:00`; owner attestation recorded
`2026-07-13T23:21:42-07:00` (`America/Los_Angeles`)

**Scope:** all 30 active MLB club home venues. Catalog v4 evaluates the seven roof
fields left `UNVERIFIED` by catalog v3. No Odds, NWS, OpenWeather, or other live
provider collection was performed.

## Verification Semantics

- `VERIFIED` means qualifying evidence directly supports the exact field value and no authoritative conflict remains.
- `UNVERIFIED` means a value is retained, but the reviewed evidence is indirect, area-specific, operational, or otherwise insufficient.
- `UNKNOWN` means no usable classification is retained.

Structural validity is not factual verification. An open-air classification is not inferred from omission in a roof list, imagery, weather exposure, or a roofed subarea. Authoritative conflicts must be preserved and force `UNVERIFIED`; source-count voting is prohibited.

Every roof-evidence source records its stable source ID, publisher, source title,
URL, retrieval date, timezone-aware `retrieved_at`, page/heading/section locator,
evidence kind, minimal relied-upon factual text, observed value, notes, revision,
normalization version, and SHA-256 of the normalized minimal text.
`DSE_EVIDENCE_TEXT_NORM_V1` applies Unicode NFC, newline normalization, trim, and
whitespace collapse before hashing.

Catalog v4 adds `exhaustive_classification` and
`owner_attested_direct_observation`; it does not reinterpret `inference_only`.
`DSE_EXHAUSTIVE_CLASSIFICATION_V1` requires the exact reviewed first-party source,
defined universe, subject membership, completed contrary category, venue-bound set-
complement derivation, minimal source fragments, deterministic checksums, and no
authoritative conflict. `DSE_OWNER_ATTESTED_DIRECT_OBSERVATION_V1` is restricted to
the named owner, exact Sutter physical venue, static `roof_type`, exact attestation,
timestamp, and checksums. It cannot establish weather or game-specific roof status.

Catalogs v2 and v3 remain immutable historical files. Any evidence, classification,
or provenance change requires a new catalog/evidence revision and normal review.

## Count Reconciliation

State | Catalog v2 | Catalog v3 | Catalog v4
--- | ---: | ---: | ---:
Roof `VERIFIED` | 8 | 23 | 30
Roof `UNVERIFIED` | 22 | 7 | 0
Roof `UNKNOWN` | 0 | 0 | 0

The physical roof inventory did not change: 22 `open`, seven `retractable`, and one
`fixed`. Regression tests compare all 30 v2, v3, and v4 team-to-roof enum mappings.
The seven retractable classifications and Tropicana Field's fixed classification
remain verified and unchanged.

## Readiness States

- `stadium_catalog_readiness`: **READY**. All 30 active home-venue roof fields are
  `VERIFIED` under catalog v4.
- `launch_slate_venue_readiness`: **NOT_EVALUATED**. No live launch slate was collected. On Thursday, each game must independently resolve to a current venue and pass every material venue/weather gate. A game at an unresolved venue is blocked, but a slate without that venue does not make the full catalog ready.
- Production release: **NO-GO**. Catalog readiness does not accept Phase 2, validate
  a live launch slate, approve a prediction, or authorize publication.

## Catalog v3 Open-Air Evidence Pass (Historical)

The authoritative source record in `mlb_stadiums.v3.json` contains the exact title, locator, minimal evidence text, and checksum summarized here.

Team | Canonical physical venue | Final state | Field-level source | Result
--- | --- | --- | --- | ---
ATH | `sutter-health-park-west-sacramento` | UNVERIFIED | [`MLB_ATH_OPEN_AREA_CONTEXT_2026`](https://www.mlb.com/athletics/ballpark/information/guide) | Selected hospitality areas are described as open-air; the whole venue is not classified.
ATL | `truist-park-cobb` | UNVERIFIED | [`MLB_ATL_ROOF_CANOPY_CONTEXT_2026`](https://www.mlb.com/braves/ballpark/information/guide?bpexternal=true) | A canopy is described; whole-venue roof type is not.
BAL | `oriole-park-camden-yards-baltimore` | VERIFIED | [`MLB_GROUP4_OPEN_AIR_2026`](https://www.mlb.com/news/ranking-every-mlb-outfield-by-fielding-difficulty) | The named venue is in a group directly classified as open-air.
BOS | `fenway-park-boston` | VERIFIED | [`MLB_FENWAY_NON_DOMED_2026`](https://www.mlb.com/news/ranking-every-mlb-outfield-by-fielding-difficulty) | Fenway's venue subsection directly classifies it as non-domed.
CHC | `wrigley-field-chicago` | VERIFIED | [`MLB_CHC_OPEN_AIR_2026`](https://www.mlb.com/cubs/guide/wrigley-field-weather-guide-by-month) | Club FAQ directly classifies the ballpark as open-air.
CWS | `rate-field-chicago` | UNVERIFIED | [`MLB_CWS_WEATHER_CONTEXT_2026`](https://www.mlb.com/whitesox/ballpark/information/guide) | Umbrella policy is operational context, not a roof classification.
CIN | `great-american-ball-park-cincinnati` | UNVERIFIED | [`MLB_CIN_WEATHER_CONTEXT_2026`](https://www.mlb.com/reds/ballpark/information/guide?partnerId=redirect-fan-safety) | Rain-delay exposure is contextual and does not establish roof type.
CLE | `progressive-field-cleveland` | VERIFIED | [`MLB_GROUP4_OPEN_AIR_2026`](https://www.mlb.com/news/ranking-every-mlb-outfield-by-fielding-difficulty) | The named venue is in a group directly classified as open-air.
COL | `coors-field-denver` | VERIFIED | [`MLB_COL_OPEN_AIR_2026`](https://www.mlb.com/rockies/tickets/specials/themes/peanut-allergy-friendly) | Club page directly classifies Coors Field as open-air.
DET | `comerica-park-detroit` | VERIFIED | [`MLB_DET_OPEN_AIR_2018`](https://www.mlb.com/press-release/detroit-tigers-to-offer-peanut-friendly-games-during-the-2018-season-268678578) | Club release directly classifies Comerica Park as open-air.
KC | `kauffman-stadium-kansas-city` | VERIFIED | [`MLB_KC_ROOF_NEVER_BUILT_2021`](https://www.mlb.com/news/kauffman-stadium-arrowhead-stadium-connection) | Official construction history states the venue-wide rolling roof never came to fruition.
LAA | `angel-stadium-anaheim` | UNVERIFIED | [`MLB_LAA_SUN_CONTEXT_2026`](https://www.mlb.com/angels/ballpark/accessibility-guide) | Exposed seating does not rule out a fixed or retractable whole-venue roof.
LAD | `dodger-stadium-los-angeles` | VERIFIED | [`MLB_LAD_OPEN_AIR_2026`](https://www.mlb.com/dodgers/tickets/specials/peanut-allergy) | Dodgers page directly classifies Dodger Stadium as open-air and uses current field branding on the same page.
MIN | `target-field-minneapolis` | VERIFIED | [`MBA_MIN_OPEN_AIR_2011`](https://ballparkauthority.com/PDFs/2011CAFR.pdf) | The public ballpark owner directly describes the completed venue as open air.
NYM | `citi-field-queens` | VERIFIED | [`MLB_NYM_OPEN_AIR_2026`](https://www.mlb.com/mets/ballpark/information) | Mets page directly calls Citi Field an open-air ballpark.
NYY | `yankee-stadium-bronx-2009` | VERIFIED | [`MLB_GROUP4_OPEN_AIR_2026`](https://www.mlb.com/news/ranking-every-mlb-outfield-by-fielding-difficulty) | The named venue is in a group directly classified as open-air.
PHI | `citizens-bank-park-philadelphia` | UNVERIFIED | [`MLB_PHI_OPEN_AREA_CONTEXT_2026`](https://www.mlb.com/phillies/ballpark/information/facts-figures-fun-features) | Open-air modifies concourses and open outfield describes design, not whole-venue roof type.
PIT | `pnc-park-pittsburgh` | UNVERIFIED | [`MLB_PIT_OPEN_AREA_CONTEXT_2026`](https://www.mlb.com/pirates/ballpark/events/venues/main-concourse) | A covered-outdoor concourse is a subarea and cannot classify the whole venue.
SD | `petco-park-san-diego` | VERIFIED | [`MLB_SD_OPEN_AIR_2021`](https://www.mlb.com/padres/fans/health-guidelines) | Padres page directly classifies Petco Park as an outdoor, open-air ballpark.
SF | `oracle-park-san-francisco` | VERIFIED | [`MLB_SF_OPEN_AIR_2013`](https://www.mlb.com/giants/news/terence-moore-candlestick-park-leaves-us-with-frosty-baseball-memories/c-66191512), [`MLB_SF_ORACLE_RENAME_2019`](https://www.mlb.com/giants/news/giants-oracle-agree-to-naming-rights-deal-c302553388) | Official open-air evidence under the AT&T Park name is joined to Oracle Park by an official same-venue rename source.
STL | `busch-stadium-st-louis-2006` | VERIFIED | [`MLB_STL_OPEN_AIR_2021`](https://www.mlb.com/press-release/press-release-cardinals-to-host-fans-at-busch-stadium-in-2021) | Club release directly describes Busch Stadium's open-air footprint.
WSH | `nationals-park-washington` | VERIFIED | [`MLB_WSH_OPEN_AIR_2026`](https://www.mlb.com/nationals/ballpark/events/outdoor-venues/ballpark) | The official entire-ballpark page directly describes an open-air setting.

No authoritative evidence conflict was found. At catalog v3, the seven unresolved
fields reflected evidence insufficiency, not a contrary source value.

## Catalog v4 Completion

Team | Canonical physical venue | v4 state | Evidence path | Derivation result
--- | --- | --- | --- | ---
ATH | `sutter-health-park-west-sacramento` | VERIFIED open | `owner_attested_direct_observation` | Exact owner attestation bound only to Sutter's static roof field.
ATL | `truist-park-cobb` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 2 member; outside the completed eight-venue roofed set.
CIN | `great-american-ball-park-cincinnati` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 2 member; outside the completed eight-venue roofed set.
CWS | `rate-field-chicago` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 3 member; outside the completed eight-venue roofed set.
LAA | `angel-stadium-anaheim` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 2 member; outside the completed eight-venue roofed set.
PHI | `citizens-bank-park-philadelphia` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 5 member; outside the completed eight-venue roofed set.
PIT | `pnc-park-pittsburgh` | VERIFIED open | `DSE_EXHAUSTIVE_CLASSIFICATION_V1` | Explicit Group 3 member; outside the completed eight-venue roofed set.

The MLB derivation uses the article's exact five-group, 29-venue population, its
explicit Athletics/Sutter exclusion, its first five covered-roof venues, and the
sentence naming Miami, Seattle, and Milwaukee as the remaining closeable-roof
venues. Runtime validation pins the reviewed group membership, exclusion, source
fragments, roofed subset, source identity, and checksums; coordinated list or text
rewrites fail closed. The new exhaustive record retains only 21 words of minimal
source fragments; structured identities, locators, and checksums carry the rest.

The Sutter evidence kind is `owner_attested_direct_observation`. The exact stored
attestation is: "I live in Sacramento and have been to Sutter Health Park multiple
times. There is no roof." It is explicitly internal firsthand project-owner evidence,
not MLB, venue-operator, or licensed-provider documentation. Before commercial
release, it must be revalidated under the commercial evidence policy or explicitly
accepted by the commercial release owner.

## Existing Verified Roofs

Team | Roof | Field-level source
--- | --- | ---
ARI | retractable | `MLB_ARI_ROOF`
HOU | retractable | `MLB_HOU_ROOF`
MIA | retractable | `MLB_MIA_ROOF_HISTORY`
MIL | retractable | `MLB_MIL_ROOF_HISTORY`
SEA | retractable | `MLB_SEA_ROOF`
TB | fixed | `MLB_TB_2026`
TEX | retractable | `MLB_TEX_FACTS`
TOR | retractable | `MLB_TOR_HISTORY`

These eight sources were re-read and upgraded to the v3 checksum-bearing evidence contract. Their enums and physical venue identities did not change. Unknown retractable-roof operational status remains unknown; it is never inferred from the physical roof enum.

## Identity And Analytical Boundaries

- `dodger-stadium-los-angeles` remains one physical venue. `Dodger Stadium` is the base/current alias and `UNIQLO Field at Dodger Stadium` is the current branded display name. Neither is former-venue evidence.
- All 30 current club mappings, canonical physical keys, coordinates, IANA timezones, and exact aliases remain valid.
- All 30 outfield bearings remain `UNVERIFIED`; `verified_outfield_bearing()` returns null and no field-relative wind fact may use them.
- The 29 missing elevations remain `UNKNOWN`; Coors Field's retained elevation remains `UNVERIFIED`.
- Neutral, international, alternate-site, and future temporary games still require event-level venue evidence.

## Defects Corrected

1. The active catalog now has a new evidence revision instead of rewriting catalog v2.
2. Roof evidence is reproducible at field level with titles, locators, timezone-aware retrieval, minimal text, deterministic checksums, and explicit revision policy.
3. Direct evidence, inference-only evidence, and observed roof enum are machine-validated separately.
4. Fifteen fields were promoted only after direct official evidence; seven indirect cases remain fail-closed.
5. Deterministic inventory now emits `stadium_catalog_readiness` independently of any event slate.
6. Regression coverage proves physical roof enums, fixed/retractable classifications, the weather gate, and the publication boundary were not weakened.

## Catalog v3 Verification Results (Historical)

Command | Actual result
--- | ---
`stadium_catalog_inventory()` | 30 records; 23 VERIFIED, 7 UNVERIFIED, 0 UNKNOWN roof fields; zero validation errors; `stadium_catalog_readiness=BLOCKED`
Focused stadium/canonical/weather/publication/release tests | 158 passed
`python -m pytest -q` | 538 passed, 5 skipped, 1 warning
`python -m ruff check .` | PASS
`python -m mypy` | PASS, 63 source files
`python -m pip check` | PASS
Locked `pip-audit` | PASS, no known vulnerabilities
Hash-locked development-install dry run | PASS
Fresh migration | PASS, schema/user version 4
Schema fingerprint | PASS, `e9080c8543c6c98f29af79ba9e7f837d6a10d9a5beb6f41a6535c3ab6fe9587e`
SQLite integrity | `ok`
SQLite foreign-key check | zero violations
Configured-secret scan | 112 candidate tracked files, 2 configured secret values, 0 matches

The five local skips require Windows symbolic-link privileges; exact-head hosted CI must exercise them. Local Docker remains unavailable. Real Docker and full CI acceptance must pass against the exact pushed branch head and are reported separately without changing that tested commit.

## Catalog v3 Unresolved Items (Historical)

- **BLOCKER - stadium catalog:** ATH, ATL, CIN, CWS, LAA, PHI, and PIT lack qualifying whole-venue roof evidence. Full catalog readiness is not achieved.
- **BLOCKER - release evidence:** Phase 2 remains paused and was not resumed.
- **NON-BLOCKER - event evaluation pending:** `launch_slate_venue_readiness` is not evaluated until the approved live Thursday slate exists.
- **NON-BLOCKER - deferred analytical fact:** all 30 outfield bearings remain unverified and suppressed.
- **NON-BLOCKER - observability/data gap:** elevation coverage remains incomplete.

No live odds, NWS, or OpenWeather request was made; no prediction was created; no publication was performed.

## Catalog v4 Verification Results (2026-07-14)

Command | Actual result
--- | ---
`stadium_catalog_inventory()` | 30 records; 30 VERIFIED, 0 UNVERIFIED, 0 UNKNOWN roof fields; 22 open, 7 retractable, 1 fixed; zero validation errors; `stadium_catalog_readiness=READY`
Focused stadium/canonical/weather/publication/release tests | 208 passed
`python -m pytest -q` | 561 passed, 5 skipped, 1 warning
`python -m ruff check .` | PASS
`python -m mypy` | PASS, 63 source files
`python -m pip check` | PASS
Locked `pip-audit` | PASS, no known vulnerabilities
Hash-locked clean development-install dry run | PASS
Fresh migration | PASS, schema/user version 4
Schema fingerprint | PASS, `e9080c8543c6c98f29af79ba9e7f837d6a10d9a5beb6f41a6535c3ab6fe9587e`
SQLite integrity | `ok`
SQLite foreign-key check | zero violations
Configured-secret scan after final staging | 113 candidate tracked files, 2 configured secret values, 0 matches

The five local skips still require Windows symbolic-link privileges. The exact new
branch head must exercise them in hosted CI and pass the real Docker verifier. That
post-push result is recorded in the PR/exact-head completion record so the tested
commit does not require a self-referential follow-up documentation commit.

## Current Unresolved Items

- **BLOCKER - release evidence:** Phase 2 remains paused and was not resumed.
- **BLOCKER - production reliability:** the live launch-slate, event-level venue and
  weather validation, sealed reviewed prediction, candidate evaluation, mandatory
  local human review, and immutable package procedure have not run.
- **BLOCKER - commercial evidence boundary:** the internal Sutter owner attestation
  must be revalidated under the commercial evidence policy or explicitly accepted by
  the commercial release owner before commercial release.
- **NON-BLOCKER - event evaluation pending:**
  `launch_slate_venue_readiness=NOT_EVALUATED` until the approved live Thursday slate.
- **NON-BLOCKER - deferred analytical fact:** all 30 outfield bearings remain
  unverified and suppressed; elevation coverage remains incomplete.

Catalog v4 completes stadium-catalog roof readiness. It does not authorize a launch,
prediction, approval, or publication. Production remains **NO-GO**.
