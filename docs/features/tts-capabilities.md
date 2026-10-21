# TTS capability guidance

Setup → Speech engine describes the selected local or external mode. Changes preview the unsaved selection; generation continues to use saved configuration until Save Configuration. Voice assignments and hidden field values are kept. Unsupported assignments show a warning on Voices; nothing automatically substitutes or saves a voice. Version timelines and ensemble members still undergo native server checks.

The server owns the descriptor and dispatch contract in tts_capabilities.py. Local mode supports custom, clone, LoRA, design and ensemble categories. External mode supports custom and clone voices, and ensembles whose members are supported. Built-in LoRA is described using the same LoRA capability. AA2 returns completed audio files; no incremental streaming or independent speed parameter is promised. External model language support is not inferred.

The read-only descriptor is included in GET /api/config under capabilities.tts_modes. Reading it does not load a model or contact a speech server. Missing or unknown descriptors display unavailable guidance; current settings remain intact and existing server-side refusals remain authoritative.

## Internal render windows (experimental)

Setup → Audio & timing → Internal render windows can split a long saved line into smaller speech calls and assemble one take. The feature is off by default. Enter an explicit character limit for the service and voice category you intend to use, optionally enter a whitespace-word limit, enable it, and Save Configuration. Blank category limits retain existing rendering. Disabling keeps your draft limits. Settings apply to subsequent renders; existing selected takes stay selected.

Limits count the final spoken text after occurrence and shared pronunciation rules. Internal windows preserve that prepared text and do not edit saved lines, source text, speaker assignments or export pauses. Whole words and attached punctuation must fit; an impossible limit fails the render instead of cutting a word or exceeding the limit. Ensembles use the smallest configured limit across the ensemble and its actual members. A line that already fits retains the existing single-call or native batch path.

Blank sentence, clause and paragraph gap targets preserve natural silence. Explicit targets can add missing silence; removing quiet edges also requires a positive trim cap. Fades and continuation crossfades are separately opt-in. The first and last edges of the parent line are retained. Begin with trimming, fades and crossfades at zero, and listen to consonants, breaths, sentence endings and cadence before adopting a policy. AA2 supplies no automatically qualified backend budgets or recommended cadence values.

Each successful split produces one parent take with a `synthesis_receipt` tied to its audio revision: prepared-text hash, exact window spans and audio hashes, policy, and pre-encoding PCM frame/seam accounting. These PCM counts do not describe final MP3 padding. A failed or cancelled parent retains the previous take and discards private window files. In-flight provider calls may finish before cancellation releases ownership. Completed windows are not retained for resume; resumable synthesis is separate work.
