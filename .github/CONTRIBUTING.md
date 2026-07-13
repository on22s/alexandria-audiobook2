# Contributing to Alexandria

Thanks for your interest in contributing! Here are a few guidelines to help PRs get reviewed and merged smoothly.

## Branch Workflow

- **`main`** is the stable release branch. Users install from `main`.
- **`dev`** is the integration branch where new features land first.

**All pull requests must target the `dev` branch, not `main`.** PRs targeting `main` will be asked to retarget.

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

Before submitting a PR, run the API test suite to make sure nothing is broken:

```bash
cd app
python test_api.py              # Quick tests (~37) — no TTS/LLM needed
python test_api.py --full       # Full tests (~49) — requires running TTS + LLM
python -m unittest test_regressions.py
```

Quick mode tests config, upload, scripts CRUD, voice config, chunks, status polling, voice design listing, LoRA models/datasets listing, dataset builder CRUD, and error handling — all without loading TTS models. If quick tests pass, your changes are unlikely to break existing functionality.

If your PR modifies or adds API endpoints, add corresponding tests to `test_api.py`.

## Getting Started

1. Fork the repo and create a feature branch from `dev`
2. Make your changes
3. Run `python test_api.py` and `python -m unittest test_regressions.py`; verify both pass
4. Test manually by restarting the app
5. Open a PR targeting `dev`
