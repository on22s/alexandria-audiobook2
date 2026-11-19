import os

from experimental_prompts import get_template_fields
from prompt_loader import load_prompts_file

_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "..", "review_prompts.txt")
_prompt_cache = {"prompts": None}
REVIEW_TEMPLATE_FIELDS = ("context", "batch")


def validate_review_template(template):
    """Require both payloads the review pass fills in a saved user prompt."""
    fields = get_template_fields(template, "Review")
    for name in fields:
        if name not in REVIEW_TEMPLATE_FIELDS:
            raise ValueError("Review user prompt allows only {context} and {batch}, without formatting")
    for field in REVIEW_TEMPLATE_FIELDS:
        if field not in fields:
            raise ValueError(f"Review user prompt needs the {{{field}}} payload")
    return template


def load_review_prompts():
    """Read review_prompts.txt from disk and return (system_prompt, user_prompt).

    Uses a file-version cache to pick up edits without restarting the app,
    avoiding redundant disk reads when the file hasn't changed.
    """
    return load_prompts_file(
        _PROMPTS_FILE, 2,
        missing_msg=(
            f"review_prompts.txt not found at {os.path.abspath(_PROMPTS_FILE)}. "
            "This file is required for the review pass."
        ),
        malformed_msg=(
            "review_prompts.txt is malformed: expected exactly one "
            "'---SEPARATOR---' delimiter."
        ),
        cache=_prompt_cache,
    )


# Keep unrelated app features available when this optional workflow resource is
# damaged; review itself still fails loudly when it tries to load the prompts.
try:
    REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT = load_review_prompts()
except RuntimeError:
    REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT = None, None
