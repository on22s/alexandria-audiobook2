# Experimental incremental cast discovery (#1931)

In Setup, enable **Experimental incremental cast and Step 2 aliases**, then Save
Configuration. It is off by default. Set **Cast source characters per request**
(default 30,000) and **Cast reply budget (tokens)** (default 8,000).
The reply budget also applies to the existing whole-book mode. Profile input,
output and total-context limits still apply; these controls do not enlarge them.

Build cast list processes every character of the same prepared source generation
uses, in consecutive windows. Each request includes the complete validated roster
with every retained alias from all earlier windows. Only source text counts toward
the character target; instructions and the growing roster also consume context.
The whole-book 1,200,000-character limit remains in the default mode. Experimental
mode can process larger books without dropping a tail or truncating the roster.
Windows pack complete paragraphs and end at blank-line paragraph breaks, including
blank lines containing Unicode whitespace. Unicode line separators (U+2028) follow
the same rule; a single Unicode paragraph separator (U+2029) is also a paragraph
boundary. Publisher-colophon removal uses the same paragraph boundaries and retains the story
as an exact source substring, including its separators. Single wrapped line breaks stay inside their paragraph. Separators count toward
the strict character cap, and all whitespace is preserved. If any paragraph plus
its separator exceeds the cap, discovery refuses before sending any model requests;
increase **Cast source characters per request** to fit it, or supply a source with
blank-line paragraph breaks. The latter is necessary for paragraph-per-line text
that otherwise becomes one oversized unit; raising the cap cannot override model
context limits. Editing the source changes its byte identity: existing casts remain
bound to the old source, and a build pinned to the old hash refuses. Select the edited
source again before building a new cast. The builder never splits
the paragraph or silently exceeds the cap. Smaller windows can still lose identity
context, so review the resulting cast. Accuracy improvements have not been established.

A reply may contribute an empty list. Exact established names or aliases add to
that same canonical identity. New names remain separate; there is no fuzzy identity
merge. Contradictory alias ownership stops the build and reports the conflict.
Protected narration, UNKNOWN and group aliases are refused and reported. A malformed
reply aborts; a reply cut off at its token limit is discarded and retried up to
three times. Other unfinished or unknown completion reasons are refused in experimental
mode even if the JSON parses. No repair of truncated JSON is attempted.

Progress appears in the existing Script task log, including window offsets,
roster size and estimated input/requested output tokens. Manual transport uses the
same windows, budgets, validation and paste panel. Cancel stops the task. Any
failure, conflicting ownership, empty final result or changed source preserves the
previous complete cast. There is no window checkpoint or partial-cast resume;
retry starts discovery from the beginning.

Only a nonempty validated final cast is atomically published under the source-byte
hash. Its provenance records raw/prepared hashes, preprocessing, all window ranges,
window/request hashes, budgets, attempts and available token usage. New incremental
builds record `window_boundary_policy: paragraph-v1`. Progress totals use the actual
packed-window count, which can exceed a simple character-count estimate. Whitespace-only
windows remain in coverage with status `skipped_whitespace`, zero attempts and zero
usage; they do not send a model request or change the roster. Their request hash and
completion reason are null. New builds record `empty_window_policy: skip-whitespace-v1`. Source identity
is checked before preparation and again under the final publication lock. Incremental
completion reasons are recorded per window; there is no single top-level response.
Usage totals cover accepted window responses only, excluding discarded attempts. A
missing usage field stays unknown rather than being counted as zero.
Configured profile limits check the full prompt, including the roster, before each
experimental window. If limits are undeclared, the provider can still reject a
request; the character target alone cannot guarantee model-context fit.

The switch also appends explicit canonical-name/alias associations to every Step 2
request, including the default prompt, alternate variants and custom presets.
The prompt preview uses the current experimental checkbox and synthetic sample aliases.
Request sizing uses the same evidence. Generation logs a diagnostic if the switch is on
but no cast aliases are loaded; building a cast remains optional. Step 2 validation/canonicalization remains
unchanged. The opt-in and alias evidence participate in checkpoint identity; legacy
prompts and checkpoint semantics stay unchanged with the switch off. Existing
alias-oriented variants retain their previous behavior when it is off.

Batch **Build a cast list for each book first** uses these saved settings, builds
only missing casts, and refuses to generate a book after its cast build fails.
Existing casts are reused. As before, book preflight is an estimate: a cast discovered
after preflight is checked against the actual request budget during generation.

CLI (uses the saved settings unless overridden):

~~~text
python -u app/cast_list.py synthetic.txt --out cast.json --incremental --source-chars 30000 --max-tokens 12000
python -u app/three_pass_generate.py synthetic.txt --cast-file cast.json --cast-aliases
~~~

Use --no-incremental or --no-cast-aliases to override the opt-in.

POST /api/cast_list/build accepts strip_front_matter plus optional incremental,
source_chars and max_tokens overrides. Omitted controls use the saved configuration.
POST /api/generate_script/batch/start accepts the same controls in cast_list_settings;
the batch snapshots them for all books, including its Step 2 alias switch.
The persistent generation keys are cast_list_incremental, cast_list_source_chars,
and cast_list_max_tokens. No provider credential or source text is stored in this
feature's provenance.
