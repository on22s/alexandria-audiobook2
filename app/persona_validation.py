"""Validation shared by generated and manually recovered persona payloads."""


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
    description = description.strip()
    ref_text = ref_text.strip()
    if len(description) > 4000 or len(ref_text) > 2000:
        raise ValueError("persona description or ref_text is too long")
    return {"description": description, "ref_text": ref_text}
