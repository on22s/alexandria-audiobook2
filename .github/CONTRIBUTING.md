# Contributing to Alexandria

Thanks for your interest in contributing! Here are a few guidelines to help PRs get reviewed and merged smoothly.

## Branch Workflow

- **`main`** is the stable release branch. Users install from `main`.

All pull requests target `main`. Required verification must pass before merge.

## PR Guidelines

### Keep PRs focused
Each PR should address **one feature or fix**. Small, focused PRs are easier to review, less likely to introduce regressions, and get merged faster. If you have multiple ideas, open multiple PRs.

### Don't include personal tooling
- No CI/CD configs for tools the project doesn't use (Gemini workflows, etc.)
- No AI assistant config files (`.claude/skills/`, `.cursor/`, etc.)
- No shell scripts with hardcoded local paths
- Add these to your fork's `.gitignore` instead

### Don't replace existing files wholesale
- Don't rewrite `README.md`, `requirements.txt`, or other core files — modify them incrementally
- If a feature needs documentation, add it alongside existing docs

### Preserve cross-platform compatibility
Alexandria runs on Linux, Windows, and macOS. Avoid platform-specific APIs without fallbacks (e.g., `select.select()` on pipes doesn't work on Windows).

### Preserve existing functionality
- Don't add top-level imports that break startup on systems without optional dependencies (e.g., `import torch` in `app.py`)
- Don't remove existing tests — update them if behavior changes
- Don't remove existing imports unless you've verified no code path uses them

## Project Structure

Keep app code in `app/`. New modules should go there, not at the repo root.

```
app/           # Application code (FastAPI, TTS, frontend)
builtin_lora/  # Pre-trained LoRA presets
```

Launcher scripts (`install.js`, `start.js`, etc.) stay at the root.

## Testing

Run the commands below from `app/` unless noted otherwise:

```bash
python -m unittest discover -s . -p 'test_*.py'   # Unit suite (CPU, no server needed)
python run_isolated_api_tests.py         # Live API suite vs a disposable server + safe fixtures
python run_isolated_api_tests.py --full  # Full GPU/LLM suite, isolated (pre-release check)
python -m tests.test_api --url http://127.0.0.1:4200  # Live API suite vs an already-running server
python update_api_contract_snapshots.py --check
python update_test_inventory.py --check  # Verify checked-in test discovery
python verify_release.py          # All required local gates, quick mode
python verify_release.py --full   # Release gate: requires zero failures/skips
```

Unit tests live in `app/tests/`, which is a package: keep its `__init__.py`,
or `unittest discover` quietly runs 0 tests. Run `test_api` as a module
(`-m tests.test_api`), not as a path.

The isolated runner spins up a server against a throwaway `ALEXANDRIA_DATA_DIR`,
so it never touches your real books/voices. `--full` additionally requires a
running LM Studio (`localhost:1234`) and a downloaded built-in voice; it exercises
real TTS generation, review, export, voice design, and LoRA — run it before a
release. CI runs the quick suites automatically; `--full` is manual (needs GPU).

Quick mode tests config, upload, scripts CRUD, voice config, chunks, status polling, voice design listing, LoRA models/datasets listing, dataset builder CRUD, and error handling — all without loading TTS models. If quick tests pass, your changes are unlikely to break existing functionality.

If your PR modifies or adds API endpoints, add corresponding tests to `app/tests/test_api.py`.
Run `python update_api_contract_snapshots.py --check` to review a summary of any
contract drift. For an intentional change, run `python update_api_contract_snapshots.py`, review both JSON diffs in
`app/api_contract/`, and include the intentional contract update in the PR.

If test discovery changes, run `python update_test_inventory.py` and commit the
updated inventory. After local verification and GitHub checks pass on the same
commit, `python mark_pr_ready.py` verifies the branch and marks its draft PR ready.

Test fixtures must use synthetic sentences or public-domain text, never passages
from books the project has no right to republish.

### Before you commit: `./ready.sh`

From the repository root, run `./ready.sh` before each commit. It regenerates
the derived files CI checks (indexes, audits, the test inventory), lists what
changed so you can stage it, and then runs the verifier. The first run also
installs the repository's git hooks (`core.hooksPath=.githooks`), which rebuild
those derived files automatically after merges and rebases. That is local git
config, so run it once in every new clone or worktree. It needs the app
virtualenv at `app/env`.

## Getting Started

1. Fork the repo and create a feature branch from `main`
2. Make your changes
3. Run `./ready.sh`, and run the tests above that cover your change; verify they pass
4. Test manually by restarting the app
5. Open a PR targeting `main`
