# Batch 05: Script generation, review and LLM handling

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

This batch repairs source publication, resume coordinates, task admission, EPUB extraction, source health, and speaker/lexicon handling. The changes preserve existing structural refusal, checkpoint locking, GPU ownership, and retry limits.

- [x] [#794](https://github.com/on22s/alexandria-audiobook2/issues/794) — Source encoding repair records applied=true when structural acceptance refuses output
- [x] [#795](https://github.com/on22s/alexandria-audiobook2/issues/795) — Source health misses even numbers of unmatched curly opening quotes
- [x] [#796](https://github.com/on22s/alexandria-audiobook2/issues/796) — LLM source repair review samples can omit the actual replacement site
- [x] [#797](https://github.com/on22s/alexandria-audiobook2/issues/797) — LLM source repair truncates prior output and leaves applied report on write failure
- [x] [#798](https://github.com/on22s/alexandria-audiobook2/issues/798) — Review checkpoint resume can drop unreviewed source text after an entry split
- [x] [#805](https://github.com/on22s/alexandria-audiobook2/issues/805) — Start Over deletes recovery artifacts before competing task admission refuses it
- [x] [#806](https://github.com/on22s/alexandria-audiobook2/issues/806) — EPUB extraction concatenates adjacent table-cell text into words
- [x] [#807](https://github.com/on22s/alexandria-audiobook2/issues/807) — EPUB TOC labels targeting the same anchor are inserted in reverse order
- [x] [#820](https://github.com/on22s/alexandria-audiobook2/issues/820) — Source repair hint hardcodes a POSIX interpreter path on Windows
- [x] [#821](https://github.com/on22s/alexandria-audiobook2/issues/821) — Three-pass source preprocessing skips publisher removal on CRLF books
- [x] [#822](https://github.com/on22s/alexandria-audiobook2/issues/822) — Initially unknown speaker age and gender never settle to later known traits
- [x] [#823](https://github.com/on22s/alexandria-audiobook2/issues/823) — Malformed pronunciation lexicons silently disable respellings without failure evidence

Regression coverage includes real source-repair output and report artifacts under publication failure, source/output checkpoint coordinates after splits, Start Over racing another admitted task, EPUB table cells and equal-anchor TOC labels, CRLF source preparation, evolving speaker traits, and malformed lexicon diagnostics.

Windows command guidance is verified through platform-boundary simulation on Linux; native Windows execution is not claimed. Full release validation and its counts are recorded in the PR description.
