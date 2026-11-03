# Take review queue

The Editor's existing Quality inbox is now the **Take review queue**. It uses
the same saved review facts, selected-line inspector and immutable take archive.
There is no new job queue, inference provider, validator or storage budget.

1. Load a saved book and refresh review. The queue displays its book token and
   saved chunk revision. Rows identify the stable line UID, selected take and
   audio revision, alongside the reasons for review.
2. Filter confirmed speech differences, listening suspicions, unavailable checks,
   historical evidence, missing evidence, or other existing render/voice findings.
   No current evidence is not a clean result; a low ASR error is not voice approval.
   **All saved lines** also exposes unrendered lines and no-content candidates.
3. Choose **Inspect line** to use the existing selected-take playback and evidence,
   or **Review retained takes** to open its saved candidates. Pending drafts must
   be saved or discarded explicitly. Listing and paging do not save edits, run
   checks, load a model, generate, or change selected audio.
4. Audition a labelled archived candidate without selecting it. Its version,
   audio hash, recorded speech result (if valid), and restoration-evidence
   availability are shown. Historical receipt metadata must bind to the archived
   book, line, inputs, audio revision, byte count and hash; it is not a current
   quality approval. Invalid/missing evidence remains unavailable/not recorded.
   Pending, purging and deleted entries are not available for audition or restore;
   their metadata is not proof that complete archived audio exists.
5. Explicitly choose **Check & restore** only after listening. This reuses the
   existing restoration transaction: verify current inputs, voice, settings and
   pronunciation, validate the saved bytes, and retain the displaced take.
   Verification may load the native model; browsing does not. Unsupported hosts
   do not offer restoration. Existing cancellation and recovery controls apply.

Book, revision and selection changes invalidate queue actions and late candidate
responses. Current-state server checks still control any restoration; an old row
is never authority to replace another line. Source words and unrelated chunks
are not edited by queue browsing. A failed restore does not become acceptance.

The existing archive limits, protection/deletion/undo controls and pagination
remain unchanged. There is no automatic winner selection or destructive pruning.
Candidate status summarizes saved evidence, not a fresh audio analysis. Refresh
after external changes; absence of saved findings is not complete verification.

Regression controls use synthetic source/audio: stale revisions, book switches,
drafts, late responses, unavailable history, missing or mismatched receipts, and
explicit restoration failure. No private assets or live models are used for this
feature's qualification; browser layout, listening quality and native restoration
qualification remain separate from these tests.
