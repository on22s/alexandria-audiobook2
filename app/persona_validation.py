"""Validation shared by generated and manually recovered persona payloads."""

from utils import get_unsafe_text_controls


def validate_persona_payload(payload):
    """Return normalized persona text or raise ``ValueError``."""
    if not isinstance(payload, dict):
        raise ValueError("persona output must be a JSON object")
    description = payload.get("description")
    ref_text = payload.get("ref_text")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("persona description is required and must be a string")
    if not isinstance(ref_text, str) or not ref_text.strip():
        raise ValueError("persona ref_text is required and must be a string")
    for field, text in (("description", description), ("ref_text", ref_text)):
        controls = get_unsafe_text_controls(text)
        if controls:
            raise ValueError(f"persona {field} contains unsafe controls: {', '.join(controls)}")
    description = description.strip()
    ref_text = ref_text.strip()
    if len(description) > 4000 or len(ref_text) > 2000:
        raise ValueError("persona description or ref_text is too long")
    return {"description": description, "ref_text": ref_text}


def get_reference_samples(character_ref):
    """Return exact, bounded dialogue samples; long lines use a source prefix."""
    lines = list(character_ref.get("sample_lines", [])) if isinstance(character_ref.get("sample_lines"), list) else []
    for observation in character_ref.get("observations", []):
        if isinstance(observation, dict) and isinstance(observation.get("sample_lines"), list):
            lines.extend(observation["sample_lines"])
    samples = []
    for line in lines:
        if not isinstance(line, str) or not line.strip():
            continue
        text = line.strip()
        if len(text) > 2000:
            import re
            boundary = re.search(r"[.!?](?:[\"’”])?(?=\s|$)", text[:2000])
            if boundary:
                text = text[:boundary.end()]
            else:
                prefix = text[:2000]
                text = prefix.rsplit(" ", 1)[0] if " " in prefix else prefix
        if not get_unsafe_text_controls(text) and text not in samples:
            samples.append(text)
    return samples


def validate_compiled_persona_payload(payload, samples):
    """Reject fabricated, paraphrased, or stitched reference dialogue."""
    result = validate_persona_payload(payload)
    if result["ref_text"] not in samples:
        raise ValueError("ref_text must exactly copy one supplied reference sample")
    return result
