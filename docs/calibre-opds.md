# Calibre OPDS import

The optional **browse an OPDS catalog** panel in **Script**, beside the existing
uploads selector, connects to one catalog you configure. Opening the panel makes
no catalog request. This workflow uses the existing EPUB extraction and source
preview pipeline; it does not start annotation or speech generation.

## Choose and import one book

1. Enter the catalog's OPDS root URL, including any path prefix. Load saved
   settings to inspect an existing configuration, or explicitly save new settings.
   Username and password are optional for an anonymous catalog. A blank password
   keeps the stored value; the clear checkbox removes it on the next save.
2. Choose **Browse catalog**. Open individual categories or use **Next page** and
   **First page**. Each action fetches one feed page. Unsupported and protected
   acquisitions are disabled with their reason; DRM is not bypassed.
3. Choose **Preview metadata** for an EPUB. Review its title, authors, format,
   series and catalog/source identity. This is the catalog provider's read-only
   metadata, kept separately from editable defaults. Preview fetches no EPUB.
4. Explicitly capture the previewed source. If browsing or acquisition crosses an
   origin boundary, inspect the displayed destination and approve that exact
   destination. Every new redirect destination requires a separate approval;
   approval does not trust the whole origin.
5. When capture finishes, inspect the provider identity, source and final URLs,
   SHA-256 and exact byte size. Choose **Import this captured source** once, or
   **Discard captured source**. Import consumes the verified byte snapshot through
   AA2's existing upload transaction. A staged-path change cannot replace those
   confirmed bytes during import.
6. The receipt names the new existing upload. Select that file from the existing
   uploads selector to use the normal source-preview/scope flow. Import refreshes
   the upload list but does not select it or replace the current book.

EPUB extraction produces derived TXT and a source/provenance companion recording
source-byte identity and extraction decisions. The captured EPUB is consumed and
its staged file removed after import; this feature does not retain an original
EPUB archive. Keep your catalog copy if you need the original EPUB later. Provider
metadata continues to identify the OPDS entry even when EPUB package metadata
used by the extraction pipeline differs.

## Credentials and local state

The root URL, username and revision are stored in local app data. Passwords require
a secure OS credential store through `keyring`; they are never returned to the UI
or written into the catalog configuration. If that store is unavailable, saving
or using password authentication fails explicitly; anonymous browsing can still
work. Prefer HTTPS for password authentication: configured HTTP catalogs use
HTTP Basic authentication without transport encryption.

Requests retain credentials only at the originally configured origin, including
across pagination, categories and redirects. Cross-origin requests omit catalog
credentials. Diagnostic and displayed URLs redact secret-bearing fields; display
URLs cannot authorize requests. Configuration edits retire stale previews and
confirmations. A credential recovery warning blocks browsing until an explicit
successful save restores a consistent configuration.

## Cancellation and uncertain responses

**Cancel capture** requests cancellation; `cancelling` is not a completed cancel.
Keep the job pane until status confirms its outcome. Cancelling a worker's pending
redirect review ends that job. A capture that finished before cancellation is
discarded instead. Changing the source retires its preview and cancels or discards
unconfirmed capture work. A confirmed import may finish after a source change;
its receipt stays in the job pane without restoring the old source view.

If a POST response is lost or invalid, the UI reports **UNKNOWN** and sends no
automatic retry. For an unknown import, use **Check status** with the retained job
token to reconcile its state. A confirmed imported status may be available even
when the original receipt was lost. Inspect existing uploads before proceeding.

For an unknown capture start or replay with no new job token, the UI cannot check
or cancel that attempt using a previous job's token. Resetting the view does not
free this ownership. Explicitly save the catalog settings successfully to retire
that local attempt and invalidate old previews/unconfirmed captures. This does
not establish that the download never ran or was cancelled: a backend worker may
still block a new capture until cleanup finishes. Browse and preview again after
saving. Unknown import ownership is not cleared by a settings save.

## Bounds and qualification

Default bounds are 2,000,000 bytes per feed, 200 entries per page, 40 pages
and 1,000 distinct entries per browse session, and five redirects. Capture is capped at 200 MiB and at the existing
upload limit, whichever is lower; the upload quota remains authoritative. One
live capture is owned by the production service singleton. Staging residue blocks
new captures until safely cleaned; failed cleanup does not silently reset quota.
Tokens and unconfirmed captures expire after ten minutes. Expired actions require
fresh browsing and preview, rather than silent retries.

Network reads use a 20-second timeout with cancellation and elapsed-budget
checks. DNS, multi-address connects, trickled headers or chunk framing can extend
wall-clock time; this is not a hard twenty-second deadline. No full catalog
crawl, background synchronization, search UI or multi-process shared-data lease
is provided. Same-page duplicate catalog IDs can collapse into one listing.

Qualification uses synthetic localhost feeds, EPUBs, service/HTTP tests and the
real JavaScript under the existing DOM fixture. Real Calibre interoperability,
large-book performance and native browser visual behavior remain unqualified.
