# LLM request budgets

In **Setup → LLM Settings**, each Local or Remote profile has three optional token limits:

| Setting | Blank behavior | Effect |
| --- | --- | --- |
| Total context budget | Use detected loaded context, when available | Bounds the combined prompt, output and safety reserve. A declaration cannot raise a smaller detected context. |
| Input ceiling | Inherit the optional Advanced global input ceiling | Bounds the estimated complete prompt, including instructions, cast and surrounding text. |
| Output ceiling | No profile cap: each task keeps its own retry ceiling | When set, bounds completion allowances and retry escalation, including reasoning where the provider counts it toward output. |

Values must be positive whole numbers. There is no universal upper cap or unlimited/bypass setting. Input and output ceilings are separate constraints, not evidence that their sum fits the loaded context. For example, an input ceiling and output ceiling of 4,096 each cannot both be consumed in a 4,096-token context.

If the loaded context is 8,192 and your declaration is 4,096, Alexandria uses 4,096. If the loaded context is 4,096 and your declaration is 8,192, Alexandria still uses 4,096. When the endpoint cannot expose its context, the declaration supplies the missing budget. Declare the context actually supported by your loaded model, rather than its advertised maximum.

Larger three-pass source/context targets require a detected or declared context. Without one, the start request explains which Setup field to set before reserving a background job. A managed LM Studio model that is not loaded yet counts as known, because the run loads it at its planned context first. Planning and actual HTTP/manual requests use the same limit resolver. Oversized prompts are refused; output budgets may be reduced to fit the remaining context. Existing source-fidelity checks, bounded retries and GPU admission remain active.

Token counts use the existing conservative character estimate, not a provider-specific tokenizer. Server-side context enforcement remains necessary. Declaring a smaller budget restricts requests; it does not release VRAM already allocated by the server or reload the model.

## Existing configurations

Nothing changes for an existing configuration until a profile limit is set. Baseline Response Tokens stays a starting budget, not a cap: truncation retries still escalate above it to each task's own ceiling (16384, or the baseline if higher, for three-pass; 32768 for review), so a reasoning model is not cut off at the baseline. Set a profile Output ceiling to cap a model; the combined context check still applies.

No new global input ceiling is imposed on existing configurations: its absent/blank value retains context-based checking. A blank profile input ceiling inherits the global one at run time without changing saved profile settings. Manual transport enforces the same declared limits before presenting a request for copying. Failover uses the secondary profile's limits rather than enlarging them to the primary model's budget.
