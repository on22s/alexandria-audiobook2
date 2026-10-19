# TTS operational diagnostics

In Setup → Speech engine, expand TTS timing and support report and select Refresh timings. Download support report exports the bounded observations, without starting a model or job. Records cover this app process across books, retain at most 50 operations, and disappear on restart. Only the last ten are shown in the UI.

Model preparation includes cached lookups. Reference preparation can include nested model preparation. Inference and codec decoding share one measured interval because the library exposes one call. External request time includes server/network work; server-internal stages remain unavailable. Stage totals can overlap across nested calls and concurrent workers; do not add them or treat them as book ETAs. Audio duration counts files validated/written by the engine, including intermediate ensemble renders; it is not a guarantee that a final book was exported. Failed renders can retain observations of earlier completed work.

The report accepts operational enums, counts and measured times only. It excludes text, speaker/book names, paths, URLs, raw exceptions and credentials; existing private troubleshooting logs remain separate. The existing diagnostics bundle size limit is 64 KiB. Missing measurements are null, not zeros or inferred successes. No inference speed or voice-quality improvement is claimed.
