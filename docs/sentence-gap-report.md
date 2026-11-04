# Internal sentence-gap report (report-only, #1719)

Offline analysis of sentence gaps **inside** one rendered take. This is not
the between-take `dialogue_continuity` gap suggestion, and it never edits
audio.

```bash
python -m sentence_gap_report \
  --audio take.wav --text-file prepared.txt \
  --word-timings word_timings.json --provenance provenance.json \
  --out sentence_gap_report.json
```

Inputs (all bounded, read-only):

- `--audio`: WAV take, ≤64 MiB, ≤120 s, 1–2 channels. Its SHA-256 must be
  `provenance.audio_sha256`; the file is stream-hashed before and after
  decoding (1 MiB blocks, in-stream limit) and the run fails if it changed or
  the path was replaced. Conventional RIFF/WAVE extents are checked on that
  same handle. Decoded float64 allocation is independently capped at 128 MiB,
  including compressed containers accepted by the decoder; the header budget
  is checked before sample decoding. Original bytes are never rewritten.
- `--text` / `--text-file`: prepared text of the take (≤20,000 chars, read
  with a limit+1 byte bound before decoding). Its SHA-256 must be
  `provenance.text_sha256`.
- `--word-timings`: a word-timing **evidence envelope** (bare offset lists do
  not prove transcript or timing provenance and are rejected)::

      {
        "display_text": "<prepared text>",
        "segments": [{"word": str, "start": float, "end": float}, ...],
        "spans": [{"from": int, "to": int, "start": float, "end": float}, ...],
        "duration": float, "provider": str, "language": "en",
        "binding": {"audio_sha256": <pcm digest>, "text_sha256": <text digest>,
                    "provider": str, "language": "en", "payload_sha256": <payload digest>},
        "fingerprint": "<word_timing.get_timing_fingerprint(binding)>"
      }

  `segments` are the originally observed ASR words (as heard, before any
  validated-span filtering) and `spans` must be exactly what the existing
  validator returns for them:
  `word_timing.get_validated_word_spans(display_text, segments, duration)`.
  The envelope is accepted only when `display_text` equals the analysed text,
  the binding audio/text digests match the actual PCM/text being analysed,
  provider/language agree with provenance, the fingerprint matches the
  binding, and the duration matches the audio. Coalesced multiword spans
  wholly inside one sentence remain valid; spans straddling a sentence
  boundary make it ambiguous and are skipped. A cache that omits
  `segments`/`binding` (for example an Editor word-timing cache without the
  original observed words) fails closed rather than being relabelled with
  the current take's provenance. No ASR is run and no `word` is synthesized
  from authored offsets.
- `--provenance`: `{audio_sha256, text_sha256, provider, language:"en"}`.
  Missing or stale provenance rejects the run. A stale file digest rejects
  after the first streaming hash, before container validation/decoding: when
  an input is both stale and malformed, the identity error takes precedence.
  Accepted inputs retain the post-decode hash and path/size mutation fences.
  A `file_sha256` in provenance
  is carried into the report only when the CLI itself verified the audio file
  bytes; the decoded-PCM library API rejects an unverified file claim.
- `--explicit-pause-after`: comma-separated sentence indices carrying an
  explicit user pause; these are zero-based ordinals from exported
  `get_sentence_spans(text)`. Those boundaries are always skipped and preserved.
  The library API accepts only an iterable of indices (a falsy scalar such as
  `0` is rejected, never silently treated as "no pauses").
- `--out`: report JSON, at most 256 KiB measured on the exact emitted UTF-8
  bytes (indent=2, LF), published via a unique temp file plus an atomic
  rename. The output path must not alias any input file (audio, text file,
  word-timings or provenance); cancellation immediately before publication
  and any write/replace failure leave the prior good report untouched.
  Transient Windows sharing/access errors retry at most five times with
  50/100/200/400 ms backoff and cancellation before every attempt. Cleanup is
  best effort: a locked owned temp is diagnosed on stderr, never silently
  presented as removed. Existing `<out>.tmp` files are never used.

The report contains, per sentence boundary: source boundary/reason, original
character coordinates, ASR search window, raw measured quiet interval and
separate safe quiet interval
(channel-aware: every channel must be below −45 dBFS; never outside the
search window or the audio), proposed pause and extension in ms, and an
explicit skip reason when no proposal is made. Within-word dips, mismatched
transcript words, uncertain/overlapping/ambiguous boundaries, missing
neighbour timing, and quiet intervals too close to a word edge are skipped,
never edited. Unmatched observed words overlapping a search window also
force a skip, even if the shared span validator filtered them out. Only the
left sentence owns the current boundary certainty; a missing terminator on
its right neighbour does not invalidate it. Longer silence that abuts word
edges can still yield a proposal from its safe interior (≥30 ms from each
edge, ≥120 ms). The longest eligible safe interval is selected; its raw
measurement remains separate. Coordinate-map insertions use the safe start. **ASR timestamps
alone never authorize an insertion, speech removal or phoneme crop.**

`GapCoordinateMap` defines original↔derived time maps (identity/no-op and
composition included) for any future application. Maps whose finite insertions
would overflow or saturate the derived duration are rejected at construction.
Any separately approved later application must use derived playback/export
artifacts, preserve samples outside affected regions, and share timeline
accounting with `tts.get_pause_duration_ms` / `tts.iter_timeline` under
`export_limits.MAX_PAUSE_MS`. No such application exists in this slice.

Tests use synthetic audio only; no ASR/synthesis is run and no private
voice/text/audio is uploaded.

Identity and evidence preparation:

- `provenance.audio_sha256` is the file-byte SHA-256 at the CLI input. In the
  library and returned provenance it is the decoded-PCM digest; the CLI also
  returns its verified `file_sha256`. These identities are deliberately distinct.
- `get_pcm_digest(pcm)` hashes decoded samples as little-endian float64
  (`<f8`), C order, shape (frames, channels), at the stored rate and channels.
  Mono (frames,) has the same bytes as (frames, 1). For files, decode with
  `soundfile.read(dtype='float64', always_2d=True)`. Do not hash file bytes,
  a pre-quantization render buffer, planar channels or ASR-resampled audio as
  PCM identity. Integer library arrays are rejected: let the decoder normalize
  them first. The report's `pcm_digest_format` records this representation.
- The binding's `payload_sha256` covers the six envelope fields
  `display_text, segments, spans, duration, provider, language` serialized as
  UTF-8 JSON with sorted keys, compact separators, ensure_ascii=False and
  allow_nan=False. The existing timing fingerprint hashes the complete binding,
  including this payload digest. The report carries the payload digest for
  audit. This detects inconsistent edits; it is integrity, not authentication
  or an attestation of the observations' producer.

To prepare an envelope from **already captured** observed segments, use the
public read-only helper. It runs the shared span validator, never ASR:

```python
import hashlib
import json
import soundfile as sf
from sentence_gap_report import get_word_timing_evidence

text = open('prepared.txt', encoding='utf-8').read()
pcm, rate = sf.read('take.wav', dtype='float64', always_2d=True)
segments = json.load(open('observed-segments.json', encoding='utf-8'))
evidence = get_word_timing_evidence(text=text, pcm=pcm, sample_rate=rate,
                                    segments=segments, provider='your-observed-provider')
with open('word_timings.json', 'w', encoding='utf-8') as output:
    json.dump(evidence, output, ensure_ascii=False, allow_nan=False)
with open('take.wav', 'rb') as source:
    digest = hashlib.sha256()
    for block in iter(lambda: source.read(1024 * 1024), b''):
        digest.update(block)
    file_digest = digest.hexdigest()
provenance = dict(audio_sha256=file_digest,
                  text_sha256=hashlib.sha256(text.encode('utf-8')).hexdigest(),
                  provider=evidence['provider'], language='en')
with open('provenance.json', 'w', encoding='utf-8') as output:
    json.dump(provenance, output)
```

Use bounded input acquisition for untrusted files; the CLI independently enforces
its caps and identity/mutation checks. A stripped Editor cache or
`render_word_timing` result has discarded the original segments and cannot be
upgraded by inventing observed words from authored offsets. There is no Editor
integration or new ASR execution in this feature.

Measurement and resource limits:

- Provider word windows may touch, leaving no measurable inter-word search
  interval; such boundaries are reported as `no_gap_in_asr_search_window`.
  The search never widens into a word to recover a pause the provider omitted.
- Quiet means absolute channel-max RMS below −45 dBFS, not proof of absent
  speech. A take whose samples are all below that floor is skipped as
  `take_below_quiet_threshold`; there is no relative loudness or SNR model.
- Text (20,000 characters / 80,000 UTF-8 bytes), spans (2,048), observed
  segments (4,096), input JSON (1 MiB), and emitted report (256 KiB) are
  independent caps. The report cap usually becomes binding at a few hundred
  boundaries (roughly 300, depending on coordinates, reasons and measurements).
  It is never truncated; an early conservative byte floor avoids obviously
  oversized builds and the exact final/publish byte checks remain authoritative.
- Cancellation is polled between validation passes, per digest/RMS chunk and
  boundary, after the shared transcript matcher returns, and before each
  publication attempt. The shared matcher is not interruptible internally.
