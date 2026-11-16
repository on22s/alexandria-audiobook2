import os

import utils


def load_prompts_file(path, num_parts, missing_msg, malformed_msg, cache):
    """Read a ---SEPARATOR----delimited prompts file and split it into
    `num_parts` stripped strings.

    Shared by default_prompts.py/persona_prompts.py/review_prompts.py, which
    differ only in path/part-count/messages. Uses a file-version cache (the
    caller's own dict, so each prompt file gets its own) to pick up edits
    without restarting the app while avoiding redundant reads and splitting on
    every call within one process - app.py's get_config()/get_default_prompts()
    call all three loaders together, repeatedly, on the same request paths.
    """
    try:
        token = utils.get_file_cache_token(path)
        malformed_match = token is not None and cache.get('malformed_token') == token
        valid_match = token is not None and cache.get('file_token') == token
        if malformed_match and utils.is_file_cache_token_trusted(token, cache.get('malformed_captured_at_ns')):
            raise RuntimeError(malformed_msg)
        if (valid_match and cache.get('prompts') is not None and
                utils.is_file_cache_token_trusted(token, cache.get('file_captured_at_ns'))):
            return cache['prompts']
        payload, (file_version, content_hash, token, captured_at_ns) = utils.read_file_snapshot(
            path, opener=None if malformed_match or valid_match else open)
    except FileNotFoundError:
        raise RuntimeError(missing_msg)
    except OSError as exc:
        raise RuntimeError(f"Error reading {path}: {exc}") from exc

    if (cache.get("malformed_version") == file_version and
            cache.get("malformed_hash") == content_hash):
        cache['malformed_token'] = token
        cache['malformed_captured_at_ns'] = captured_at_ns
        raise RuntimeError(malformed_msg)
    if (cache.get("file_version") == file_version and
            cache.get("content_hash") == content_hash and cache.get("prompts") is not None):
        cache['file_token'] = token
        cache['file_captured_at_ns'] = captured_at_ns
        return cache["prompts"]

    try:
        raw = payload.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
    except Exception as e:
        raise RuntimeError(f"Error reading {path}: {e}")

    # No maxsplit: a file with one extra stray delimiter (e.g. a copy-paste
    # slip while editing) must still produce the wrong part count and raise
    # malformed_msg, rather than silently absorbing the extra delimiter (and
    # everything after it) into the last part's text.
    parts = raw.split("---SEPARATOR---")
    if len(parts) != num_parts:
        cache["malformed_version"] = file_version
        cache["malformed_hash"] = content_hash
        cache["malformed_token"] = token
        cache["malformed_captured_at_ns"] = captured_at_ns
        raise RuntimeError(malformed_msg)

    prompts = tuple(p.strip() for p in parts)
    if not all(prompts):
        cache["malformed_version"] = file_version
        cache["malformed_hash"] = content_hash
        cache["malformed_token"] = token
        cache["malformed_captured_at_ns"] = captured_at_ns
        raise RuntimeError(malformed_msg)
    cache.pop("malformed_version", None)
    cache.pop("malformed_hash", None)
    cache.pop("malformed_token", None)
    cache.pop("malformed_captured_at_ns", None)
    cache["file_version"] = file_version
    cache["content_hash"] = content_hash
    cache["file_token"] = token
    cache["file_captured_at_ns"] = captured_at_ns
    cache["prompts"] = prompts
    return prompts
