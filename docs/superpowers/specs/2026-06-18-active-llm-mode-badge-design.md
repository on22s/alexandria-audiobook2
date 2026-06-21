# Active LLM mode indicator (Setup tab)

*2026-06-18*

## Problem

The Setup tab's "LLM Location" dropdown (`app/static/index.html`) is a
client-side, unsaved selection until "Save" is clicked. The actually active
profile — the one real LLM calls, `/api/lmstudio/status`, and
`/api/lmstudio/optimize` all use — is whatever `llm_mode` was last persisted
to `config.json`. These can silently diverge: a user can switch the dropdown
to Remote, successfully Test Connection against it (which posts the typed
values directly, bypassing saved config), and walk away believing they're on
Thunder while every real call still goes to Local. Nothing in the UI
currently distinguishes "what the dropdown shows" from "what's actually
active."

## Goal

Make the actually-active LLM mode visible at all times on the Setup tab,
independent of the dropdown's current (possibly unsaved) value, so this
mismatch is self-evident rather than requiring backend inspection.

## Design

### 1. Badge markup

Add a neutral badge next to the existing "LLM Location" label
(`app/static/index.html` ~line 515):

```html
<label class="form-label">LLM Location
  <span id="llm-active-mode-badge" class="badge bg-secondary ms-1">Active: Local</span>
</label>
```

Always visible, single neutral style (`bg-secondary`) — no conditional
warning styling. When it matches the dropdown, it's confirmation; when it
diverges, the divergence itself is the signal.

### 2. State tracking

Introduce `savedLlmMode`, distinct from the existing `currentLlmMode` (which
already tracks the dropdown's live, possibly-unsaved value):

- In `loadConfig()`, immediately after `currentLlmMode = config.llm_mode ||
  'local'`: set `savedLlmMode = currentLlmMode` and call a new
  `renderActiveLlmModeBadge()` that sets the badge text to `Active: Local` or
  `Active: Remote (Thunder / network)` based on `savedLlmMode`.
- In the config-save handler, after `await API.post('/api/config', config)`
  resolves successfully: set `savedLlmMode = currentLlmMode`, then call
  `renderActiveLlmModeBadge()` again.
- `onLlmModeChange()` is **not** modified — switching the dropdown must not
  move the badge. That gap is the point.

### 3. No backend changes

`GET /api/config` already returns `llm_mode`; nothing server-side needs to
change.

## Out of scope

- Not touching the "Optimize LM Studio settings" badge/wording (the
  always-visible-by-the-dropdown placement was chosen specifically over
  extending that badge).
- Not changing `testLlmConnection()`'s behavior or messaging.
- Not auto-saving on dropdown change, and not blocking/warning the user from
  leaving the tab with unsaved changes — purely a passive indicator.

## Testing / verification plan

1. Load Setup tab fresh: badge matches whatever `llm_mode` is in
   `config.json` (e.g. "Active: Local").
2. Switch dropdown to Remote without saving: badge still reads "Active:
   Local".
3. Click Save: badge updates to "Active: Remote (Thunder / network)".
4. Reload the page: badge still reads "Active: Remote (Thunder / network)",
   matching persisted `config.json`.
5. Switch back to Local and Save: badge returns to "Active: Local".
