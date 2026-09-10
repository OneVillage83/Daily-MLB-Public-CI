# TDL-01 sanitized public CI handoff

Updated: 2026-09-09T01:05:03-07:00 (America/Los_Angeles)

This branch is a supplemental public CI execution surface for private Daily-MLB
commit `074fbe1fc4d2d4f3adf293c0335f924954021739`. The private repository remains
the source, architecture, scientific, historical-evidence, and release authority.

The immutable public-safe content commit is
`493648e5cfafa3f51a5599f1538ab72b66407545`. The machine-readable mapping is
`ci-mirror/private-public-map.json`. Its 18 mirrored files have equal private and
public Git blob identities; the canonical manifest SHA-256 is
`831574ad8875ac1f18ac2d219691b07865f62592afef8f46808383a0f1ad9ed4`.

The retained Gate2 ZIP, evidence manifest, automatic restore fixture, private
A1/A2 code, private architecture/scientific documents, raw/provider data,
databases, credentials, logs, generated evidence, and other unevaluated private
files were not copied. The public application baseline is the previously
published pre-A1 commit `c87603ff3f682d8414b646ff990e25bba1fb2251` and is not
equivalent to the private application tree.

The focused workflow tests the exact dependency locks, artifact containment and
evidence-scanner code, offline pybaseball compatibility, Docker verifier contract,
audits, and lock installation. The established `quality` workflow also runs. Its
Docker job is an actual build/runtime/schema/integrity check for the sanitized
public baseline. Because private application code is excluded, it is supplemental
runtime/tooling evidence and not production-equivalent private exact-head proof.

Workflow IDs, final public head, conclusions and executed-step evidence will be
recorded after GitHub Actions completes. Scientific permissions, PIT behavior,
model promotion, registry authority, and production architecture are unchanged.
