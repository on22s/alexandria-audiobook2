# Self-healing Thunder Compute remote LLM setup

*2026-06-22*

## Problem

A live "is Thunder set up?" check on this session's running instance
(`kwalnfcc`) surfaced three independent, unsynced staleness points that all
trip whenever the Thunder instance is deleted and recreated:

1. **No way to start the remote LM Studio server itself.** The "Optimize LM
   Studio settings" toggle (`/api/lmstudio/optimize` ->
   `apply_remote_lmstudio_settings` in `app/lmstudio_settings.py`) only runs
   `lms unload`/`lms load` over SSH — it assumes the server is already up.
   `lms` binds `127.0.0.1` by default, so on a freshly created instance the
   Thunder port-forward shows "Nothing running here" until someone manually
   runs `lms server start --port 1234 --bind 0.0.0.0 --cors` over SSH. No
   route or button does this anywhere (confirmed by grep across `app.py`,
   `lmstudio_settings.py`, `static/index.html`).
2. **`config.json`'s remote `base_url` goes stale.** Each new Thunder
   instance gets a new uuid (e.g. `vf8oc702` -> `kwalnfcc` across two
   instance-creation cycles this week), and the forwarded URL embeds that
   uuid (`https://<uuid>-1234.thundercompute.net/v1`). `llm_remote.base_url`/
   `llm.base_url` in `config.json` are static strings with no sync mechanism,
   so they silently point at a deleted instance until manually edited.
3. **The `tnr-<id>` SSH alias also goes stale, independently of #2.**
   `~/.ssh/config`'s `Host tnr-0` block hardcodes the previous instance's
   IP/port/`IdentityFile` and only updates when `tnr connect <id>` is run
   manually — which itself always drops into an interactive shell (no
   non-interactive "just refresh the config" mode). Symptom seen live this
   session: the Optimize button failing with
   `ssh: connect to host <old-ip> port <old-port>: Connection refused`.

`tnr status --json` (confirmed live) gives everything needed to resolve all
three live, non-interactively: `{id, uuid, ip, port (ssh), httpPorts}` per
running instance. The SSH key for each instance is always at
`~/.thunder/keys/<uuid>` (confirmed: one file per uuid ever created).

## Goal

Make the existing "Optimize LM Studio settings" toggle — and the automatic
pre-batch-job self-heal that already calls the same underlying function —
fully self-healing across Thunder instance recreation, with no new UI
controls and no regression to the generic (non-Thunder) remote-LLM path.

## Design

### 1. New resolver: `resolve_thunder_target`

In `app/lmstudio_settings.py`:

```python
def resolve_thunder_target(ssh_alias: str) -> Optional[dict]:
```

- If `ssh_alias` matches `^tnr-(\d+)$`, shells `tnr status --json`
  (non-interactive, confirmed), parses it, and finds the instance whose
  `id` equals the captured group.
- On match, returns
  `{"instance_id", "uuid", "ip", "ssh_port", "http_port", "key_path"}`
  (`http_port` = `1234` unconditionally — the fixed convention this codebase
  already hardcodes everywhere else for the LM Studio endpoint, e.g. the
  `https://<uuid>-1234.thundercompute.net/v1` pattern in
  `_validate_local_llm_base_url`'s allowlist and every observed instance's
  `httpPorts`; `key_path` = `~/.thunder/keys/<uuid>`). Takes only `ssh_alias`
  as input — no `base_url` parameter, avoiding any dependency on a value
  that might itself be stale.
- Returns `None` (never raises) when: the alias doesn't match the
  `tnr-<digits>` pattern (not a Thunder alias — generic remote endpoints are
  untouched), `tnr` isn't on `PATH`, the command exits non-zero, the JSON is
  malformed, or no instance with that id is currently running. `None` always
  means "fall back to today's literal-alias behavior."

### 2. `_ssh_run` accepts a resolved target

`_ssh_run(ssh_target, remote_cmd, timeout, connect_timeout)`: `ssh_target`
becomes `str | dict`.
- `str` (today's behavior, unchanged): `["ssh", ..., ssh_alias, "bash -lc " + quoted]`.
- `dict` (from `resolve_thunder_target`): builds
  `["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
  "-o", f"ConnectTimeout={connect_timeout}", "-i", key_path, "-p",
  str(ssh_port), f"ubuntu@{ip}", "bash -lc " + quoted]` — connects directly,
  bypassing `~/.ssh/config` entirely.

### 3. `apply_remote_lmstudio_settings` resolves, starts the server, verifies

- Calls `resolve_thunder_target(ssh_alias)` first.
- **If resolved:** prepends `lms server start --port <http_port> --bind
  0.0.0.0 --cors >/dev/null 2>&1; ` to the existing
  `lms unload ...; lms load ...` command (confirmed idempotent and
  self-correcting live: safe to re-run even if already running, and it
  rebinds even if previously bound to `127.0.0.1`). Runs over the resolved
  target dict via `_ssh_run`.
  - On SSH success, computes
    `new_base_url = f"https://{uuid}-{http_port}.thundercompute.net/v1"`
    and does a verification `GET {new_base_url}/models` (timeout ~10s).
    - Verified reachable: returns `(True, msg, new_base_url)`.
    - SSH succeeded but the verification GET fails: returns
      `(False, msg, new_base_url)` with `msg` explaining the SSH step
      succeeded but the public endpoint is still unreachable, and logs via
      `_log_llm_failure("optimize_verify", detail)` (detail includes the
      resolved target's ip/ssh_port/uuid/http_port — never the key file
      contents — plus the GET's error/response).
  - On SSH/lms failure: logs via the existing `_log_llm_failure("optimize",
    detail)`, with `detail` now prefixed by the resolved target's
    ip/ssh_port/uuid so it's unambiguous that resolution succeeded but the
    remote command itself failed.
  - On resolution failure (`resolve_thunder_target` returned `None` because
    of an actual error, not a pattern-mismatch — i.e. `tnr status --json`
    ran but errored): logs via a new
    `_log_llm_failure("thunder_resolve", detail)`, with `detail` containing
    the original `ssh_alias`, the parsed instance id (if the regex matched),
    and the **raw** `tnr status --json` stdout/stderr/exit code.
- **If `resolve_thunder_target` returns `None` because the alias simply
  doesn't match `tnr-<digits>`:** behave exactly as today — treat
  `ssh_alias` as a literal `~/.ssh/config` alias, no resolution attempted,
  no new log kind. This preserves the generic "remote = any OpenAI-compatible
  endpoint" path untouched.
- Return signature becomes `(ok: bool, msg: str, base_url: Optional[str])` —
  `base_url` is non-`None` only on the Thunder-resolved path; `None` on the
  legacy-alias path (signals "unchanged, don't touch config's base_url").

### 4. Persisting the last-known-good state

On a **verified-reachable** success only, persist to `config.json`:
```json
"llm_remote": {
  ...,
  "last_synced": {"uuid": "...", "ip": "...", "ssh_port": ..., "base_url": "...", "timestamp": "..."}
}
```
This gives a baseline to diff against in a future failure log, rather than
each failure log standing alone.

### 5. `ensure_ideal_settings` propagates the healed URL

Return signature gains a 4th value: `(is_remote, status, message, base_url)`.
- Local mode / legacy-alias remote: `base_url` is just the input echoed back
  (no behavior change).
- Resolved-Thunder remote: `base_url` is the fresh URL from step 3 (or the
  original if resolution/verification failed — never silently substitutes a
  broken URL for a working one).

### 6. Callers use and persist the healed URL

- **`app.py`'s `POST /api/lmstudio/optimize`:** if the returned `base_url`
  differs from what's stored, persist it into `config.json`'s
  `llm_remote.base_url` (and mirror into `llm.base_url` since remote is
  active, matching the existing mirror-on-save convention) and include it in
  the JSON response.
- **`review_script.py` (~line 823-827):** call `ensure_ideal_settings` (as
  today) but now use its returned `base_url` — not the original local
  variable — for `OpenAI(base_url=...)`. If it changed, persist the same two
  keys as the route above (`llm_remote.base_url` + mirrored `llm.base_url`).
- **`find_nicknames.py` (~line 313-327):** reorder so `OpenAI(...)`
  construction happens *after* `ensure_ideal_settings`, using its returned
  `base_url`. If it changed, persist the same two keys as above.

### 7. UI

No new fields or buttons.
- `toggleLmStudioOptimize()`: on a successful response that includes a
  changed `base_url`, update `#llm-url`'s value and show
  "Base URL auto-synced to `<url>`" in the existing message area next to the
  toggle.
- On failure, the existing alert/toast (HTTP 502 detail + log path) is
  unchanged in shape — the log path now may point at
  `llm_thunder_resolve_*.log` or `llm_optimize_verify_*.log` depending on
  which stage failed, instead of always `llm_optimize_*.log`.
- The "Remote SSH host alias" field's form-text gets one added sentence:
  a `tnr-<id>`-shaped value now resolves live each time (no more manual
  `tnr connect` needed after recreating the instance); any other value is
  still used as a literal `~/.ssh/config` alias, unchanged.

## Out of scope

- `generate_script.py` (single-book generation) does not call
  `ensure_ideal_settings` today and this design does not add that call —
  out of scope for this fix (pre-existing gap, unrelated to instance churn).
- No change to local-mode (`llm_mode=local`) behavior at all —
  `resolve_thunder_target` is never invoked on that path.
- No Thunder REST API / credential integration — resolution is entirely via
  the already-authenticated local `tnr` CLI, consistent with how this
  codebase already shells out to `lms`/`ssh` rather than adding new
  credential storage.
- No change to `_validate_ssh_alias` or to how non-`tnr-*` aliases are
  validated/used.
- Not handling the case of multiple Thunder instances with ambiguous/
  reused `id` values beyond what `tnr status --json` itself reports — if
  Thunder's `id` semantics turn out not to be a stable per-account slot
  across every account configuration, that's a follow-up, not blocking this
  fix (this session only observed one instance, id `"0"`, across two
  create/delete cycles).

## Testing / verification plan

1. `resolve_thunder_target` parsing, against captured `tnr status --json`
   fixtures: matched id, non-`tnr-*` alias (no-op), instance-id-not-found,
   malformed JSON, `tnr` missing from `PATH`.
2. Live smoke test on the current instance: remove `~/.ssh/config`'s
   `Host tnr-0` block (simulating staleness) and confirm
   `apply_remote_lmstudio_settings` still succeeds via the resolved direct
   target.
3. Live smoke test of the reachability check: stop the remote LM Studio
   server (`lms server stop` over the resolved target), run Optimize,
   confirm it restarts the server with `--bind 0.0.0.0` and the post-success
   verification GET passes.
4. Confirm legacy fallback: configure a non-`tnr-*` literal SSH alias and
   confirm behavior (commands run, success/failure paths) is unchanged from
   today's code.
5. Confirm `config.json`'s `llm_remote.base_url` and `llm_remote.last_synced`
   are actually persisted after a successful run, and that the Setup tab's
   `#llm-url` field reflects the new value without a manual page reload.
6. Confirm `review_script.py`/`find_nicknames.py` batch jobs recover
   end-to-end (use the healed `base_url` for their actual LLM calls) when
   started cold against a recreated instance, without any manual Optimize
   click first.
