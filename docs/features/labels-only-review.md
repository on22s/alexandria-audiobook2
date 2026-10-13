# Experimental labels-only review

Full-text review can reject useful speaker corrections when the model changes punctuation or moves text (#1174). Experimental labels-only review requests one `{n, speaker, instruct}` object per source entry. Code binds the validated labels back to deep copies of the originals, preserving text, entry order, metadata and literal-source policy.

Run `review_script.py --labels-only`, or set `prompts.review_labels_only` to true in configuration. This mode uses its own labels-only prompt, rather than the custom full-text review templates. It is off by default. The ordinary review remains available for lossless boundary changes; the labels-only model response cannot split or merge source entries. Existing configured post-processing, including consecutive-narrator merging, still runs afterward.

Missing, duplicate or out-of-range indexes, extra fields and invalid labels cause normal validation retries. Exhausted batches keep the existing caller fallback/resume behavior. Review receipts include the enabled mode so a completed full-text review cannot satisfy a labels-only run.

Repeated paired live PDNC evaluation must establish accuracy and retry tradeoffs before this becomes the default. The pilot uses controlled speaker mistakes on gold quotation spans; that is narrower than review of arbitrary segmented books.

The request schema fixes each batch’s count and allowed indexes using the shared helper from #1178; duplicate-index validation remains required. Explicit `NARRATOR` types, and untyped entries already labelled `NARRATOR`, cannot receive character labels. An explicit `SPOKEN` type permits correcting an erroneous narrator label. The shared schema helper from #1178 is included on the current main base.
