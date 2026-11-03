# Inspect recorded speech input

In Editor, select a line and refresh review, then expand **Speech input and saved
evidence**. Authored line text, recorded API speech and the optional current-settings
preview are separate. Inspecting them does not generate audio, save edits or load a model.

- **Current** means the saved receipt passes existing identity and file-freshness checks.
  It is not a pronunciation or listening-quality approval.
- **Historical** retains recorded input but does not attest current audio bytes. The
  existing speech-evidence message explains changed, unavailable or legacy audio,
  including hosts whose filesystem timestamps cannot establish freshness.
- **Stale** distinguishes another book/line, another take, changed saved inputs and
  unverifiable audio. Stale exact text is not presented as selected-take proof.
- **Invalid** means malformed or incomplete evidence; **not recorded** means exact
  API input is absent. A receipt from unsuccessful input admission can instead be
  classified stale when its saved identity no longer matches. Neither
  is a clean result. A later rejected attempt is separate from the retained take.

The displayed binding names book, line, selected take, audio revision, review snapshot,
saved-input digest and recorded text/audio hashes. The review snapshot is current view
identity, not a historical capture revision (which this receipt does not record).
The input digest covers saved line
fields and voice revision, not a complete historical engine configuration. Missing
engine settings and normalization traces remain unavailable, never reconstructed
from today's configuration. Older takes are not backfilled by opening the inspector.

Saved occurrence decisions describe the recorded synthesis preparation: a rule applied
once, or was not applied with its recorded reason (duplicate, overlap, stale anchor,
scope mismatch, or another refusal). Recorded context and text digests remain visible.
These are historical decisions, not claims that today's rules/settings match. A changed
take, authored text or speaker prevents showing that trace as belonging to the selection.
Occurrence substitutions affect a synthesis copy; they do not edit authored Script text.

**Preview saved line with current settings** remains an explicit, separate read-only
action. It can show current normalization and pronunciation proposals, not historical
proof. Refresh after changes; pending drafts must be saved or discarded first.

Regression fixtures use synthetic text and audio metadata. They do not qualify model
behavior, listening quality, browser layout or screen-reader behavior.
