/* Task guides navigate existing screens; they never start a job or change a book. */
function buildTaskGuides() {
  return [
    {
      id: "first-book",
      title: t('guides.first-book.title', "Finish your first audiobook"),
      summary: t('guides.first-book.summary', "A small listening check before a whole book."),
      image: "book",
      steps: [
        [
          t('guides.first-book.step.1.title', "Connect your services"),
          "setup",
          "settings-tts",
          t('guides.first-book.step.1.instruction', "Choose your configured LLM and speech engine. Save changes yourself, then test the connection."),
          t('guides.first-book.step.1.expected', "The selected services respond; no generation starts from this guide."),
          t('guides.first-book.step.1.cost', "Connection tests may load a local model or contact your API."),
        ],
        [
          t('guides.first-book.step.2.title', "Build and review the script"),
          "script",
          "script-tab",
          t('guides.first-book.step.2.instruction', "Use a short text first. Generate, inspect the dialogue labels, and save your book. Review speaker assignments against the source."),
          t('guides.first-book.step.2.expected', "A saved script with the intended words and speakers."),
          t('guides.first-book.step.2.cost', "Script generation uses your LLM; remote providers may charge."),
        ],
        [
          t('guides.first-book.step.3.title', "Choose and listen to voices"),
          "voices",
          "voices-tab",
          t('guides.first-book.step.3.instruction', "Assign each character a voice. Generate a small sample and listen before marking it ready."),
          t('guides.first-book.step.3.expected', "Each character has a voice you have checked."),
          t('guides.first-book.step.3.cost', "Samples use TTS. Start one GPU job at a time."),
        ],
        [
          t('guides.first-book.step.4.title', "Render, listen and export"),
          "editor",
          "editor-tab",
          t('guides.first-book.step.4.instruction', "Generate a small selection first. Listen for missing words and wrong voices; fix those before generating the rest. Open the export guide when ready."),
          t('guides.first-book.step.4.expected', "Checked line audio, followed by a finished export."),
          t('guides.first-book.step.4.cost', "TTS uses the GPU. Cancel from the running task; review saved progress before retrying."),
        ],
      ],
    },
    {
      id: "single-narrator",
      title: t('guides.single-narrator.title', "Single narrator audiobook"),
      summary: t('guides.single-narrator.summary', "One voice for the whole book, using the existing screens."),
      image: "book",
      steps: [
        [
          t('guides.single-narrator.step.1.title', "Import and confirm your source"),
          "script",
          "script-single-area",
          t('guides.single-narrator.step.1.instruction', "Import the book if you have not yet. In the source preview, read the actual extracted text (filename, encoding and excerpt limits are shown; it is the original extracted text, before annotation/preparation), then explicitly confirm the current text. Annotation still runs later and speaker attribution still occurs — this checklist never rewrites dialogue or source words. If the preview is hidden, the guide feedback names the hidden pane: turn off Generate multiple books to show the single-book area; opening this step never changes the book or starts a job."),
          t('guides.single-narrator.step.1.expected', "You have explicitly confirmed the extracted text for this book."),
          t('guides.single-narrator.step.1.cost', "Opening this step loads no model. Confirming the preview writes nothing."),
        ],
        [
          t('guides.single-narrator.step.2.title', "Generate and review the script"),
          "script",
          "script-tab",
          t('guides.single-narrator.step.2.instruction', "Generate the annotated script with the existing Generate control. Turn on Preserve source words only through its existing checkbox if you want it. Review speakers against the source before moving on."),
          t('guides.single-narrator.step.2.expected', "A saved script with the intended words and speakers."),
          t('guides.single-narrator.step.2.cost', "Script generation uses your LLM; remote providers may charge."),
        ],
        [
          t('guides.single-narrator.step.3.title', "Choose one voice and apply it book-wide"),
          "voices",
          "voices-tab",
          t('guides.single-narrator.step.3.instruction', "Pick one existing voice on any card (built-in, LoRA, clone or saved-designed — all use the normal Voices resources), then use 'Use this speaker's voice for the book': review every affected speaker and line count, confirm explicitly, and check the ready state in Voices afterwards. Full-cast workflows stay available for other books."),
          t('guides.single-narrator.step.3.expected', "Every speaker uses the one chosen voice, ready only after your explicit mark."),
          t('guides.single-narrator.step.3.cost', "Preview may save your already-authorized pending card edits exactly as Voices does, but never applies the book-wide voice. Confirming saves the book-wide voice change; listening/sample playback is separate."),
        ],
        [
          t('guides.single-narrator.step.4.title', "Generate and listen to one sample line"),
          "editor",
          "editor-tab",
          t('guides.single-narrator.step.4.instruction', "Press one ordinary row Generate control on a short line, listen with the row player, then mark readiness with the existing Voices Ready controls. A sample is one line only — the remaining lines are generated in the next step. This guide never plays or approves audio for you."),
          t('guides.single-narrator.step.4.expected', "One listened-to sample for the chosen voice."),
          t('guides.single-narrator.step.4.cost', "One row Generate uses TTS. Start one GPU job at a time."),
        ],
        [
          t('guides.single-narrator.step.5.title', "Generate the remaining line audio"),
          "editor",
          "btn-batch-fast",
          t('guides.single-narrator.step.5.instruction', "Press Render Pending yourself to generate audio for every line that is not done yet (it never replaces finished lines; Regenerate All under Whole-book actions is only for replacing everything). Wait for the Batch complete/incomplete result, then check every line's status. If you press Cancel while it runs or lines fail, fix the cause and press Render Pending again — a cancelled or failed batch is not finished audio. Merge All in the next step only combines chunks that already have audio."),
          t('guides.single-narrator.step.5.expected', "Every line has generated audio, or any failures are known and being fixed."),
          t('guides.single-narrator.step.5.cost', "Render Pending uses TTS/GPU and can take a long time. Cancel with the Cancel button; review saved progress before retrying. The guide never starts it for you."),
        ],
        [
          t('guides.single-narrator.step.6.title', "Merge, export and verify"),
          "editor",
          "btn-merge",
          t('guides.single-narrator.step.6.instruction', "Press Merge All yourself in Editor (Whole-book actions) after checking line audio and source-match warnings, and wait for the merged result — reaching this step does not mean a merge has happened. Then choose M4B, MP3/WAV chapters or Audacity in Audio, wait for the explicit completion result, download, and play the beginning and ending. An earlier downloaded audiobook will not update itself."),
          t('guides.single-narrator.step.6.expected', "A downloaded file you have actually checked."),
          t('guides.single-narrator.step.6.cost', "Merging uses CPU and temporary disk space; export has its own Cancel control. A failed or cancelled job is not a finished audiobook. The guide does not press Merge All for you."),
        ],
      ],
    },
    {
      id: "repair",
      title: t('guides.repair.title', "Repair a character’s audio"),
      summary: t('guides.repair.summary', "Change the affected lines while keeping the rest."),
      image: "repair",
      steps: [
        [
          t('guides.repair.step.1.title', "Inspect the affected lines"),
          "editor",
          "editor-tab",
          t('guides.repair.step.1.instruction', "Find the line and check its words, speaker and assigned voice. Preserve the original source text when correcting a speaker label."),
          t('guides.repair.step.1.expected', "A clear list of lines that need repair."),
          t('guides.repair.step.1.cost', "Inspection uses no model."),
        ],
        [
          t('guides.repair.step.2.title', "Fix the voice assignment"),
          "voices",
          "voices-tab",
          t('guides.repair.step.2.instruction', "If the voice is wrong, correct its assignment or timeline. Listen to a sample before applying it to many lines."),
          t('guides.repair.step.2.expected', "The character uses the intended voice for these lines."),
          t('guides.repair.step.2.cost', "A voice sample uses TTS; saving an assignment does not regenerate audio."),
        ],
        [
          t('guides.repair.step.3.title', "Regenerate only what changed"),
          "editor",
          "editor-tab",
          t('guides.repair.step.3.instruction', "Use the line’s Generate action. If chapter/speaker repair controls are available, load options and preview every selected line before confirming replacement."),
          t('guides.repair.step.3.expected', "Only the explicitly selected lines have new audio."),
          t('guides.repair.step.3.cost', "Generation uses TTS. Cancellation can leave completed lines saved; inspect status before retrying."),
        ],
        [
          t('guides.repair.step.4.title', "Listen and rebuild the export"),
          "audio",
          "audio-tab",
          t('guides.repair.step.4.instruction', "Listen to repaired lines in context, then export again. An earlier downloaded audiobook will not update itself."),
          t('guides.repair.step.4.expected', "A new export containing the repaired audio."),
          t('guides.repair.step.4.cost', "Export is CPU work; keep the previous download until the new file is checked."),
        ],
      ],
    },
    {
      id: "dataset",
      title: t('guides.dataset.title', "Prepare a training dataset"),
      summary: t('guides.dataset.summary', "Clean source, checked transcripts, then training."),
      image: "dataset",
      steps: [
        [
          t('guides.dataset.step.1.title', "Choose suitable source audio"),
          "preparer",
          "preparer-tab",
          t('guides.dataset.step.1.instruction', "Use audio you are allowed to process. Prefer a clear single speaker. Choose an output project before starting preparation."),
          t('guides.dataset.step.1.expected', "A deliberate source and destination."),
          t('guides.dataset.step.1.cost', "Preparation may use ASR and GPU models. Private audio stays private unless you explicitly upload it elsewhere."),
        ],
        [
          t('guides.dataset.step.2.title', "Inspect prepared clips"),
          "preparer",
          "preparer-tab",
          t('guides.dataset.step.2.instruction', "Download and unpack the prepared dataset ZIP, then listen to its clips and check its transcripts in your local audio/text tools. Remove music, overlap, clipping and unsuitable clips before training."),
          t('guides.dataset.step.2.expected', "Short, clean clips with matching text."),
          t('guides.dataset.step.2.cost', "Inspection costs no model time; transcription reruns may use GPU."),
        ],
        [
          t('guides.dataset.step.3.title', "Review generated samples if used"),
          "dataset-builder",
          "dataset-builder-tab",
          t('guides.dataset.step.3.instruction', "Dataset Builder is an alternative for synthetic samples. Choose a project, inspect its rows and listen before saving it as a training dataset. Do not generate everything just to open this screen."),
          t('guides.dataset.step.3.expected', "A reviewed dataset saved for training."),
          t('guides.dataset.step.3.cost', "Synthetic sample generation uses TTS. It is separate from audio preparation."),
        ],
        [
          t('guides.dataset.step.4.title', "Train and compare a sample"),
          "training",
          "training-tab",
          t('guides.dataset.step.4.instruction', "Select the reviewed dataset and check training settings. Start training yourself. When it finishes, compare a short sample before using the adapter in a book."),
          t('guides.dataset.step.4.expected', "An adapter with a listening check, not an assumption of improved quality."),
          t('guides.dataset.step.4.cost', "Training uses GPU time. Cancel or wait for the active job before starting another."),
        ],
      ],
    },
    {
      id: "export",
      title: t('guides.export.title', "Finish and export a book"),
      summary: t('guides.export.summary', "Check completeness and choose the delivery format."),
      image: "export",
      steps: [
        [
          t('guides.export.step.1.title', "Check what is ready"),
          "editor",
          "editor-tab",
          t('guides.export.step.1.instruction', "Confirm the intended lines have playable audio. Resolve failures, missing files and source-match warnings before merging."),
          t('guides.export.step.1.expected', "No unexplained missing or failed lines."),
          t('guides.export.step.1.cost', "This check starts no generation."),
        ],
        [
          t('guides.export.step.2.title', "Create the initial merged audio"),
          "editor",
          "btn-merge",
          t('guides.export.step.2.instruction', "Press Merge All yourself after checking the lines and source-match warnings. Wait for completion. This creates the initial MP3 and reveals the Result export options."),
          t('guides.export.step.2.expected', "A completed merged MP3 in Result."),
          t('guides.export.step.2.cost', "Merging uses CPU and temporary disk space. The guide does not press Merge All for you."),
        ],
        [
          t('guides.export.step.3.title', "Review export options"),
          "audio",
          "audio-tab",
          t('guides.export.step.3.instruction', "Choose M4B for chapters/metadata, MP3 or WAV chapter files for separate tracks, or Audacity for editing. Expand the appropriate options and preview names/range. If these options are hidden, complete Merge All in Editor first. Changed only keeps earlier unchanged chapters. Optional mastering, when available, is off by default; check its measured results."),
          t('guides.export.step.3.expected', "An explicit export scope and processing choice."),
          t('guides.export.step.3.cost', "Mastering adds analysis/encoding passes; it does not need TTS or retraining."),
        ],
        [
          t('guides.export.step.4.title', "Download and verify"),
          "audio",
          "audio-tab",
          t('guides.export.step.4.instruction', "Wait for the explicit completion result. Download, play the beginning and ending, and check chapter navigation where applicable. Keep a previous good export until this one is verified."),
          t('guides.export.step.4.expected', "A downloaded file you have actually checked."),
          t('guides.export.step.4.cost', "Cancel with the export’s Cancel control. A failed or cancelled job is not a finished new audiobook."),
        ],
      ],
    },
  ];
}

let TASK_GUIDES = [];
let activeTaskGuide = null;
let taskGuideStep = 0;
function renderTaskGuides() {
    TASK_GUIDES = buildTaskGuides();
    const cards = document.getElementById('task-guide-cards');
    cards.replaceChildren();
    for (const guide of TASK_GUIDES) {
        const button = document.createElement('button'); button.type = 'button'; button.className = 'task-guide-card';
        const title = document.createElement('strong'); title.textContent = guide.title;
        const description = document.createElement('span'); description.textContent = guide.summary;
        button.append(title, description); button.addEventListener('click', () => openTaskGuide(guide.id)); cards.appendChild(button);
    }
}
function openTaskGuide(id, step = 0) {
    TASK_GUIDES = buildTaskGuides();
    const guide = TASK_GUIDES.find(item => item.id === id);
    if (!guide || !Number.isInteger(step) || step < 0 || step >= guide.steps.length) {
        showToast(t('guides.toast.unavailable', 'This guide step is unavailable. Choose a guide from Home.'), 'warning'); return;
    }
    if (currentTabName !== 'home' && isEditorEditPending()) {
        showToast(t('guides.toast.pending-edits', 'Resolve pending or failed Editor edits before opening a guide.'), 'warning'); return;
    }
    activeTaskGuide = guide; taskGuideStep = step;
    if (currentTabName !== 'home') { activateTab('home'); }
    const panel = document.getElementById('task-guide-detail'); panel.hidden = false;
    document.getElementById('task-guide-return').hidden = false;
    document.getElementById('task-guide-title').textContent = guide.title;
    const image = document.getElementById('task-guide-image'); image.src = `/static/guides/${guide.image}.svg`; image.alt = guide.summary;
    const [title, tab, target, instruction, expected, cost] = guide.steps[step];
    const heading = document.getElementById('task-guide-step-title'); heading.textContent = t('guides.panel.step-heading', '{step} of {total} · {title}', {step: step + 1, total: guide.steps.length, title});
    document.getElementById('task-guide-instruction').textContent = instruction;
    document.getElementById('task-guide-expected').textContent = expected;
    document.getElementById('task-guide-cost').textContent = cost;
    document.getElementById('task-guide-prev').disabled = step === 0;
    document.getElementById('task-guide-next').disabled = step === guide.steps.length - 1;
    document.getElementById('task-guide-feedback').textContent = t('guides.panel.checklist-note', 'Steps are a checklist, not automatic progress. Open a screen when ready.');
    heading.focus({preventScroll: true}); panel.scrollIntoView({block: 'start', behavior: 'instant'});
}
function onTaskGuideStep(delta) {
    if (activeTaskGuide) { openTaskGuide(activeTaskGuide.id, taskGuideStep + delta); }
}
function closeTaskGuide() {
    activeTaskGuide = null;
    document.getElementById('task-guide-detail').hidden = true;
    document.getElementById('task-guide-return').hidden = true;
    document.getElementById('task-guide-cards').querySelector('button')?.focus();
}
function returnToTaskGuide() {
    if (activeTaskGuide) { openTaskGuide(activeTaskGuide.id, taskGuideStep); }
}
function openTaskGuideTarget() {
    if (!activeTaskGuide) { return; }
    const [, tab, id] = activeTaskGuide.steps[taskGuideStep];
    const target = document.getElementById(id);
    if (!target || !document.getElementById(`${tab}-tab`)) {
        document.getElementById('task-guide-feedback').textContent = t('guides.panel.screen-unavailable', 'This screen is unavailable in this version. Your work is unchanged.'); return;
    }
    if (isEditorEditPending()) {
        document.getElementById('task-guide-feedback').textContent = t('guides.panel.editor-edits-pending', 'Editor edits are pending or failed. Resolve them in Editor before using this guide link.'); return;
    }
    // Tab-container targets start display:none, so they must be activated
    // before judging visibility (Home->tab must work). Settings sections
    // (e.g. #settings-tts) start hidden and are revealed by the existing
    // openConfigField helper, so they are never refused as mode-hidden.
    // Other mode-hidden targets (e.g. the single-book area in batch mode)
    // navigate to their tab first, then give guidance naming the visible
    // control that reveals them, without opening the hidden target.
    const isTabContainer = id === `${tab}-tab`;
    const isSettingsSection = target.closest && target.closest('[data-settings-section]') !== null;
    const isHidden = () => (target.style && target.style.display === 'none')
        || target.hidden === true || (target.closest && target.closest('[hidden]') !== null);
    // Guide navigation is read-only regarding NEW/default voice configs:
    // the Voices activation asks loadVoices to skip its default autosave
    // for unconfigured speakers. Already-authorized pending saves still
    // flush through the ordinary loadVoices path.
    if (currentTabName !== tab) {
        activateTab(tab, true, tab === 'voices' ? {readOnlyVoices: true} : null);
    }
    if (!isTabContainer && !isSettingsSection && isHidden()) {
        document.getElementById('task-guide-feedback').textContent = id === 'script-single-area'
            ? t('guides.panel.single-area-hidden', 'The single-book source area is hidden because batch mode is on. On the Script tab, turn off Generate multiple books to show the single-book area, then open this step again.')
            : t('guides.panel.target-hidden', 'The target is hidden by the current mode. Adjust the visible controls shown on this screen to reveal it, then open the target again.'); return;
    }
    if (!target.matches('button, input, select, textarea, a[href], [tabindex]')) { target.setAttribute('tabindex', '-1'); }
    openConfigField(target);
}
renderTaskGuides();
