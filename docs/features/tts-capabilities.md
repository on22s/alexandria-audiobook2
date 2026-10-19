# TTS capability guidance

Setup → Speech engine describes the selected local or external mode. Changes preview the unsaved selection; generation continues to use saved configuration until Save Configuration. Voice assignments and hidden field values are kept. Unsupported assignments show a warning on Voices; nothing automatically substitutes or saves a voice. Version timelines and ensemble members still undergo native server checks.

The server owns the descriptor and dispatch contract in tts_capabilities.py. Local mode supports custom, clone, LoRA, design and ensemble categories. External mode supports custom and clone voices, and ensembles whose members are supported. Built-in LoRA is described using the same LoRA capability. AA2 returns completed audio files; no incremental streaming or independent speed parameter is promised. External model language support is not inferred.

The read-only descriptor is included in GET /api/config under capabilities.tts_modes. Reading it does not load a model or contact a speech server. Missing or unknown descriptors display unavailable guidance; current settings remain intact and existing server-side refusals remain authoritative.
