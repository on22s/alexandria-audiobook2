import os
import sys
import json
import time
import re
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


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script_path = os.path.join(root, "annotated_script.json")
    voice_config_path = os.path.join(root, "voice_config.json")
    app_config_path = os.path.join(os.path.dirname(__file__), "config.json")

    if not os.path.exists(script_path):
        print(f"Error: {script_path} not found. Generate script first.")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        script = json.load(f)

    # Collect sample lines per speaker
    samples = {}
    for entry in script:
        speaker = (entry.get("speaker") or entry.get("type") or "").strip()
        if not speaker:
            continue
        samples.setdefault(speaker, []).append(entry.get("text", "").strip())

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

    for speaker, lines in samples.items():
        try:
            print(f"Generating persona for: {speaker} ({len(lines)} lines samples)")

            # Build prompt with up to 8 representative lines
            sample_text = "\n".join(lines[:8])
            user_prompt = (
                f"You are a voice designer assistant. Given the following example lines spoken by a character named '{speaker}':\n\n"
                f"{sample_text}\n\n"
                "Produce a JSON object with two keys: 'description' and 'ref_text'.\n"
                "- 'description': a concise natural-language voice persona describing age, gender, timbre, accent, speaking rate, typical emotional tone, and delivery guidance (2-3 sentences).\n"
                "- 'ref_text': a 1-2 sentence short sample (one or two sentences) that best captures this character's voice and can be used as a TTS reference.\n"
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
            parsed = extract_json_object(text)
            if not parsed:
                print(f"Warning: LLM did not return parseable JSON for {speaker}. Response preview:\n{text[:300]}")
                continue

            description = parsed.get("description", "").strip()
            ref_text = parsed.get("ref_text", "").strip()

            if not description:
                print(f"Warning: Empty description for {speaker}, skipping")
                continue

            if not ref_text:
                # Fallback: use first non-empty sample line
                ref_text = next((ln for ln in lines if ln), "")

            # Generate a VoiceDesign preview and save stable copy
            try:
                wav_path, sr = engine.generate_voice_design(description=description, sample_text=ref_text)
                # Copy to named file in designed_voices directory
                dest_dir = os.path.join(root, "designed_voices")
                os.makedirs(dest_dir, exist_ok=True)
                safe = sanitize_filename(speaker)
                dest_path = os.path.join(dest_dir, f"{safe}_preview.wav")
                try:
                    # Prefer atomic copy
                    import shutil
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
                meta_path = os.path.join(dest_dir, f"{safe}_meta.json")
                with open(meta_path, "w", encoding="utf-8") as mf:
                    json.dump({"description": description, "ref_text": ref_text, "preview": os.path.relpath(dest_path, root)}, mf, indent=2, ensure_ascii=False)

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

                    voice_id = f"{safe}_{int(time.time())}"
                    manifest.append({
                        "id": voice_id,
                        "name": speaker,
                        "description": description,
                        "sample_text": ref_text,
                        "filename": os.path.basename(dest_path)
                    })
                    with open(manifest_path, 'w', encoding='utf-8') as mf:
                        json.dump(manifest, mf, indent=2, ensure_ascii=False)
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
        with open(voice_config_path, "w", encoding="utf-8") as vf:
            json.dump(voice_config, vf, indent=2, ensure_ascii=False)
        print(f"Updated voice_config saved to {voice_config_path}")
    except Exception as e:
        print(f"Failed to save voice_config.json: {e}")


if __name__ == '__main__':
    main()
