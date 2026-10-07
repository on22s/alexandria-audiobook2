# Batch 05: Script generation, review and LLM handling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#794](https://github.com/on22s/alexandria-audiobook2/issues/794) — Source encoding repair records applied=true when structural acceptance refuses output
- [ ] [#795](https://github.com/on22s/alexandria-audiobook2/issues/795) — Source health misses even numbers of unmatched curly opening quotes
- [ ] [#796](https://github.com/on22s/alexandria-audiobook2/issues/796) — LLM source repair review samples can omit the actual replacement site
- [ ] [#797](https://github.com/on22s/alexandria-audiobook2/issues/797) — LLM source repair truncates prior output and leaves applied report on write failure
- [ ] [#798](https://github.com/on22s/alexandria-audiobook2/issues/798) — Review checkpoint resume can drop unreviewed source text after an entry split
- [ ] [#805](https://github.com/on22s/alexandria-audiobook2/issues/805) — Start Over deletes recovery artifacts before competing task admission refuses it
- [ ] [#806](https://github.com/on22s/alexandria-audiobook2/issues/806) — EPUB extraction concatenates adjacent table-cell text into words
- [ ] [#807](https://github.com/on22s/alexandria-audiobook2/issues/807) — EPUB TOC labels targeting the same anchor are inserted in reverse order
- [ ] [#820](https://github.com/on22s/alexandria-audiobook2/issues/820) — Source repair hint hardcodes a POSIX interpreter path on Windows
- [ ] [#821](https://github.com/on22s/alexandria-audiobook2/issues/821) — Three-pass source preprocessing skips publisher removal on CRLF books
- [ ] [#822](https://github.com/on22s/alexandria-audiobook2/issues/822) — Initially unknown speaker age and gender never settle to later known traits
- [ ] [#823](https://github.com/on22s/alexandria-audiobook2/issues/823) — Malformed pronunciation lexicons silently disable respellings without failure evidence

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
