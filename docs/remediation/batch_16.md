# Batch 16: bounded tooling and truthful run receipts

Git file enumeration uses binary NUL-delimited paths so Unicode, newline and quoted names refer to actual files. Release enumeration and cloud source-provenance probes have execution deadlines through the shared subprocess owner; timed-out hooks are stopped and reaped. Clean-checkout admission remains intact.

Corpus planning stops on failed discovery or plan display instead of reporting a successful empty plan. Whisper download success is retained after publication when old-bundle cleanup fails, with an explicit warning and retained backup. The queue parser reads terminal status fields instead of matching failure words in job names. Stage and cached-stage entry points reject missing dependency arguments before shifting or invoking a checker/worker. Artifact commits resolve the repository root consistently and retain their explicit file scope.

Subset reruns clear stale status flags while preserving ZIP artifacts. Runner and watchdog output listings share full filename and byte-size formatting. The structural audit accepts a bare output filename, and CSV collection retains numeric seed zero while distinguishing missing seeds.

Validation against main `857542fa`: 116 integrated focused tests pass without skips. All three release shards pass (4,099 + 1,379 + 1,564 = 7,042 unit-test executions), and 70 quick API checks pass; 12 full-mode API cases are explicitly skipped. The failed second-shard copied cloud fixture was corrected to include its required subprocess owner and the full shard rerun passed. This branch's main baseline predates #933's exact-identity/discovery fix; its execution count includes that baseline's duplicate fixture-class discovery and does not claim unique coverage. Native Git/Bash/filesystem tests use disposable synthetic inputs; no native Windows/macOS, live cloud model, ASR-quality or GPU inference is claimed.

Tracker: #917.

- [x] Fixes #879
- [x] Fixes #880
- [x] Fixes #882
- [x] Fixes #883
- [x] Fixes #885
- [x] Fixes #888
- [x] Fixes #889
- [x] Fixes #890
- [x] Fixes #893
- [x] Fixes #894
- [x] Fixes #896
- [x] Fixes #897
