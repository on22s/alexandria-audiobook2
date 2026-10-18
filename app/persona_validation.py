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


def get_reference_samples(character_ref, max_chars=2000):
    """Return exact, bounded dialogue samples; long lines use a source prefix."""
    lines = list(character_ref.get("sample_lines", [])) if isinstance(character_ref.get("sample_lines"), list) else []
    for observation in character_ref.get("observations", []):
        if isinstance(observation, dict) and isinstance(observation.get("sample_lines"), list):
            lines.extend(observation["sample_lines"])
    samples = []
    for line in lines:
        if not isinstance(line, str) or not line.strip():
            continue
        text = get_bounded_sample(line.strip(), max_chars)
        if not get_unsafe_text_controls(text) and text not in samples:
            samples.append(text)
    return samples


def get_bounded_sample(text, max_chars, latest_boundary=False):
    """Return ``text`` cut to at most ``max_chars``, at a sentence end when possible.

    The first sentence end is used unless ``latest_boundary`` asks for the last
    one that fits, which keeps as much of a long line as the limit allows.
    """
    if len(text) <= max_chars:
        return text
    import re
    # A CJK sentence end needs no following space; a Latin one does, so the
    # point in "3.14" or "Mr.Smith" is not taken for one.
    ends = list(re.finditer(r"[.!?](?:[\"’”])?(?=\s|$)|[。！？](?:[」』”])?", text[:max_chars]))
    if ends:
        return text[:(ends[-1] if latest_boundary else ends[0]).end()]
    prefix = text[:max_chars]
    return prefix.rsplit(" ", 1)[0] if " " in prefix else prefix


DESIGN_TEXT_MIN_CHARS = 12
DESIGN_TEXT_MAX_CHARS = 500
# How many voices one Design click may make from a chosen line (#1313).
DESIGN_COUNT_MAX = 100


def get_design_text_range(config):
    """-> (min_chars, max_chars) for Voice Design text from an app config dict (#1232)."""
    tts = (config or {}).get("tts") or {}
    def bounded(key, default):
        try:
            return max(1, min(int(tts.get(key, default)), DESIGN_TEXT_MAX_CHARS))
        except (TypeError, ValueError):
            return default
    max_chars = bounded("design_text_max_chars", DESIGN_TEXT_MAX_CHARS)
    return min(bounded("design_text_min_chars", DESIGN_TEXT_MIN_CHARS), max_chars), max_chars


def get_design_text_candidates(lines, min_chars=DESIGN_TEXT_MIN_CHARS, max_chars=DESIGN_TEXT_MAX_CHARS):
    """Return the lines a voice may be designed from (#1232).

    Lines longer than ``max_chars`` are cut at a sentence end. Lines inside the
    range are returned in source order. When none are, the result is the one
    longest line below ``min_chars``, never a longer one.
    """
    bounded = []
    for line in lines or []:
        if not isinstance(line, str) or not line.strip():
            continue
        text = get_bounded_sample(line.strip(), max_chars, latest_boundary=True)
        if not get_unsafe_text_controls(text) and text not in bounded:
            bounded.append(text)
    in_range = [text for text in bounded if min_chars <= len(text) <= max_chars]
    if in_range:
        return in_range
    shorter = [text for text in bounded if len(text) < min_chars]
    return [max(shorter, key=len)] if shorter else []


def get_selectable_design_texts(lines):
    """Every distinct line, cut to the design cap, in source order: the texts a
    user may pick to design a voice from (#1232)."""
    return get_design_text_candidates(lines, 1, DESIGN_TEXT_MAX_CHARS)


def validate_compiled_persona_payload(payload, samples):
    """Reject fabricated, paraphrased, or stitched reference dialogue."""
    result = validate_persona_payload(payload)
    if result["ref_text"] not in samples:
        raise ValueError("ref_text must exactly copy one supplied reference sample")
    return result
