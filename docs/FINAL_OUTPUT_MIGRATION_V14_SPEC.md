# Final-output migration v14 specification

Migration name: `final_output_pipeline_v1_temporal_persistence`.

The append-only migration adds 11 phase-specific tables, 9 explicit indexes, validation/sealing triggers, and paired immutability triggers for PDF Report, Infographic, Final QC, and Human Review. It also rebuilds the two existing run tables only to admit schema version 14, following the accepted migration convention.

Parent/child structure:

- PDF attempt -> snapshot -> ordered games.
- Infographic attempt -> snapshot -> feed/story variants.
- Final QC attempt -> optional PASS snapshot and ordered checks.
- Human Review immutable record -> consumed attempt evidence.

Exact upstream IDs/checksums, policy identity, canonical JSON, counts, checksums, semantic artifact paths, and active run/attempt ownership are validated. First seal changes only `sealed_at`; every sealed parent and child is update/delete protected. Failed attempts prohibit snapshot identity. Fresh v14 and exact v13-to-v14 upgrade fingerprints must match. Every v1-v13 migration statement and identity remains frozen.

Implemented identity:

- migration checksum: `b55f91f1826442c43b4c6e210188c7ec5f48885d2f5c191a23849bf4aa17b1e0`;
- schema fingerprint: `9586d679479cf38e2e4f238cc3a804ef1700dca629ec744ce43abebaa7df0266`;
- formal v14 statements: 161;
- tables: 11;
- explicit indexes: 9;
- validation/sealing triggers: 17;
- immutability triggers: 22.

The frozen v13 migration remains 155 statements with checksum `9606657f9497cd54444ecb35680f05d7003a0db535d2e9a1fcc594c9bbf63089` and fingerprint `d33d27ba07d21aa35584e0afe3c39334deba30170d761897acb13e9e04a65fee`.
