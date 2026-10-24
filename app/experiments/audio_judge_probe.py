"""Audio-receipt probe for the #1547 listening critic (Gate A).

Sends declared public-domain clips to an audio-capable chat model and checks
that the answer comes from the audio, not from the prompt. The judge is the
Setup page's "remote" LLM profile, so no audio model becomes the annotation
model. Any manifest without ``"license": "public-domain"`` is refused (Rule 27).

Usage:
    python app/experiments/audio_judge_probe.py --manifest probe.json
    python app/experiments/audio_judge_probe.py --manifest probe.json \
        --content-style both --max-spend-usd 5
"""

import argparse
import base64
from difflib import SequenceMatcher
import json
import math
import os
import re
import struct
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from config_settings import load_app_config
from llm_provider import make_llm_client

INSTRUCTION = (
    "Transcribe the speech in this audio verbatim. Do not guess. If the audio "
    "contains no speech, return an empty transcript. Reply as JSON: "
    '{"transcript": "..."}'
)
MIN_ITEM_RECALL = 0.6
MIN_PASS_FRACTION = 0.6
MAX_CONTROL_WORDS = 1
WORD_RE = re.compile(r"[a-z0-9']+")


def normalize_words(text):
    """Lowercase word tokens, dropping punctuation and empty entries."""
    return WORD_RE.findall((text or "").lower())


def word_recall(expected, actual):
    """Ordered word recall, so a bag of the same words cannot pass."""
    wanted = normalize_words(expected)
    if not wanted:
        raise ValueError("Expected text has no words to score")
    got = normalize_words(actual)
    matcher = SequenceMatcher(None, wanted, got, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / len(wanted)


def load_manifest(path):
    """Load and validate a public-domain probe manifest."""
    manifest = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if manifest.get("license") != "public-domain":
        raise ValueError("Refusing non-public-domain audio: set \"license\": \"public-domain\"")
    items = manifest.get("items")
    if not isinstance(items, list) or len(items) < 3:
        raise ValueError("Manifest needs at least three items")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not item.get("audio") or not item.get("text"):
            raise ValueError(f"Manifest item {index} needs audio and text")
        if not Path(item["audio"]).is_file():
            raise ValueError(f"Manifest item {index} audio not found: {item['audio']}")
    control = manifest.get("control")
    if control is not None and not Path(control).is_file():
        raise ValueError(f"Manifest control audio not found: {control}")
    return manifest


def get_judge_config(config):
    """Read the Setup page's remote profile as the audio judge."""
    profile = (config or {}).get("llm_remote")
    if not isinstance(profile, dict):
        raise ValueError("Configure the remote LLM profile in Setup as the audio judge")
    if not (profile.get("base_url") or "").strip():
        raise ValueError("The remote LLM profile has no base_url")
    if not (profile.get("model_name") or "").strip():
        raise ValueError("The remote LLM profile has no model_name")
    if profile.get("transport") == "manual":
        raise ValueError("The remote LLM profile uses manual transport; the probe needs live audio replies")
    from core import _validate_local_llm_base_url
    _validate_local_llm_base_url(profile["base_url"])
    return profile


def encode_audio(path, fmt=None):
    """Return base64 audio and its format for an OpenAI-compatible content part."""
    audio_path = Path(path)
    data = audio_path.read_bytes()
    if not data:
        raise ValueError(f"Empty audio file: {path}")
    suffix = (fmt or audio_path.suffix.lstrip(".") or "wav").lower()
    return base64.b64encode(data).decode("ascii"), suffix


def build_audio_content(style, data, fmt):
    """Build the provider's audio content part in a supported shape."""
    if style == "input_audio":
        return {"type": "input_audio", "input_audio": {"data": data, "format": fmt}}
    if style == "audio_url":
        return {"type": "audio_url", "audio_url": {"url": f"data:audio/{fmt};base64,{data}"}}
    raise ValueError(f"Unknown content style: {style}")


def get_model_pricing(base_url, model, timeout=30):
    """Read per-token prices from the endpoint's public model list, if present."""
    url = base_url.rstrip("/") + "/models"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "aa2-probe"}), timeout=timeout) as response:
            payload = json.load(response)
    except Exception:
        return None
    for entry in payload.get("data") or []:
        if entry.get("id") == model:
            pricing = entry.get("pricing") or {}
            try:
                return float(pricing["prompt"]), float(pricing["completion"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def get_response_cost(usage, pricing):
    """Return reported or estimated cost in USD; None when it cannot be known."""
    reported = None
    if isinstance(usage, dict):
        reported = usage.get("cost")
    else:
        reported = getattr(usage, "cost", None)
    if reported is None:
        model_extra = getattr(usage, "model_extra", None) or {}
        reported = model_extra.get("cost")
    if reported is not None:
        return float(reported)
    if pricing is None or usage is None:
        return None
    prompt_tokens = getattr(usage, "prompt_tokens", None) if not isinstance(usage, dict) else usage.get("prompt_tokens")
    completion_tokens = getattr(usage, "completion_tokens", None) if not isinstance(usage, dict) else usage.get("completion_tokens")
    if prompt_tokens is None or completion_tokens is None:
        return None
    return prompt_tokens * pricing[0] + completion_tokens * pricing[1]


def ensure_spend_capacity(spent_usd, max_spend_usd, next_cost_usd):
    """Refuse another call when it would exceed the declared budget."""
    if next_cost_usd is None:
        return
    if spent_usd + next_cost_usd > max_spend_usd:
        raise RuntimeError(f"Spend cap reached: ${spent_usd:.4f} + ${next_cost_usd:.4f} > ${max_spend_usd:.2f}")


def parse_transcript(content):
    """Accept a JSON transcript reply or the raw text."""
    if not isinstance(content, str):
        return ""
    text = content.strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
    if isinstance(parsed, dict) and isinstance(parsed.get("transcript"), str):
        return parsed["transcript"]
    return text


def evaluate_gate(results, min_item_recall=MIN_ITEM_RECALL,
                  min_pass_fraction=MIN_PASS_FRACTION,
                  max_control_words=MAX_CONTROL_WORDS):
    """Gate A: audio-only answers pass, the silent control does not."""
    if not results:
        return False, ["no probe results"]
    if any(result.get("error") for result in results):
        return False, [f"request failed: {result.get('error')}" for result in results if result.get("error")]
    passed = [result for result in results if result.get("recall", 0) >= min_item_recall]
    needed = max(1, math.ceil(min_pass_fraction * len(results)))
    reasons = [f"audio recall passed {len(passed)}/{len(results)} (need {needed})"]
    control_words = len(normalize_words(results[0].get("control_transcript", "")))
    if control_words > max_control_words:
        reasons.append(f"silent control produced {control_words} words")
    return len(passed) >= needed and control_words <= max_control_words, reasons


def write_silent_wav(path, seconds=1.0, frame_rate=16000):
    """Write a silent mono 16-bit WAV for the negative control."""
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(frame_rate)
        frames = int(seconds * frame_rate)
        stream.writeframes(struct.pack("<h", 0) * frames)


def request_transcript(client, model, audio_path, style, max_tokens=200):
    """Send one audio clip and return (transcript, usage, request_style)."""
    data, fmt = encode_audio(audio_path)
    styles = ("input_audio", "audio_url") if style == "both" else (style,)
    last_error = None
    for candidate in styles:
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": INSTRUCTION},
                    build_audio_content(candidate, data, fmt),
                ]}],
                temperature=0,
                max_tokens=max_tokens,
                extra_body={"usage": {"include": True}},
            )
            return parse_transcript(response.choices[0].message.content), getattr(response, "usage", None), candidate
        except Exception as exc:
            last_error = exc
    raise last_error


def load_ledger(path):
    if not path.exists():
        return {"spent_usd": 0.0, "entries": []}
    return json.loads(path.read_text(encoding="utf-8"))


def save_ledger(path, ledger):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def redact_profile(profile):
    return {key: ("[REDACTED]" if key == "api_key" and value else value)
            for key, value in profile.items()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, help="Public-domain probe manifest")
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--out", default=None)
    parser.add_argument("--ledger", default=None)
    parser.add_argument("--max-spend-usd", type=float, default=None)
    parser.add_argument("--content-style", choices=("input_audio", "audio_url", "both"), default="both")
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    profile = get_judge_config(load_app_config(args.config))
    max_spend = args.max_spend_usd if args.max_spend_usd is not None else float(profile.get("max_spend_usd") or 5)
    out = Path(args.out) if args.out else APP_DIR.parent / "ab_test_runtime" / "listening_critic_1547" / "gate_a_receipt.json"
    ledger_path = Path(args.ledger) if args.ledger else out.with_suffix(".spend.json")
    ledger = load_ledger(ledger_path)

    pricing = get_model_pricing(profile["base_url"], profile["model_name"])
    client = make_llm_client(profile, timeout=args.timeout)

    control_path = manifest.get("control")
    temporary = None
    if control_path is None:
        temporary = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        temporary.close()
        write_silent_wav(temporary.name)
        control_path = temporary.name

    started = time.time()
    results = []
    control_transcript = None
    error = None
    try:
        control_transcript, control_usage, style = request_transcript(
            client, profile["model_name"], control_path, args.content_style)
        control_cost = get_response_cost(control_usage, pricing)
        ensure_spend_capacity(ledger["spent_usd"], max_spend, control_cost)
        ledger["spent_usd"] += control_cost or 0.0
        ledger["entries"].append({"kind": "control", "cost_usd": control_cost,
                                  "style": style, "at": time.time()})
        for item in manifest["items"]:
            transcript, usage, style = request_transcript(
                client, profile["model_name"], item["audio"], args.content_style)
            cost = get_response_cost(usage, pricing)
            ensure_spend_capacity(ledger["spent_usd"], max_spend, cost)
            ledger["spent_usd"] += cost or 0.0
            ledger["entries"].append({"kind": "item", "audio": item["audio"], "cost_usd": cost,
                                      "style": style, "at": time.time()})
            results.append({
                "audio": item["audio"],
                "expected": item["text"],
                "transcript": transcript,
                "recall": word_recall(item["text"], transcript),
                "control_transcript": control_transcript,
                "cost_usd": cost,
                "style": style,
            })
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        save_ledger(ledger_path, ledger)
        if temporary is not None:
            os.unlink(temporary.name)

    if error is None:
        passed, reasons = evaluate_gate(results)
    else:
        passed, reasons = False, [f"probe failed: {error}"]
    receipt = {
        "gate": "A",
        "passed": passed,
        "reasons": reasons,
        "error": error,
        "profile": {**redact_profile(profile), "base_url": profile["base_url"],
                    "model_name": profile["model_name"]},
        "manifest": str(Path(args.manifest).resolve()),
        "control_audio": control_path,
        "control_transcript": control_transcript,
        "pricing_per_token": pricing,
        "spent_usd": ledger["spent_usd"],
        "max_spend_usd": max_spend,
        "elapsed_seconds": round(time.time() - started, 3),
        "results": results,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "reasons": reasons, "receipt": str(out),
                      "spent_usd": receipt["spent_usd"]}, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
