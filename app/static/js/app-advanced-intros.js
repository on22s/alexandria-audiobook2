function getAdvancedToolIntro(tool) {
    return {
        designer: [
            uiMessage('advanced.intro.designer.heading', 'What is Designer for?'),
            uiMessage('advanced.intro.designer.body', 'Create a new voice by describing how it should sound, then listen to a short sample and save it for use in Voices. Use this when you want to invent a voice; if you already have a recording to copy, use the import controls in Voices instead.'),
            uiMessage('advanced.intro.designer.need1', 'A voice description and a sentence to read aloud.'),
            uiMessage('advanced.intro.designer.need2', 'A speech model that supports voice design, set up in Configuration.')
        ],
        casting: [
            uiMessage('advanced.intro.casting.heading', 'What is Casting for?'),
            uiMessage('advanced.intro.casting.body', 'Browse saved designed voices across books, listen to samples and keep a shortlist. Use this to find or reuse a voice; create a new one in Designer instead. Review marks and collections organize your choices without changing a book until you confirm an assignment.'),
            uiMessage('advanced.intro.casting.need1', 'Saved designed voices; listening also needs a saved audio sample.'),
            uiMessage('advanced.intro.casting.need2', 'An active book with characters if you want to assign voices.')
        ],
        preparer: [
            uiMessage('advanced.intro.preparer.heading', 'What is Preparer for?'),
            uiMessage('advanced.intro.preparer.body', 'Turn a WAV or MP3 recording into short audio clips with matching text, packaged as a training ZIP. Use this when you have a recording to teach a voice; for clips made from a voice description, use Dataset instead.'),
            uiMessage('advanced.intro.preparer.need1', 'A WAV or MP3 recording with speech you want to use for training.'),
            uiMessage('advanced.intro.preparer.need2', 'The preparation tools installed; some options need extra models.')
        ],
        dataset: [
            uiMessage('advanced.intro.dataset.heading', 'What is Dataset for?'),
            uiMessage('advanced.intro.dataset.body', 'Build a training ZIP by generating and checking short speech samples from a voice description. Use this to make synthetic training examples, not to split an existing recording (use Preparer for that). You can listen, edit sample text and regenerate clips before exporting.'),
            uiMessage('advanced.intro.dataset.need1', 'A dataset project with a voice description and sample sentences.'),
            uiMessage('advanced.intro.dataset.need2', 'A working speech model that supports voice design.')
        ],
        training: [
            uiMessage('advanced.intro.training.heading', 'What is Training for?'),
            uiMessage('advanced.intro.training.body', 'Teach the speech model a voice using a training dataset, then test the resulting voice add-on (LoRA adapter). Use this after checking your samples; it does not turn a book into an audiobook. For normal book reading with an existing voice, use Voices and Audio Editor instead.'),
            uiMessage('advanced.intro.training.need1', 'An uploaded training ZIP with audio clips and matching text.'),
            uiMessage('advanced.intro.training.need2', 'The training tools and model files installed, with enough memory to run them.')
        ],
        voicelab: [
            uiMessage('advanced.intro.voicelab.heading', 'What is Voice Lab for?'),
            uiMessage('advanced.intro.voicelab.body', 'Run several voice-training steps in order, such as checking clips, grouping matching voices, training and comparing results. Use this for a collection of datasets; use Training for a single dataset you want to train by hand. Depending on the steps you choose, it produces combined datasets, trained voice add-ons and reports.'),
            uiMessage('advanced.intro.voicelab.need1', 'Dataset ZIPs and the input folders required by your chosen steps.'),
            uiMessage('advanced.intro.voicelab.need2', 'Most steps need a supported GPU, installed tools and model files; check Pipeline Settings first.')
        ],
        reports: [
            uiMessage('advanced.intro.reports.heading', 'What are Reports for?'),
            uiMessage('advanced.intro.reports.body', 'Read saved script-review reports, inspect past runs and review checkpoints, or run a benchmark from a manifest. Use this to understand what happened and compare runs; it does not generate your audiobook. It shows saved findings and, when you run a benchmark, creates new benchmark results.'),
            uiMessage('advanced.intro.reports.need1', 'A saved report, run or review checkpoint to inspect.'),
            uiMessage('advanced.intro.reports.need2', 'To run a benchmark: a valid benchmark manifest and the tools and models it requires.')
        ]
    }[tool];
}

function renderAdvancedToolIntros() {
    for (const host of document.querySelectorAll('[data-advanced-intro]')) {
        const tool = host.getAttribute('data-advanced-intro');
        const messages = getAdvancedToolIntro(tool);
        if (!messages) { continue; }
        const storageKey = 'alex-advanced-intro-collapsed-' + tool;
        let collapsed = false;
        try { collapsed = window.localStorage.getItem(storageKey) === 'true'; } catch (error) { }
        const heading = document.createElement(tool === 'reports' ? 'h4' : 'h5');
        heading.className = 'advanced-intro-heading';
        const toggle = document.createElement('button');
        toggle.setAttribute('type', 'button');
        toggle.className = 'advanced-intro-toggle';
        toggle.id = 'advanced-intro-' + tool + '-toggle';
        renderUiMessage(toggle, messages[0]);
        renderUiMessage(toggle, uiMessage('advanced.intro.toggle-help', 'Show or hide what this tool does and what you need before using it.'), 'title');
        const content = document.createElement('div');
        content.id = 'advanced-intro-' + tool + '-content';
        toggle.setAttribute('aria-controls', content.id);
        const body = document.createElement('p');
        renderUiMessage(body, messages[1]);
        const needs = document.createElement('p');
        needs.className = 'fw-bold mb-1';
        renderUiMessage(needs, uiMessage('advanced.intro.needs', 'What you need first'));
        const list = document.createElement('ul');
        list.className = 'mb-0';
        for (const message of messages.slice(2)) {
            const item = document.createElement('li');
            renderUiMessage(item, message);
            list.append(item);
        }
        function applyExpandedState() {
            toggle.setAttribute('aria-expanded', String(!collapsed));
            content.hidden = collapsed;
        }
        toggle.addEventListener('click', () => {
            collapsed = !collapsed;
            applyExpandedState();
            try { window.localStorage.setItem(storageKey, String(collapsed)); } catch (error) { }
        });
        applyExpandedState();
        heading.append(toggle);
        content.append(body, needs, list);
        host.className = 'advanced-intro';
        host.setAttribute('aria-labelledby', toggle.id);
        host.replaceChildren(heading, content);
    }
}

document.addEventListener('DOMContentLoaded', renderAdvancedToolIntros);
