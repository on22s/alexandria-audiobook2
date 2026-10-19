        // ── Field help (ⓘ tooltips) ───────────────────────────────────
        // One entry per static field id across workflow tabs, advanced tabs and
        // shared dialogs. `what` says what the control does. `recommend` appears
        // only where a measurement backs it, and then `source` names that
        // measurement (a PR, a RECIPES.md section, or the code that enforces
        // it); everything else states the default and when to change it.
        // tests/test_field_help.py fails if a visible control on those tabs
        // has no entry, or if a recommendation has no source.
        /** @type {Object<string, {what: string, recommend?: string, source?: string}>} */
        const FIELD_HELP = {
            'setup-settings-category': {what: 'Choose which Setup category to display. Edits in other categories are retained. Save Configuration applies all edited categories.'},
            'setup-settings-search': {what: 'Find available Setup settings by their labels and explanations, then jump to the original control. Does not search field values or save settings.'},
            // Setup: LLM connection
            'llm-mode': {what: 'Which LLM profile is active: Local (this machine) or Remote (a server elsewhere). Each profile keeps its own address, model and limits.'},
            'llm-url': {what: 'Address of the OpenAI-compatible server (LM Studio, llama.cpp, Ollama, a hosted API), ending in /v1.'},
            'llm-key': {what: 'API key sent with each request. Local servers usually accept any value. Use env:NAME to read it from an environment variable instead of saving it.'},
            'llm-model': {what: 'Model name sent with each request. It must match a model the server has loaded or offers.'},
            'llm-context-length': {what: 'Profile token budget for prompt and reply. A lower detected context still wins. Reducing this caps requests; it does not unload the model or free GPU memory.'},
            'llm-max-input-tokens': {what: 'Largest prompt this profile may send. Blank inherits the global input ceiling. Lower it for providers with a smaller input limit.'},
            'llm-max-output-tokens': {what: 'Largest reply this profile may request. Blank means no profile cap; each task still has its own safety ceiling.'},
            'llm-request-timeout': {what: 'How long to wait for a reply before treating the request as failed. Blank uses the app default. Raise it for slow models on long chunks.'},
            'llm-connect-timeout': {what: 'How long to wait to open a connection. Blank uses the request timeout.'},
            'llm-request-interval': {what: 'Minimum seconds between requests. Use it to stay under a provider rate limit; 0 sends as fast as replies arrive.'},
            'llm-api-retry-limit': {what: 'How many times a failed API call (connection error, rate limit, server error) is retried before giving up. Blank uses each stage’s default.'},
            'llm-on-api-exhaustion': {what: 'What happens when API retries run out: fail that chunk, or pause the run so you can fix the server and Resume from the same request.'},
            'llm-retry-initial-delay': {what: 'Wait before the first API retry, in seconds. Later retries multiply it by the backoff multiplier.'},
            'llm-retry-multiplier': {what: 'Each API retry waits this many times longer than the last.'},
            'llm-retry-max-delay': {what: 'Upper limit on the wait between API retries, in seconds.'},
            'llm-retry-jitter': {what: 'Randomises each retry wait by ± this fraction so parallel workers do not retry at the same moment.'},
            'llm-provider-headers': {what: 'Extra HTTP headers (a JSON object) sent with every request, for providers that need them. Leave empty otherwise.'},
            'llm-transport': {what: 'Send requests to the server above, or Manual: each request appears on the Script tab for you to answer elsewhere and paste back. The reply is checked exactly as a model’s would be.'},
            'llm-on-this-gpu': {what: 'Whether the LLM shares this machine’s GPU with audio generation. If not, script work can run while audio renders instead of waiting for the card. Auto treats a local address as yes and a remote one as no.'},
            'llm-reasoning-effort': {what: 'Sent as reasoning_effort on every request from this profile, for models that think before answering. Server default leaves it unset.', recommend: 'medium on a base model when time allows: minor-character attribution rose 57 → 63% at about 3× the time.', source: 'RECIPES.md → Prompt (reasoning on the base model)'},
            'llm-provider-extra-body': {what: 'Extra JSON fields merged into every request body, for provider-specific options. Output-size fields here are still capped by the task limits.'},
            'llm-failover': {what: 'If this profile gives up (retries exhausted or a content-policy refusal), the rest of the run switches to the other profile. You are asked to confirm first when that would use a billed endpoint.'},
            'llm-ssh': {what: 'SSH host alias for the remote machine. Only the Optimize button uses it, to reconfigure a remote LM Studio.'},

            // Setup: TTS and rendering
            'tts-mode': {what: 'Local loads the built-in Qwen3-TTS model on this machine. External sends lines to a running Gradio TTS server.'},
            'tts-url': {what: 'Address of the external Qwen3-TTS Gradio server.'},
            'tts-external-urls': {what: 'Extra external TTS servers, one per line. Batch rendering spreads lines across them. Empty uses only the address above.'},
            'tts-external-timeout': {what: 'A line that takes longer than this on an external server is marked failed instead of waited on.'},
            'tts-device': {what: 'Where the local TTS model runs. Auto picks the best available GPU, else the CPU.'},
            'tts-language': {what: 'Language the audio is spoken in. It does not change script generation; edit the prompts for that.'},
            'parallel-workers': {what: 'How many TTS requests run at once. More can be faster on a large GPU or server pool, but uses more memory.'},
            'batch-seed': {what: 'Seed for audio generation. A fixed number makes a re-render of the same line repeatable; blank or -1 is random.'},
            'compile-codec': {what: 'Compiles the audio codec with torch.compile.', recommend: 'Leave off: it currently has no effect on render speed.', source: '#1113 / #1120 measurement'},
            'batch-group-by-type': {what: 'Renders lines grouped by voice type for GPU efficiency, instead of in book order.'},
            'sub-batch-enabled': {what: 'Splits a batch by text length so short lines are not padded to the length of long ones.'},
            'tts-max-new-tokens': {what: 'Upper limit on audio generated per chunk. Too low cuts long lines short; lower values fit bigger batches.'},
            'sub-batch-min-size': {what: 'A batch is only split once it has at least this many chunks.'},
            'sub-batch-ratio': {what: 'A batch is split when its longest line is more than this many times its shortest.'},
            'sub-batch-max-items': {what: 'Hard cap on lines per sub-batch. 0 picks a size from the VRAM estimate.'},
            'pause-between-speakers': {what: 'Silence inserted when the speaker changes, in milliseconds, when audio is merged.'},
            'pause-same-speaker': {what: 'Silence inserted between consecutive lines by the same speaker, in milliseconds.'},
            'reproducible-takes': {what: 'Regenerating an unchanged line gives the same take, and the Editor shows a Different take button for a new one. Covers one-at-a-time renders only; batch renders share one random state.'},
            'design-text-min-chars': {what: 'Shortest character line a persona voice is designed from. Very short lines like "Okay" make a voice hard to judge and a weak clone reference.'},
            'design-text-max-chars': {what: 'Longest character line a persona voice is designed from, up to 500. Longer lines are cut at a sentence end.'},

            // Setup: LLM generation settings
            'max-tokens': {what: 'Starting reply size for LLM requests. A verified larger context can raise it up to each task’s safety ceiling; a profile output ceiling caps it.'},
            'max-input-tokens': {what: 'Largest prompt any profile may send. Blank keeps the existing context checks. A profile’s own input ceiling overrides it.'},
            'temperature': {what: 'Sampling randomness for single-pass generation and review. 0 always picks the most likely token. The three-pass steps have their own temperatures below.'},
            'top-p': {what: 'Nucleus sampling: only tokens within this cumulative probability are considered.'},
            'top-k': {what: 'Only the K most likely tokens are considered. 0 disables it.'},
            'min-p': {what: 'Tokens less likely than this fraction of the top token are dropped. 0 disables it.'},
            'presence-penalty': {what: 'Discourages repeating tokens that already appeared. 0 disables it.'},
            'banned-tokens': {what: 'Comma-separated tokens the model may not produce. To turn reasoning off, use Reasoning effort rather than banning the think tag.'},
            'merge-narrators': {what: 'During review, joins neighbouring NARRATOR lines that share a delivery note into one longer line. Leave off for finer per-line voice control.'},

            // Setup: three-pass annotation
            'tp-chunk-size': {what: 'How much book text Step 1 sends per request when splitting it into lines. Bigger pieces mean fewer requests, but a longer reply that is redone whole if one word comes back wrong.'},
            'tp-attribute-batch-size': {what: 'How many lines Step 2 sends together when deciding who speaks.', recommend: '25, the size every attribution measurement used.', source: 'RECIPES.md → Prompt (product batch 25)'},
            'tp-attribute-context-chars': {what: 'Extra book text shown before and after each Step 2 request, as evidence only. Each 1,000 characters adds about 300 tokens per request.'},
            'tp-instruct-context-chars': {what: 'Extra book text shown around each Step 3 (delivery notes) request.', recommend: '0. In a blind review the default never lost to the old behaviour, and no non-zero value was better.', source: '#1109 evaluation'},
            'tp-attribute-target-chars': {what: 'Size Step 2 requests by characters instead of line count. 0 keeps line-based batching.'},
            'tp-instruct-target-chars': {what: 'Size Step 3 requests by characters instead of line count. 0 keeps line-based batching.'},
            'tp-segment-output-ratio': {what: 'Step 1’s reply budget as a multiple of the text sent. Raise it for models that write a lot of formatting around the text.'},
            'tp-segmentation': {what: 'How Step 1 finds the dialogue: from quote marks, from the model, or both.', recommend: 'Quote marks first, model for the rest. Every three-pass result was measured with it, and books with em-dash or unmarked dialogue need it.', source: 'RECIPES.md → Pass 1: dialogue detection'},
            'tp-pass1-format': {what: 'For chunks the quote marks don’t settle: rewrite the text (Full) or label numbered sentences (Compact, experimental, which falls back to Full on any unusable reply).', recommend: 'Full text. Compact used fewer tokens but scored a little lower on clean books.', source: '#744 / #1123'},
            'tp-delivery-direction': {what: 'For the Step 3 notes: keep today’s notes (Current) or ask the model to also say how each line answers the one before it and to mark pauses (a beat, a cut-off, trailing off) that become silence between lines. The experimental modes are being evaluated in a blind listening test and are off by default.', recommend: 'Current notes. The experimental modes need the default Step 3 prompt preset, and changing this restarts an unfinished book instead of resuming it.', source: '#1258'},
            'tp-quoted-must-be-spoken': {what: 'Rejects a Step 1 reply that types quoted text as narration.'},
            'tp-unquoted-must-be-narrator': {what: 'Rejects a Step 1 reply that types unquoted text as speech.'},
            'tp-keep-whole-batch': {what: 'Keeps a Step 2 batch whose only problems are names written in lower case in the book or an unnamed quoted line. Those lines are marked for review instead of re-asked.'},
            'tp-group-rule': {what: 'Unnamed dialogue from a group on the cast list (e.g. THE GUARDS) is assigned to that group instead of UNKNOWN.'},
            'tp-speaker-traits': {what: 'Also records gender, age group and ageless status for each line. This adds to every Step 2 reply; check the inferred traits against the book.'},
            'tp-segment-temperature': {what: 'Sampling randomness for Step 1 (splitting text into lines).'},
            'tp-attribute-temperature': {what: 'Sampling randomness for Step 2 (who speaks).'},
            'tp-instruct-temperature': {what: 'Sampling randomness for Step 3 (delivery notes).'},
            'context-rescue-windows': {what: 'When a line can’t be attributed from its chunk, it is retried with this much surrounding text (characters), smallest first.'},
            'context-rescue-retries': {what: 'Attempts per context-rescue window before moving to the next, larger one.'},
            'lmstudio-optimize-toggle': {what: 'Reloads LM Studio with tuned settings: locally context 8192 and parallel 1 (kept low for VRAM safety); remotely context 98304 and parallel 2 over SSH. Off restores LM Studio’s defaults.'},

            // Setup: prompts
            'prompt-preset-select': {what: 'How Step 2 asks the model who speaks each line. michel2_full is the default. michel2_spoken labels only the quotes and fills narration in code. If you serve an attribution adapter, keep the prompt it was trained on (usually michel2_full).', recommend: 'michel2_spoken on Qwen3-14B without an adapter: +6.5 points on a local gold set (p = 0.001) with about 3× less Step 2 time. Not yet measured on other models.', source: '#1171 / #1176 A/Bs'},
            'pass1-prompt-preset-select': {what: 'Prompt preset for Step 1 (splitting text into lines). Default uses the checked-in prompt.'},
            'pass1-system-prompt': {what: 'Step 1 system prompt. Edit only to customise; the text must still come back word for word.'},
            'pass1-user-prompt': {what: 'Step 1 user prompt template. It must keep {chunk}.'},
            'system-prompt': {what: 'Attribution instruction block. Default-shaped prompts must keep {roster} and {batch}; passage-shaped prompts (michel, michel2…) get the roster and marked passage added around them.'},
            'user-prompt': {what: 'Attribution user instruction. The pipeline fills {roster} and {batch}.'},
            'prompt-example': {what: 'Worked example shown to the model before the roster. Used by the michel2_shot preset.'},
            'pass3-prompt-preset-select': {what: 'Prompt preset for Step 3 (delivery notes). Default uses the checked-in prompt.'},
            'pass3-system-prompt': {what: 'Step 3 system prompt for writing delivery notes.'},
            'pass3-user-prompt': {what: 'Step 3 user prompt template. It must keep {batch}.'},
            'review-system-prompt': {what: 'Instructions for the review pass: which mistakes to fix and how.'},
            'review-user-prompt': {what: 'Template for each review batch. It must keep {context} and {batch}.'},
            'persona-system-prompt': {what: 'System instruction for voice-persona generation, shared by basic and advanced modes.'},
            'persona-user-prompt': {what: 'Template for a basic persona. It uses {speaker}, {narrator_context} and {sample_lines}.'},
            'persona-reference-chars': {what: 'How much character evidence the advanced persona compilation may send per request. The model’s context may force smaller requests.'},
            'persona-unknown-network-context': {what: 'Use the allowance above for network models whose context size is unknown. Turn on only if the provider supports large requests; rejections fall back to smaller batches.'},
            'persona-advanced-prompt': {what: 'Template for the advanced persona compilation. It uses {character_ref}.'},

            // Script tab
            'script-batch-mode': {what: 'Generate scripts for several books in one run instead of one.'},
            'file-upload': {what: 'The book to annotate: plain text, Markdown or EPUB.'},
            'existing-upload-select': {what: 'Reuse a book uploaded earlier instead of uploading it again.'},
            'script-first-person-narrator': {what: 'For books narrated in the first person: the narrator’s exact character name, so their narration and dialogue get one voice. Leave empty otherwise.'},
            'script-batch-files': {what: 'The books for a multi-book run: text, Markdown or EPUB.'},
            'script-collision-policy': {what: 'What a multi-book run does when a book already has a saved script: skip it, save a new version, or back up and replace it.'},
            'script-batch-build-cast-lists': {what: 'Builds a cast list for each book that has none first, one extra request per book, so unnamed speakers get their own label instead of UNKNOWN.', recommend: 'On: in an A/B on 5 PDNC books, unnamed speakers went from 0% to 72% correct and named speakers from 69% to 93%.', source: '#653 / #728 cast-list A/B'},
            'review-batch-mode': {what: 'Review several saved books in one run instead of the current one.'},
            'review-dedupe-speakers': {what: 'Merges different names for the same character (e.g. a first name and a full name) into one speaker and voice.'},
            'script-strip-front-matter': {what: 'Removes known non-story boilerplate (translator’s notes, tables of contents) before the text is sent to the LLM.'},
            'script-literal-source-preserving': {what: 'Keeps the book’s exact words through annotation and review. Numbers and symbols may still be expanded later for speech.'},
            'review-force-rerun': {what: 'Review again even if nothing changed since the last completed review.'},
            'review-context-window': {what: 'Neighbouring lines shown on each side of every reviewed line, so review can fix boundaries and speakers using the surrounding scene.'},
            'review-batch-find-nicknames': {what: 'Before merging names, discovers each book’s nicknames and shares them across the series.'},
            'review-batch-bidirectional': {what: 'Runs nickname discovery forward and then backward, so early books pick up names that are explained later. Needs nickname discovery; about twice the time.'},
            'review-batch-context-window': {what: '0 runs the standard review. Above 0, each line is reviewed with that many neighbouring lines on each side.'},
            'speaker-review-source': {what: 'Which speaker labels to check: the current script or an attribution checkpoint.'},
            'speaker-review-reference': {what: 'A saved script to compare the labels against, used as a provisional reference.'},
            'speaker-review-max-pairs': {what: 'Most label pairs sent for review in one run (up to 20).'},
            'speaker-review-partial': {what: 'Allow reviewing labels from an attribution checkpoint that has not finished.'},
            'speaker-review-consent': {what: 'Confirms that the shown book and reference excerpts may be sent to this model endpoint. Remote providers may charge for it.'},
            'save-script-name': {what: 'Name to save the current script under in the script library.'},

            // Voices tab
            'voices-state-filter': {what: 'Show all characters, or only those with or without state cards (per-state voices, e.g. a time skip).'},
            'voices-hide-ready': {what: 'Hide characters already marked ready.'},
            'narrator-strategy': {what: 'How the narrator voice is chosen: one global voice, or varied by scene focus, chapter, focus character, or the character’s gender and age.'},
            'narrator-focus': {what: 'Character whose narrator variant is previewed.'},
            'narrator-version': {what: 'Narrator voice version to preview.'},
            'voices-scope': {what: 'Assign voices only to characters that have none, or to every character (replacing current voices).'},
            'voices-keep-in-library': {what: 'Save the current voices to the library before they are replaced.'},
            'advanced-persona-toggle': {what: 'Advanced persona mode: gathers evidence about each character across the whole script before writing the persona, instead of one sample.'},
            'persona-batch-size': {what: 'Script lines per persona-evidence request in advanced mode.'},
            'persona-context-lines': {what: 'Number of sample spoken lines per character included in the persona prompt.'},
            'persona-context-custom': {what: 'Custom number of sample spoken lines per character included in the persona prompt.'},
            'persona-recovery-speaker': {what: 'The character whose persona you are recovering by hand.'},
            'persona-recovery-json': {what: 'The persona JSON reply you got from another LLM, pasted here to validate and save.'},
            'persona-recovery-state': {what: 'Save the recovered persona for the base character or one of its states.'},
            'persona-recovery-samples': {what: 'Dialogue samples included when you copy the persona prompt.'},
            'persona-recovery-narration': {what: 'Narration context included when you copy the persona prompt.'},
            'cast-select': {what: 'The saved cast (character → voice mapping) applied to this book.'},

            // Editor tab
            'chk-drift-only': {what: 'Show only chunks the drift check flagged as sounding unlike their speaker’s reference voice.'},

            // Result tab
            'm4b-title': {what: 'Book title written into the M4B metadata.'},
            'm4b-author': {what: 'Author written into the M4B metadata.'},
            'm4b-narrator': {what: 'Narrator credit written into the M4B metadata.'},
            'm4b-year': {what: 'Year written into the M4B metadata.'},
            'm4b-description': {what: 'Short description written into the M4B metadata.'},
            'm4b-cover-input': {what: 'Cover image embedded in the M4B.'},
            'm4b-per-chunk': {what: 'One M4B chapter per chunk instead of grouping by detected chapter headings.'},
            'm4b-require-ready': {what: 'Refuse to export until every speaker is marked ready.'},
            'chapter-template': {what: 'Filename pattern for chapter files. Fields: {chapter_number} {chapter_name} {book_name} {series_name} {volume_number}. The extension is added for you.'},
            'chapter-template-preset': {what: 'Saved filename patterns. Pick one to fill the template.'},
            'chapter-padding': {what: 'Zero-padding for chapter numbers in filenames (1, 01 or 001).'},
            'chapter-format': {what: 'Audio format of the chapter files.'},
            'chapter-book-name': {what: 'Value used for {book_name} in filenames.'},
            'chapter-series-name': {what: 'Value used for {series_name} in filenames.'},
            'chapter-volume': {what: 'Value used for {volume_number} in filenames.'},
            'chapter-selection': {what: 'Export only these chapters, e.g. 1, 3, 5-7. Empty exports all.'},
            'chapter-per-chunk': {what: 'Use the same chapter detection as the M4B export (one chapter per chunk).'},
            'chapter-changed-only': {what: 'Export only chapters whose audio changed since their last export. Turn off for a complete set.'},
            'chapter-require-ready': {what: 'Refuse to export until every speaker is marked ready.'},

            // Advanced tabs and shared dialogs
            'manual-llm-reply': {what: 'Paste only the answer to the current manual request. Submit checks it against that request before the waiting task continues.'},
            'design-description': {what: 'Describe the voice to create, such as its tone, accent and delivery. Generate a new preview after changing this description.'},
            'design-sample-text': {what: 'The words spoken in the designed-voice preview. Changing this text requires a new preview before saving.'},
            'design-voice-name': {what: 'Name used to save this designed voice so it can be selected again.'},
            'design-alias-select': {what: 'Link this voice to an existing character alias when saving it. None leaves it without an alias link.'},
            'prep-batch-mode': {what: 'Prepare several audio files as separate training datasets. Batch mode uses the shared language, confidence, noise and diarization settings.'},
            'prep-audio-file': {what: 'WAV or MP3 recording to transcribe and split into a voice-training dataset.'},
            'prep-model': {what: 'Optional primary GGUF model path used for annotation during preparation. Blank uses the configured default.'},
            'prep-fallback-model': {what: 'Optional fallback GGUF model path for annotation during preparation.'},
            'prep-source-file': {what: 'Optional EPUB or text file matching the recording, used for source-guided chunking and alignment recovery.'},
            'prep-source-threshold': {what: 'Minimum source-match ratio from 0 to 1. Matching chunks use source spelling; lower-scoring chunks are dropped unless Keep unaligned chunks is enabled.'},
            'prep-keep-unaligned': {what: 'Keep chunks that fail source matching instead of dropping them. Their text may need inspection before training.'},
            'prep-source-start': {what: 'Word position in the source text where alignment should begin.'},
            'prep-source-start-text': {what: 'Find this text in the source and use it as the alignment starting point.'},
            'prep-no-auto-anchor': {what: 'Disable automatic selection of a starting anchor in the source text.'},
            'prep-chunk-size': {what: 'Target duration in seconds for audio chunks produced during preparation.'},
            'prep-min-chunk-duration': {what: 'Shortest audio chunk to keep, in seconds. Shorter chunks are discarded.'},
            'prep-batch-size': {what: 'Number of chunks grouped into an LLM annotation batch during single-file preparation.'},
            'prep-resume': {what: 'Resume preparation using the existing dataset_temp working files instead of starting the single-file run from scratch.'},
            'prep-enrich-with-llm': {what: 'Run the optional LLM enrichment stage and show its speaker, narration-style and emotion settings.'},
            'prep-allow-cpu-fallback': {what: 'Permit enrichment on the CPU when GPU support is unavailable. CPU enrichment can take substantially longer.'},
            'prep-llm-model-path': {what: 'GGUF model path for the optional enrichment stage.'},
            'prep-enrich-speaker': {what: 'Include speaker-attribution information in LLM enrichment.'},
            'prep-enrich-narration': {what: 'Include narration-style descriptions in LLM enrichment.'},
            'prep-enrich-emotion': {what: 'Include emotional-tone descriptions in LLM enrichment.'},
            'prep-batch-files': {what: 'Choose the recordings to prepare as separate datasets in batch mode.'},
            'prep-output': {what: 'Filename of the training-dataset ZIP produced by single-file preparation.'},
            'prep-lang': {what: 'Language of the recording being prepared for training.'},
            'prep-confidence': {what: 'Minimum average word-alignment confidence from 0 to 1 for keeping a chunk. Higher values reject more chunks.'},
            'prep-snr': {what: 'Minimum signal-to-noise ratio in decibels for keeping a chunk. Higher values reject noisier audio.'},
            'prep-diarization-mode': {what: 'Off skips speaker separation. Auto checks for multiple speakers; Always diarize runs speaker separation regardless.'},
            'prep-hf-token': {what: 'Hugging Face token with access to the diarization model. It is used for this run and is not saved.'},
            'vl-rocm_python': {what: 'Python interpreter used by the Voice Lab pipeline. It needs the dependencies required by the selected stages.'},
            'vl-profiler_model': {what: 'Optional GGUF model used to write voice profiles. Blank uses the profiler default.'},
            'vl-zips_dir_cfg': {what: 'Default dataset input folder saved in the Voice Lab pipeline settings.'},
            'vl-epub_dirs': {what: 'Folders searched for EPUB prose context when generating voice descriptions. Enter one folder per line.'},
            'vl-zips_dir': {what: 'Dataset input folder for this pipeline run, containing narrator subfolders.'},
            'vl-stage-quality': {what: 'Audit clip health and duplicate evidence. This stage reports warnings rather than silently removing clips.'},
            'vl-stage-dedup': {what: 'Cluster voices, merge confirmed matches and remove exact duplicate clips before training.'},
            'vl-stage-train': {what: 'Train a LoRA adapter for each deduplicated voice dataset.'},
            'vl-stage-evaluate': {what: 'Generate fixed evaluation probes and report speaker-similarity, silence and clipping warnings.'},
            'vl-stage-profile': {what: 'Build acoustic and LLM descriptions of the voices.'},
            'vl-stage-name': {what: 'Create descriptive voice names and, when enabled below, rename the adapters.'},
            'vl-device': {what: 'Device requested for pipeline model work: automatic selection, CUDA or CPU.'},
            'vl-target-loss': {what: 'Stop training early when average training loss reaches this target or lower. Loss alone does not establish voice quality.'},
            'vl-max-epochs': {what: 'Maximum number of passes through each voice dataset before training stops.'},
            'vl-lora-r': {what: 'Rank of the adapter trained by Voice Lab, controlling its capacity and memory requirements.'},
            'vl-candidate-checkpoints': {what: 'Number of retained checkpoint candidates requested for evaluation, up to two. Zero requests no candidate checkpoints.'},
            'vl-name-apply': {what: 'Allow the naming stage to rename adapters. Unchecked produces a dry-run preview.'},
            'vl-name-overwrite': {what: 'Allow the naming stage to replace descriptive names that adapters already have.'},
            'lora-dataset-file': {what: 'Training-dataset ZIP to upload. Upload it before selecting it for training.'},
            'lora-adapter-name': {what: 'Name of the LoRA adapter created by this training run.'},
            'lora-dataset-select': {what: 'Uploaded dataset whose audio and transcripts will be used to train this adapter.'},
            'lora-epochs': {what: 'Number of full passes through the selected training dataset.'},
            'lora-lr': {what: 'Learning rate: the size of each training weight update.'},
            'lora-batch-size': {what: 'Number of training samples processed together in a step. Larger batches require more memory.'},
            'lora-rank': {what: 'Capacity of the LoRA adapter. It also affects adapter size and training memory use.'},
            'lora-alpha': {what: 'Scaling factor applied to LoRA updates relative to the adapter rank.'},
            'lora-grad-accum': {what: 'Number of steps whose gradients are accumulated before a weight update. Effective batch size is batch size multiplied by this value.'},
            'lora-language': {what: 'Language token used during training. Choose the language of the dataset recordings.'},
            'lora-test-adapter': {what: 'Trained adapter to use when generating test audio.'},
            'lora-test-text': {what: 'Words to synthesize for the selected adapter test.'},
            'lora-test-instruct': {what: 'Optional delivery instruction for test audio, such as warm or gentle narration.'},
            'dsb-project-select': {what: 'Saved dataset-building project to open, including its sample rows and generated audio.'},
            'dsb-description': {what: 'Voice description used to synthesize this dataset project’s samples.'},
            'dsb-global-seed': {what: 'Project seed used for dataset generation. Blank or -1 lets the server select a seed; a sample seed of 0 or higher overrides it.'},
            'dsb-ref-select': {what: 'Generated sample selected as the dataset project’s reference clip.'},
            'benchmark-manifest': {what: 'JSON object defining benchmark stage, targets, fixtures and settings. It must pass Preflight before Start is enabled.'},
            'clone-import-ref-text': {what: 'Exact transcript of the reference recording, including punctuation. It is required for importing the clip.'},
            'clone-import-source-title': {what: 'Optional title or description documenting where the reference recording came from.'},
            'clone-import-source-url': {what: 'Optional source link recorded with this reference clip.'},
            'clone-import-rights-basis': {what: 'Optional record of why you may use this recording, such as your own recording, a licence or permission.'},
            'clone-import-rights-confirmed': {what: 'Confirm that you have permission to use the recording. Import also requires its exact transcript.'},
            'manual-copy-text': {what: 'Read-only text for manual copying when the browser refuses automatic clipboard access.'},
            'preset-editor-name': {what: 'Name of the preset to save. An existing name replaces that saved preset.'},
            'preset-editor-description': {what: 'Optional note describing when to use this preset.'},
        };

        const ACTION_HELP = {
            'previewSetupChanges': 'Compare the current form with normalized saved settings. Preview does not save, reset the engine or contact a model. Credentials and prompt contents are hidden.',
            'closeSetupPreview': 'Close the settings preview without saving or changing form values.',
            'openSetupSettingsSection': 'Display this settings category while keeping edits in every category. Jump to this Setup section without changing or saving settings.',
            'openSetupSetting': 'Open the matching setting and move keyboard focus to its original control. Does not change its value.',
            'clearSetupSettingsSearch': 'Clear the settings search and its results without changing your configuration.',
            'reloadPageAfterConfirmation': 'Reload the page to use the running app’s latest files. Confirm first so unsaved work is not discarded accidentally.',
            'cycleTheme': 'Switch the app between its available light and dark themes.',
            'copyManualPrompt': 'Copy the current waiting task’s prompt so you can answer it in another model or chat tool.',
            'submitManualReply': 'Validate the pasted answer against the current manual request and continue the waiting task if it is accepted.',
            'autoConfigureSettings': 'Inspect the available hardware and fill suggested TTS settings into this form. Review and Save to apply them.',
            'loadConfig': 'Reload saved app settings into this form.',
            'toggleLlmKeyVisibility': 'Show or conceal the API key in this form. This changes its display, not the saved key.',
            'testLlmConnection': 'Contact the configured model endpoint to check that it can answer a test request.',
            'loadMissingPromptDefaults': 'Fill missing prompt fields with defaults while keeping existing prompt edits.',
            'savePromptPreset': 'Save the current prompt settings under a preset name for later reuse.',
            'deletePromptPreset': 'Remove the selected saved prompt preset.',
            'previewAttributionPrompt': 'Show the attribution prompt assembled from the current settings before running annotation.',
            'savePassPromptPreset': 'Save the prompt settings for this annotation pass as a reusable preset.',
            'deletePassPromptPreset': 'Remove the selected preset for this annotation pass.',
            'resetPrompts': 'Replace the prompt fields with their defaults.',
            'refreshSavedConfigFeedback': 'Refresh the saved-settings feedback shown beside the configuration form.',
            'requestTaskNotifications': 'Ask the browser for permission to show desktop notifications when tasks finish.',
            'buildCastList': 'Use the book input to build a list of characters for annotation.',
            'loadCastList': 'Refresh the cast-list editor from its saved data.',
            'cancelCastList': 'Request cancellation of the current cast-list task.',
            'scriptBatchSelectAll': 'Select or clear the saved books in the script-generation batch list.',
            'scriptBatchSort': 'Reorder the script-generation book list using the chosen sort order.',
            'startBookPreflight': 'Run a model sample test on the selected book before deciding whether to generate its full script.',
            'cancelBookPreflight': 'Cancel the current book preflight check.',
            'pauseResumeScript': 'Pause the script task or let a paused task continue.',
            'snapshotScript': 'Save an in-progress script snapshot so its current output can be inspected.',
            'cancelScript': 'Request cancellation of the current script-generation task.',
            'retryScriptGeneration': 'Retry script generation using its retained recovery state and current settings.',
            'pauseResumeBatchScript': 'Pause or resume the script-generation batch.',
            'cancelBatchScript': 'Request cancellation of the script-generation batch.',
            'findNicknames': 'Ask the configured model to find character names and aliases in the script.',
            'loadCharacterAliases': 'Refresh the character-alias editor from saved aliases.',
            'pauseResumeNicknames': 'Pause or resume the alias-detection task.',
            'cancelNicknames': 'Request cancellation of alias detection.',
            'reviewBatchSelectAll': 'Select or clear the saved books in the review batch list.',
            'loadReviewBatchScripts': 'Refresh the saved-book list used for batch review.',
            'reviewBatchSort': 'Reorder the review book list using the chosen sort order.',
            'startBatchReview': 'Review the selected saved scripts with the configured model and review settings.',
            'pauseResumeReview': 'Pause or resume review of the current script.',
            'cancelReview': 'Request cancellation of the current script review.',
            'pauseResumeBatchReview': 'Pause or resume the batch-review task.',
            'cancelBatchReview': 'Request cancellation of batch review.',
            'saveScript': 'Save the active annotated script to the book library under the entered name.',
            'previewNarratorSelection': 'Show which narrator voice the selected strategy, focus and version would choose. This does not generate or play audio.',
            'generatePersonas': 'Generate character personas and voice-design information from the current script.',
            'cancelPersonas': 'Request cancellation of persona generation.',
            'refreshPersonaVoiceResources': 'Refresh the characters, states and saved voice resources used for persona recovery.',
            'copyPersonaPrompt': 'Copy a persona-recovery prompt for the selected character and context.',
            'recoverPersona': 'Validate the pasted persona response and save it for the selected character or state.',
            'suggestVoices': 'Ask the model to suggest voice assignments for the current characters.',
            'applyAllVoiceSuggestions': 'Apply all currently displayed voice suggestions to the book’s assignments.',
            'clearVoiceSuggestions': 'Discard the displayed suggestions without applying them.',
            'createCast': 'Create a reusable cast for storing character-to-voice assignments.',
            'deleteCast': 'Delete the selected saved cast.',
            'openCastSave': 'Choose which current character voices to save into the selected cast.',
            'openCastApply': 'Match the selected cast’s members to characters in the current book before applying voices.',
            'openCastApplyBulk': 'Choose saved books to receive the selected cast’s voice assignments.',
            'generateDesignPreview': 'Synthesize the sample text using the voice description so you can hear the design before saving it.',
            'saveDesignedVoice': 'Save the generated preview and its matching description as a reusable designed voice.',
            'stopDesignedVoicePlayback': 'Stop the currently playing designed-voice preview.',
            'loadDesignedVoices': 'Refresh the saved designed-voice list.',
            'startPreparer': 'Transcribe and segment the selected recordings into training datasets using these preparation settings.',
            'cancelPreparer': 'Request cancellation of the active single-file or batch preparation task.',
            'loadPreparerOutputs': 'Refresh the completed training-dataset downloads.',
            'saveVoicelabConfig': 'Save the pipeline interpreter, model and search-folder settings.',
            'voicelabInspect': 'Inspect the selected input folder and show which voice datasets are available.',
            'startVoicelab': 'Preflight the selected stages and resources, then ask for confirmation before starting the pipeline.',
            'pauseResumeVoicelab': 'Pause or resume the Voice Lab pipeline.',
            'cancelVoicelab': 'Request cancellation of the Voice Lab pipeline.',
            'copyVoicelabDiagnostics': 'Copy a sanitized pipeline diagnostics summary for troubleshooting.',
            'downloadVoicelabDiagnostics': 'Download a sanitized pipeline diagnostics summary as JSON.',
            'uploadLoraDataset': 'Upload the selected dataset ZIP so it can be used for adapter training.',
            'loadLoraDatasets': 'Refresh the uploaded training-dataset list.',
            'startLoraTraining': 'Train a LoRA adapter from the selected dataset and settings.',
            'cancelLoraTraining': 'Request cancellation of the active adapter-training task.',
            'loadLoraModels': 'Refresh the installed and trained adapter list.',
            'runLoraTest': 'Generate test audio from the selected adapter, text and delivery instruction.',
            'dsbCreateProject': 'Create a dataset-building project for a voice description and sample rows.',
            'dsbDeleteProject': 'Delete the selected dataset-building project.',
            'dsbAddRow': 'Add a new editable sample row to this dataset project.',
            'dsbExport': 'Download the sample text, delivery and seed definitions as JSON. This does not include generated audio.',
            'dsbGenerateAll': 'Generate audio for project samples using the selected generation action.',
            'dsbCancel': 'Request cancellation of dataset sample generation.',
            'dsbSave': 'Package the completed audio samples and their text as a training dataset, using the selected reference sample.',
            'playSequence': 'Play the generated chunks in script order for review.',
            'startRender': 'Generate chunk audio using the selected render action and current voice assignments.',
            'cancelRender': 'Request cancellation of the current audio-rendering task.',
            'runDriftCheck': 'Compare generated chunks with their speaker references and flag possible voice drift for listening review.',
            'openTextDiff': 'Show the source-versus-script text comparison for inspecting changes.',
            'exportAudacity': 'Build an Audacity project from the generated audio and timeline.',
            'removeM4bCover': 'Remove the selected cover image from this export configuration.',
            'exportM4B': 'Export generated audio as an M4B audiobook with the entered metadata and chapter settings.',
            'cancelM4B': 'Request cancellation of the current M4B export.',
            'saveChapterTemplatePreset': 'Save the current chapter filename pattern as a reusable preset.',
            'deleteChapterTemplatePreset': 'Delete the selected chapter filename preset.',
            'exportChapterTemplatePresets': 'Download the saved chapter filename presets as JSON.',
            'loadReports': 'Refresh the list of saved review reports.',
            'loadRunHistory': 'Refresh recorded task runs and their results.',
            'onBenchmarkPreflight': 'Validate the benchmark manifest and environment before enabling Start.',
            'onBenchmarkStart': 'Run the benchmark whose current manifest passed Preflight.',
            'onBenchmarkCancel': 'Request cancellation of the benchmark and wait for its current work to finish.',
            'loadCheckpoints': 'Refresh interrupted-review checkpoints and current batch-review progress.',
            'onExplainReport': 'Ask the configured model for an optional plain-language explanation of this report. Generated advice still needs checking.',
            'onCancelReportExplanation': 'Request cancellation of this explanation. An in-flight model call may finish before cancellation becomes terminal; the report is retained.',
            'loadExistingScriptUploads': 'Refresh book uploads available for script generation.',
            'onCopyRecoveryPrompt': 'Copy the prompt for the generation step waiting for manual recovery.',
            'onInjectRecoverySegmentation': 'Validate the pasted recovery answer and inject the accepted segmentation into the waiting run.',
            'onSkipRecoveryChunk': 'Continue with a fallback for this chunk: UNKNOWN speaker labels for failed attribution, or quote-based NARRATOR/SPOKEN segmentation. The text is retained.',
            'addCastListRow': 'Add an editable character entry to the cast list.',
            'saveCastList': 'Save the cast-list entries shown in this editor.',
            'deleteCastList': 'Delete the saved cast list.',
            'addAliasRow': 'Add an editable character-name and alias row.',
            'saveCharacterAliases': 'Save the alias mappings shown in the editor.',
            'toggleCandidatePlayback': 'Play or stop this saved voice-candidate preview.',
            'favoriteVoiceCandidate': 'Toggle this candidate’s favorite marker for easier comparison later.',
            'selectVoiceCandidate': 'Use this saved candidate as the character’s active voice.',
            'deleteVoiceCandidate': 'Delete this saved voice candidate.',
            'regeneratePersona': 'Generate a new persona for this character or state using the current script evidence.',
            'generateAgeVersion': 'Generate an additional age-related voice version for this character.',
            'designFromChosenLine': 'Design the requested number of voice candidates from the chosen character line.',
            'openStatePersonaRecovery': 'Open manual persona recovery for this character state.',
            'removeStatePersona': 'Remove the saved persona for this state.',
            'editPersonaVoiceAudit': 'Edit the recorded link and reasoning between this persona and its assigned voice.',
            'setVoiceApproval': 'Record the selected review status for this persona or voice. This records your decision; it does not generate audio.',
            'addVoiceVersion': 'Save a named version of this character’s current voice settings.',
            'openVoiceStates': 'Show voice changes across the character’s states or timeline.',
            'suggestMoreVoices': 'Generate additional voice candidates for this character to compare.',
            'uploadCloneVoice': 'Choose a recording to import as this character’s cloning reference.',
            'playCloneVoice': 'Play this voice’s current reference recording.',
            'deleteCloneVoice': 'Remove the selected cloning reference.',
            'openVoiceDesignEditor': 'Open this assigned designed voice in the Designer for editing.',
            'applyStableVoiceSeeds': 'Apply stable voice seeds so unchanged settings can produce repeatable takes.',
            'applyVoiceSuggestion': 'Apply this suggested voice assignment to its character.',
            'deleteCastMember': 'Remove this member from the saved cast.',
            'submitCastSave': 'Save the selected current-book voices into the cast using the displayed matching and threshold settings.',
            'renderCastMembers': 'Refresh the cast-member editor with the current selection and filtering settings.',
            'submitCastApply': 'Apply the displayed cast matches to this book’s character voices.',
            'castBulkSelectAll': 'Select or clear saved books in this cast-application list.',
            'loadCastBulkScripts': 'Refresh the saved-book list for applying a cast to multiple books.',
            'castBulkSort': 'Reorder the bulk cast-application book list.',
            'openCastApplyBulkMatch': 'Review character matches for the selected saved books before applying this cast.',
            'removeStylePoint': 'Remove this voice-change point from the character’s timeline.',
            'chooseMainVoiceStates': 'Choose the main voice for this character’s state timeline.',
            'applyVoiceStates': 'Save and apply the displayed voice-state timeline.',
            'clearVoiceStates': 'Clear this character’s saved voice-state timeline.',
            'recoverVoiceDraft': 'Restore this retained voice-edit draft for review.',
            'discardStoredVoiceDraft': 'Discard this retained voice-edit draft.',
            'discardVoiceEditsAndReload': 'Discard unsaved voice edits and reload the saved assignments.',
            'scrollToChunkRow': 'Jump to the script line referenced by this comparison result.',
            'retryDeliveryInstructions': 'Retry generation of delivery instructions using the retained recovery state.',
            'cancelDeliveryInstructions': 'Request cancellation of delivery-instruction generation.',
            'loadChunks': 'Refresh the chunk editor from the current book’s saved chunks.',
            'generateChunk': 'Generate audio for this script line using its current speaker and delivery settings.',
            'toggleChunkExpand': 'Show or hide this line’s expanded editing controls.',
            'insertChunkAfter': 'Insert a new editable script line immediately after this one.',
            'deleteChunk': 'Delete this script line.',
            'voiceChangesHere': 'Create a voice-change point starting at this line for its character.',
            'differentTake': 'Render this line again with a new seed to produce a different take.',
            'viewReport': 'Open this saved review report for reading and optional explanation.',
            'auditSavedScript': 'Inspect this saved script’s audit information before using it.',
            'loadScript': 'Load this saved book as the active script and its associated voice assignments.',
            'deleteScript': 'Delete this saved book.',
            'playDesignedVoice': 'Play this saved designed-voice preview.',
            'openDesignedVoiceForEdit': 'Load this saved voice’s description and sample into the Designer.',
            'deleteDesignedVoice': 'Delete this saved designed voice.',
            'deleteLoraDataset': 'Delete this uploaded training dataset.',
            'onToggleFavoriteAdapter': 'Toggle this adapter’s favorite marker.',
            'downloadBuiltinAdapter': 'Download the selected built-in adapter so it can be used locally.',
            'playLoraPreview': 'Play this adapter’s preview. If none is saved, generate a preview first; the first request may take longer.',
            'testLoraModel': 'Select this adapter in the test-audio controls.',
            'recoverLoraCheckpointSwap': 'Open recovery for an interrupted checkpoint promotion or rollback.',
            'openLoraCandidateComparison': 'Compare retained checkpoint candidates for this adapter.',
            'openLoraBlindReview': 'Open a blind listening review of retained adapter candidates.',
            'openLoraReviewHistory': 'Show saved listening-review history for this adapter.',
            'promoteLoraCandidate': 'Replace the active adapter with this retained checkpoint candidate.',
            'rollbackLoraPromotion': 'Restore the adapter backup retained before the last promotion.',
            'deleteLoraRollbackBackup': 'Delete the retained rollback backup.',
            'deleteLoraModel': 'Delete this trained adapter.',
            'submitLoraBlindReview': 'Save the ratings and notes from this blind listening review.',
            'clearLoraReviewHistory': 'Clear the saved listening-review history for this adapter.',
            'openLoraModelsTab': 'Open the Training tab’s adapter list to inspect pipeline results.',
            'dsbLoadProjects': 'Refresh the saved dataset-building project list.',
            'dsbOnProjectChange': 'Load the selected dataset-building project.',
            'dsbGenSample': 'Generate or regenerate this row’s audio using its text, delivery and seed.',
            'dsbRemoveRow': 'Remove this sample row from the dataset project.',
            'selectVoiceVersion': 'Replace the character’s current voice settings with the selected saved version.',
            'onVoiceReadyChange': 'Mark whether this character’s voice is ready to be used for audio generation.',
            'toggleVoiceType': 'Choose the synthesis mode for this character, such as a stock voice, clone or adapter.',
            'onDesignedVoiceSelect': 'Assign the selected saved designed voice to this character.',
            'handleCloneVoiceUpload': 'Import the selected recording and its required reference details for voice cloning.',
            'saveVoicesDebounced': 'Save the edited voice assignment settings after a short delay.',
            'reapplyCastSaveThreshold': 'Recalculate which cast-save matches meet the displayed threshold.',
            'loadDesignLines': 'Load this character’s dialogue lines so one can be used to design a voice.',
        };

        const ITEM_HELP = {
            'settings-llm-advanced-toggle': 'Show or hide request timeouts and retry backoff for the selected LLM location. Expanding this group does not change its settings.',
            'script-recovery-inject': 'Paste a replacement model reply for the failed chunk, then validate it before applying it.',
            'config-save-button-top': 'Save the current configuration so subsequent tasks use these settings.',
            'config-save-button': 'Save the current configuration so subsequent tasks use these settings.',
            'llm-model-refresh': 'Ask the configured endpoint for its served model names and update the model picker.',
            'btn-gen-script': 'Generate an annotated script for the current book, using retained progress when available.',
            'btn-gen-script-fresh': 'Discard saved generation progress for this text and start again from its first chunk.',
            'btn-review-script': 'Review the current annotated script with the configured model and review settings.',
            'btn-review-script-contextual': 'Review script entries with surrounding context using the selected context-window setting.',
            'speaker-review-load': 'Refresh available label-review sources and saved references.',
            'speaker-review-preview': 'Preview speaker-label disagreements and the excerpts that a review would send to the model.',
            'speaker-review-start': 'Start the previewed speaker-label review after the required source/reference consent.',
            'speaker-review-cancel': 'Request cancellation of the current speaker-label review.',
            'speaker-review-cancel-previous': 'Request cancellation of the previous speaker-label review still running.',
            'speaker-review-refresh': 'Refresh the status and result of this speaker-label review.',
            'btn-merge': 'Merge generated chunk audio into the combined output using the configured pauses.',
            'chapter-preview-btn': 'Preview chapter boundaries and filenames for the current export settings.',
            'chapter-export-btn': 'Export the selected chapters into individual audio files.',
            'chapter-cancel-btn': 'Request cancellation of chapter export.',
            'btn-cancel-merge': 'Request cancellation of the audio merge.',
            'clone-import-submit': 'Validate the transcript and permission confirmation, then import this reference recording.',
            'manual-copy-close': 'Close the manual-copy dialog.',
            'preset-editor-cancel': 'Close this preset editor without saving its changes.',
            'preset-editor-submit': 'Save the entered preset name and description.',
            'confirmModalCancel': 'Dismiss this confirmation without performing the proposed action.',
            'confirmModalOk': 'Confirm the specific action described in this dialog.',
            'dsb-import-button': 'Choose sample definitions in a JSON file to import into this dataset project.',
            'dsb-btn-gen-all': 'Generate audio for dataset sample rows that still need it.',
            'dsb-btn-regen-all': 'Regenerate audio for every sample row in the selected dataset project.',
            'cast-save-threshold': 'Minimum line count used to preselect characters whose voices will be saved to this cast.',
            'btn-cast-apply-bulk-submit': 'Apply the reviewed cast-to-character matches to the selected saved books.',
            'blind-rating': 'Your overall listening score for this blind comparison, from 1 to 5.',
            'blind-notes': 'Notes on what you heard in this blind listening comparison.',
            'blind-pref-a': 'Record that you preferred sample A in this blind comparison.',
            'blind-pref-b': 'Record that you preferred sample B in this blind comparison.',
            'blind-pref-tie': 'Record that you had no preference between the two samples.',
        };

        const NAV_HELP = {
            'setup': 'Configure the model connection, synthesis settings and prompts used by the app.',
            'script': 'Import a book, annotate its script, review speaker labels and save the result.',
            'voices': 'Assign and review each character’s voice, references and changes over time.',
            'editor': 'Edit script lines and generate or listen to their individual audio chunks.',
            'audio': 'Listen to combined audio and export the audiobook or individual chapters.',
            'designer': 'Create a reusable voice from a description and a spoken sample.',
            'preparer': 'Turn recordings into transcribed, segmented voice-training datasets.',
            'dataset-builder': 'Write sample rows and generate audio to build a voice-training dataset.',
            'training': 'Upload training data, train adapters and compare or test saved models.',
            'voicelab': 'Run the voice-dataset, training, evaluation, profiling and naming pipeline.',
            'reports': 'Read review reports, task history, checkpoints and benchmark status.',
        };

        const DYNAMIC_FIELD_HELP = {
            '.design-line-select': 'Character dialogue line used as the spoken sample for voice design.',
            '.design-count': 'Number of voice candidates to design from the chosen line.',
            '.voice-ready': 'Whether this character’s voice is ready for audio generation.',
            '.voice-seed': 'Seed for this voice configuration. -1 requests a random seed.',
            '.alias-select': 'Character whose voice settings this name should share as an alias.',
            '.voice-type': 'Synthesis mode for this character: stock, built-in adapter, clone, trained adapter, design or ensemble.',
            '.voice-select': 'Stock CustomVoice speaker used for this character.',
            '.character-style': 'Character-specific delivery or accent instructions for the stock voice.',
            '.builtin-lora-select': 'Installed built-in adapter assigned to this character.',
            '.builtin-lora-style': 'Character-specific style instruction used with the built-in adapter.',
            '.designed-voice-select': 'Saved recording or designed voice used as this character’s cloning reference.',
            '.ref-text': 'Exact transcript of the cloning reference recording.',
            '.persona-description': 'Saved persona description associated with this character’s voice.',
            '.ref-audio': 'Path to the recording used as this character’s cloning reference.',
            '.lora-adapter-select': 'Trained adapter used to synthesize this character’s dialogue.',
            '.lora-character-style': 'Character-specific style instruction used with the trained adapter.',
            '.voice-presentation': 'Optional, user-confirmed description of how this character should sound. Used by model-based voice suggestions; saving it does not change the assigned voice or rendered audio.',
            '.design-description': 'Base description for this character’s designed voice; per-line delivery notes are appended.',
            '.ensemble-member': 'Include this character’s assigned voice in the combined ensemble.',
            '.cast-list-name': 'Canonical character name or description used in this book’s cast list.',
            '.cast-list-aliases': 'Other names for this cast member, separated by commas.',
            '.nick-alias': 'Alias or nickname to associate with a canonical character name.',
            '.nick-canonical': 'Canonical character name that this alias or nickname should resolve to.',
            '.cast-save-check': 'Include this character’s configured voice when saving the cast.',
            '.cast-save-scope': 'Whether this narrator assignment is shared or limited to its displayed scope.',
            '.cast-apply-check': 'Apply the selected saved cast voice to this character.',
            '.cast-apply-target': 'Saved cast member whose voice should be assigned to this character.',
            '.voice-state-source': 'Voice assignment used from this character-state boundary onward.',
            '.script-batch-upload-check': 'Include this existing uploaded book in the batch script-generation queue.',
            '.review-batch-check': 'Include this saved script in the batch review.',
            '.cast-bulk-check': 'Include this saved book when applying the selected cast to multiple books.',
            '.chunk-text': 'Words spoken in this script line. Saving changes can require regenerating its audio.',
            '.chunk-instruct': 'Delivery instruction for this script line’s audio.',
            '[data-voice-roster="speaker"]': 'Speaker assigned to this script line.',
            '[data-voice-roster="alias"]': 'Canonical character whose voice settings this alias should share.',
            '[data-direction]': 'Direction of the proposed alias mapping: the alias will resolve to the selected canonical name.',
            '[data-apply]': 'Review and confirm saving this one alias in the shared registry for later Review runs. This does not rewrite the current script.',
        };

        const CHANGED_FIELD_HELP = {
            "updateChunk": {
                "speaker": "Speaker assigned to this script line.",
                "text": "Words spoken in this script line. Edit the text before regenerating its audio.",
                "instruct": "Delivery or emotion instruction for this script line.",
                "style": "Style instruction for this script line.",
                "pause_after": "Pause after this script line, in milliseconds. Leave blank to use the default pause."
            },
            "dsbUpdateRow": {
                "emotion": "Emotion or delivery style used when synthesizing this sample.",
                "text": "Words spoken in this dataset sample.",
                "seed": "Sample seed. Blank or -1 uses the project seed; a non-negative value overrides it."
            }
        };

        /** The tooltip text for one entry: what it does, then any sourced recommendation. */
        function getFieldHelpText(entry) {
            let text = entry.what;
            if (entry.recommend) {
                text += ' Recommended: ' + entry.recommend + ' (source: ' + entry.source + ')';
            }
            return text;
        }

        const HELP_ITEMS = 'input:not([type="hidden"]),select,textarea,button,a[href],summary,audio[controls],video[controls],[role="button"],[onclick]';
        const helpTipNodes = new WeakMap();
        const helpTipOwners = new WeakMap();
        const helpActionGroups = new WeakMap();
        const directHelpControls = new WeakSet();
        const directHelpGlyphs = new WeakMap();
        const fieldHelpHandlers = new Set(['selectVoiceVersion', 'onVoiceReadyChange', 'toggleVoiceType', 'onDesignedVoiceSelect', 'handleCloneVoiceUpload', 'loadDesignLines', 'reapplyCastSaveThreshold']);
        let helpTipSequence = 0;

        function getHelpEntry(registry, key) {
            return Object.hasOwn(registry, key) ? registry[key] : null;
        }

        function getItemHelpText(control) {
            if (control.classList.contains('help-tip') || control.type === 'hidden') { return ''; }
            if (control.type === 'file' && (control.hidden || control.style.display === 'none')) { return ''; }
            if (getHelpEntry(FIELD_HELP, control.id)) { return getFieldHelpText(FIELD_HELP[control.id]); }
            if (getHelpEntry(ITEM_HELP, control.id)) { return ITEM_HELP[control.id]; }
            if (control.dataset.helpKey && getHelpEntry(ACTION_HELP, control.dataset.helpKey)) { return ACTION_HELP[control.dataset.helpKey]; }
            if (control.classList.contains('nav-link') && getHelpEntry(NAV_HELP, control.dataset.tab)) { return NAV_HELP[control.dataset.tab]; }
            for (const [selector, text] of Object.entries(DYNAMIC_FIELD_HELP)) {
                if (control.matches(selector)) { return text; }
            }
            const handler = ['onclick', 'onchange', 'oninput', 'onfocus'].map(name => control.getAttribute(name) || '').join(';');
            for (const [name, fields] of Object.entries(CHANGED_FIELD_HELP)) {
                const match = new RegExp('\\b' + name + '\\([^,]+,\\s*[\'"]([^\'"]+)[\'"]').exec(handler);
                if (match && getHelpEntry(fields, match[1])) { return fields[match[1]]; }
            }
            for (const match of handler.matchAll(/\b([A-Za-z_$][\w$]*)\s*\(/g)) {
                if (getHelpEntry(ACTION_HELP, match[1]) && (!control.matches('input,select,textarea') || fieldHelpHandlers.has(match[1]))) { return ACTION_HELP[match[1]]; }
            }
            if (handler.includes("closest('.cast-list-row').remove()")) { return 'Remove this cast-list row from the editor. Save the cast list to retain the edit.'; }
            if (handler.includes("closest('.nick-alias-row').remove()")) { return 'Remove this alias row from the editor. Save the aliases to retain the edit.'; }
            if (control.classList.contains('navbar-brand')) { return 'Return to the app’s Setup view.'; }
            if (handler.includes("'auto-config-banner'")) { return 'Dismiss the suggested-settings notice while keeping the values shown in the form.'; }
            if (handler.includes("'prompt-preview-panel'")) { return 'Close the prompt preview without changing the current prompt settings.'; }
            if (handler.includes('[data-tab=dataset-builder]')) { return 'Open the Dataset tab to build a new training dataset.'; }
            if (handler.includes("'chapter-preset-import'")) { return 'Choose a JSON file containing chapter filename presets to import.'; }
            if (handler.includes("'lora-comparison-panel'")) { return 'Close the adapter comparison or listening-review panel.'; }
            if (handler.includes("'stale-build-banner'")) { return 'Dismiss the notice about changed app files without reloading the page.'; }
            if (control.hasAttribute('data-config-save-action')) { return 'Save the current app configuration.'; }
            if (control.dataset.bsDismiss === 'modal') { return 'Close this dialog without confirming its pending action.'; }
            if (control.dataset.bsDismiss === 'toast') { return 'Dismiss this notification.'; }
            if (control.tagName === 'BUTTON' && control.closest('.chunk-actions')) { return 'Generate this script line’s audio using its current text, delivery and assigned voice.'; }
            if (control.tagName === 'INPUT' && control.id.startsWith('script-batch-narrator-')) { return 'Exact character name to use as narrator for this book in the batch. Leave blank to use the normal narrator assignment.'; }
            if (control.tagName === 'TEXTAREA' && control.hasAttribute('readonly') && control.closest('#script-recovery-panel')) { return 'Source text or batch entries for the failed chunk. Compare these with the replacement model reply.'; }
            if (control.tagName === 'TEXTAREA' && control.closest('#manual-llm-retained-replies')) { return 'Read-only copy of an earlier model reply, retained so an unsent response is not lost.'; }
            if (control.tagName === 'BUTTON' && control.disabled && (control.getAttribute('aria-label') || '').startsWith('Candidate ') && (control.getAttribute('aria-label') || '').endsWith(' is in use')) { return 'This saved voice candidate is already assigned to the character.'; }
            if (control.tagName === 'A' && control.closest('.toast-body')) { return 'Undo the recently deleted script line while this undo action remains available.'; }
            if (control.tagName === 'A' && (control.getAttribute('href') || '').startsWith('/api/logs/')) { return 'Download the full task log for troubleshooting.'; }
            if (control.dataset.bsToggle === 'collapse') {
                return control.dataset.bsTarget === '#navbarNav' ? 'Show or hide the app navigation menu.' : 'Show or hide this section’s additional settings.';
            }
            if (control.tagName === 'SUMMARY') { return 'Show or hide the details described by this heading.'; }
            if (control.matches('audio[controls],video[controls]')) { return 'Play, pause or seek through this media preview and adjust its volume. Playback does not regenerate the audio.'; }
            if (control.hasAttribute('download')) { return 'Download this file to your computer.'; }
            const title = (control.getAttribute('title') || '').trim();
            if (title.length >= 25) { return title; }
            return '';
        }

        function disposeHelpTooltip(tip) {
            const popup = document.getElementById(tip.getAttribute('aria-describedby'));
            if (popup?.classList.contains('help-tooltip')) { tip.removeAttribute('aria-describedby'); }
            if (typeof bootstrap !== 'undefined') { bootstrap.Tooltip.getInstance?.(tip)?.dispose(); }
        }

        function removeItemHelp(control) {
            if (directHelpControls.has(control)) {
                disposeHelpTooltip(control);
                directHelpControls.delete(control);
                directHelpGlyphs.get(control)?.remove();
                directHelpGlyphs.delete(control);
                control.removeAttribute('data-help-tip');
                control.removeAttribute('data-bs-title');
                control.removeAttribute('data-help-covered');
            }
            const tip = helpTipNodes.get(control);
            if (!tip) { return; }
            helpTipNodes.delete(control);
            helpTipOwners.delete(tip);
            disposeHelpTooltip(tip);
            tip.remove();
            const group = helpActionGroups.get(control);
            if (group) {
                if (group.isConnected && control.parentElement === group) { group.insertAdjacentElement('afterend', control); }
                group.remove();
                helpActionGroups.delete(control);
            }
            control.removeAttribute('data-help-covered');
        }

        function applyHelpTipVisibility(control, tip) {
            tip.hidden = control.hidden || control.style.display === 'none';
        }

        function renderItemHelp(control) {
            const text = getItemHelpText(control);
            let tip = helpTipNodes.get(control);
            if (!text) {
                removeItemHelp(control);
                control.removeAttribute('data-help-covered');
                return;
            }
            if (control.closest('.navbar') || control.tagName === 'SUMMARY' || control.classList.contains('settings-category')) {
                if (tip) { removeItemHelp(control); }
                if (control.getAttribute('data-bs-title') !== text) {
                    disposeHelpTooltip(control);
                    control.setAttribute('data-bs-title', text);
                }
                control.setAttribute('data-help-tip', '');
                control.setAttribute('data-help-covered', '');
                directHelpControls.add(control);
                let glyph = directHelpGlyphs.get(control);
                if (!glyph) {
                    glyph = document.createElement('span');
                    glyph.className = 'help-nav-glyph';
                    glyph.textContent = 'ⓘ';
                    glyph.setAttribute('aria-hidden', 'true');
                    directHelpGlyphs.set(control, glyph);
                }
                if (glyph.parentElement !== control) { control.appendChild(glyph); }
                return;
            }
            if (directHelpControls.has(control)) { removeItemHelp(control); }
            if (!tip) {
                tip = document.createElement('span');
                tip.className = 'help-tip';
                tip.textContent = 'ⓘ';
                tip.tabIndex = 0;
                tip.setAttribute('role', 'img');
                tip.setAttribute('data-help-tip', '');
                tip.setAttribute('data-help-for', control.id || 'item-' + (++helpTipSequence));
                const stopAction = event => { event.preventDefault(); event.stopPropagation(); };
                tip.addEventListener('click', stopAction);
                tip.addEventListener('keydown', event => {
                    if (event.key === 'Enter' || event.key === ' ') { stopAction(event); }
                });
                helpTipNodes.set(control, tip);
                helpTipOwners.set(tip, control);
            }
            if (tip.getAttribute('data-bs-title') !== text) {
                disposeHelpTooltip(tip);
                tip.setAttribute('data-bs-title', text);
                tip.setAttribute('aria-label', 'Help: ' + text);
            }
            const label = control.labels?.[0] || control.closest('label');
            const anchor = label || control;
            const oldGroup = helpActionGroups.get(control);
            if (oldGroup && control.parentElement !== oldGroup) {
                oldGroup.remove();
                helpActionGroups.delete(control);
            }
            if (!label && control.matches('button,a[href]') && !control.parentElement?.classList.contains('help-action-group')
                    && (control.parentElement?.classList.contains('justify-content-between') || control.matches('.report-list-item'))) {
                const listAction = control.matches('.report-list-item');
                const group = document.createElement(listAction ? 'div' : 'span');
                group.className = 'help-action-group' + (listAction ? ' help-list-action' : '');
                control.insertAdjacentElement('afterend', group);
                group.appendChild(control);
                helpActionGroups.set(control, group);
            }
            if (tip.previousElementSibling !== anchor) { anchor.insertAdjacentElement('afterend', tip); }
            control.setAttribute('data-help-covered', '');
            applyHelpTipVisibility(control, tip);
        }

        function renderHelpTips(root = document) {
            if (root.nodeType === 1 && root.matches(HELP_ITEMS)) { renderItemHelp(root); }
            root.querySelectorAll?.(HELP_ITEMS).forEach(renderItemHelp);
        }

        function ensureHelpTooltips() {
            if (!window._helpTooltips && typeof bootstrap !== 'undefined') {
                const dismissed = new WeakSet();
                const clickedNavigation = new WeakSet();
                const getPopup = trigger => document.getElementById(trigger.getAttribute('aria-describedby'));
                const getDirectControlHelp = target => {
                    const trigger = target?.closest?.('[data-help-tip]');
                    return trigger && directHelpControls.has(trigger) ? trigger : null;
                };
                const hideShownHelp = root => {
                    let hidden = false;
                    root.querySelectorAll('[data-help-tip][aria-describedby]').forEach(trigger => {
                        if (!getPopup(trigger)?.classList.contains('show')) { return; }
                        dismissed.add(trigger);
                        bootstrap.Tooltip.getInstance(trigger)?.hide();
                        hidden = true;
                    });
                    return hidden;
                };
                document.body.addEventListener('inserted.bs.tooltip', event => {
                    const trigger = event.target;
                    if (!trigger.hasAttribute('data-help-tip')) { return; }
                    const popup = getPopup(trigger);
                    popup?.addEventListener('mouseleave', () => {
                        if (!trigger.matches(':hover,:focus-within')) { bootstrap.Tooltip.getInstance(trigger)?.hide(); }
                    });
                });
                document.body.addEventListener('show.bs.tooltip', event => {
                    if (clickedNavigation.has(event.target)) { event.preventDefault(); return; }
                    dismissed.delete(event.target);
                });
                document.body.addEventListener('click', event => {
                    const trigger = getDirectControlHelp(event.target);
                    if (!trigger) { return; }
                    clickedNavigation.add(trigger);
                    dismissed.add(trigger);
                    bootstrap.Tooltip.getInstance(trigger)?.hide();
                }, true);
                for (const name of ['mouseover', 'focusin']) {
                    document.body.addEventListener(name, event => {
                        const trigger = getDirectControlHelp(event.target);
                        if (!trigger || (event.relatedTarget?.nodeType && trigger.contains(event.relatedTarget))) { return; }
                        if (clickedNavigation.delete(trigger)) { bootstrap.Tooltip.getInstance(trigger)?.show(); }
                    }, true);
                }
                document.body.addEventListener('hide.bs.tooltip', event => {
                    const trigger = event.target;
                    if (trigger.hasAttribute('data-help-tip') && !dismissed.has(trigger) && getPopup(trigger)?.matches(':hover')) {
                        event.preventDefault();
                    }
                });
                document.body.addEventListener('keydown', event => {
                    if (event.key !== 'Escape') { return; }
                    if (hideShownHelp(document) && event.target.closest?.('[data-help-tip], .help-tooltip')) {
                        event.preventDefault(); event.stopPropagation();
                    }
                }, true);
                for (const name of ['hide.bs.modal', 'hide.bs.collapse']) {
                    document.body.addEventListener(name, event => { hideShownHelp(event.target); });
                }
                window._helpTooltips = new bootstrap.Tooltip(document.body, {
                    selector: '[data-help-tip]', trigger: 'hover focus', html: false,
                    placement: 'top', fallbackPlacements: ['top', 'bottom'], customClass: 'help-tooltip', delay: {show: 0, hide: 180}});
            }
            if (window._helpObserver || typeof MutationObserver === 'undefined') { return; }
            window._helpObserver = new MutationObserver(records => {
                for (const record of records) {
                    if (record.type === 'attributes') {
                        if (!record.target.classList.contains('help-tip') && record.target.matches(HELP_ITEMS)) { renderItemHelp(record.target); }
                        continue;
                    }
                    if (record.target?.isConnected && record.target.matches?.(HELP_ITEMS)) { renderItemHelp(record.target); }
                    for (const node of record.removedNodes) {
                        if (node.nodeType !== 1) { continue; }
                        if (node.classList.contains('help-tip')) {
                            const owner = helpTipOwners.get(node);
                            if (owner?.isConnected) { renderItemHelp(owner); }
                            continue;
                        }
                        const removed = node.matches(HELP_ITEMS) ? [node] : [];
                        removed.push(...node.querySelectorAll(HELP_ITEMS));
                        for (const control of removed) {
                            if (!control.isConnected) { removeItemHelp(control); }
                        }
                    }
                    for (const node of record.addedNodes) {
                        if (node.nodeType === 1 && !node.classList.contains('help-tip') && node.isConnected) { renderHelpTips(node); }
                    }
                }
            });
            window._helpObserver.observe(document.body, {childList: true, subtree: true,
                attributes: true, attributeFilter: ['hidden', 'style', 'onclick', 'onchange', 'data-help-key', 'class']});
        }

        renderHelpTips();
        ensureHelpTooltips();
