# PDF Report V1 persistence specification

Schema v14 owns `pdf_report_attempt_evidence`, `pdf_report_snapshots`, and `pdf_report_games`. Attempts bind the exact ordered v13 upstream identity, policy JSON/checksum, phase-input checksum, outcome, warnings, and immutable manifest. Successful snapshots bind canonical JSON, semantic checksum, PDF checksum/size/page count/preflight, three semantic artifact paths, game/warning counts, timestamps, and one-time sealing.

The exact semantic artifact directory is `pdf_report/snapshots/<semantic_checksum>/`. First seal may modify only `sealed_at`. Child rows and sealed snapshots are immutable. Active controller attempt, exact upstream snapshot/checksum ownership, counts, contiguous ordinals, and artifact paths are schema validated.

Repository reconstruction is relational and exact-ID anchored. Top-level canonical JSON is comparison evidence, not the sole source of truth. A successful write is verified before seal, after seal, and after reopening SQLite.
