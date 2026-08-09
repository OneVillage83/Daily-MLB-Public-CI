# Human Review V1 design

Phase 15 is the hard manual boundary. Contract `DSE_HUMAN_REVIEW_V1` accepts only `approve` or `reject`. Software never authors either decision.

An immutable review binds run/date, exact PASS Final QC snapshot/checksum, exact PDF Report snapshot/semantic/PDF artifact checksums, exact Infographic snapshot/checksum and feed/story artifact checksums, reviewer identifier, reviewed-at time, optional notes, and canonical review checksum. APPROVE is schema- and repository-valid only for exact PASS QC. REJECT is a legitimate terminal human decision and returns succeeded-with-warnings; neither decision publishes anything.

When no unconsumed review exists, the controller raises the dedicated awaiting-human condition before starting an attempt. Phases 1-14 remain completed, Human Review remains pending with no failed or repeated attempts, and resume is safe. `scripts/human_review.py` provides `show-target`, `approve`, and `reject` commands displaying the exact checksum target.
