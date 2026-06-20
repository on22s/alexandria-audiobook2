import os

from prompt_loader import load_prompts_file

_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "..", "persona_prompts.txt")
_prompt_cache = {"mtime": None, "prompts": None}


def load_persona_prompts():
    """Read persona_prompts.txt from disk and return (system_prompt, user_prompt, advanced_prompt).

    Uses an mtime-based cache to pick up edits without restarting the app,
    avoiding redundant disk reads when the file hasn't changed.
    """
    return load_prompts_file(
        _PROMPTS_FILE, 3,
        missing_msg=(
            f"persona_prompts.txt not found at {os.path.abspath(_PROMPTS_FILE)}. "
            "This file is required for persona generation."
        ),
        malformed_msg=(
            "persona_prompts.txt is malformed: expected exactly two "
            "'---SEPARATOR---' delimiters (system prompt, basic user prompt, "
            "advanced compilation prompt)."
        ),
        cache=_prompt_cache,
    )


# Cached at import time — used by generate_personas.py (subprocess, fresh each run)
PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT, PERSONA_ADVANCED_PROMPT = load_persona_prompts()
