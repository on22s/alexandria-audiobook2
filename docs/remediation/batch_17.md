# Batch 17: protect evidence and verify completion

Repeat rescoring refuses missing, unreadable or partial checkpoint populations before replacing saved scores. Previously saved run identities must remain represented, and a complete valid retry still publishes current scores. The collector reads UTF-8 artifacts and scoring gold explicitly, preserving non-ASCII rows under legacy text defaults.

Whisper asset verification bounds Git revision and tracked-change probes through the shared subprocess owner, stops/reaps stalled hooks and retains the pinned-source/checksum refusals. The shared capture helper is identical to the independent helper in #934.

The subset watchdog reports success only after observing the session and finding the runner's Completed receipt plus ZIP outputs. An abort takes precedence, missing/empty/legacy observer receipts refuse, and the watchdog preserves the authoritative runner receipt instead of manufacturing or overwriting it.

Validation against main `857542fa`: 21 focused tests pass without skips, including actual rescoring CLI failure/retry artifacts, a cp1252-default encoding simulation, native fsmonitor timeout and process-reaping controls, and native Bash with controlled tmux/notification boundaries. All release shards pass: 3,833 + 1,609 + 1,594 = 7,036 unit-test executions, plus 70 quick API checks; 12 full-mode API cases explicitly skipped. This baseline predates #933's exact-identity/discovery fix, so the execution count includes its pre-existing duplicate fixture discovery and does not claim unique coverage. Native Windows/macOS, desktop notifications, live model downloads, ASR quality and GPU inference are not claimed.

The Batch 9 document's release count is corrected to 6,970, matching its saved post-Batch-8 integration gate logs. This publishes the documentation correction that followed its merged PR.

Tracker: #917.

- [x] Fixes #898
- [x] Fixes #899
- [x] Fixes #901
- [x] Fixes #908
