"""Strict preset selection for the experimental three-pass request formats."""
from string import Formatter

EXPERIMENTAL_PROMPT_FIELDS = {
    "pass1_compact": "units", "pass3_gaps": "batch", "pass3_directed": "batch",
}


def validate_experimental_template(template, field):
    """Require one complete payload, with no field traversal or format conversion."""
    if not isinstance(template, str) or not template.strip() or len(template) > 100_000:
        raise ValueError("Experimental user prompt must contain 1 to 100000 characters")
    fields = []
    for _, name, spec, conversion in Formatter().parse(template):
        if name is not None:
            if name != field or spec or conversion:
                raise ValueError(f"Experimental user prompt allows only {{{field}}}, without formatting")
            fields.append(name)
    if fields != [field]:
        raise ValueError(f"Experimental user prompt needs exactly one {{{field}}} payload")
    return template


def resolve_experimental_prompt(config, family):
    """Resolve only the active format, refusing damaged or ambiguous saved selections.

    None means use its checked-in prompt, preserving legacy request identity.
    Inactive presets do not affect a run's request or resume identity.
    """
    field = EXPERIMENTAL_PROMPT_FIELDS[family]
    prompts = config.get("prompts") or {}
    active = prompts.get(f"{family}_preset", "default")
    if active == "default":
        return None, None
    if not isinstance(active, str) or not active.strip() or len(active) > 80:
        raise ValueError(f"Invalid {family} preset selection")
    presets = prompts.get(f"{family}_prompt_presets") or []
    if not isinstance(presets, list):
        raise ValueError(f"Invalid {family} preset list")
    matches = [preset for preset in presets if isinstance(preset, dict) and preset.get("name") == active]
    if len(matches) != 1:
        raise ValueError(f"Selected {family} preset is missing or ambiguous: {active}")
    preset = matches[0]
    system = preset.get("system_prompt")
    if not isinstance(system, str) or not system.strip() or len(system) > 100_000:
        raise ValueError(f"Selected {family} preset needs nonblank system instructions")
    user = validate_experimental_template(preset.get("user_prompt"), field)
    return system, user
