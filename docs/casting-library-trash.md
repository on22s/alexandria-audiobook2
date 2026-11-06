# Casting library Trash

Trashed voices in the casting library are retained indefinitely. There is no expiration or automatic cleanup, and Trash/Restore preserves all review annotations.

Active voices can receive ordinary review edits. Trashed voices can also be edited, but only when the edit declares the trashed lifecycle; a stale active-mode edit is refused with 409. Ordinary mark patches cannot set lifecycle.

Trash, Restore, and Delete permanently are not undoable. After a lifecycle operation, prior undo/redo entries for the affected voices are discarded so they cannot target the new lifecycle state.

Permanent deletion writes a catalog tombstone (`lifecycle: deleted`) in the casting marks. It removes only the catalogue entry. Shared audio files, persona and Designer assets, manifests, saved casts, and existing book assignments are preserved and remain usable.

Server safeguards: lifecycle changes use the dedicated all-or-none batch endpoint with expected-lifecycle checks under the marks lock, legal transitions only (`active -> trashed`, `trashed -> active`, `trashed -> deleted`), and no path out of `deleted`. New clone and saved-cast acquisitions refuse trashed voices with 409 and deleted voices with 404.

Trash is a catalogue policy, not revocation of audio references or clone configurations already obtained while the voice was active. Existing saved candidates, casts, and book assignments can still use those preserved assets. A deleted catalogue ID stays deleted even if files at the same ID are recreated; a genuinely new library entry needs a new ID.

Shared tag colours remain shared across active and trashed entries. Search and facet counts are mode-specific; explicitly committed circle exclusions remain effective when switching between Active and Trash. Source-folder warnings are global because unreadable folders cannot be assigned a trustworthy lifecycle.

Malformed stored lifecycle data blocks library listing and new acquisitions as well as edits. The app refuses to reset or overwrite the damaged marks file; repair it before reloading. Previously saved casts and book assignments remain intact.
