# Script Review attribution corrections (#1951)

Script Review may correct any speaker label, including NARRATOR. Corrections work
in both directions: NARRATOR to a character and a character to NARRATOR. Earlier review
gates treated quote-derived
`spoken=false` and legacy type/speaker labels as
attribution locks. This rejected legitimate character corrections even when the
model preserved every source word. Those labels describe fallible prior analysis;
they cannot prove that a speaker correction is wrong.

The shared review validators now permit those corrections in classic, literal,
index-only, punctuation-recovery and exhaustion-only partial-label paths. Common
request guidance tells the model to use contextual evidence, keep actual narration
as NARRATOR, and return only the required response fields. Single, contextual, batch,
manual-transport and benchmark review use the same request/validation path.

Text identity, complete indexes, nonempty speaker strings, instruction types,
source metadata and explicit pauses remain validated. The model cannot overwrite
spoken, type, source spans or private provenance. The corrected speaker drives
voice grouping; preserved quote-derived fields remain source hints. Source-aligned
comparison tools retain their frozen segmentation contract. A lossless boundary change still
cannot discard conflicting metadata or move a source pause. Partial retention still
retains original text, reports incomplete status and leaves failed batches retryable.
The initial generation attribution gate remains unchanged.

For #1958, classic review explicitly requests only the three string fields
`speaker`, `text` and `instruct`. `spoken`, `source_span`, speaker demographics,
pauses and provenance belong to the source, not the model response. They are
restored from the original entries before publication; both a three-field reply
and a reply echoing metadata go through the same binding. Model-supplied metadata
cannot overwrite the originals. Missing required review fields still fail
validation and retry. A fidelity pass means the source can be preserved, not that
the model reproduced all input keys or made semantically correct speaker choices.

The response contract also applies to custom and literal classic-review prompts.
Index-only review keeps its separate `{n, speaker, instruct}` contract. Review
algorithm identity is 9 for the current review semantics, invalidating older
completed receipts, checkpoints and benchmark evidence.

For #1957, a full-text response may move part of an entry between narrator and
character labels even when the resulting model entry overlaps different source
metadata. Binding partitions that model entry at the original source boundaries
where facts differ or an interior pause must be retained. Each fragment keeps the
model's speaker/instruct and only its own source entry's metadata, with a pause
attached only at that source entry's end. An inherited source span retains its
original enclosing scope; the reviewer cannot invent new offsets or replace it
with a neighbour's span. Compatible source entries may still merge.

This applies by default through the shared classic and literal review binder,
including sequential/contextual CLI, batch, manual transport and benchmarks.
Index-only review retains its fixed boundaries. Complete ordered text remains
mandatory; truncation, missing words and unbound text remain rejected. Metadata
comparison distinguishes nested Boolean and numeric values as well as strings.
Regression coverage generates 1,024 source/response boundary overlays and checks
word ownership, model labels, metadata, pause positions, input immutability and
binding idempotence without adding a testing dependency.

Regression fixtures are original synthetic sentences. The reporter's attachments
were used only for local reproduction; their source and derived text are excluded
from the repository and public evidence. A fidelity gate checks preservation, not
semantic speaker accuracy: an unchanged original label remains structurally valid,
and the reviewer is responsible for deciding which attribution fits the context.
