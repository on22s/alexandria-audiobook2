# Casting library Phase 2: partial implementation

Collections use the existing voice-library casts, JSON persistence and locking.
Create or choose a collection in Casting, then add a catalogue voice with a member
name. Existing member names are rejected, not overwritten. Review the active-book
character mappings before applying; the existing book-token fence and cast apply
path remain authoritative. Generic character labels retain their source-book scope.

Cards show name-normalized variants (lowercase, trimmed, collapsed whitespace).
This groups possible variants across books and regenerations, not verified speaker
identities. Clear filters leaves the variant group. Health counts cover the whole
catalogue; health filters combine with the ordinary filters. Skipped folders are
listed separately because they cannot be represented as usable voices.

Coverage tables describe the current filtered view, before pagination. They include
zero combinations within its books, retain unspecified tags, and show up to twelve
lowest-count combinations. Age tags can overlap. These are keyword counts, not
audio-derived demographics.

## Unfinished: similar by audio

Do not treat this branch as complete Phase 2. Audio similarity, its bounded
content-addressed embedding cache and background task are not implemented.
The preliminary ECAPA probe did not establish the required separation, and
name-matched clips are not independently verified same-speaker regenerations.
No identity or perceptual quality claims follow from these measurements. No
listening tests were run. The local measurement set is not in the repository.

Before implementing or scaling similarity, supply provenance-backed same-speaker
regenerations, random negatives and a deliberately different voice, then retain
the measured control test. The exploratory probe is reproducible from a locally
available catalogue, but explicitly never reports identity qualification:

    cd app
    PYTHONPATH=. python experiments/casting_similarity_probe.py /path/to/designed_voices

Tests of the summary include reversed and nonfinite controls; these test reporting,
not ECAPA's actual discrimination. The UI fixtures cover collection persistence,
review and cancellation, normalized variants, health negatives, overlapping ages,
empty/zero coverage and text-only rendering. The scale fixture consumes a local
catalogue JSON produced by get_casting_catalog and reports fake-DOM timing only:

    node app/tests/casting_scale_fixture.js /path/to/repo /path/to/catalogue.json
