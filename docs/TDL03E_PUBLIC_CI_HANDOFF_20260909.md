# TDL-03E sanitized public CI handoff

Updated: 2026-09-09T23:48:26-07:00 (America/Los_Angeles)

This branch is a supplemental public CI execution surface for private Daily-MLB
authoritative head `c4d8575401114a4f5f5ec7923a48e56b96fa4097` on
`codex/ddc6-mlb-consumer-20260909`. The private implementation is
`61944cc2d4c1974c6b3a92a6d790cf9b5b128e80`; the later private commits contain
only receipts, an empty validation trigger, and documentation. The private
repository remains the source, architecture, scientific, evidence, and release
authority.

The approved mirror surface contains 17 previously disclosed dependency,
artifact-containment, scan, compatibility, and Docker-verifier contract paths.
Every path has the same Git blob ID in the private authoritative head and inherited
public commit `875ae18830c24bc1e699f26024fcdac3daf1ff25`. The canonical 17-entry
manifest SHA-256 is
`941f78a42081b00933cd2d07499e659ca1cf2f18ef949d230223361f24b44f9f`.
No private source file needed copying because the sanctioned blobs were already
identical.

The current private Dockerfile differs and is excluded. Private A1/A2/V17/DDC
consumer implementation, migrations, tests, configuration, retained/provider
evidence, fixtures, databases, logs, credentials, generated producer packages,
scientific documents, and all other private paths were not copied. The public
application baseline remains the previously disclosed pre-A1 tree and is not
equivalent to the private application tree.

The established `quality` and `tdl01-sanitized-exact-content` workflows will run
against the exact public branch head. The Docker job is a real build/runtime/schema
check only for the sanitized public baseline. It does not exercise or certify the
excluded private Dockerfile, schema V17, DDC consumer, forecast-window/evaluation
persistence, manual runtime, or publication pipeline.

The exact tested public SHA, run and job IDs, conclusions, counts, and executed-step
evidence will be appended after GitHub Actions completes. Scientific permissions
remain `0 AVAILABLE / 5 BLOCKED / 22 MISSING`; PIT, model promotion, registry,
Recommendation Gate, and production architecture are unchanged.
