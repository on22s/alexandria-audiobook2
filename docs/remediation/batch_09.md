# Batch 09: Audio generation, exports and voice persistence

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#762](https://github.com/on22s/alexandria-audiobook2/issues/762) — Generated WAV validation accepts impossible near-limit RIFF lengths
- [ ] [#763](https://github.com/on22s/alexandria-audiobook2/issues/763) — Generated WAV writer silently converts non-finite samples into full-scale PCM
- [ ] [#789](https://github.com/on22s/alexandria-audiobook2/issues/789) — Smart chapter grouping promotes short dialogue to extra chapter headings
- [ ] [#790](https://github.com/on22s/alexandria-audiobook2/issues/790) — Sentence-ending quotes and Unicode punctuation prevent same-speaker chunk merging
- [ ] [#791](https://github.com/on22s/alexandria-audiobook2/issues/791) — Failed multi-chapter export leaves replaced audio under the previous manifest
- [ ] [#792](https://github.com/on22s/alexandria-audiobook2/issues/792) — Repeated corrupt chunk loads overwrite the earlier recovery backup
- [ ] [#793](https://github.com/on22s/alexandria-audiobook2/issues/793) — Audacity export publishes despite cancellation during track writing
- [ ] [#809](https://github.com/on22s/alexandria-audiobook2/issues/809) — Clone deletion loses WAV when manifest publication fails
- [ ] [#810](https://github.com/on22s/alexandria-audiobook2/issues/810) — Designed voice update commits metadata even when audio replacement fails
- [ ] [#815](https://github.com/on22s/alexandria-audiobook2/issues/815) — Voice suggestions can apply book A casting to book B after an active-book switch
- [ ] [#816](https://github.com/on22s/alexandria-audiobook2/issues/816) — Persona recovery overwrites a newly active book after validating the previous book
- [ ] [#873](https://github.com/on22s/alexandria-audiobook2/issues/873) — Pending-file deletion error escapes external TTS timeout recovery

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
