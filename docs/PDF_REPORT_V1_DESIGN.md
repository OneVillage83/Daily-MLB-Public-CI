# PDF Report V1 production design

Phase 12 is a deterministic presentation layer over exact sealed v13 evidence. It does not predict, calculate value, make a recommendation, rank, approve, or publish.

## Frozen production semantics

`DSE_MLB_PDF_REPORT_V1` retains every Rankings game in Daily Slate order. The only decisions are `recommend`, `pass`, and `avoid`; the only rank is `recommendation_rank`. Moneyline is the only predicted V1 market. The report does not contain fixture-era BET/LEAN, Top Confidence, Best Value, expected-run, spread-prediction, or total-prediction fields.

The prediction provider is displayed exactly as retained: `reviewed_analyst`, `DSE_REVIEWED_ANALYST_V1`, and `uncalibrated`. Value fields are copied from exact Value Engine outcome records and are never recomputed.

## Exact lineage

Assembly resolves stored snapshot IDs and checksums for Rankings, Recommendation Gate, Value Engine, Predictions, Matchup Packet, and Data Quality. Historical reads use these stored identities, not latest-for-run substitution. Each game binds the exact ranking entry, gate game, value game, prediction game, packet game, and quality game checksum.

## Existing visual system reused

The accepted ReportLab layout, colors, local fonts, card/table primitives, width handling, invariant metadata, preflight, and page-count logic remain in use. Production adaptation changes semantic labels and fields only. The document remains visibly `pre_review`.

## Artifacts

```text
pdf_report/snapshots/<semantic_checksum>/
  pdf_report_document_v1.json
  daily_mlb_report_v1.pdf
  render_manifest_v1.json
pdf_report/attempts/<run_id>/attempt_<phase_attempt:04d>.json
```

The semantic checksum, PDF SHA-256, byte count, page count, policy/render versions, artifact paths, and exact upstream checksums are persisted and verified after close/reopen. Publication approval is impossible in Phase 12.
