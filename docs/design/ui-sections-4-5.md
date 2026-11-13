# UI programme sections 4-5: states, ingest order, dialogs

Scope note for the remaining two sections of the editor/navigation review. It
records what the surfaces already do, what changed, and what a later pass would
have to add. Anything not listed as implemented is still open.

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
| Setup connection check | nothing until run | `Testing...` | one failed pill per fact | |

Rules that keep these honest:

- A failed reload must never look like a pending load. The table used to keep the
  "Loading chunks..." row when the first request failed; it now replaces that row
  with the failure text.
- States never claim work happened. "No audio" is not "rendered silently".
- Retry lives next to the thing that failed.

## 5a. Script stepped ingest

The Script tab holds one long card: upload, existing upload, DOCX preview, OPDS,
then generation. The reader cannot see the order. The card now opens with an
explicit three-step order (choose the text, accept it into this project, generate
the script); each step moves focus to the matching control instead of duplicating
it, and the strip never marks a step as done.

Still open: live per-step completion. That needs the ingest state machine to
publish "text accepted into the project" and "script generated for the active
book" as separate facts; the strip must not infer them from a filename.

## 5b. Dialogs

Kept from earlier phases: one shared confirm modal (`#confirmModal`) driven by
`showConfirm`, a queue so two confirmations cannot overlap, and a failure path
that disposes the modal if `show()` throws.

Now: the dialog body is tied to the dialog with `aria-describedby`, alongside the
existing `aria-labelledby`, so a screen reader announces the specific action, not
just "Please confirm".

Still open: a single width/spacing scale for every modal, and replacing the three
one-off modals (script batch, casting, scripts) with the shared confirm/prompt
component where they only ask a question.

## Verification

State and dialog rules are pinned by `test_ui_markup_gate` (labellability of new
controls), `test_ui_guidance_js` (confirm text never claims to save or apply), and
`test_ui_catalog_parity` (every new string keyed in all four catalogs).
