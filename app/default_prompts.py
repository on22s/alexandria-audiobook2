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


# Cached at import time — used by generate_script.py (subprocess, fresh each run).
# Guard the load so a missing/malformed default_prompts.txt can't crash importers
# at module load: app.py imports this module for the Setup tab and must still
# start. generate_script uses these as fallbacks (params/config prompts normally
# override them) and fails loudly if it ends up with no usable prompt.
try:
    DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = load_default_prompts()
except RuntimeError:
    DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT = None, None


_SEGMENT_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_segment.txt")
_ATTRIBUTE_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_attribute.txt")
_INSTRUCT_FILE = os.path.join(os.path.dirname(__file__), "default_prompts_instruct.txt")
_segment_cache = {"mtime": None, "prompts": None}
_attribute_cache = {"mtime": None, "prompts": None}
_instruct_cache = {"mtime": None, "prompts": None}


def _load_pair(path, cache, name):
    return load_prompts_file(
        path, 2,
        missing_msg=f"{name} not found at {os.path.abspath(path)}.",
        malformed_msg=f"{name} is malformed: expected one '---SEPARATOR---'.",
        cache=cache)


def load_segment_prompts():
    return _load_pair(_SEGMENT_FILE, _segment_cache, "default_prompts_segment.txt")


def load_attribute_prompts():
    return _load_pair(_ATTRIBUTE_FILE, _attribute_cache, "default_prompts_attribute.txt")


def load_instruct_prompts():
    return _load_pair(_INSTRUCT_FILE, _instruct_cache, "default_prompts_instruct.txt")
