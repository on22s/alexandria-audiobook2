# Alexandria UI guidelines

This app uses a vanilla-JS single-page UI with Bootstrap 5 primitives. New UI
should extend the existing system instead of introducing a second component
framework.

## Structure

- Keep the primary path visible: Setup → Script → Voices → Editor → Result.
- Keep Designer, Preparer, Dataset, Training, Voice Lab, and Reports grouped as
  advanced tools.
- Put the next action and the current state near the control that changes it.
- Do not hide essential errors or progress in a toast alone.

## Tokens and themes

Use the semantic CSS variables in `app/static/index.html` for surfaces, text,
borders, accents, focus rings, radii, spacing, and motion. A component must
remain readable in Light, Night, Super Night, and Cyberpunk themes.

Cyberpunk is an accent theme, not a reason to reduce readability: reserve neon
colors for focus, selection, status, and small highlights. Do not use color as
the only way to communicate state.

## Component states

Interactive components should have an intentional treatment for:

- default, hover, focus-visible, active, and disabled;
- loading and in-progress;
- success, error, and empty states.

Loading states must describe real work. Error states should say what failed and
what the user can do next. Success states should identify what changed.

## Accessibility and responsive behavior

- Use real labels and accessible names for controls.
- Keep keyboard focus visible with `:focus-visible`.
- Use live status regions for asynchronous results.
- Preserve reduced-motion behavior.
- Check layouts at 320, 375, 414, 768, and desktop widths.
- Do not require horizontal scrolling for primary navigation or actions.

## Content

Prefer plain, specific language. Settings and prompt descriptions should explain
what is sent, where it runs, whether it costs money, and how to recover from a
failure. Never invent progress, metrics, or completion claims.

## Verification

Before merging UI work, run `./ready.sh` and complete
`docs/UI_TEST_CHECKLIST.md`. For prompt changes, verify that both the checked-in
default and a saved custom preset are visible and selectable.
