# Infographic V1 production design

Phase 13 selectively ports the deterministic fixture-era SVG design without merging or cherry-picking its historical branch. It consumes only the exact sealed Phase 12 semantic report.

`DSE_MLB_INFOGRAPHIC_DOCUMENT_V1` produces feed `1080x1350` and story `1080x1920` SVG variants under `DSE_MLB_INFOGRAPHIC_SVG_RENDER_V1`. Recommendation rank 1 is the only Pick of the Day. Later cards are the remaining `recommend` rows in exact `recommendation_rank` order. `pass` and `avoid` never become cards. Displayed probability, edge, EV, price, bookmaker count, quality, and weather text are copied from report evidence; the phase does not calculate value, make decisions, or rerank.

The dark-navy Daily Edge visual language, deterministic SVG renderer, local/no-network assets, Weather Watch, full-report call to action, and pre-review notice are retained. SVG is the canonical V1 publication-format source; PNG derivatives are deferred rather than adding a fragile dependency.

Artifacts are stored below `infographic/snapshots/<infographic_checksum>/` as `infographic_document_v1.json`, `daily_mlb_feed_4x5.svg`, `daily_mlb_story_9x16.svg`, and `render_manifest_v1.json`.
