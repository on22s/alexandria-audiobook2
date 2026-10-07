# Batch 11: Editor, rendering and playback UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#777](https://github.com/on22s/alexandria-audiobook2/issues/777) — Editor status polling returns 500 for a persisted chunk row accepted by normal listing
- [ ] [#840](https://github.com/on22s/alexandria-audiobook2/issues/840) — Expanded Editor rows update pause controls instead of completed audio actions
- [ ] [#841](https://github.com/on22s/alexandria-audiobook2/issues/841) — Cancelled batch render leaves untouched pending rows marked generating
- [ ] [#842](https://github.com/on22s/alexandria-audiobook2/issues/842) — Saving an Editor row makes polling discard unsaved edits and focus in another row
- [ ] [#849](https://github.com/on22s/alexandria-audiobook2/issues/849) — Paused task activity incorrectly says Working and waiting on the model
- [ ] [#850](https://github.com/on22s/alexandria-audiobook2/issues/850) — Voice suggestion completion blurs the style field being edited
- [ ] [#851](https://github.com/on22s/alexandria-audiobook2/issues/851) — Older Text integrity response can overwrite a newer differences report
- [ ] [#854](https://github.com/on22s/alexandria-audiobook2/issues/854) — Delayed prior-book chunk snapshot is displayed while the new book refresh waits
- [ ] [#858](https://github.com/on22s/alexandria-audiobook2/issues/858) — Clone voice playback failures produce unhandled rejection without UI feedback
- [ ] [#860](https://github.com/on22s/alexandria-audiobook2/issues/860) — Designer saves edited synthesis inputs with the previous preview audio
- [ ] [#914](https://github.com/on22s/alexandria-audiobook2/issues/914) — Batch render completion counts another book’s rows after a post-render book switch

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
