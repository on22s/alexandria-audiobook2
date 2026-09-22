# Helper review fixes

Both helper review lists are addressed in this branch:

- Object extraction rejects arrays and scalars.
- Long sanitized filenames hash the original input.
- Basic authentication rejects invalid Base64 and compares UTF-8 bytes.
- JSON writes reject nonpositive retries and sync file contents before rename.
- Paired JSON writes reject equivalent target paths.
- Disk probe failures block disk-dependent work.
- LM Studio status parsing skips malformed entries and reports failed commands
  and undecodable output as unavailable.
- ETA reporting falls back to all registered task names.
- Chunk splitting rejects nonpositive sizes.
- Manifest, script, library, and review-checkpoint readers handle the reported
  malformed shapes; invalid stored line counts warn and contribute zero.
- Invalid chunk collections take the existing backup/regeneration path.
- Audio loading, incremental export preflight, and fingerprints share a realpath
  containment check, including symlink escapes.

The new regression module tests rejected inputs, valid controls, preserved write
artifacts, regenerated chunks and backups, and path containment.

Review corrections and limits: `_compute_eta` intentionally mutates shared live
state, which project Rule 17 explicitly allows. Its monotonic guard is preserved.
The earlier age-based file-lock cleanup observation did not establish a reachable
long-held critical section; lock ownership redesign is not part of these fixes.
Audio-library ResourceWarnings remain separate cleanup observations. These fixes
do not certify that every helper works for every input, or certify the full suite.
