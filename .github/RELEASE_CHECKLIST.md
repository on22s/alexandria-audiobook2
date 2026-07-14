# Release checklist

1. Start LM Studio on `localhost:1234` with the configured model loaded.
2. Confirm a built-in LoRA voice is downloaded and the GPU is otherwise idle.
3. From `app/`, run `python verify_release.py --full`.
4. Require the final `RELEASE VERIFICATION PASSED (full)` message. The full API
   summary must report zero failed and zero skipped tests.
5. Review `git status` and confirm verification did not change either API
   contract snapshot or add runtime fixtures.
6. If contract drift is intentional, regenerate snapshots explicitly, review
   both JSON diffs, commit them, and rerun the full verifier.

For ordinary development without GPU services, `python verify_release.py` runs
the same gates in quick mode and requires exactly the 12 documented GPU skips.
