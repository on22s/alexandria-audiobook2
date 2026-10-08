# DeepSeek bug fixes — batch 7

- [x] #960 Retain explicitly empty ensemble membership; prefill only missing choices.
- [x] #961 Reconcile acknowledged suggestions from submitted values after dismissal.
- [x] #962 Preserve newer suggestions and local edits when older applications finish.
- [x] #963 Distinguish confirmed deletion from subsequent refresh failure.
- [x] #964 Guard pending bulk cast applications and restore retry after failure.
- [x] #965 Give compare, blind review and history a shared panel request identity.
- [x] #966 Keep backup/disk status visibly unknown while displaying adapters; expose retry.
- [x] #967 Lock dataset row mutations during single-sample generation and retain project ownership checks.

78 focused tests pass, including held acknowledgements, stale success/error, explicit-empty/missing membership, retry and current-response controls. Node VM tests run actual frontend handlers; no browser or GPU inference is claimed.

Full ready.sh pending.
