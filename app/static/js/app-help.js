        // ── Field help (ⓘ tooltips) ───────────────────────────────────
        // One entry per control id on the Setup, Script, Voices, Editor and
        // Result tabs. `what` says what the control does. `recommend` appears
        // only where a measurement backs it, and then `source` names that
        // measurement (a PR, a RECIPES.md section, or the code that enforces
        // it); everything else states the default and when to change it.
        // tests/test_field_help.py fails if a visible control on those tabs
        // has no entry, or if a recommendation has no source.
        /** @type {Object<string, {what: string, recommend?: string, source?: string}>} */
        const FIELD_HELP = {
            // Setup: LLM connection
            'llm-mode': {what: 'Which LLM profile is active: Local (this machine) or Remote (a server elsewhere). Each profile keeps its own address, model and limits.'},
            'llm-url': {what: 'Address of the OpenAI-compatible server (LM Studio, llama.cpp, Ollama, a hosted API), ending in /v1.'},
            'llm-key': {what: 'API key sent with each request. Local servers usually accept any value. Use env:NAME to read it from an environment variable instead of saving it.'},
            'llm-model': {what: 'Model name sent with each request. It must match a model the server has loaded or offers.'},
            'llm-context-length': {what: 'Total tokens (prompt plus reply) the model can handle. Blank uses the context the server reports. Set it only if detection is wrong.'},
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
            'persona-context-lines': {what: 'How many lines of surrounding narration go with each persona request.'},
            'persona-context-custom': {what: 'Custom number of surrounding lines for persona requests.'},
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
        };

        /** The tooltip text for one entry: what it does, then any sourced recommendation. */
        function getFieldHelpText(entry) {
            let text = entry.what;
            if (entry.recommend) {
                text += ' Recommended: ' + entry.recommend + ' (source: ' + entry.source + ')';
            }
            return text;
        }

        /**
         * Adds a ⓘ help button for every FIELD_HELP control present in the page:
         * inside its <label for=…> or wrapping <label>, or, with no label, the
         * tooltip goes on the control itself. Safe to call more than once.
         */
        function renderHelpTips() {
            Object.keys(FIELD_HELP).forEach(id => {
                const control = document.getElementById(id);
                if (!control) { return; }
                const text = getFieldHelpText(FIELD_HELP[id]);
                const label = document.querySelector('label[for="' + id + '"]') || control.closest('label');
                if (!label) {
                    if (!control.hasAttribute('data-help-tip')) {
                        control.setAttribute('data-help-tip', '');
                        control.setAttribute('data-bs-title', text);
                    }
                    return;
                }
                if (label.querySelector('.help-tip[data-help-for="' + id + '"]')) { return; }
                const button = document.createElement('button');
                button.type = 'button';
                button.className = 'help-tip';
                button.textContent = 'ⓘ';
                button.setAttribute('data-help-tip', '');
                button.setAttribute('data-help-for', id);
                button.setAttribute('data-bs-title', text);
                button.setAttribute('aria-label', 'Help: ' + text);
                label.appendChild(button);
            });
        }

        function ensureHelpTooltips() {
            if (window._helpTooltips || typeof bootstrap === 'undefined') { return; }
            window._helpTooltips = new bootstrap.Tooltip(document.body, {
                selector: '[data-help-tip]', trigger: 'hover focus', html: false, placement: 'top'});
        }

        renderHelpTips();
        ensureHelpTooltips();
