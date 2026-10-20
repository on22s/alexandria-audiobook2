# Dated experiment artifacts and drivers

This directory retains historical measurements, their producers and logs.
The [2026-10-20 archival catalog](../docs/remediation/audit_20261020_historical.md)
identifies all 18 retained driver observations and the dated probe and identity
verifier boundaries. Preserve those originals. A completion message, existing
artifact or old zero exit is insufficient evidence for a current campaign.

Before reusing a listed driver, create a separately named current runner and
output directory using the contracts in that catalog. In particular,
`experiments/three_pass_grimgar25_20260815/run.sh` can dispatch after readiness
exhaustion or a failed directory change. Its worker exit still propagates.
The self-hashed `evidence/thunder_results_20260930/verify_identity.py` belongs to
its recorded runtime; it hashes `ab_test_runtime/ecapa`, while the current
worker resolves `cache/ecapa`. This archival disposition does not recertify
those historical results against the current worker.
