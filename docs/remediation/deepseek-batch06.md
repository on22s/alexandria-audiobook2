# DeepSeek bug fixes — batch 6

Approved scope: core UI state and stale replies. Preserve backend selection order, remote cost admission, cancellation ownership, user presets and independent drafts.

- [x] #954 Persist an audio publication revision through the status journal and use it for playback retention and fresh URLs. Legacy rows also compare rendering inputs.
- [x] #955 Serialize file/existing/saved-book selection and reject stale UI acknowledgements and failures.
- [x] #956 Invalidate batch preparation if its queue, narrator fields or collision policy change before dispatch.
- [x] #957 Bind connection diagnostics and button restoration to the tested profile and latest request.
- [x] #958 Select the attribution default recipe and its correct prompt shape while preserving user preset definitions.
- [x] #959 Compare acknowledged answer text so later unsent drafts are retained independently.
- [x] All six UI regression tests fail against pre-fix code; 92 focused tests pass, including actual audio publication, journal recovery and controlled Node UI handlers.
- [ ] Complete ready.sh and publish the draft PR.

Node DOM harnesses verify handler behavior; native browser playback and GPU inference were not tested.

Full ready.sh passed: 7,057 unit tests, 70 API checks; 12 API generation checks skipped.
