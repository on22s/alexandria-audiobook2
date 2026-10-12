# Literal source preservation

This opt-in annotation profile protects ordered words in the **preprocessed
source**, not the original file's bytes or punctuation. Legacy scripts and the
unchecked default retain existing behavior.

Enable “Preserve source words” on the Script tab, pass
`literal_source_preserving: true` to single or batch generation, or use
`--literal-source-preserving` with either annotation CLI. The profile selects
source-preserving prompts and a separate checkpoint identity. Custom speech
expansions belong to later speech preparation; this profile does not add new
number, abbreviation, pronunciation or symbol converters.

The first generated entry carries `_literal_source_policy`: a versioned digest
and count of source words, the preprocessed-source hash, prompt-policy version
and, for CLI publication, input-file hash and front-matter choice. It contains
no source path, credentials or second copy of the book. This metadata travels
with the script when saved or copied. Generation under the default profile
produces an unmarked script; changing profiles does not reuse the other
profile's checkpoint.

Review inherits the marked script's policy. It validates narration before
creating a client, uses label/delivery-only prompts, rejects changed words
during bounded retries, isolates checkpoints, and verifies the final output
after postprocessing. It restores the policy metadata before atomic
publication. A mismatch refuses publication and exits unsuccessfully rather
than printing a clean completion summary. An incomplete review may preserve
the source text and checkpoint without claiming complete review.

Project TTS admission validates the marked script and protected chunk inputs
before engine creation. Chunk provenance distinguishes the original narration
from the existing speakable-entry projection and subsequent speech preparation.
Those existing speech transformations remain permitted: for example `&` may
become “and”. The gate does not compare expanded speech back to literal source
tokens. Editing a protected chunk's narration, mixing policies or retaining
marked chunks after removing their script policy refuses rendering. Regenerate
annotation/chunks to deliberately change the source or profile. Speaker,
delivery and pause edits do not change this word identity.

Scope limits: comparison is case-sensitive, with NFC and typographic-apostrophe
equivalence and preserved lexical marks/joiners. Punctuation, symbols, speaker
accuracy and acoustic fidelity are not certified. Continuous CJK text split
across annotation entries can conservatively refuse. Preprocessing still
performs its established source-health and publisher/front-matter operations;
use `--no-strip-front-matter` when appropriate. This is a policy for the
annotation/review/Project TTS paths, not a security boundary against manually
rewriting both narration and its provenance, and not a policy for unrelated
standalone voice-design/sample calls.
