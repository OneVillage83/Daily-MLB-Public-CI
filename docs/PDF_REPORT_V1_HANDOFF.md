# PDF Report V1 production handoff

Phase 12 now has a production contract, exact-upstream repository, content-addressed artifact publisher, attempt manifest, handler, replay verification, and controller registration.

Public API: `PdfReportPhaseHandler`, `PdfReportRepository`, `assemble_production_pdf_report`, `render_production_pdf_report`, and production artifact publication/verification. Attempt outcomes are `assembled`, `input_failed`, `assembly_failed`, `rendering_failed`, `verification_failed`, and `persistence_failed`.

The repository inserts unsealed relational evidence, reconstructs it, verifies canonical semantic JSON and all render artifacts, seals once, commits, reopens, resolves the exact stored upstream IDs, and repeats verification. Exact replay is idempotent; conflicting replay and artifact tampering fail closed. Failed attempts have a manifest and no successful snapshot.

The pre-existing PDF contracts, renderer, artifact safety code, and focused visual tests were reused. Only the semantic adapter and production persistence/controller surface were added. The report remains `pre_review`; Infographic consumes its exact sealed semantic checksum next.
