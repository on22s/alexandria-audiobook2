"""Dedicated prompts selected only by the explicit literal annotation profile."""
# Checkpoint identity for the set of passes these prompts are applied to. v2:
# Pass 1 and review only; Pass 2/3 (index-only, text frozen) use the ordinary
# prompts since 46b5eb0a. Bumped so a v1 checkpoint is not resumed with Pass 2
# results produced under the old prompts. Script provenance
# (literal_policy.PROMPT_VERSION) is unchanged: the word guarantee did not change.
CHECKPOINT_PROMPT_POLICY = "literal_prompts_v2"
LITERAL_RULE = (
    "Preserve every source word, its case, lexical spelling and order. "
    "Do not omit attribution, insert words, change pronouns, summarize, correct "
    "grammar, expand numbers or Roman numerals, verbalize symbols, abbreviations "
    "or pronunciation. Any desired speech expansions belong in later speech "
    "preparation, not narration. "
    "You may assign speaker and delivery labels and split or merge entries. "
    "Dialogue delimiters and whitespace may change; source words must not."
)

ANNOTATION_SYSTEM = (
    "Annotate audiobook narration as a JSON array of objects with speaker, text "
    "and instruct string fields. Use NARRATOR for narration and grounded "
    "character names for dialogue. Delivery notes belong only in instruct. "
    + LITERAL_RULE
)
ANNOTATION_USER = "Annotate this complete source without changing its words:\n{chunk}"
REVIEW_SYSTEM = (
    "Review an audiobook script. Return a JSON array with speaker, text and "
    "instruct string fields. Correct labels and delivery notes only. " + LITERAL_RULE
)
REVIEW_USER = "{context}\nReview this complete batch:\n{batch}"


def get_literal_system_prompt(base):
    """Keep a pass-specific schema while overriding its speech-expansion rules."""
    return base + "\n\nEXPLICIT LITERAL ANNOTATION POLICY (takes precedence):\n" + LITERAL_RULE
