# Batch 12: Voice and cast UI

Tracker: [#917](https://github.com/on22s/alexandria-audiobook2/issues/917).

Task-only draft: no bug fix or completion is claimed. Add verified fixes to this branch and check tasks only after their changes are committed, pushed and validated.

- [ ] [#830](https://github.com/on22s/alexandria-audiobook2/issues/830) — Concurrent narrator strategy saves can persist an older choice than the dropdown shows
- [ ] [#831](https://github.com/on22s/alexandria-audiobook2/issues/831) — Late narrator preview response replaces status for newer focus selection
- [ ] [#832](https://github.com/on22s/alexandria-audiobook2/issues/832) — Pending cast match mixes old proposals with the newly selected cast and apply target
- [ ] [#833](https://github.com/on22s/alexandria-audiobook2/issues/833) — Batch script queue rebuild erases narrator names for still-selected uploads
- [ ] [#834](https://github.com/on22s/alexandria-audiobook2/issues/834) — Generation failure markup makes loaded-source checks block retry and book actions
- [ ] [#835](https://github.com/on22s/alexandria-audiobook2/issues/835) — Cast-list retrieval failure is displayed as confirmed absence and no selected book
- [ ] [#844](https://github.com/on22s/alexandria-audiobook2/issues/844) — Unchanged Voices save erases clone default style and persona description
- [ ] [#845](https://github.com/on22s/alexandria-audiobook2/issues/845) — Truncated state version IDs silently overwrite distinct selected voices
- [ ] [#846](https://github.com/on22s/alexandria-audiobook2/issues/846) — Late LoRA suggestion response restores old-cast choices under the new cast
- [ ] [#847](https://github.com/on22s/alexandria-audiobook2/issues/847) — Pending voice timeline save can mutate a newly loaded book
- [ ] [#852](https://github.com/on22s/alexandria-audiobook2/issues/852) — Late library response hides a newly created cast and rolls selection back
- [ ] [#853](https://github.com/on22s/alexandria-audiobook2/issues/853) — Bulk cast Apply shows green success when every book failed

For each task: recheck current main, reproduce the failure, implement a surgical fix, verify failure and recovery cases, and record validation/platform limits. Use closing keywords only for implemented fixes.
