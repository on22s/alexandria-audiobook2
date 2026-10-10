# Speaker-label suggestions

Use **Script → Speaker-label suggestions** to compare speaker labels with a saved script for the same text. The reference is provisional: a matching passage or two agreeing model judgments do not establish a character's identity.

## Review a snapshot

1. Load the book you want to inspect. Keep generation stopped while reviewing. Save the provisional reference in **Saved Scripts** first.
2. Open **Speaker-label suggestions**, then select **Load review options**. Opening the panel does not make model requests.
3. Choose **Current script** or an available **Completed attribution prefix**, and select the saved reference. Choose a maximum of 1–20 label pairs. An incomplete checkpoint requires **Allow an incomplete attribution checkpoint**.
4. Select **Preview evidence**. This reads saved data without calling the model. Check the source and reference excerpts. Checkpoint text and types must match the frozen source exactly; ambiguous or unmatched reference passages are skipped. Native scripts without explicit types use the displayed narrator-label inference.
5. Check the displayed model, endpoint, context limit and request estimate. Give consent to send the displayed excerpts, then select **Start speaker-label review**. A remote provider may charge for these requests.

This is a stopped-run snapshot review, not a live view of generation. A partial checkpoint must contain a valid attributed prefix. Any change to the book, source, checkpoint, script, reference, model settings or related saved state can make the snapshot stale and require another preview.

## Results, limits and recovery

Each pair receives separate `none` and `low` reasoning judgments, with reasons and any errors shown. All suggestions remain provisional, including results from a complete script.

Each unchanged snapshot has a lifetime limit of **40 model attempts**. Requests have no hidden automatic retries. Successful unchanged judgments are cached; explicitly starting the same unchanged snapshot again can reuse them, and remaining requests still count toward the same limit. An exhausted budget does not silently restart.

**Cancel review** requests a stop; an already-sent model request may still finish. Closing the panel does not cancel a run. After a page reload or lost response, **Load review options** retrieves the available active/recent report without starting or resuming model calls. An interrupted run requires an explicit new Start. If the book changes, the previous evidence is hidden and changed evidence stops further requests; **Cancel previous book's review** is available when an older run is still active.

## Apply one alias deliberately

An **Apply alias…** control appears only when both judgments explicitly suggest the same identity and the backend accepts the pair. Uncertain, contradictory, failed, protected or unsafe pairs cannot be applied here.

Inspect the evidence, choose the alias direction and confirm that single change. Apply saves one alias in the **shared character registry used by later Review runs**. It does not immediately rewrite the script or regenerate audio. The shared registry may affect other books that use it.

After an Apply, the report becomes stale. Load options and preview again before another change. Concurrent registry edits and changed evidence are rejected rather than silently applied. If an Apply response is lost, check **Edit aliases** before trying again.
