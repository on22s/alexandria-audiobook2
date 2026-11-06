# Reference cleanup (optional affine gain / DC removal)

## Exact scope
Identity by default. Cleanup only applies a full-length affine transform:
optional DC-offset removal followed by gain 0.25–4.0 (default 1.0), over the
entire clip. No trim, denoise, EQ, highpass, silence removal, speed change, or
other processing. The existing format-normalize / `import_reference_audio`
gate runs first with unchanged thresholds (24 kHz mono, 3–30 s, clipping,
silence, peak); the candidate is measured with the same checks. A candidate
that fails verification or admission is refused at preparation — it is never
presented as ready.

## Waveform integrity (measured) vs quality (unmeasured)
The candidate is verified by exact recomputation from the normalized original
plus the canonical plan: frame count, sample rate and channel must match
exactly, and every sample must equal the expected PCM16 value within 1 LSB of
rounding. Prepare returns and displays original and candidate measurements,
samples checked, max sample difference, clipping, duration and hashes before
acceptance. ASR transcript identity, speaker identity and naturalness are
UNMEASURED — no semantic or quality claim is made.

## Provenance and accept flow
Prepare requires the exact transcript and rights confirmation (same shared
validation as ordinary import, with bounded field lengths) and binds them,
plus source filename/channel count, plan and digests, to the prepared session
via a canonical provenance digest. Accept must echo the exact values; any
transcript, rights, source, channel, gain or DC edit after prepare
invalidates the candidate (server session deleted, listens cleared). Accept
re-verifies the candidate, publishes through a staged file with post-copy
digest check (no overwrite of an existing voice), and appends the manifest
entry as the commit point. A preassigned publication id makes replay after a
crash reconcile to the already-accepted voice instead of minting a second
one. The accepted voice gets a distinct “-cleaned” display name; the current
selection never changes until the user chooses the new voice.

Shared transcript/source validation also applies to ordinary import: transcript
is limited to 4000 characters, source title to 512, source URL to 2048, and
rights basis to 1024. The import form exposes these limits. Import, preparation,
preview discard and acceptance cannot overlap within a modal. Preparing again
discards the previous preview first; a failed discard retains it for retry.
An HTTP 400 acceptance refusal retires the stale preview; temporary contention
(409/503) retains it. A lost acceptance reply may mean publication completed:
the UI retires the uncertain preview, refreshes the library, and warns against
accepting or importing again before checking it. A successful response remains
an acknowledgement even when its body or the subsequent list refresh is lost.

## Durable lineage
On acceptance the raw staged source and normalized original are retained as
immutable files under `.reference-cleanup-lineage/<voice_id>/` beside the
manifest receipt (digests, plan, implementation `reference_cleanup:
reference-cleanup-v1`, PCM16 encoder, source filename/channel count,
verification evidence, transcript/rights binding). Rejecting a candidate
before acceptance touches neither the original selection nor the source stage.
The raw file is named `source.wav` for storage but retains its original container
bytes (which may be MP3, FLAC or OGG); `source_filename` preserves the upload name.
Discard means only "preview removed if present"; already accepted voices remain
in the library. Explicit deletion of an accepted cleaned voice removes its
owned raw/normalized lineage too, using its deterministic voice-id directory,
never receipt-supplied paths. A lineage removal failure is surfaced and keeps
the manifest entry and candidate audio available for a deletion retry.

## Cancellation and limits
Cleanup work is admitted through two full-pipeline slots (copy, normalize,
apply, verify) before expensive decoding, with a fresh 2 GB free-space check
at prepare and accept; an unavailable disk probe is reported as unavailable,
not as a measured shortfall. This is a coarse guard, not a disk reservation.
Cancellation observed before the final commit checks refuses publication,
including after a manifest-lock wait. Cancellation after the manifest write
starts cannot atomically undo that write or an acknowledged publication; a lost
reply must therefore be treated as uncertain, not as proof of failure.
Sessions expire after 1800 s and expired sessions are swept before the
8-session cap is applied. Sessionless/corrupt directories use their modification
time with the same grace interval, and locked active preparations are skipped.
Invalid, expired or corrupt handles are controlled 400 refusals. Clipping
control is conservative: samples that would round beyond PCM16 range refuse,
even if a file could represent them after clipping.

## Hard-crash limitation
Normal exception paths roll back their staged publication files. A process
kill or power loss before the manifest commit can instead leave an unlisted
candidate or lineage directory on disk. These publication remnants are not
automatically swept: deleting lineage by guessing ownership could remove
accepted source evidence. The raw source is upload-capped at 512 MiB and the
normalized audio is duration-bounded, but repeated crashes can accumulate
remnants. They do not become selectable voices; operator recovery must compare
them with the manifest before removal. Accepted lineage is intentionally
retained rather than expired with the preview session, until explicit deletion
of the accepted voice. The preview-directory sweep does not guess ownership
of publication remnants.
