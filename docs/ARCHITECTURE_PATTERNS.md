# Architecture patterns

Alexandria uses small, practical versions of familiar design patterns. These
are descriptions of boundaries that already exist, not requirements to add
frameworks, interfaces, or Java-style class hierarchies.

## Provider and voice selection

| Pattern | Implementation | Boundary |
| --- | --- | --- |
| Adapter | `app/llm_provider.py`, `app/tts.py`, `app/attribution_adapter.py` | Provider-specific clients, TTS backends, and served LoRA adapters are checked or used through app-level operations. |
| Strategy | `app/tts.py`, `app/attribution_prompt_variants.py` | Voice categories, narrator strategies, prompt variants, and local/remote inference select one behavior while preserving the caller contract. |
| Failover | `app/llm_provider.py` | `FailoverClient` switches once to the configured secondary profile and stays there for the remainder of a run. |

Keep these decisions centralized. A new caller should use the existing
resolver or provider helper instead of reimplementing local/remote or voice
classification logic.

## Work pipelines and task control

| Pattern | Implementation | Boundary |
| --- | --- | --- |
| Pipeline | `app/generate_script.py`, `app/review_script.py`, `app/project.py` | Intake, generation/review, quality recovery, rendering, and export are staged operations with checkpoints or progress. |
| State | `app/core.py` (`process_state`) | Long-running work exposes running, paused, cancelled, failed, and completed state to routes and status views. |
| Command | `app/core.py` and task routers | Start, pause, resume, and cancel operations act on a registered task rather than directly exposing worker internals. |
| Facade | `app/project.py` (`ProjectManager`) | Project loading, chunk rendering, timeline assembly, and export are presented as higher-level operations. |

Every new long-running GPU or LLM task must register in `process_state` and
use `claim_gpu_task` unless it is explicitly CPU-only. Cancellation and pause
must preserve the existing checkpoint and cleanup behavior.

## Safety boundaries

- `claim_gpu_task` performs the atomic final GPU-lock check; a preliminary
  status check is not sufficient because two requests can race.
- Adapter verification treats an endpoint that cannot answer
  `/lora-adapters` as **unknown**, not proof that the adapter is absent.
- Retry and failover decisions must remain bounded and consistent. A normal-
  looking fallback result must never hide a provider or configuration error.
- Batch routing must preserve voice-specific configuration. Dynamic narrator
  selection and ensembles are kept out of incompatible shared batch paths.

## When not to add a pattern

Do not add a factory, event bus, dependency-injection framework, ORM/repository
layer, or abstract class merely to match a catalog entry. Add an abstraction
only when a second real implementation or a repeated, testable decision makes
the existing boundary difficult to maintain.

The pattern names are useful vocabulary for design discussion; the existing
Python modules and flat-file storage remain the source of truth.

## Verification

Relevant regression coverage includes:

- `app/tests/test_attribution_adapter.py`
- `app/tests/test_profile_failover.py`
- `app/tests/test_llm_provider.py`
- `app/tests/test_multi_process_state.py`
- `app/tests/test_runtime_regressions.py`

When changing one of these boundaries, run the focused tests first and then
`./ready.sh` before committing.
