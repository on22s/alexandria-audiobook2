# Setup UI catalog acceptance record (#1732)

## Completed scope

The enhancement requests an incremental English-keyed catalog for one bounded
workflow, not a translation of the whole application. The implemented workflow is
Setup setting discovery and the read-only configuration comparison: finder text
and feedback, preview action/title/close, Saved/Would save headings and the
default-count summary. English is the default. Pseudo-English exercises expansion
and placeholder handling; it is not a reviewed translation.

No qualified human-reviewed additional locale is supplied. The issue explicitly
permits pseudo-localization until one is available. RTL is not supported: RTL
locale codes take the same English fallback as other unsupported locales, with
no direction changes. Additional workflows and reviewed locales are future work,
not prerequisites for this bounded foundation.

## Acceptance criteria and evidence

| Requested criterion | Implementation and executable controls |
| --- | --- |
| UI locale is separate from book, TTS, transcription and settings | `alex-ui-locale` is a browser preference. The discovery test traps configuration value reads/writes and fingerprints the form across locale changes. Preview tests compare submitted payloads and request counts. No configuration schema or language fields are written. |
| Predictable English fallback, placeholders and escaping | Unsupported locales use English; absent/empty/mismatched translation templates use English. Unknown markup keys retain authored English text or placeholders. Prototype keys cannot access inherited catalog entries. Formatter tests cover missing/invalid values, placeholder mismatch and literal replacement payloads. The fallback test rejects writes to unknown nodes and traps HTML sinks; it does not simulate an HTML parser. Production text and placeholder sinks do not parse HTML. |
| Stable IDs, API codes, hooks and numeric requests | Markup/catalog parity pins authored default text, not the numeric seed count. Real inline preview actions are exercised; attributes, focused evidence nodes and payloads are retained. Counts 0, 1 and 500 are checked across repeated locale changes and a pending response. No dates are introduced or reformatted by this workflow. |
| Help, recommendations and critical warnings retain meaning/provenance | Setting names, values, default markers, contextual help, recommendations, server notices, preview status and safety warnings remain English and are not rewritten by the catalog sweep. Preview tests pin warning/evidence text across locale changes. Existing help and field-help suites remain in the focused test set. |
| Meaningful fallback, expansion, state and workflow tests | Discovery tests exercise unavailable locales, unknown keys, placeholder mismatch, pseudo expansion, restoration, unrelated/foreign storage events, storage clearing and blocked storage. Preview tests cover current-locale response rendering, no repost, stale-response refusal and unchanged evidence. Unsupported RTL codes are explicitly tested as English text and selector fallback; document/body direction attributes and property writes are fenced, not claimed as RTL qualification. |
| Exact coverage and review status | README and the selector hint name the covered strings and English-only boundaries. Contextual help names the covered strings and preserved values. No reviewed real translation, full-interface translation, browser accessibility qualification or model/runtime qualification is claimed. |

## Validation boundaries

The JavaScript controls execute production scripts in the existing synthetic DOM
harness through Node. They do not establish real-browser layout, accessibility
or visual quality. Pseudo-English is a layout-testing mechanism, not evidence
that every expanded label fits every viewport. Existing English warning and help
text is preserved rather than represented as translated or newly requalified.

Unknown message keys passed directly to `getSetupUiMessage` still return the key
as a diagnostic value because there is no English source for an arbitrary key.
The DOM sweep instead preserves the authored fallback for uncatalogued markup;
the exact markup gate continues to reject such keys in checked-in production
markup, so this runtime fallback does not hide catalog drift in CI.

The existing singular default-count wording is unchanged. Grammar cleanup is
independent of catalog acceptance; it does not change the numeric count.
