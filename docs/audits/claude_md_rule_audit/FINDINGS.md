# CLAUDE.md Rule-Compliance Audit — Findings Log

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`
Progress: `docs/audits/claude_md_rule_audit/PROGRESS.md`

Findings use a single incrementing `F-001, F-002, ...` counter across the whole audit. Use the Finding Template and Fix-now criteria from the plan's Task 1.

---

### [F-001] Rule 9 — `atomic_json_write` retry budget quietly halved for manifest cache write
- **Piece:** P02 — app/hf_utils.py
- **Location:** `app/hf_utils.py:44` (`fetch_builtin_manifest`)
- **Severity:** low
- **Description:** `_atomic_json_write(entries, local_path, max_retries=3)` overrides the shared helper's default `max_retries=5` (see `app/utils.py:69`) with no comment explaining why this call site needs a smaller retry budget than every other caller. This is the only call site in the repo that overrides `max_retries`.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either restore the default 5 retries, or add a comment explaining why the local manifest-cache write can tolerate fewer retries than other atomic writes (e.g. it's a best-effort offline-fallback cache rather than authoritative state).
