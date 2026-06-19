# Active LLM Mode Badge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an always-visible "Active: Local / Remote" badge next to the Setup tab's LLM Location dropdown that reflects the last-*saved* `llm_mode`, independent of the dropdown's current unsaved selection.

**Architecture:** Pure frontend change in `app/static/index.html` (no build step, no backend changes — `GET /api/config` already returns `llm_mode`). One new Bootstrap badge element plus one new JS state variable (`savedLlmMode`) and a tiny render function, wired into the two places that already know the saved mode: `loadConfig()` (on page load) and the config-save handler (after a successful save).

**Tech Stack:** Vanilla JS, Bootstrap 5 badge classes (`badge bg-secondary`), matching existing patterns in this file (e.g. the `lmstudio-status-badge` element).

**Test approach:** This project has no JS/browser test harness (`app/test_api.py` is HTTP-level only; Playwright is listed in `requirements-test.txt` but no Playwright test files exist in this repo). Per project convention (CLAUDE.md: "For UI or frontend changes... use the feature in a browser before reporting complete"), verification is manual via the dev server — no new test infrastructure is introduced for a 4-line UI change.

**Commits:** Per global instructions, do not commit any of this without the user explicitly asking. Stop after manual verification and let the user review the diff.

---

### Task 1: Add the badge markup

**Files:**
- Modify: `app/static/index.html:515`

- [ ] **Step 1: Add the badge span inside the LLM Location label**

Find this exact block (line 515):

```html
                            <label class="form-label">LLM Location</label>
                            <select class="form-select" id="llm-mode" onchange="onLlmModeChange()">
```

Replace with:

```html
                            <label class="form-label">LLM Location <span id="llm-active-mode-badge" class="badge bg-secondary ms-1">Active: Local</span></label>
                            <select class="form-select" id="llm-mode" onchange="onLlmModeChange()">
```

- [ ] **Step 2: Visually confirm the badge renders**

This step has no automated check (markup-only, no JS wired yet). Defer visual confirmation to Task 4's manual verification, which exercises the fully wired badge.

---

### Task 2: Add `savedLlmMode` state and the render function

**Files:**
- Modify: `app/static/index.html:2125` (state declarations)
- Modify: `app/static/index.html:2134-2140` (insert new function after `syncCurrentLlmProfile`)

- [ ] **Step 1: Add the `savedLlmMode` variable next to `currentLlmMode`**

Find (line 2125):

```javascript
        let currentLlmMode = 'local';
```

Replace with:

```javascript
        let currentLlmMode = 'local';
        let savedLlmMode = 'local';   // last-saved llm_mode; drives the "Active:" badge
```

- [ ] **Step 2: Add `renderActiveLlmModeBadge()` after `syncCurrentLlmProfile()`**

Find (lines 2134-2140):

```javascript
        function syncCurrentLlmProfile() {
            llmProfiles[currentLlmMode] = {
                base_url: document.getElementById('llm-url').value,
                api_key: document.getElementById('llm-key').value,
                model_name: document.getElementById('llm-model').value
            };
        }
```

Replace with:

```javascript
        function syncCurrentLlmProfile() {
            llmProfiles[currentLlmMode] = {
                base_url: document.getElementById('llm-url').value,
                api_key: document.getElementById('llm-key').value,
                model_name: document.getElementById('llm-model').value
            };
        }

        // Reflects the last-SAVED llm_mode, not the dropdown's live selection
        // (currentLlmMode) - the gap between the two is what tells the user
        // their Location change hasn't taken effect yet.
        function renderActiveLlmModeBadge() {
            const badge = document.getElementById('llm-active-mode-badge');
            if (!badge) return;
            badge.textContent = 'Active: ' + (savedLlmMode === 'remote' ? 'Remote (Thunder / network)' : 'Local');
        }
```

- [ ] **Step 3: No automated check available**

Pure function addition with no caller yet (wired in Tasks 3-4). Nothing to run until then.

---

### Task 3: Initialize the badge on page load

**Files:**
- Modify: `app/static/index.html:2201` (inside `loadConfig()`)

- [ ] **Step 1: Set `savedLlmMode` and render right after `currentLlmMode` is read from config**

Find (lines 2201-2202):

```javascript
                currentLlmMode = config.llm_mode || 'local';
                document.getElementById('llm-mode').value = currentLlmMode;
```

Replace with:

```javascript
                currentLlmMode = config.llm_mode || 'local';
                savedLlmMode = currentLlmMode;
                renderActiveLlmModeBadge();
                document.getElementById('llm-mode').value = currentLlmMode;
```

- [ ] **Step 2: Manual check**

Run the app (see Task 4 for exact commands) and confirm the badge shows the correct mode on first load before touching anything.

---

### Task 4: Update the badge after a successful Save, then verify end-to-end

**Files:**
- Modify: `app/static/index.html:2426-2427` (config-save handler)

- [ ] **Step 1: Update `savedLlmMode` after the save POST succeeds**

Find (lines 2426-2427):

```javascript
                await API.post('/api/config', config);
                showToast('Configuration Saved!', 'success');
```

Replace with:

```javascript
                await API.post('/api/config', config);
                savedLlmMode = currentLlmMode;
                renderActiveLlmModeBadge();
                showToast('Configuration Saved!', 'success');
```

- [ ] **Step 2: Start the app and open the Setup tab**

Use the project's `run` skill (or, if running manually: `cd app && source env/bin/activate && python app.py`, then open the printed `http://127.0.0.1:<port>` URL) and navigate to the Setup tab.

- [ ] **Step 3: Verify initial state matches `config.json`**

```bash
python3 -c "import json; print(json.load(open('app/config.json')).get('llm_mode'))"
```

Confirm the badge text matches: `local` → "Active: Local", `remote` → "Active: Remote (Thunder / network)".

- [ ] **Step 4: Verify the badge does NOT move when only the dropdown changes**

In the browser, switch the "LLM Location" dropdown to the opposite value. Confirm the badge text is unchanged.

- [ ] **Step 5: Verify the badge updates after Save**

Click "Save". Confirm the badge now reflects the new dropdown value (e.g. switched to Remote → badge reads "Active: Remote (Thunder / network)").

- [ ] **Step 6: Verify persistence across reload**

Reload the page. Confirm the badge still shows the post-save value, and re-run the Step 3 command to confirm `config.json`'s `llm_mode` matches.

- [ ] **Step 7: Verify the round trip back**

Switch the dropdown back to the original value and Save again. Confirm the badge follows.

- [ ] **Step 8: Stop — do not commit**

Leave the working tree as-is for the user to review (`git diff app/static/index.html`). Do not run `git commit` unless the user explicitly asks.

---

## Self-Review

- **Spec coverage:** Badge markup (Task 1) ✓, `savedLlmMode` state (Task 2) ✓, `loadConfig()` wiring (Task 3) ✓, save-handler wiring (Task 4) ✓, all 5 spec verification steps covered (Task 4, Steps 3-7) ✓. `onLlmModeChange()` deliberately untouched per spec — confirmed no task modifies it.
- **Placeholders:** None — every step shows exact before/after code.
- **Type/name consistency:** `savedLlmMode`, `currentLlmMode`, `renderActiveLlmModeBadge()`, `llm-active-mode-badge` element id are used identically across Tasks 1-4.
