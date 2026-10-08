# DeepSeek bug fixes — batch 9

Approved scope: saved-book and voice API persistence. Keep explicit clearing, automatic omitted-reference selection, book-token checks, receipt identity and transactional refusal.

- [x] #982 Refuse unavailable explicit reference selections before replacing a dataset.
- [x] #983 Give batch review reports unique publication names and retain native eligibility receipts.
- [x] #984 Report malformed active/saved JSON as validation errors before publishing a book transaction.
- [x] #985 Preserve omitted fields in existing speaker saves; retain explicit clears and full new-speaker defaults.
- [x] #986 Resolve only supported modern/legacy narrator labels without merging unrelated character names.
- [x] 58 focused tests pass through native HTTP handlers and persisted dataset/report/book/voice artifacts.
- [ ] Complete ready.sh and publish the draft PR.

Tests exercise CPU fixtures and actual persistence, without model inference or GPU use.

Validation: 58 focused tests; ready.sh passed with 7,067 unit tests and 70 isolated API checks. The quick gate skipped 12 generation checks. No production GPU inference or native browser run is claimed.
