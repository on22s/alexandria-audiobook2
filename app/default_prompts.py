import os

from prompt_loader import load_prompts_file

_PROMPTS_FILE = os.path.join(os.path.dirname(__file__), "..", "default_prompts.txt")
_prompt_cache = {"mtime": None, "prompts": None}


def load_default_prompts():
    """Read default_prompts.txt from disk and return (system_prompt, user_prompt).

    Uses an mtime-based cache to pick up edits without restarting the app,
    avoiding redundant disk reads when the file hasn't changed.
    """
    return load_prompts_file(
        _PROMPTS_FILE, 2,
        missing_msg=(
            f"default_prompts.txt not found at {os.path.abspath(_PROMPTS_FILE)}. "
            "This file is required for LLM prompt defaults."
        ),
        malformed_msg=(
            "default_prompts.txt is malformed: expected exactly one "
            "'---SEPARATOR---' delimiter."
        ),
        cache=_prompt_cache,
    )


# Cached at import time — used by generate_script.py (subprocess, fresh each run)
DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = load_default_prompts()
