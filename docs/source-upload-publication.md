# Source upload publication

Text uploads keep one owned file descriptor from bounded writing through hashing
and native publication. A private stage pathname is never reopened to select the
payload. EPUB extraction reads the captured descriptor bytes; its extracted text
gets a new bounded owner. Sound asset decoding retains its separate named-stage
protocol.

Linux uses `O_TMPFILE` and a proc-fd link into the pinned upload directory. macOS
uses an unnamed temporary file and `fclonefileat` into that directory. Windows
keeps a protected file handle and renames through that handle without replacing
an occupied destination. Windows link counts are checked before and after rename;
refusal deletes only the held handle's unaccepted link, preserving an external
alias. These operations require filesystem support. Missing support fails the
upload; there is no pathname copy or ordinary overwriting rename fallback.

Live owners contribute their authoritative written size to the upload quota.
Their names, when present, are excluded from the named-file scan to avoid double
counting or stale Windows directory-entry sizes. The request holds the upload
quota lease through publication and cleanup.

Source bytes and any digest-bound EPUB provenance are complete before source
visibility. A writer-close error after visibility is an unknown outcome: the
route retains the source and provenance even if the visibility callback has not
run. Cancellation waits for outstanding worker operations before cleanup.

POSIX stages have no pathname and disappear if the process dies before source
publication. Such an unacknowledged request must be retried. Windows may retain a
private named stage after abrupt process death. Orphan provenance is preserved
and skipped by later filename allocation. Once a source is published, callback or
directory-sync failures preserve it. These rules do not guarantee durability
against physical power loss, protect already-public files from arbitrary later
local changes, or eliminate every concurrent mutation interval in companion
metadata cleanup.
