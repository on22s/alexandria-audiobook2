# Experimental prompt presets

Issue #1937 adds editable prompts under Setup → Prompt Studio for:

- **Compact labels**: a separate Pass 1 system/user preset. The user template must contain exactly one {units} placeholder. The pipeline inserts every numbered source unit; retain the JSON array of {n, type} labels. The existing quote parser, source-preserving segment validation and ordinary Pass 1 fallback remain active.
- **Notes with timed pauses**: a separate Pass 3 preset for the gaps mode. Keep exactly one {batch} placeholder and the numbered n/instruct/gap reply format.
- **Directed replies and intent only**: a shared Pass 3 preset for directed and action_only modes. Both request n, answers, action, delivery and gap. Intent only omits delivery when composing the final instruction.

System instructions are literal text. User templates use Python format syntax: escape literal braces as {{ and }}; only the one complete payload placeholder is allowed, without field traversal, conversion, or formatting. Blank, oversized or malformed presets are rejected on config save. A missing, duplicate or damaged active experimental selection refuses execution before a model request. Do not remove the documented response fields: the existing response validators still apply.

Each family supports Save preset and Setup, Delete preset and save Setup, and switching between saved presets and checked-in Default. These actions share the existing transaction and stale-editor guards, preserve other Setup edits, and save through the ordinary config API. Reset restores every checked-in prompt selection while retaining saved presets. An incomplete defaults response changes no fields and can be retried. Help and controls are localized in English, Chinese, Japanese and Latin American Spanish.

Saving a preset does not enable a generation experiment. Select Compact labels or the desired delivery direction under Script strategy, then save Setup. Experimental delivery modes still require the ordinary Step 3 preset to be Default; their own presets are edited separately. Defaults remain ordinary Pass 1 and delivery direction off.

The saved settings are prompts.pass1_compact_preset / pass1_compact_prompt_presets, pass3_gaps_preset / pass3_gaps_prompt_presets, and pass3_directed_preset / pass3_directed_prompt_presets. Presets store name, description, system_prompt and user_prompt. The application, single-book and batch CLI paths share get_three_pass_run_params and the canonical request builders. HTTP and manual transports receive the same prompt and complete payload. Budget admission measures those constructed requests; custom instructions do not bypass model limits. Active custom text changes checkpoint identity; inactive families do not. Default requests and checkpoint identity stay compatible.

Validation uses synthetic source and mocked responses; no live model-quality improvement or GPU qualification is implied.
