"""Pure profile inheritance and request-budget checks shared by all transports."""


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


def _get_usage_int(usage, name):
    """Read one integer usage field from an SDK object or a plain dict."""
    value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
    return value if type(value) is int else None


def _get_cached_prompt_tokens(usage):
    """Cached prompt tokens in any spelling a supported server may report."""
    cached = 0
    for field in ("prompt_tokens_details", "input_tokens_details"):
        details = usage.get(field) if isinstance(usage, dict) else getattr(usage, field, None)
        if details is not None:
            cached = max(cached, _get_usage_int(details, "cached_tokens") or 0)
    for field in ("prompt_cache_hit_tokens", "cache_read_input_tokens"):
        cached = max(cached, _get_usage_int(usage, field) or 0)
    return cached


def get_suspect_prompt_tokens(messages, usage):
    """Describe a prompt-token report too low to have read the whole request.

    A server whose real context is smaller than the configured one cuts the
    prompt and answers from the rest, reporting the smaller count in
    ``usage.prompt_tokens``. The request estimator is the only token measure
    available without a model tokenizer, and deliberately assumes 3 characters
    per token; dense Latin prose still tokenizes at about 4, and CJK at about
    1, so a faithful report is never below half the estimate. Missing,
    malformed or zero counts are no evidence at all.

    Cached tokens are part of the prompt, never evidence of truncation: the
    supported servers report them as a subset of ``prompt_tokens`` (OpenAI,
    llama.cpp, LM Studio, Ollama), while a provider reporting only the
    uncached remainder puts the rest in a separate field, so both are counted.
    """
    reported = _get_usage_int(usage, "prompt_tokens")
    if reported is None or reported <= 0:
        return None
    estimate = get_prompt_token_estimate(messages)
    if (reported + _get_cached_prompt_tokens(usage)) * 2 >= estimate:
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
