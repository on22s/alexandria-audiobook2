# Batch 08: Datasets, Voice Lab and training

Batch training now keeps durable source/checkpoint-bound registration receipts, so a completed adapter can be registered after an interrupted manifest write without retraining or overwriting concurrent entries. Changed sources or weights refuse recovery. Existing adapters must match the dataset identity; extraction publishes a clean replacement dataset with rollback, preserving old data when preparation fails.

The batch interpreter uses the shared Windows/Linux venv selector. Hash auditing fails when weights cannot be verified. Voice analysis records terminal failure for missing/empty inputs and returns failure for incomplete requested phases.

Validation: 93 integrated focused tests and 78 gate-follow-up tests passed. The full release gate passed 6,952 unit tests across three shards with no unit skips, plus 70 quick API tests; 12 full-mode API cases were skipped. Tests exercise native CPU artifacts, recovery, publication failures, unchanged bytes and actual CLI exit status. Windows interpreter layout was simulated on Linux; native Windows training and GPU training are not claimed.

Tracker: #917.

- [x] Fixes #895
- [x] Fixes #902
- [x] Fixes #903
- [x] Fixes #904
- [x] Fixes #905
- [x] Fixes #906
- [x] Fixes #907
