import os
import sys
import json
import time
import re
import argparse
import shutil
import tempfile
from openai import OpenAI

from tts import TTSEngine, sanitize_filename


def extract_json_object(text):
    # Find first JSON object in text
    start = text.find('{')
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    end = None
    for i, ch in enumerate(text[start:], start):
        if esc:
            esc = False
            continue
        if ch == '\\':
            esc = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end is None:
        return None
    obj_text = text[start:end]
    try:
        return json.loads(obj_text)
    except Exception:
        return None


def normalize_speaker_name(name):
    if not isinstance(name, str):
        return ""
    s = name.strip().lower()
    # Remove common honorifics and punctuation for alias heuristics
    s = re.sub(r'^(mr|mrs|ms|miss|dr|prof|sir|lady|lord)\.?\s+', '', s)
    s = re.sub(r'[^a-z0-9\s]', '', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def pick_ref_text(lines):
    for ln in lines:
        if ln and len(ln.strip()) >= 12:
            return ln.strip()
    return next((ln.strip() for ln in lines if ln and ln.strip()), "")


def parse_alias_decision(text):
    parsed = extract_json_object(text)
    if not parsed:
        return {
            "is_alias": False,
            "alias_of": "",
            "description": "",
            "ref_text": "",
            "reason": "unparseable"
        }
    return {
        "is_alias": bool(parsed.get("is_alias", False)),
        "alias_of": str(parsed.get("alias_of", "") or "").strip(),
        "description": str(parsed.get("description", "") or "").strip(),
        "ref_text": str(parsed.get("ref_text", "") or "").strip(),
        "reason": str(parsed.get("reason", "") or "").strip()
    }

def _atomic_json_write(data, target_path):
    directory = os.path.dirname(target_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description="Generate personas for speakers in annotated script")
    parser.add_argument("--new-only", action="store_true", help="Process only speakers missing from voice_config.json")
    parser.add_argument("--alias-check", action="store_true", help="Use LLM + heuristics to decide alias_of vs truly new character")
    parser.add_argument("--speakers", default="", help="Optional comma-separated speaker allowlist")
    parser.add_argument("--narration-window", type=int, default=4, help="How many preceding narrator lines to include as intro context")
    args = parser.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_path = os.path.join(root, "annotated_script.json")
    voice_config_path = os.path.join(root, "voice_config.json")
    app_config_path = os.path.join(os.path.dirname(__file__), "config.json")

    if not os.path.exists(script_path):
        print(f"Error: {script_path} not found. Generate script first.")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    # Collect sample lines per speaker + first-appearance narrator context
    samples = {}
    first_index = {}
    for i, entry in enumerate(script):
        speaker = (entry.get("speaker") or entry.get("type") or "").strip()
        if not speaker:
            continue
        samples.setdefault(speaker, []).append(entry.get("text", "").strip())
        if speaker not in first_index:
            first_index[speaker] = i

    narrator_context = {}
    window = max(1, int(args.narration_window or 4))
    for speaker, idx in first_index.items():
        context_lines = []
        j = idx - 1
        while j >= 0 and len(context_lines) < window:
            prev = script[j]
            prev_speaker = (prev.get("speaker") or prev.get("type") or "").strip().upper()
            prev_text = (prev.get("text") or "").strip()
            if prev_speaker == "NARRATOR" and prev_text:
                context_lines.append(prev_text)
            j -= 1
        narrator_context[speaker] = list(reversed(context_lines))

    # Load LLM config
    config = {}
    if os.path.exists(app_config_path):
        try:
            with open(app_config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load app/config.json: {e}")

    llm_cfg = config.get("llm", {})
    base_url = llm_cfg.get("base_url", "http://localhost:11434/v1")
    api_key = llm_cfg.get("api_key", "local")
    model_name = llm_cfg.get("model_name", "richardyoung/qwen3-14b-abliterated:Q8_0")

    client = OpenAI(base_url=base_url, api_key=api_key)

    # Load existing voice_config (preserve other fields)
    voice_config = {}
    if os.path.exists(voice_config_path):
        try:
            with open(voice_config_path, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except Exception:
            voice_config = {}

    engine = TTSEngine({"tts": config.get("tts", {})})

    selected_speakers = list(samples.keys())
    if args.new_only:
        selected_speakers = [s for s in selected_speakers if s not in voice_config]
    if args.speakers.strip():
        allow = {s.strip() for s in args.speakers.split(",") if s.strip()}
        selected_speakers = [s for s in selected_speakers if s in allow]

    if not selected_speakers:
        print("No speakers to process.")
        return

    print(f"Processing {len(selected_speakers)} speakers")

    for speaker in selected_speakers:
        lines = samples.get(speaker, [])
        try:
            print(f"Generating persona for: {speaker} ({len(lines)} lines samples)")

            # Alias check: fast heuristic first, then optional LLM adjudication
            existing_names = [n for n in voice_config.keys() if n != speaker]
            norm_self = normalize_speaker_name(speaker)
            heuristic_alias = ""
            for candidate in existing_names:
                if normalize_speaker_name(candidate) == norm_self and norm_self:
                    heuristic_alias = candidate
                    break

            if heuristic_alias:
                print(f"Heuristic alias detected: {speaker} -> {heuristic_alias}")
                voice_entry = voice_config.get(speaker, {})
                voice_entry.update({
                    "alias_of": heuristic_alias,
                    "seed": voice_entry.get("seed", -1),
                })
                voice_config[speaker] = voice_entry
                continue

            # Build prompt with up to 8 representative lines
            sample_text = "\n".join(lines[:8])

            intro_ctx = narrator_context.get(speaker, [])
            intro_blob = "\n".join(intro_ctx) if intro_ctx else "(No nearby narrator intro lines found.)"

            if args.alias_check:
                candidate_blob = "\n".join(f"- {name}" for name in existing_names) if existing_names else "(none)"
                user_prompt = (
                    f"You are a voice-casting assistant. A speaker named '{speaker}' appeared in an audiobook script.\n\n"
                    f"Narrator context before first appearance (most useful for introductions):\n{intro_blob}\n\n"
                    f"Sample spoken lines by '{speaker}':\n{sample_text}\n\n"
                    f"Existing configured characters:\n{candidate_blob}\n\n"
                    "Decide whether this is an alias/variant of an existing character or truly a new character.\n"
                    "Return ONLY one JSON object with keys:\n"
                    "- is_alias: boolean\n"
                    "- alias_of: string (existing character name if alias, else empty string)\n"
                    "- reason: short reason\n"
                    "- description: if new character, concise natural-language persona (age, gender, timbre, accent, pace, tone, delivery) in 2-3 sentences\n"
                    "- ref_text: if new character, 1-2 sentence representative sample\n"
                    "If uncertain, prefer is_alias=false."
                )
            else:
                user_prompt = (
                    f"You are a voice designer assistant. Given the following narrator-intro context and lines by character '{speaker}':\n\n"
                    f"Narrator context before first appearance:\n{intro_blob}\n\n"
                    f"Character lines:\n{sample_text}\n\n"
                    "Produce a JSON object with two keys: 'description' and 'ref_text'.\n"
                    "- 'description': a concise natural-language voice persona describing age, gender, timbre, accent, speaking rate, typical emotional tone, and delivery guidance (2-3 sentences).\n"
                    "- 'ref_text': a 1-2 sentence short sample that best captures this character's voice and can be used as a TTS reference.\n"
                    "Only output the JSON object and nothing else."
                )

            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "You produce concise JSON only."},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.3,
                max_tokens=400,
            )

            text = response.choices[0].message.content.strip()
            if args.alias_check:
                decision = parse_alias_decision(text)
                if decision["is_alias"] and decision["alias_of"] in existing_names:
                    print(f"LLM alias detected: {speaker} -> {decision['alias_of']} ({decision['reason']})")
                    voice_entry = voice_config.get(speaker, {})
                    voice_entry.update({
                        "alias_of": decision["alias_of"],
                        "seed": voice_entry.get("seed", -1),
                    })
                    voice_config[speaker] = voice_entry
                    continue
                description = decision["description"]
                ref_text = decision["ref_text"]
            else:
                parsed = extract_json_object(text)
                if not parsed:
                    print(f"Warning: LLM did not return parseable JSON for {speaker}. Response preview:\n{text[:300]}")
                    continue
                description = str(parsed.get("description", "") or "").strip()
                ref_text = str(parsed.get("ref_text", "") or "").strip()

            if not description:
                print(f"Warning: Empty description for {speaker}, skipping")
                continue

            if not ref_text:
                # Fallback: use first representative sample line
                ref_text = pick_ref_text(lines)

            # Generate a VoiceDesign preview and save stable copy
            try:
                wav_path, sr = engine.generate_voice_design(description=description, sample_text=ref_text)
                # Copy to named file in designed_voices directory
                dest_dir = os.path.join(root, "designed_voices")
                os.makedirs(dest_dir, exist_ok=True)
                safe = sanitize_filename(speaker)
                voice_id = f"{safe}_{int(time.time_ns())}"
                dest_filename = f"{voice_id}_preview.wav"
                dest_path = os.path.join(dest_dir, dest_filename)
                try:
                    shutil.copy2(wav_path, dest_path)
                except Exception as e:
                    print(f"Warning: Could not copy preview for {speaker}: {e}")

                # Update voice_config: use clone type referencing the designed preview
                voice_entry = voice_config.get(speaker, {})
                voice_entry.update({
                    "type": "clone",
                    "ref_audio": os.path.relpath(dest_path, root).replace('\\\\', '/'),
                    "ref_text": ref_text,
                    "description": description,
                    "character_style": description,
                    "seed": -1
                })
                voice_config[speaker] = voice_entry

                # Save metadata for this designed voice
                meta_path = os.path.join(dest_dir, f"{voice_id}_meta.json")
                _atomic_json_write({"description": description, "ref_text": ref_text, "preview": os.path.relpath(dest_path, root)}, meta_path)

                # Register in designed_voices/manifest.json so UI can list it for review
                try:
                    manifest_path = os.path.join(dest_dir, 'manifest.json')
                    manifest = []
                    if os.path.exists(manifest_path):
                        try:
                            with open(manifest_path, 'r', encoding='utf-8') as mf:
                                manifest = json.load(mf)
                        except Exception:
                            manifest = []

                    stale_entries = [entry for entry in manifest if entry.get("name") == speaker]
                    manifest = [entry for entry in manifest if entry.get("name") != speaker]
                    for stale in stale_entries:
                        stale_filename = stale.get("filename") or ""
                        if stale_filename:
                            stale_path = os.path.join(dest_dir, stale_filename)
                            if os.path.exists(stale_path) and os.path.abspath(stale_path) != os.path.abspath(dest_path):
                                try:
                                    os.remove(stale_path)
                                except OSError:
                                    pass
                        stale_meta = stale.get("id")
                        if stale_meta:
                            stale_meta_path = os.path.join(dest_dir, f"{stale_meta}_meta.json")
                            if os.path.exists(stale_meta_path):
                                try:
                                    os.remove(stale_meta_path)
                                except OSError:
                                    pass

                    manifest.append({
                        "id": voice_id,
                        "name": speaker,
                        "description": description,
                        "sample_text": ref_text,
                        "filename": os.path.basename(dest_path)
                    })
                    _atomic_json_write(manifest, manifest_path)
                except Exception as e:
                    print(f"Warning: could not update manifest for {speaker}: {e}")

                print(f"Persona generated and preview saved for {speaker}: {dest_path}")

            except Exception as e:
                print(f"Error generating voice preview for {speaker}: {e}")
                # Still save description so user can re-trigger preview later
                voice_entry = voice_config.get(speaker, {})
                voice_entry.update({"type": "design", "description": description, "ref_text": ref_text})
                voice_config[speaker] = voice_entry

            # Throttle a little to avoid overwhelming LLM
            time.sleep(0.5)

        except Exception as e:
            print(f"Unhandled error for {speaker}: {e}")

    # Persist voice_config
    try:
        _atomic_json_write(voice_config, voice_config_path)
        print(f"Updated voice_config saved to {voice_config_path}")
    except Exception as e:
        print(f"Failed to save voice_config.json: {e}")


if __name__ == '__main__':
    main()
