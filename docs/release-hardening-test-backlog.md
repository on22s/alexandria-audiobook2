# Release-hardening test backlog

These checks are not required for the current review-fix batch, but are release
blockers before Alexandria Audiobook is declared production-ready.

- Inject atomic-write failures and prove existing config/output files survive.
- Exercise successful, failed, and interrupted LM Studio runtime restoration.
- Fuzz corrupt, partial, stale, and future-version three-pass checkpoints.
- Cover analyzer matrices with incomplete and statistically incomparable arms.
- Run recorded model-like duplicate-dialogue fixtures through all three passes.
- Perform a clean-state launcher/runtime smoke test on every supported platform.
- Run the complete release verifier: API contracts, exports, GPU locking, resume,
  cancellation, and long-running task recovery.
