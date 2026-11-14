# UI programme sections 4-5: states, ingest order, dialogs

Scope note for the remaining two sections of the editor/navigation review. It
records what the surfaces already do, what changed, and what a later pass would
have to add. Anything not listed as implemented is still open.

The #1938 implementation and its remaining qualification limits are recorded in
[UI follow-ups](ui-followups-1938.md).

## 4. Card and row states

Every async surface owes the reader four states: empty, loading, failed (with a
retry), and stale-but-usable. The state must appear where the reader is looking,
not only in a status line somewhere else on the page.

| Surface | Empty | Loading | Failed | Notes |
| --- | --- | --- | --- | --- |
| Editor chunk table | `core.editor.no-chunks` row | `core.editor.loading` row | `core.editor.load-failed` row, now inside the table when no rows were ever loaded | The status line above the table keeps the retry button. |
| Editor review panel | "Refresh review" prompt | inert until a book is loaded | stale notice from `markEditorReviewStale` | Review facts are explicitly separate from row data. |
| Result / audio tab | empty-state block | task log renderer | task outcome handled on completion | Covered by `test_result_empty_state_hides_after_success_but_not_failed_or_cancelled_merge`. |
| Voices cast list | placeholder text | existing refresh status | toast plus retained list | |
| Setup connection check | nothing until run | spinner and `Testing…` | failed facts plus troubleshooting | Endpoint-adjacent results include total round-trip milliseconds. |

Rules that keep these honest:

- A failed reload must never look like a pending load. The table used to keep the
  "Loading chunks..." row when the first request failed; it now replaces that row
  with the failure text.
- States never claim work happened. "No audio" is not "rendered silently".
- Retry lives next to the thing that failed.

## 5a. Script stepped ingest

The Script tab holds one long card: upload, existing upload, DOCX preview, OPDS,
then generation. The reader cannot see the order. The card now opens with an
explicit order (choose text, accept it, analyze characters/context, generate the
script); each step moves focus to the matching control instead of duplicating
it. The read-only saved-state summary never infers completed work from a filename.

`/api/script/ingest-status` now distinguishes a verified selected source, a saved
validated character roster, and an actual comparison between the saved script
and prepared source. Selection, roster refresh and task completion update or
invalidate the display. A source match does not certify that a particular model
task ran. Unknown, source differences and unavailable remain explicit states.

## 5b. Dialogs

Kept from earlier phases: one shared confirm modal (`#confirmModal`) driven by
`showConfirm`, a queue so two confirmations cannot overlap, and a failure path
that disposes the modal if `show()` throws.

Now: the dialog body is tied to the dialog with `aria-describedby`, alongside the
existing `aria-labelledby`, so a screen reader announces the specific action, not
just "Please confirm".

The #1938 audit confirmed ordinary script-batch, Casting and saved-script
confirmations already use the shared component. Remaining dedicated dialogs
contain interactive listening/review or clone-import forms, not just a question;
they retain their controls, identity guards and Bootstrap focus handling. A
single width/spacing scale for every specialized modal remains separate design
work.

## Verification

State and dialog rules are pinned by `test_ui_markup_gate` (labellability of new
controls), `test_ui_guidance_js` (confirm text never claims to save or apply), and
`test_ui_catalog_parity` (every new string keyed in all four catalogs).
