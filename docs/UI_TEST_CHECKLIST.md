# Alexandria UI test checklist

Use this checklist for UI changes and release candidates. Record failures as
issues rather than relying on a screenshot that only covers the happy path.

## Core workflow

- [ ] Open the app and confirm Setup is the selected tab.
- [ ] Load a script and confirm the loaded-file state is visible.
- [ ] Open Prompt Customization and confirm attribution, Pass 1, and Pass 3
      defaults are populated.
- [ ] Select a saved custom preset and confirm its text replaces the default.
- [ ] Switch through Script, Voices, Editor, and Result without losing state.
- [ ] Start, cancel, recover, and finish a representative task.
- [ ] Confirm errors identify the failed action and a recovery path.
- [ ] Confirm export controls report completion and failure locally.

## Responsive widths

- [ ] 320px
- [ ] 375px
- [ ] 414px
- [ ] 768px
- [ ] Desktop width

At each width, check that navigation, buttons, tables, modals, forms, prompt
textareas, and status messages remain usable without clipped labels.

## Accessibility

- [ ] Tab through the page and verify every action has visible focus.
- [ ] Activate every navigation tab from the keyboard.
- [ ] Verify dialogs have a title, usable close/cancel actions, and focus return.
- [ ] Verify asynchronous status is announced without interrupting unrelated work.
- [ ] Verify information is not conveyed by color alone.
- [ ] Check Light, Night, Super Night, and Cyberpunk contrast.
- [ ] Enable reduced motion and confirm no essential feedback disappears.

## Final checks

- [ ] `node --check app/static/js/app-core.js`
- [ ] `git diff --check`
- [ ] `./ready.sh`
- [ ] Quick API suite passes; intentional skips are recorded.
