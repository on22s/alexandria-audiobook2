# Opt-in terminal pause markup

Editor supports the explicitly selected `aa2-terminal-pause-v1` authoring format.
It maps a fixed terminal pause to the existing saved line's `pause_after` field.
It is not a second renderer, automatic importer, or expressive voice system.

## Author, preview, resolve, apply

1. In Editor, explicitly save a line ending in `[[pause:250ms]]`.
2. Select the saved line and open **Opt-in pause markup** in the line inspector.
3. Choose **Preview saved-line markup**. Inspect the original and proposed spoken
   text, pause, and source spans. Preview neither saves nor starts inference.
4. If the proposed pause differs from an existing saved pause, explicitly choose
   **Keep my saved pause** or **Replace with the proposed pause**. Pauses are never
   silently added, maximized, or randomized.
5. Choose **Confirm application** and accept the confirmation dialog. This edits
   only the selected saved Editor line. Audio generation remains a separate action.

Until application, all text is literal, including directives. Generating a line
without opting in can therefore speak its bracketed notation. Merely opening the
inspector, previewing, declining confirmation, or discarding a preview changes no
saved text, pause, source, script, or audio.

## Syntax and limits

- Use exactly `[[pause:Nms]]`, with an integer from **0 through 760000**: the
  existing export pause limit, not an additional pause allocation mechanism.
- A directive must follow spoken text and end the saved line, apart from whitespace.
  For an internal pause, explicitly split the line in Editor first. This format
  never splits or reorders lines, speakers, or chapters automatically.
- Escape a literal opener as `\[[`. Explicit application removes the escape
  backslash, leaving `[[` as literal spoken text, not a control. A receipt prevents
  a repeated preview from reinterpreting that already-applied literal opener.
  If you later edit that spoken text and author more markup, escape its literal
  openers again: a new authored line is parsed afresh, never silently reinterpreted.
- Ordinary single-bracketed prose stays literal. Unknown, nested, malformed,
  unsupported voice directives and excessive durations refuse with a one-based
  character location and actionable remedy. The saved-line limit is 32768
  Unicode code points. Source/config fingerprint reads are capped at 8 MiB each.
- Named voices, URLs, files, downloads and expressive annotations are unsupported;
  markup never resolves them or loads a model.

Other occurrence-pronunciation decisions are not silently rewritten or discarded.
If a changed line has such decisions, explicitly review/remove them first, apply
markup, then recreate them against the new text.

## Persistence, provenance and existing audio

Application uses existing book/edit admission, chunks locks, take-history policy
and atomic chunks persistence. It retains the line UID, source-entry indices,
speaker, chapter and delivery instruction. Original source bytes and Annotated
Script are untouched; rebuilding chunks from Script is not markup application and
can restore the original literal authored line.

The same atomic line write stores a `narration_markup` receipt containing original
and spoken text, pause choice, format, source spans and input/result fingerprints.
Span offsets are zero-based Unicode code points in the receipt's original text;
end offsets are exclusive. Existing source-location facts move into the receipt,
because they must not be represented as locating newly edited words.
The existing chapter role is rebound to the new text so removing a control does
not create or remove an export chapter boundary. Subsequent applications retain
prior receipts in a flat history, including their original source facts. A line
allows at most eight applications; further applications refuse rather than drop
old receipts. Preserve the history and author a new line for further markup.

Changed text becomes pending and detaches old audio from the active line; its file
bytes remain on disk. Enabled take retention uses existing bookkeeping. Speech
verification/timing/drift evidence is invalidated rather than reused for changed
words. Nothing automatically renders or exports the new line.

## Stale previews and uncertain responses

Confirmation binds the selected book, saved UID/row, Script, selected source,
voice configuration and settings. Changed inputs refuse application and require
a new preview. Dirty drafts, selection/book changes and late UI responses cannot
apply an earlier preview to another line.

Repeating an identical successful request returns `already_applied` without another
write, provided its captured inputs/result still match. If a response is lost,
refresh Editor and explicitly preview the saved line: `previous_application`
shows its persisted receipt. Inspect it before retrying. Discarding a preview
is not an undo of an application already sent to the server.

## Verification limits

Synthetic tests exercise parsing, Unicode spans, refusals, real HTTP routes, task
ownership, atomic persistence, retained takes, fake-engine dispatch arguments, and
real JavaScript UI functions. They are not browser/accessibility, real-speech
listening, or provider/model qualification.
