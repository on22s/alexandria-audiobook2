/* Task guides navigate existing screens; they never start a job or change a book. */
const TASK_GUIDES = [
  {
    "id": "first-book",
    "title": "Finish your first audiobook",
    "summary": "A small listening check before a whole book.",
    "image": "book",
    "steps": [
      [
        "Connect your services",
        "setup",
        "settings-tts",
        "Choose your configured LLM and speech engine. Save changes yourself, then test the connection.",
        "The selected services respond; no generation starts from this guide.",
        "Connection tests may load a local model or contact your API."
      ],
      [
        "Build and review the script",
        "script",
        "script-tab",
        "Use a short text first. Generate, inspect the dialogue labels, and save your book. Review speaker assignments against the source.",
        "A saved script with the intended words and speakers.",
        "Script generation uses your LLM; remote providers may charge."
      ],
      [
        "Choose and listen to voices",
        "voices",
        "voices-tab",
        "Assign each character a voice. Generate a small sample and listen before marking it ready.",
        "Each character has a voice you have checked.",
        "Samples use TTS. Start one GPU job at a time."
      ],
      [
        "Render, listen and export",
        "editor",
        "editor-tab",
        "Generate a small selection first. Listen for missing words and wrong voices; fix those before generating the rest. Open the export guide when ready.",
        "Checked line audio, followed by a finished export.",
        "TTS uses the GPU. Cancel from the running task; review saved progress before retrying."
      ]
    ]
  },
  {
    "id": "repair",
    "title": "Repair a character’s audio",
    "summary": "Change the affected lines while keeping the rest.",
    "image": "repair",
    "steps": [
      [
        "Inspect the affected lines",
        "editor",
        "editor-tab",
        "Find the line and check its words, speaker and assigned voice. Preserve the original source text when correcting a speaker label.",
        "A clear list of lines that need repair.",
        "Inspection uses no model."
      ],
      [
        "Fix the voice assignment",
        "voices",
        "voices-tab",
        "If the voice is wrong, correct its assignment or timeline. Listen to a sample before applying it to many lines.",
        "The character uses the intended voice for these lines.",
        "A voice sample uses TTS; saving an assignment does not regenerate audio."
      ],
      [
        "Regenerate only what changed",
        "editor",
        "editor-tab",
        "Use the line’s Generate action. If chapter/speaker repair controls are available, load options and preview every selected line before confirming replacement.",
        "Only the explicitly selected lines have new audio.",
        "Generation uses TTS. Cancellation can leave completed lines saved; inspect status before retrying."
      ],
      [
        "Listen and rebuild the export",
        "audio",
        "audio-tab",
        "Listen to repaired lines in context, then export again. An earlier downloaded audiobook will not update itself.",
        "A new export containing the repaired audio.",
        "Export is CPU work; keep the previous download until the new file is checked."
      ]
    ]
  },
  {
    "id": "dataset",
    "title": "Prepare a training dataset",
    "summary": "Clean source, checked transcripts, then training.",
    "image": "dataset",
    "steps": [
      [
        "Choose suitable source audio",
        "preparer",
        "preparer-tab",
        "Use audio you are allowed to process. Prefer a clear single speaker. Choose an output project before starting preparation.",
        "A deliberate source and destination.",
        "Preparation may use ASR and GPU models. Private audio stays private unless you explicitly upload it elsewhere."
      ],
      [
        "Inspect prepared clips",
        "preparer",
        "preparer-tab",
        "Download and unpack the prepared dataset ZIP, then listen to its clips and check its transcripts in your local audio/text tools. Remove music, overlap, clipping and unsuitable clips before training.",
        "Short, clean clips with matching text.",
        "Inspection costs no model time; transcription reruns may use GPU."
      ],
      [
        "Review generated samples if used",
        "dataset-builder",
        "dataset-builder-tab",
        "Dataset Builder is an alternative for synthetic samples. Choose a project, inspect its rows and listen before saving it as a training dataset. Do not generate everything just to open this screen.",
        "A reviewed dataset saved for training.",
        "Synthetic sample generation uses TTS. It is separate from audio preparation."
      ],
      [
        "Train and compare a sample",
        "training",
        "training-tab",
        "Select the reviewed dataset and check training settings. Start training yourself. When it finishes, compare a short sample before using the adapter in a book.",
        "An adapter with a listening check, not an assumption of improved quality.",
        "Training uses GPU time. Cancel or wait for the active job before starting another."
      ]
    ]
  },
  {
    "id": "export",
    "title": "Finish and export a book",
    "summary": "Check completeness and choose the delivery format.",
    "image": "export",
    "steps": [
      [
        "Check what is ready",
        "editor",
        "editor-tab",
        "Confirm the intended lines have playable audio. Resolve failures, missing files and source-match warnings before merging.",
        "No unexplained missing or failed lines.",
        "This check starts no generation."
      ],
      [
        "Create the initial merged audio",
        "editor",
        "btn-merge",
        "Press Merge All yourself after checking the lines and source-match warnings. Wait for completion. This creates the initial MP3 and reveals the Result export options.",
        "A completed merged MP3 in Result.",
        "Merging uses CPU and temporary disk space. The guide does not press Merge All for you."
      ],
      [
        "Review export options",
        "audio",
        "audio-tab",
        "Choose M4B for chapters/metadata, MP3 or WAV chapter files for separate tracks, or Audacity for editing. Expand the appropriate options and preview names/range. If these options are hidden, complete Merge All in Editor first. Changed only keeps earlier unchanged chapters. Optional mastering, when available, is off by default; check its measured results.",
        "An explicit export scope and processing choice.",
        "Mastering adds analysis/encoding passes; it does not need TTS or retraining."
      ],
      [
        "Download and verify",
        "audio",
        "audio-tab",
        "Wait for the explicit completion result. Download, play the beginning and ending, and check chapter navigation where applicable. Keep a previous good export until this one is verified.",
        "A downloaded file you have actually checked.",
        "Cancel with the export’s Cancel control. A failed or cancelled job is not a finished new audiobook."
      ]
    ]
  }
];
let activeTaskGuide = null;
let taskGuideStep = 0;
function renderTaskGuides() {
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
    const guide = TASK_GUIDES.find(item => item.id === id);
    if (!guide || !Number.isInteger(step) || step < 0 || step >= guide.steps.length) {
        showToast('This guide step is unavailable. Choose a guide from Home.', 'warning'); return;
    }
    if (currentTabName !== 'home' && isEditorEditPending()) {
        showToast('Resolve pending or failed Editor edits before opening a guide.', 'warning'); return;
    }
    activeTaskGuide = guide; taskGuideStep = step;
    if (currentTabName !== 'home') { activateTab('home'); }
    const panel = document.getElementById('task-guide-detail'); panel.hidden = false;
    document.getElementById('task-guide-return').hidden = false;
    document.getElementById('task-guide-title').textContent = guide.title;
    const image = document.getElementById('task-guide-image'); image.src = `/static/guides/${guide.image}.svg`; image.alt = guide.summary;
    const [title, tab, target, instruction, expected, cost] = guide.steps[step];
    const heading = document.getElementById('task-guide-step-title'); heading.textContent = `${step + 1} of ${guide.steps.length} · ${title}`;
    document.getElementById('task-guide-instruction').textContent = instruction;
    document.getElementById('task-guide-expected').textContent = expected;
    document.getElementById('task-guide-cost').textContent = cost;
    document.getElementById('task-guide-prev').disabled = step === 0;
    document.getElementById('task-guide-next').disabled = step === guide.steps.length - 1;
    document.getElementById('task-guide-feedback').textContent = 'Steps are a checklist, not automatic progress. Open a screen when ready.';
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
        document.getElementById('task-guide-feedback').textContent = 'This screen is unavailable in this version. Your work is unchanged.'; return;
    }
    if (isEditorEditPending()) {
        document.getElementById('task-guide-feedback').textContent = 'Editor edits are pending or failed. Resolve them in Editor before using this guide link.'; return;
    }
    if (currentTabName !== tab) { activateTab(tab); }
    if (!target.matches('button, input, select, textarea, a[href], [tabindex]')) { target.setAttribute('tabindex', '-1'); }
    openConfigField(target);
}
renderTaskGuides();
