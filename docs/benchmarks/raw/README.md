# Raw benchmark reports

Full `reports/benchmarks/<preflight_id>.json` files, archived here before
they could be cleaned up from the scratch `reports/` directory. Each one
fully encodes the exact fixtures (source text hashes, chunk numbers, entry
ranges) used to produce a row in `THUNDER_COMPUTE.md` / the JSON summary, so
a stage's numbers can be re-verified or rerun later without reconstructing
fixtures from scratch.

Naming: `<stage>_<target>_<date>[_<variant>].json`. `_original` is the run a
`THUNDER_COMPUTE.md` table row currently cites; `_rerun` is a later
validation pass (e.g. after the 2026-07-18 harness/environment fixes);
`_unused-attempt` is a discarded run kept for completeness, not cited
anywhere.

Convention going forward: once a stage's report is folded into
`THUNDER_COMPUTE.md`, copy it here before the scratch `reports/benchmarks/`
copy can be removed. Only 2 of the campaign's 13 stages had their original
reports preserved before this convention started (2026-07-18) - the other 11
need to be re-run and archived here properly, rather than reconstructed from
the doc's prose after the fact.
