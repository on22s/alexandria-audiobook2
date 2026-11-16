"""Shared, English-preserving message contract for read-only book guidance."""
from functools import wraps


WORKSPACE_MESSAGES = {
    'workspace.manifest.incomplete': 'incomplete',
    'workspace.manifest.publishing': 'publishing',
    'workspace.next.voices-pending': '{count} characters need voice preparation.',
    'workspace.evidence.voices-pending': '{count} of {total} characters still need voice preparation.',
    'workspace.evidence.chunk-files': '{present} of {total} chunk audio files are present; {failed} failed and {missing} done rows are missing audio.',
    'workspace.evidence.current-bound-export': 'A bound full-book export receipt for {file} was verified at export time and its input revision still matches current script, voice, selection binding, chunk/journal and take metadata under the full source scope (metadata-only: output file version/size/mtime, no full rehash, decode or listening check; audio quality still needs listening).',
    'workspace.evidence.manifest-state': 'The three-pass manifest records a {status} script run.',
    'workspace.evidence.unreadable-book': 'The saved book could not be read ({error_type}); inspect it before loading.',
    'workspace.guidance.a-completed-review-receipt-is-recorded-for-this-script': 'A completed-review receipt is recorded for this script output (current_settings=not_rechecked; review was recorded for these entries but is not current review authority and must not skip a requested review). This satisfies planning readiness only.',
    'workspace.guidance.a-full-book-export-attempt-for-this-book-is': 'A full-book export attempt for this book is currently running; its result is not yet verified.',
    'workspace.guidance.a-full-book-export-receipt-is-bound-to-the': 'A full-book export receipt is bound to the current input revision (metadata-only; no listening check).',
    'workspace.guidance.a-generation-review-checkpoint-remains-the-last-script-or': 'A generation/review checkpoint remains; the last script or review run did not publish a final script.',
    'workspace.guidance.a-generation-review-checkpoint-shard-ownership-entry-could-not': 'A generation/review checkpoint shard ownership entry could not be verified.',
    'workspace.guidance.a-script-or-export-run-failed-inspect-its-saved': 'A script or export run failed; inspect its saved evidence before retrying.',
    'workspace.guidance.a-script-or-review-run-left-incomplete-checkpoint-evidence': 'A script or review run left incomplete checkpoint evidence.',
    'workspace.guidance.a-session-export-receipt-verified-this-output-at-export': 'A session export receipt verified this output at export time but is not bound to the current input revision (unbound, mismatched, scoped, partial or stale evidence stays historical); current inputs are not certified by this receipt.',
    'workspace.guidance.a-verified-export-receipt-exists-for-this-book-at': 'A verified export receipt exists for this book at export time but is not bound to the current inputs, so this is historical evidence only.',
    'workspace.guidance.all-chunk-audio-files-are-present-and-a-verified': 'All chunk audio files are present and a verified receipt covers a different export format, not the attached MP3/M4B outputs; no verified export receipt binds those outputs to this book.',
    'workspace.guidance.all-chunk-audio-files-are-present-and-an-output': 'All chunk audio files are present and an output file exists, but it predates the current source scope and must be rebuilt; no verified export receipt binds it to this book.',
    'workspace.guidance.all-chunk-audio-files-are-present-and-an-output-detail': 'All chunk audio files are present and an output file exists, but no verified export receipt binds it to this book.',
    'workspace.guidance.all-chunk-audio-files-are-present-but-output-files': 'All chunk audio files are present, but output files could not be inspected; no verified export receipt binds them to this book.',
    'workspace.guidance.all-chunk-audio-files-are-present-no-output-file': 'All chunk audio files are present; no output file was found.',
    'workspace.guidance.an-export-attempt-is-running-its-result-is-not': 'An export attempt is running; its result is not yet verified.',
    'workspace.guidance.an-incomplete-review-checkpoint-remains-beside-the-receipt': 'An incomplete review checkpoint remains beside the receipt.',
    'workspace.guidance.an-interrupted-script-run-has-saved-recovery-evidence-inspect': 'An interrupted script run has saved recovery evidence. Inspect it in Script; source and settings eligibility must be rechecked before resuming.',
    'workspace.guidance.book-metadata-could-not-be-read': 'Book metadata could not be read.',
    'workspace.guidance.book-metadata-is-missing': 'Book metadata is missing.',
    'workspace.guidance.choose-a-book-or-generate-its-annotated-script': 'Choose a book or generate its annotated script.',
    'workspace.guidance.chunk-audio-is-only-partially-present': 'Chunk audio is only partially present.',
    'workspace.guidance.chunk-audio-is-present-but-no-verified-export-binds': 'Chunk audio is present but no verified export binds it to this book.',
    'workspace.guidance.continue-in-editor': 'Continue in Editor',
    'workspace.guidance.continue-with-script': 'Continue with Script',
    'workspace.guidance.continue-with-script-review': 'Continue with Script review',
    'workspace.guidance.continue-with-voices': 'Continue with Voices',
    'workspace.guidance.every-character-in-the-saved-script-has-a-prepared': 'Every character in the saved script has a prepared voice.',
    'workspace.guidance.export-in-progress': 'Export in progress',
    'workspace.guidance.file-presence-is-checked-current-script-and-voice-dependencies': 'File presence is checked; current script and voice dependencies have not been rechecked. Review existing chunks in Editor before generating missing work; audio quality still needs listening.',
    'workspace.guidance.inspect-export-evidence': 'Inspect export evidence',
    'workspace.guidance.inspect-saved-book': 'Inspect saved book',
    'workspace.guidance.inspect-script-recovery': 'Inspect script recovery',
    'workspace.guidance.no-chunk-audio-has-been-prepared-for-the-active': 'No chunk audio has been prepared for the active book.',
    'workspace.guidance.no-completed-review-receipt-is-saved-for-this-book': 'No completed-review receipt is saved for this book.',
    'workspace.guidance.no-saved-book-metadata-file-book-id-falls-back': 'No saved book metadata file; book_id falls back to the saved name.',
    'workspace.guidance.no-saved-voice-settings-file-accompanies-this-book': 'No saved voice settings file accompanies this book.',
    'workspace.guidance.no-script-render-state-cannot-be-assessed': 'No script; render state cannot be assessed.',
    'workspace.guidance.no-script-to-cast': 'No script to cast.',
    'workspace.guidance.no-script-to-review': 'No script to review.',
    'workspace.guidance.open-editor-to-prepare-audio-chunks': 'Open Editor to prepare audio chunks.',
    'workspace.guidance.output-files-exist-verify-their-book-and-content-before': 'Output files exist; verify their book and content before sharing.',
    'workspace.guidance.render-export-evidence-is-not-bound-to-this-saved': 'Render/export evidence is not bound to this saved book; load it to inspect.',
    'workspace.guidance.review-bound-export-evidence': 'Review bound export evidence',
    'workspace.guidance.review-historical-export-evidence': 'Review historical export evidence',
    'workspace.guidance.review-output-files': 'Review output files',
    'workspace.guidance.review-render-or-merge-the-book-audio-in-editor': 'Review, render or merge the book audio in Editor.',
    'workspace.guidance.review-was-recorded-for-the-current-script-voice-decisions': 'Review was recorded for the current script; voice decisions are still pending.',
    'workspace.guidance.saved-audio-progress-could-not-be-read-open-editor': 'Saved audio progress could not be read. Open Editor to inspect it; no repair was attempted.',
    'workspace.guidance.saved-audio-progress-could-not-be-read-open-editor-detail': 'Saved audio progress could not be read; open Editor to inspect it before preparing new audio.',
    'workspace.guidance.saved-book-metadata-could-not-be-read-book-id': 'Saved book metadata could not be read; book_id falls back to the saved name.',
    'workspace.guidance.saved-book-metadata-names-this-book-identity': 'Saved book metadata names this book identity.',
    'workspace.guidance.saved-books-carry-no-chunk-audio-or-export-binding': 'Saved books carry no chunk-audio or export binding; active-book audio and output files are not attributed to this book. Load it to inspect render state.',
    'workspace.guidance.saved-script-entries-are-present-the-script-changed-after': 'Saved script entries are present; the script changed after its single-pass generation completed (a later review or edit).',
    'workspace.guidance.saved-script-entries-are-present-with-no-failed-or': 'Saved script entries are present with no failed or incomplete run marker.',
    'workspace.guidance.script-recorded-review-and-voices-are-ready-chunk-audio': 'Script, recorded review and voices are ready; chunk audio has not been prepared.',
    'workspace.guidance.script-recorded-review-and-voices-are-ready-render-state': 'Script, recorded review and voices are ready; render state is unknown for a saved book. Load the book to prepare audio.',
    'workspace.guidance.script-recorded-review-and-voices-are-ready-render-state-detail': 'Script, recorded review and voices are ready; render state is unknown for this view. Load the book to inspect render state.',
    'workspace.guidance.the-active-copy-differs-from-this-saved-snapshot-saved': 'The active copy differs from this saved snapshot (saved script/voice bytes do not match the active ones); active render/export evidence is not attributed to this saved book. Load it to inspect render state.',
    'workspace.guidance.the-completed-review-receipt-could-not-be-read': 'The completed-review receipt could not be read.',
    'workspace.guidance.the-completed-review-receipt-is-not-a-json-object': 'The completed-review receipt is not a JSON object.',
    'workspace.guidance.the-completed-review-receipt-version-is-unsupported': 'The completed-review receipt version is unsupported.',
    'workspace.guidance.the-last-export-attempt-was-cancelled-restart-it-from': 'The last export attempt was cancelled; restart it from the existing controls when ready.',
    'workspace.guidance.the-last-single-pass-generation-run-did-not-complete': 'The last single-pass generation run did not complete.',
    'workspace.guidance.the-latest-export-attempt-failed-inspect-its-saved-evidence': 'The latest export attempt failed; inspect its saved evidence before retrying.',
    'workspace.guidance.the-latest-full-book-export-attempt-for-this-book': 'The latest full-book export attempt for this book was cancelled; earlier receipts, if any, are unchanged.',
    'workspace.guidance.the-latest-full-book-export-attempt-for-this-book-detail': 'The latest full-book export attempt for this book failed.',
    'workspace.guidance.the-receipt-names-a-different-output-path-the-book': 'The receipt names a different output path; the book was moved or renamed after review.',
    'workspace.guidance.the-receipt-output-no-longer-matches-the-current-script': 'The receipt output no longer matches the current script entries; the script was edited after review.',
    'workspace.guidance.the-saved-book-could-not-be-read-render-state': 'The saved book could not be read; render state cannot be assessed.',
    'workspace.guidance.the-saved-generation-completion-manifest-could-not-be-read': 'The saved generation completion manifest could not be read.',
    'workspace.guidance.the-saved-script-file-is-missing': 'The saved script file is missing.',
    'workspace.guidance.the-saved-script-file-is-missing-reload-or-restore': 'The saved script file is missing; reload or restore the book before proceeding.',
    'workspace.guidance.the-saved-script-has-no-entries': 'The saved script has no entries.',
    'workspace.guidance.the-saved-script-has-no-entries-yet': 'The saved script has no entries yet.',
    'workspace.guidance.the-saved-selection-binding-could-not-be-read-loading': 'The saved selection binding could not be read; loading this book will be refused until its identity is inspected/restored.',
    'workspace.guidance.the-saved-selection-binding-does-not-match-the-current': 'The saved selection binding does not match the current script; loading this book will be refused until its identity is inspected/restored.',
    'workspace.guidance.the-saved-voice-settings-are-not-a-json-object': 'The saved voice settings are not a JSON object.',
    'workspace.guidance.the-saved-voice-settings-contain-a-non-object-entry': 'The saved voice settings contain a non-object entry.',
    'workspace.guidance.the-saved-voice-settings-could-not-be-read': 'The saved voice settings could not be read.',
    'workspace.guidance.the-script-is-saved-but-no-completed-review-matches': 'The script is saved but no completed review matches the current entries.',
    'workspace.guidance.the-three-pass-manifest-could-not-be-read': 'The three-pass manifest could not be read.',
    'workspace.guidance.the-three-pass-manifest-records-a-failed-script-run': 'The three-pass manifest records a failed script run.',
    'workspace.name.no-book-selected': 'No book selected',
    'workspace.name.unnamed': 'Current book (unnamed)',
}


WORKSPACE_PLURALS = {
    'workspace.next.voices-pending': ('count', '{count} character needs voice preparation.'),
    'workspace.evidence.voices-pending': ('total', '{count} of {total} character still needs voice preparation.'),
}
WORKSPACE_STATUSES = {
    'available': 'Available', 'cancelled': 'Cancelled', 'complete': 'Complete',
    'completed': 'Completed', 'export_in_progress': 'Export in progress',
    'export_needed': 'Export needed', 'export_recorded': 'Export recorded',
    'failed': 'Failed', 'incomplete': 'Incomplete', 'in_progress': 'In progress',
    'missing': 'Missing', 'needs_decisions': 'Decisions needed',
    'needs_review': 'Review needed', 'needs_script': 'Script needed',
    'needs_voices': 'Voices needed', 'not_started': 'Not started',
    'ready': 'Ready', 'ready_for_audio': 'Ready for audio', 'recorded': 'Recorded',
    'render_incomplete': 'Audio incomplete', 'stale': 'Out of date',
    'unavailable': 'Unavailable', 'unknown': 'Unknown',
}
for status, label in WORKSPACE_STATUSES.items():
    WORKSPACE_MESSAGES['workspace.state.' + status] = label
for message_key, (_, singular) in WORKSPACE_PLURALS.items():
    WORKSPACE_MESSAGES[message_key + '.one'] = singular
    WORKSPACE_MESSAGES[message_key + '.many'] = WORKSPACE_MESSAGES[message_key]


class _WorkspaceMessage(str):
    def __new__(cls, key, values):
        template = WORKSPACE_MESSAGES[key]
        instance = super().__new__(cls, template.format(**values))
        instance.key = key
        instance.template = template
        instance.values = values
        return instance


def get_workspace_message(key, **values):
    return _WorkspaceMessage(key, values)


def with_workspace_guidance(project):
    def serialize(value):
        if isinstance(value, dict):
            result = {}
            for field, item in value.items():
                if field == 'overall' or field == 'status' and 'evidence' in value:
                    status_key = 'workspace.state.' + (item if item in WORKSPACE_STATUSES else 'unknown')
                    result[field + '_key'] = status_key
                    result[field + '_template'] = WORKSPACE_MESSAGES[status_key]
                    result[field + '_values'] = {}
                result[field] = str(item) if isinstance(item, _WorkspaceMessage) else serialize(item)
                if isinstance(item, _WorkspaceMessage):
                    result[field + '_key'] = item.key
                    result[field + '_template'] = item.template
                    result[field + '_values'] = serialize(item.values)
                    if item.key in WORKSPACE_PLURALS:
                        count_field, _ = WORKSPACE_PLURALS[item.key]
                        result[field + '_quantity'] = item.values[count_field]
                        result[field + '_forms'] = {
                            form: {'key': item.key + '.' + form,
                                   'template': WORKSPACE_MESSAGES[item.key + '.' + form]}
                            for form in ('one', 'many')}
            return result
        if isinstance(value, list):
            return [serialize(item) for item in value]
        return value

    @wraps(project)
    def wrapped(*args, **kwargs):
        return serialize(project(*args, **kwargs))
    return wrapped
