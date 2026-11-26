"""Pure profile inheritance and request-budget checks shared by all transports."""
import re


def get_inherited_profile_limits(profile, generation):
    """Return a profile copy with the optional global input fallback.

    A blank output limit stays blank: generation.max_tokens is the starting
    budget, and each task keeps its own escalation ceiling above it (16384+
    for three-pass, 32768 for review). Inheriting it as a cap truncated
    reasoning models at the baseline on retry.
    """
    result = dict(profile)
    if result.get("max_input_tokens") is None and generation.get("max_input_tokens") is not None:
        result["max_input_tokens"] = generation["max_input_tokens"]
    return result


def get_task_output_fingerprint(params):
    """Include distinct failover policy without invalidating legacy task defaults."""
    return {name: getattr(params, name) for name, current in (
        ("task_max_tokens", "max_tokens"), ("task_hard_max_tokens", "hard_max_tokens"))
        if getattr(params, name, None) is not None
        and getattr(params, name) != getattr(params, current)}


def get_request_limits(profile, detected_context=None):
    """Resolve finite positive limits; declarations never enlarge live context."""
    limits = {}
    for name in ("context_length", "max_input_tokens", "max_output_tokens"):
        value = profile.get(name)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer or blank")
        limits[name] = value
    if detected_context:
        limits["context_length"] = min(
            limits["context_length"] or int(detected_context), int(detected_context))
    return limits


def get_prompt_token_estimate(messages):
    """Conservative text estimate, matching the production context checker."""
    return (sum(len(str(message.get("content") or "")) for message in messages or []) + 2) // 3


# A run this long of one character, or of whitespace, is packed many-to-one by
# every supported tokenizer (dot leaders, rules, indents, blank lines). Shorter
# runs are ordinary text and keep the prose rate.
_PACKED_RUN = re.compile(r"\s{8,}|(\S)\1{7,}")


def get_packed_prompt_token_estimate(messages):
    """Token estimate that does not credit compressible runs at the prose rate.

    get_prompt_token_estimate assumes 3 characters per token, which overestimates
    tokens for ordinary prose (about 4.4 characters per token) but wildly
    overestimates a prompt made of dot leaders, rules, indents or blank lines:
    tokenizers pack a long run of one character into a few tokens. Counting each
    such run as one character keeps a faithful report above half the estimate,
    so those prompts do not look truncated. Only the truncation check uses this;
    request admission keeps get_prompt_token_estimate.
    """
    length = sum(len(_PACKED_RUN.sub(lambda run: run.group(0)[0],
                                     str(message.get("content") or "")))
                 for message in messages or [])
    return (length + 2) // 3


def get_usage_count(usage, name):
    """Read one non-negative integer usage count from an SDK object or a dict.

    Some servers serialize counts as ``100.0``; an integral float is that same
    evidence. Booleans, strings, negatives and non-integral floats are not.
    """
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    return int(value) if value >= 0 else None


def get_separately_cached_prompt_tokens(usage):
    """Cached prompt tokens a server reports OUTSIDE ``prompt_tokens``.

    Only the Anthropic shape does that: ``input_tokens`` excludes
    ``cache_read_input_tokens``, which is its own field. Every other supported
    shape reports cached tokens as a SUBSET of ``prompt_tokens`` -- OpenAI
    ``prompt_tokens_details.cached_tokens``, Responses
    ``input_tokens_details.cached_tokens``, DeepSeek ``prompt_cache_hit_tokens``
    (``prompt_tokens`` = hit + miss), llama.cpp and Ollama ``cached_tokens`` --
    so adding those would hide a real truncation by up to the cached fraction.
    """
    return get_usage_count(usage, "cache_read_input_tokens") or 0


def get_suspect_prompt_tokens_message(finding):
    """The one warning line every usage-recording caller prints."""
    return (f"WARNING: server reported prompt_tokens={finding['prompt_tokens_reported']} "
            f"for a request estimated at {finding['prompt_tokens_estimate']} tokens; "
            "it appears to have read only part of the prompt (truncated request). Continuing.")


def get_suspect_prompt_tokens(messages, usage):
    """Describe a prompt-token report too low to have read the whole request.

    A server whose real context is smaller than the configured one cuts the
    prompt and answers from the rest, reporting the smaller count in
    ``usage.prompt_tokens``. The estimate counts long runs of one character or
    of whitespace once (see get_packed_prompt_token_estimate): ordinary prose
    tokenizes at about 4.4 characters per token, so a faithful report stays
    above half the estimate, and prompts tokenizers pack many-to-one (dot
    leaders, indents, blank lines) cannot cross it. Dense scripts tokenize at
    roughly one to two characters per token and sit far below it, so a CJK
    prompt cut by more than half may go unreported. Missing, malformed or zero
    counts are no evidence at all.

    Cached prompt tokens are added back only when the server reports them
    separately from ``prompt_tokens`` (see
    get_separately_cached_prompt_tokens).
    """
    reported = get_usage_count(usage, "prompt_tokens")
    if not reported:
        return None
    estimate = get_packed_prompt_token_estimate(messages)
    if (reported + get_separately_cached_prompt_tokens(usage)) * 2 >= estimate:
        return None
    return {"prompt_tokens_estimate": estimate, "prompt_tokens_reported": reported}


def get_budgeted_request(kwargs, limits):
    """Return request options bounded by profile and combined context limits."""
    from lmstudio_settings import get_effective_max_tokens
    result = dict(kwargs)
    body = dict(result.get("extra_body") or {})
    if "messages" in body:
        messages = body.pop("messages")
        # Caller messages have already passed task/runtime input admission.
        if "messages" not in result:
            result["messages"] = messages
    output_key = ("max_completion_tokens" if "max_completion_tokens" in result
                  or ("max_tokens" not in result and "max_completion_tokens" in body)
                  else "max_tokens")
    budgets = [result[name] for name in ("max_tokens", "max_completion_tokens")
               if result.get(name) is not None]
    budgets.extend(body.pop(name) for name in ("max_tokens", "max_completion_tokens")
                   if name in body)
    for value in budgets:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("Request output budget must be a positive integer")
    if "extra_body" in result:
        result["extra_body"] = body
    # Typed output has already passed the caller's task/runtime checker.
    # SDK body overrides may lower it, but must never enlarge that allowance.
    requested = min(budgets) if budgets else None
    if requested is None:
        requested = limits.get("max_output_tokens")
    if requested is None:
        # Every production caller supplies an output budget. With a declared
        # context, refusing an unbudgeted call is safer than a provider default.
        if limits.get("context_length"):
            raise ValueError("A request output budget is required with a context limit")
        get_effective_max_tokens(1, messages=result.get("messages"),
                                 max_input_tokens=limits.get("max_input_tokens"))
        return result
    if output_key == "max_completion_tokens":
        result.pop("max_tokens", None)
    result[output_key] = get_effective_max_tokens(
        requested, limits.get("context_length"), result.get("messages"),
        limits.get("max_output_tokens") or requested, scale_to_context=False,
        max_input_tokens=limits.get("max_input_tokens"))
    return result
