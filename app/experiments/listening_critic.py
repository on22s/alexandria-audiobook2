"""Per-line listening critic for the #1547 validation (content arm first).

The critic sees the expected line and the audio; it may only report content
differences (missing, repeated, inserted, or truncated words). Style, voice
and pause findings are deliberately out of scope until their own gate exists.
"""

import json
import re
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from experiments.audio_judge_probe import (
    build_audio_content, encode_audio, get_model_pricing, get_response_cost,
    get_judge_config, ensure_spend_capacity,
)

FINDING_TYPES = ("content", "voice", "style", "pause")
SEVERITIES = ("low", "medium", "high")
CONTENT_PROMPT = """You are checking one rendered audiobook line against its expected text.

Expected text: "{text}"

Listen to the audio and report only content differences: missing words,
repeated words or phrases, inserted words, or audio that stops before the
text is complete. Do not report pacing, voice quality, emotion, or style.

If the audio is a complete, single reading of the expected text, return
{{"findings": []}}. Never invent a finding or describe the audio.
Reply as JSON: {{"findings": [{{"type": "content", "severity": "low|medium|high", "evidence": "the affected words"}}]}}"""

FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def parse_findings(content):
    """Parse the critic's JSON findings, accepting a fenced or bare reply."""
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Critic returned no content")
    text = content.strip()
    fenced = FENCE_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"Critic reply is not JSON: {content[:120]!r}") from exc
    if isinstance(parsed, dict):
        findings = parsed.get("findings")
    elif isinstance(parsed, list):
        findings = parsed
    else:
        findings = None
    if not isinstance(findings, list):
        raise ValueError(f"Critic reply has no findings list: {content[:120]!r}")
    for finding in findings:
        if not isinstance(finding, dict):
            raise ValueError("Each finding must be an object")
        if finding.get("type") not in FINDING_TYPES:
            raise ValueError(f"Unknown finding type: {finding.get('type')!r}")
        if finding.get("severity") not in SEVERITIES:
            raise ValueError(f"Unknown finding severity: {finding.get('severity')!r}")
    return findings


def has_content_finding(findings):
    """True when the critic reported at least one content finding."""
    return any(finding.get("type") == "content" for finding in findings)


def build_content_prompt(expected_text):
    """Render the content-only prompt for one line."""
    return CONTENT_PROMPT.format(text=expected_text.replace('"', "'"))


def critique_line(client, model, audio_path, expected_text, max_tokens=400):
    """Send one line and return findings, usage and the raw reply."""
    data, fmt = encode_audio(audio_path)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": build_content_prompt(expected_text)},
            build_audio_content("input_audio", data, fmt),
        ]}],
        temperature=0,
        max_tokens=max_tokens,
        extra_body={"usage": {"include": True}},
    )
    raw = response.choices[0].message.content
    return parse_findings(raw), getattr(response, "usage", None), raw

ASPECTS = ("instruct", "naturalness", "emotion")
LINE_REVIEW_PROMPT = """You are reviewing one rendered audiobook line.

Expected text: "{text}"
Delivery instruct: "{instruct}"

Listen to the audio and report only issues you can hear:
- type "content": missing, repeated, inserted, or truncated words.
- type "style", aspect "instruct": the delivery contradicts or ignores the instruct.
- type "style", aspect "naturalness": robotic, unnatural, distorted, or badly spliced speech.
Do not report a finding when the take is acceptable. Do not describe the audio.
Reply as JSON: {{"findings": [{{"type": "content|style", "aspect": "instruct|naturalness",
"severity": "low|medium|high", "evidence": "a few words"}}]}}"""

SCENE_REVIEW_PROMPT = """You are reviewing one rendered audiobook scene.

Script:
{script}

Listen to the audio and report only issues you can hear:
- type "voice": a character's voice changes or is inconsistent across the scene.
- type "pause": a pause is clearly too long or too short.
- type "style", aspect "emotion": the delivery's emotion does not fit its line or the scene.
Do not report content differences here. Do not report a finding when the scene is acceptable.
Reply as JSON: {{"findings": [{{"type": "voice|pause|style", "aspect": "emotion",
"severity": "low|medium|high", "evidence": "the affected speaker, line or pause"}}]}}"""


def parse_review_findings(content):
    """Parse review findings and validate the optional aspect field."""
    findings = parse_findings(content)
    for finding in findings:
        aspect = finding.get("aspect")
        if finding["type"] == "style" and aspect not in ASPECTS:
            raise ValueError(f"Style finding needs aspect in {ASPECTS}: {aspect!r}")
    return findings


def has_review_finding(findings, finding_type, aspect=None):
    """True when a finding of this type (and aspect) is present."""
    for finding in findings:
        if finding.get("type") != finding_type:
            continue
        if aspect is None or finding.get("aspect") == aspect:
            return True
    return False


def _request_findings(client, model, prompt, audio_path, max_tokens):
    data, fmt = encode_audio(audio_path)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": [
            {"type": "text", "text": prompt},
            build_audio_content("input_audio", data, fmt),
        ]}],
        temperature=0,
        max_tokens=max_tokens,
        extra_body={"usage": {"include": True}},
    )
    raw = response.choices[0].message.content
    return parse_review_findings(raw), getattr(response, "usage", None), raw


def critique_line_review(client, model, audio_path, expected_text, instruct, max_tokens=500):
    """Review one line for content, instruct fit and naturalness."""
    prompt = LINE_REVIEW_PROMPT.format(text=expected_text.replace('"', "'"),
                                       instruct=(instruct or "No specific instruct.").replace('"', "'"))
    return _request_findings(client, model, prompt, audio_path, max_tokens)


def critique_scene_review(client, model, audio_path, script_lines, max_tokens=600):
    """Review one scene for voice consistency, pause fit and emotion."""
    script = "\n".join(f"{line['speaker']}: {line['text']}" for line in script_lines)
    prompt = SCENE_REVIEW_PROMPT.format(script=script)
    return _request_findings(client, model, prompt, audio_path, max_tokens)
