#!/usr/bin/env python3
"""
voice_profiler.py — Analyze narrator ref.wav clips and generate voice descriptions.

For each adapter in manifest.json:
  1. Extracts ref.wav from the source zip
  2. Runs librosa acoustic analysis (pitch, energy, rate, timbre)
  3. Sends feature summary + narrator name to local Qwen LLM
  4. Writes voice_profile back to manifest + exports voice_profiles.csv

Usage:
    python voice_profiler.py
    python voice_profiler.py --dry_run          # acoustic analysis only, no LLM
    python voice_profiler.py --overwrite        # re-profile already-profiled entries
    python voice_profiler.py --model /path/to/other.gguf
"""

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import zipfile

import numpy as np
import librosa

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def atomic_json_write(data, target_path):
    """Write JSON via temp file + os.replace, so a crash mid-write (Ctrl+C,
    OOM-kill, etc.) during this script's per-narrator manifest checkpoint
    can't truncate/corrupt manifest.json - a shared file that
    batch_train_lora.py, name_voices.py, and the web app's LoRA listing all
    depend on, not just this script's own progress."""
    directory = os.path.dirname(target_path) or "."
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, target_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise
REPO2_DIR  = "/home/fakemitch/pinokio/api/alexandria-audiobook2.git"
MANIFEST   = os.path.join(REPO2_DIR, "lora_models", "manifest.json")
MODEL_PATH = os.path.join(REPO2_DIR, "Qwen2.5-14B-Instruct-Q6_K.gguf")
OUTPUT_CSV = os.path.join(REPO2_DIR, "lora_models", "voice_profiles.csv")


# ── Acoustic analysis ─────────────────────────────────────────────────────────

def analyze_ref_wav(wav_bytes: bytes) -> dict:
    y, sr = librosa.load(io.BytesIO(wav_bytes), sr=22050, mono=True)
    duration = len(y) / sr

    # Pitch via YIN
    f0 = librosa.yin(y, fmin=50, fmax=400, sr=sr)
    voiced = f0[(f0 > 60) & (f0 < 380)]
    mean_f0 = float(np.mean(voiced)) if len(voiced) > 0 else 0.0
    std_f0  = float(np.std(voiced))  if len(voiced) > 0 else 0.0

    # Energy
    rms = librosa.feature.rms(y=y)[0]
    mean_rms = float(np.mean(rms))

    # Brightness (spectral centroid)
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    mean_centroid = float(np.mean(centroid))

    # Spectral rolloff — where 85% of energy lives; low = dark/warm, high = bright
    rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr, roll_percent=0.85)[0]
    mean_rolloff = float(np.mean(rolloff))

    # Voice smoothness: harmonic component energy vs total energy
    # Calibrated for speech (speech median ~0.37, max ~0.75 — much lower than instruments)
    y_harmonic = librosa.effects.harmonic(y, margin=2.0)
    harm_rms  = float(np.mean(librosa.feature.rms(y=y_harmonic)[0]))
    total_rms = float(np.mean(librosa.feature.rms(y=y)[0])) + 1e-8
    smoothness = harm_rms / total_rms

    # Spectral flatness — noise-like quality; calibrated for speech (median ~0.037)
    flatness = float(np.mean(librosa.feature.spectral_flatness(y=y)[0]))

    # Speaking rate
    onsets = librosa.onset.onset_detect(y=y, sr=sr, units='time')
    rate = len(onsets) / duration if duration > 0 else 0.0

    return {
        'mean_f0':       mean_f0,
        'std_f0':        std_f0,
        'mean_rms':      mean_rms,
        'mean_centroid': mean_centroid,
        'mean_rolloff':  mean_rolloff,
        'smoothness':    smoothness,
        'flatness':      flatness,
        'speaking_rate': rate,
        'duration':      duration,
    }


def interpret_features(f: dict) -> str:
    """Rich human-readable feature summary for the LLM prompt."""
    f0  = f['mean_f0']
    std = f['std_f0']
    rms = f['mean_rms']
    rate = f['speaking_rate']
    centroid = f['mean_centroid']
    rolloff  = f['mean_rolloff']
    smooth   = f['smoothness']
    flat     = f['flatness']

    # Pitch / gender / register + rough age decade estimate
    if f0 < 100:
        pitch_label = "very deep bass male"
        age_est = "likely 50s–70s"
    elif f0 < 125:
        pitch_label = "deep baritone male"
        age_est = "likely 40s–60s"
    elif f0 < 145:
        pitch_label = "baritone male"
        age_est = "likely 35s–55s"
    elif f0 < 165:
        pitch_label = "tenor / upper-register male"
        age_est = "likely 25s–45s"
    elif f0 < 185:
        pitch_label = "low alto / mature female"
        age_est = "likely 40s–60s"
    elif f0 < 210:
        pitch_label = "mezzo-soprano female"
        age_est = "likely 30s–50s"
    elif f0 < 250:
        pitch_label = "soprano female"
        age_est = "likely 20s–40s"
    else:
        pitch_label = "high soprano female"
        age_est = "likely teens–30s"

    # Pitch variation → delivery style
    if std < 18:
        variation = "nearly monotone, flat affect"
    elif std < 30:
        variation = "controlled, measured variation"
    elif std < 50:
        variation = "moderate natural variation"
    elif std < 70:
        variation = "expressive range"
    else:
        variation = "wide dramatic range, theatrical"

    # Voice texture — thresholds calibrated to speech (smoothness median ~0.37, max ~0.75)
    if smooth > 0.55:
        texture = "smooth, clean tone"
    elif smooth > 0.43 and flat < 0.04:
        texture = "warm, slightly rounded texture"
    elif smooth > 0.43:
        texture = "warm but somewhat airy"
    elif smooth > 0.30 and flat > 0.08:
        texture = "breathy, airy quality"
    elif smooth > 0.30 and flat < 0.04:
        texture = "moderately textured, warm"
    elif smooth > 0.30:
        texture = "slightly husky texture"
    elif flat > 0.08:
        texture = "breathy with rough edges"
    elif smooth < 0.20:
        texture = "gravelly, rough, husky"
    else:
        texture = "husky, noticeably textured"

    # Brightness
    if rolloff < 2500:
        brightness = "very dark/warm resonance"
    elif rolloff < 3500:
        brightness = "warm, rounded tone"
    elif rolloff < 5000:
        brightness = "balanced, full tone"
    elif rolloff < 7000:
        brightness = "bright, clear tone"
    else:
        brightness = "very bright, thin/crisp tone"

    # Energy
    if rms < 0.025:
        energy = "soft, intimate volume"
    elif rms < 0.05:
        energy = "moderate conversational volume"
    elif rms < 0.09:
        energy = "strong, projected delivery"
    else:
        energy = "loud, powerful projection"

    # Pace
    if rate < 2.5:
        pace = "very slow, deliberate"
    elif rate < 3.5:
        pace = "slow, measured"
    elif rate < 4.5:
        pace = "moderate pace"
    elif rate < 5.5:
        pace = "brisk pace"
    else:
        pace = "rapid-fire delivery"

    return (
        f"Pitch: {f0:.0f}Hz → {pitch_label}, {age_est}\n"
        f"Pitch variation: {std:.0f}Hz → {variation}\n"
        f"Voice texture: {texture}\n"
        f"Tone brightness: {brightness}\n"
        f"Volume: {energy}\n"
        f"Pace: {pace}"
    )


# ── EPUB extraction ──────────────────────────────────────────────────────────

from html.parser import HTMLParser

EPUB_DIRS = [
    "/home/fakemitch/Desktop/New folder/new new",
    "/home/fakemitch/Desktop/books",
]


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self._skip = True
        if tag in ('p', 'div', 'br', 'h1', 'h2', 'h3', 'h4'):
            self.parts.append('\n')

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self):
        return re.sub(r'\n{3,}', '\n\n', ''.join(self.parts)).strip()


def find_epub(dataset_id: str) -> str | None:
    """Find EPUB matching this adapter — first by ASIN, then by title tokens."""
    asin_m = re.search(r'_([bB][a-z0-9]{9}|\d{10})(?:_|$)', dataset_id)
    asin = asin_m.group(1).upper() if asin_m else None

    # Title slug: everything after narrator name, before ASIN, lowercased
    s = dataset_id.removeprefix("narrator_")
    s = re.sub(r"_char\d+_vol\d+$", "", s)
    if asin_m:
        s = s[:asin_m.start()]
    all_words = re.sub(r"[^a-z0-9 ]", " ", s.lower()).split()
    # Skip narrator name (first 2 tokens), include words >= 2 chars (catches "ex", "86")
    title_words = [w for w in all_words[2:] if len(w) >= 2]

    # Collect all candidates with scores, return best
    candidates = []
    for epub_dir in EPUB_DIRS:
        if not os.path.isdir(epub_dir):
            continue
        for fname in sorted(os.listdir(epub_dir)):
            if not fname.endswith('.epub'):
                continue
            fl = fname.lower()
            # Exact ASIN match wins immediately
            if asin and f"[{asin.lower()}]" in fl:
                return os.path.join(epub_dir, fname)
            if title_words:
                hits = sum(1 for w in title_words if re.search(rf'\b{re.escape(w)}\b', fl))
                ratio = hits / len(title_words)
                # Require >60% of title words to match
                if ratio > 0.6:
                    candidates.append((ratio, os.path.join(epub_dir, fname)))

    if candidates:
        candidates.sort(reverse=True)
        return candidates[0][1]

    # Fallback: if any title word is a standalone number, try matching that alone
    number_words = [w for w in title_words if w.isdigit()]
    for num in number_words:
        for epub_dir in EPUB_DIRS:
            if not os.path.isdir(epub_dir):
                continue
            for fname in sorted(os.listdir(epub_dir)):
                if fname.endswith('.epub') and re.search(rf'\b{num}\b', fname.lower()):
                    return os.path.join(epub_dir, fname)
    return None


def extract_epub_passage(epub_path: str, target_chars: int = 600) -> str:
    """Pull a prose passage from ~20% into the book (past front matter)."""
    try:
        import zipfile as zmod
        with zmod.ZipFile(epub_path, 'r') as zf:
            names = set(zf.namelist())
            if 'META-INF/container.xml' not in names:
                return ""
            container = zf.read('META-INF/container.xml').decode('utf-8', errors='ignore')
            opf_m = re.search(r'full-path="([^"]+\.opf)"', container)
            if not opf_m:
                return ""
            opf_path = opf_m.group(1)
            opf_dir  = os.path.dirname(opf_path)
            opf      = zf.read(opf_path).decode('utf-8', errors='ignore')

            spine_ids = re.findall(r'<itemref\s+idref="([^"]+)"', opf)
            manifest  = {m.group(1): m.group(2)
                         for m in re.finditer(r'<item\b[^>]+\bid="([^"]+)"[^>]+href="([^"]+)"', opf)}

            start = max(1, len(spine_ids) // 5)  # skip first 20%
            collected = ""
            for item_id in spine_ids[start:]:
                href = manifest.get(item_id, "")
                if not href:
                    continue
                fpath = (opf_dir + "/" + href).lstrip("/") if opf_dir else href
                if fpath not in names:
                    fpath = href
                if fpath not in names:
                    continue
                html_bytes = zf.read(fpath).decode('utf-8', errors='ignore')
                p = _TextExtractor()
                p.feed(html_bytes)
                raw = p.text()
                paras = [x.strip() for x in raw.split('\n\n') if len(x.strip()) > 80]
                for para in paras[:4]:
                    collected += para + " "
                if len(collected) >= target_chars:
                    break
            return collected[:target_chars].strip()
    except Exception:
        return ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def parse_narrator_name(dataset_id: str) -> str:
    """Extract a readable narrator name from dataset_id.
    Format: narrator_firstname_lastname_booktitle_asin_charN_volN
    """
    s = dataset_id.removeprefix("narrator_")
    s = re.sub(r"_char\d+_vol\d+$", "", s)
    # Strip ASIN (10-char B-prefix or 10-digit number) and everything after
    m = re.search(r"_(?:[bB][a-z0-9]{9}|\d{10})(?:_|$)", s)
    if m:
        s = s[:m.start()]
    parts = [p for p in s.split("_") if p]
    # Take first 2 tokens as first/last name; 3 if second looks like a middle name initial
    name_parts = parts[:2] if len(parts) > 2 else parts
    return " ".join(p.title() for p in name_parts)


def get_ref_wav(zip_path: str) -> bytes | None:
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if 'ref.wav' in zf.namelist():
                return zf.read('ref.wav')
    except Exception as e:
        print(f"  ERROR reading zip: {e}", flush=True)
    return None


def get_ref_text(zip_path: str) -> str:
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            if 'ref_text.txt' in zf.namelist():
                return zf.read('ref_text.txt').decode('utf-8').strip()
    except Exception:
        pass
    return ""


def parse_book_title(dataset_id: str) -> str:
    """Extract book title from dataset_id after stripping narrator name and suffixes."""
    s = dataset_id.removeprefix("narrator_")
    s = re.sub(r"_char\d+_vol\d+$", "", s)
    m = re.search(r"_(?:[bB][a-z0-9]{9}|\d{10})(?:_|$)", s)
    if m:
        s = s[:m.start()]
    parts = [p for p in s.split("_") if p]
    # Skip first 2 tokens (narrator first/last name)
    title_parts = parts[2:] if len(parts) > 2 else []
    return " ".join(p.title() for p in title_parts) if title_parts else ""


# ── LLM description ───────────────────────────────────────────────────────────

SYSTEM_MSG = (
    "You are a voice casting director for audiobooks. "
    "You write precise slate cards telling producers what a voice sounds like "
    "and what character type it should portray. Never use generic filler."
)

USER_TEMPLATE = """\
Write a voice casting description for this audiobook narrator.

Narrator: {narrator}
Book: {book_title}
Narrator's ref clip: "{ref_text}"
Book prose sample: "{book_passage}"

Acoustic data:
{summary}

Write ONE line (12-18 words) with TWO parts separated by " — ":
Part 1: What makes this voice DISTINCTIVE — use the book's genre/tone and the prose sample to inform the voice character
Part 2: Best CHARACTER AGE AND TYPE for this voice, specific to the kind of story it suits

BANNED: highly expressive, dynamic, engaging, tone, delivery
Use register words: baritone / tenor / alto / soprano / bass / mezzo
Use texture words: weathered / crisp / silky / husky / breathy / warm / gravelly / rich / bright / velvety

Examples:
- Rich weathered baritone, battle-worn and deliberate — best for 40s–60s fantasy commanders and veterans
- Bright clear soprano, quick-witted and playful — best for teenage to 20s anime-style heroines
- Warm intimate alto, literary and introspective — best for 30s–40s literary fiction female leads
- Deep gravelly bass, slow gothic authority — best for 60s+ Dracula-era aristocrats and elders
- Crisp bright tenor, urban and irreverent — best for 20s–30s contemporary male protagonists
- Silky cool mezzo, politically sharp and composed — best for 30s–50s sci-fi diplomats and spies

Voice casting line (one line, no quotes):"""


def llm_describe(llm, narrator: str, summary: str,
                 book_title: str = "", ref_text: str = "",
                 book_passage: str = "") -> str:
    ref_preview     = (ref_text[:120]     + "…") if len(ref_text)     > 120 else ref_text
    passage_preview = (book_passage[:400] + "…") if len(book_passage) > 400 else book_passage
    prompt = USER_TEMPLATE.format(
        narrator=narrator,
        book_title=book_title or "unknown",
        ref_text=ref_preview or "(not available)",
        book_passage=passage_preview or "(not available)",
        summary=summary,
    )
    resp = llm.create_chat_completion(
        messages=[
            {"role": "system", "content": SYSTEM_MSG},
            {"role": "user",   "content": prompt},
        ],
        max_tokens=80,
        temperature=0.6,
        stop=["\n"],
    )
    return resp['choices'][0]['message']['content'].strip().strip('"').strip("'")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Profile narrator voices via acoustics + LLM")
    parser.add_argument("--manifest",   default=MANIFEST)
    parser.add_argument("--model",      default=MODEL_PATH,    help="Path to GGUF model")
    parser.add_argument("--output_csv", default=OUTPUT_CSV)
    parser.add_argument("--dry_run",    action="store_true",   help="Acoustics only, skip LLM")
    parser.add_argument("--overwrite",  action="store_true",   help="Re-profile existing entries")
    args = parser.parse_args()

    if not os.path.exists(args.manifest):
        print(f"ERROR: manifest not found: {args.manifest} (run batch_train_lora.py first)")
        sys.exit(1)
    with open(args.manifest, encoding='utf-8') as f:
        manifest = json.load(f)

    batch = [e for e in manifest if e.get('zip_source')]
    todo  = [e for e in batch if args.overwrite or 'voice_profile' not in e]

    print(f"{len(batch)} adapters with zip_source — {len(todo)} to profile")
    if not todo:
        print("All already profiled. Use --overwrite to redo.")
        sys.exit(0)

    # Load LLM once (expensive — ~10-20s for 14B Q6)
    llm = None
    if not args.dry_run:
        if not os.path.exists(args.model):
            print(f"ERROR: model not found: {args.model}")
            sys.exit(1)
        print(f"Loading LLM: {os.path.basename(args.model)} …", flush=True)
        from llama_cpp import Llama
        llm = Llama(
            model_path=args.model,
            n_ctx=2048,
            n_gpu_layers=-1,
            verbose=False,
        )
        print("LLM ready.\n", flush=True)

    csv_rows = []

    for i, entry in enumerate(todo, 1):
        dataset_id = entry.get('dataset_id', entry.get('name', ''))
        zip_path   = entry['zip_source']
        best_loss  = entry.get('best_loss', entry.get('final_loss', 0))

        print(f"[{i:3d}/{len(todo)}] {dataset_id[:72]}", flush=True)

        wav_bytes = get_ref_wav(zip_path)
        if wav_bytes is None:
            print(f"  SKIP — no ref.wav", flush=True)
            continue

        try:
            features = analyze_ref_wav(wav_bytes)
            summary  = interpret_features(features)
        except Exception as e:
            print(f"  ERROR in acoustic analysis: {e}", flush=True)
            continue

        print(f"  {summary}", flush=True)

        narrator     = parse_narrator_name(dataset_id)
        book_title   = parse_book_title(dataset_id)
        ref_text     = get_ref_text(zip_path)
        description  = summary  # fallback for dry_run

        epub_path    = find_epub(dataset_id)
        book_passage = extract_epub_passage(epub_path) if epub_path else ""

        if epub_path:
            print(f"  epub: {os.path.basename(epub_path)}", flush=True)
        else:
            print(f"  epub: not found", flush=True)

        if llm is not None:
            try:
                description = llm_describe(llm, narrator, summary,
                                           book_title=book_title, ref_text=ref_text,
                                           book_passage=book_passage)
                print(f"  → {description}", flush=True)
            except Exception as e:
                print(f"  LLM error ({e}) — keeping acoustic summary", flush=True)

        if not args.dry_run:
            entry['voice_profile'] = description
            entry['voice_features'] = {
                'mean_f0':       round(features['mean_f0'], 1),
                'std_f0':        round(features['std_f0'], 1),
                'mean_rms':      round(features['mean_rms'], 4),
                'speaking_rate': round(features['speaking_rate'], 2),
                'mean_centroid': round(features['mean_centroid'], 0),
                'smoothness':    round(features['smoothness'], 3),
                'flatness':      round(features['flatness'], 4),
            }
            # Checkpoint manifest after each narrator
            atomic_json_write(manifest, args.manifest)

        csv_rows.append({
            'id':            entry['id'],
            'narrator':      narrator,
            'best_loss':     best_loss,
            'voice_profile': description,
            'gender_est':    'female' if features['mean_f0'] >= 165 else 'male',
            'mean_f0':       round(features['mean_f0'], 1),
            'std_f0':        round(features['std_f0'], 1),
            'speaking_rate': round(features['speaking_rate'], 2),
        })

    # Write CSV
    if csv_rows:
        with open(args.output_csv, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(csv_rows[0].keys()))
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"\nCSV: {args.output_csv}")

    print(f"Done: {len(csv_rows)} profiles written")


if __name__ == "__main__":
    main()
