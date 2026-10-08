#!/usr/bin/env python3
"""
train_lora.py - Standalone LoRA fine-tuning script for Qwen3-TTS Base model.

Runs as a subprocess with structured stdout for log capture by Alexandria.
Prints [DATA], [TRAIN], [EPOCH], [DONE], [ERROR] prefixed lines for progress tracking.

Targets the talker's attention layers with LoRA via PEFT. Training uses teacher forcing:
the full input sequence (text + ground-truth codec codes) is built, the talker forward
produces the main loss (first codec group prediction), and forward_sub_talker_finetune
produces the code predictor loss (remaining groups). Both losses backpropagate through
the LoRA-adapted talker.

Usage:
    python train_lora.py \
        --data_dir /path/to/dataset \
        --output_dir /path/to/output \
        --epochs 50 --lr 1e-6 --lora_r 64 --lora_alpha 128
"""

import argparse
import gc
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback

from device_utils import (enable_rocm_optimizations, is_oom_failure,
                          normalize_device, resolve_device)
from dataset_metadata import get_training_metadata, get_training_reference_path
from voice_manifest import validate_adapter_training_output
from utils import is_path_inside
from adapter_checkpoint_transaction import save_adapter_checkpoint, validate_adapter_checkpoint_generation
from lora_evidence import get_file_sha256

# THE ONE PLACE THE TRAINING RATE IS DECIDED. It was four places that
# disagreed: this CLI, the API request model, the UI input, and
# batch_train_lora.py - the last saying 1e-6 and the other three 5e-6.
#
# Measured on 2026-08-06, five adapters, two conditions:
#   5e-6  2 of 2 ran away - 163.8s of audio for a 7.3s line, every render,
#         hitting the token ceiling, never emitting an end of speech.
#   1e-6  3 of 3 stopped correctly - median duration 1.01x, 0.87x and 0.94x
#         the human reading, in English, Japanese and Chinese.
#
# Training loss does not reveal this (the runaway adapters reached 2.9 and
# 3.4, which look ordinary), so a wrong default costs a full training run and
# the generation run after it before anything looks amiss.
DEFAULT_LEARNING_RATE = 1e-6


def get_checkpoint_sha256(checkpoint_dir):
    digest = hashlib.sha256()
    with open(os.path.join(checkpoint_dir, "adapter_model.safetensors"), "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def get_code_lineage():
    """-> {"code_commit": sha, "code_dirty": bool} for the tree that trained this.

    Every adapter recorded its data lineage (`ref_sample_audio`) and its
    hyperparameters, and none of the 254 on disk recorded which code produced
    them. That is the one claim you cannot reconstruct later: the reference
    audio is still there to look at, the commit is not.

    Best-effort by design - a checkout without git, or an adapter trained from
    an exported copy, records None rather than failing a training run that has
    already spent the GPU.
    """
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        commit = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10)
        if commit.returncode != 0:
            return {"code_commit": None, "code_dirty": None}
        dirty = subprocess.run(
            ["git", "-C", repo, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=10)
        return {"code_commit": commit.stdout.strip() or None,
                "code_dirty": bool(dirty.stdout.strip())
                if dirty.returncode == 0 else None}
    except (OSError, subprocess.SubprocessError):
        return {"code_commit": None, "code_dirty": None}


def save_evaluation_candidate(model, output_dir, existing_records, max_candidates,
                              epoch, loss):
    """Populate one bounded candidate inside a private generation staging directory."""
    if len(existing_records) >= max_candidates:
        return list(existing_records), None
    candidate_id = f"epoch_{epoch:03d}"
    candidate_path = os.path.join(output_dir, "candidates", candidate_id)
    model.save_pretrained(candidate_path)
    record = {"id": candidate_id, "epoch": epoch, "loss": loss,
              "path": candidate_path, "sha256": get_checkpoint_sha256(candidate_path)}
    return [*existing_records, record], record


def deduplicate_evaluation_candidates(output_dir, records):
    production_hash = get_checkpoint_sha256(output_dir)
    retained = []
    skipped = []
    seen = {production_hash: "production"}
    for record in records:
        digest = record.get("sha256") or get_checkpoint_sha256(record["path"])
        duplicate_of = seen.get(digest)
        if duplicate_of:
            shutil.rmtree(record["path"])
            skipped.append({"id": record["id"], "duplicate_of": duplicate_of,
                            "sha256": digest})
            continue
        retained_record = {**record, "sha256": digest}
        retained.append(retained_record)
        seen[digest] = record["id"]
    return retained, skipped, production_hash


def get_training_reference_preview(text):
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text[:60].encode(encoding, errors="backslashreplace").decode(encoding)


def get_training_reference_text(data_dir, ref_audio_path, samples):
    ref_text_file = os.path.join(data_dir, "ref_text.txt")
    ref_sample_text = ""
    if os.path.exists(ref_text_file):
        with open(ref_text_file, "r", encoding="utf-8") as f:
            ref_sample_text = f.read().strip()
        if ref_sample_text:
            print(f"[DATA] Using ref text from ref_text.txt: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)
    if not ref_sample_text:
        from lora_evidence import get_file_sha256
        reference_hash = get_file_sha256(ref_audio_path)
        matched_sample = next((sample for sample in samples
                               if sample.get("audio_path")
                               and os.path.isfile(sample["audio_path"])
                               and get_file_sha256(sample["audio_path"]) == reference_hash), None)
        if matched_sample is not None:
            ref_sample_text = matched_sample["text"]
            print(f"[DATA] Using matching sample text as ref text: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)
        else:
            # Legacy datasets: ref.wav is typically the first sample
            ref_sample_text = samples[0]["text"]
            print(f"[DATA] Using first sample text as ref text: '{get_training_reference_preview(ref_sample_text)}...'", flush=True)

    return ref_sample_text


def get_training_checkpoint_metadata(args, samples, ref_audio_path, ref_sample_text,
                                     epochs_completed, avg_loss, best_loss, training_time, total_oom_skips):
    def _json_safe(x):
        # /api/lora/models serializes with allow_nan=False, so a NaN final_loss
        # (an all-OOM-skipped run leaves avg_loss=NaN) or the initial inf best_loss
        # would 500 the whole model listing. Coerce any non-finite value to None.
        return x if isinstance(x, (int, float)) and float("-inf") < x < float("inf") else None

    meta = {
        "model_name": args.model_name,
        "epochs": epochs_completed,
        "requested_epochs": args.epochs,
        "lr": args.lr,
        "lora_r": args.lora_r,
        "lora_alpha": args.lora_alpha,
        "gradient_accumulation_steps": args.gradient_accumulation_steps,
        "batch_size": args.batch_size,
        "num_samples": len(samples),
        "final_loss": _json_safe(avg_loss),
        "best_loss": _json_safe(best_loss),
        "training_time_seconds": round(training_time, 1),
        "oom_skips": total_oom_skips,
        "language": args.language,
        "ref_sample_audio": ref_audio_path,
        "ref_sample_text": ref_sample_text,
        **get_code_lineage(),
    }
    return meta


def save_training_checkpoint(model, output_dir, ref_audio_path, ref_sample_text, metadata,
                             existing_records, max_candidates, epoch, loss,
                             keep_candidate=False, finalize=False):
    """Publish production, retained candidates and matching evidence as one generation."""
    validate_adapter_training_output(output_dir)
    outcome = None

    def write_generation(staging):
        nonlocal outcome
        records = []
        for record in existing_records:
            if not isinstance(record.get('id'), str) or not re.fullmatch(r'epoch_[0-9]{3,}', record['id']):
                raise ValueError('Invalid training candidate identity')
            path = os.path.join(staging, 'candidates', record['id'])
            digest = get_checkpoint_sha256(path)
            if record.get('sha256') and record['sha256'] != digest:
                raise ValueError('Retained training candidate changed before publication')
            records.append({**record, 'path':path, 'sha256':digest})
        if model is not None:
            model.save_pretrained(staging)
        saved = None
        if keep_candidate:
            records, saved = save_evaluation_candidate(model, staging, records,
                                                       max_candidates, epoch, loss)
        skipped = []
        if finalize:
            records, skipped, production_hash = deduplicate_evaluation_candidates(staging, records)
        else:
            production_hash = get_checkpoint_sha256(staging)
        ref_dest = os.path.join(staging, 'ref_sample.wav')
        shutil.copy2(ref_audio_path, ref_dest)
        meta = {**metadata, 'ref_sample_text':ref_sample_text,
                'checkpoint_sha256':production_hash,
                'reference_audio_sha256':get_file_sha256(ref_dest),
                'evaluation_candidates':[
                    {key:record[key] for key in ('id', 'epoch', 'loss', 'sha256')}
                    for record in records], 'evaluation_candidate_skips':skipped}
        with open(os.path.join(staging, 'training_meta.json'), 'w', encoding='utf-8') as handle:
            json.dump(meta, handle, indent=2, ensure_ascii=False, allow_nan=False)
        for record in records:
            shutil.copy2(ref_dest, os.path.join(record['path'], 'ref_sample.wav'))
            candidate_meta = {**meta, 'candidate_id':record['id'], 'candidate_epoch':record['epoch'],
                              'candidate_loss':record['loss'], 'checkpoint_sha256':record['sha256'],
                              'production_checkpoint_sha256':production_hash}
            with open(os.path.join(record['path'], 'training_meta.json'), 'w', encoding='utf-8') as handle:
                json.dump(candidate_meta, handle, indent=2, ensure_ascii=False, allow_nan=False)
            validate_adapter_checkpoint_generation(record['path'])
        published = [{**record, 'path':os.path.join(output_dir, 'candidates', record['id'])}
                     for record in records]
        outcome = (published, next((record for record in published
                                   if saved and record['id'] == saved['id']), None), skipped)

    save_adapter_checkpoint(output_dir, write_generation)
    return outcome


def parse_args():
    parser = argparse.ArgumentParser(description="LoRA fine-tuning for Qwen3-TTS Base model")
    parser.add_argument("--data_dir", required=True, help="Directory containing metadata.jsonl and audio files")
    parser.add_argument("--output_dir", required=True, help="Directory to save the LoRA adapter")
    parser.add_argument("--model_name", default="Qwen/Qwen3-TTS-12Hz-1.7B-Base",
                        help="Base model name or path")
    parser.add_argument("--epochs", type=int, default=50, help="Number of training epochs")
    parser.add_argument("--lr", type=float, default=DEFAULT_LEARNING_RATE,
                        help="Learning rate")
    parser.add_argument("--batch_size", type=int, default=1, help="Batch size (samples per step)")
    parser.add_argument("--lora_r", type=int, default=32, help="LoRA rank")
    parser.add_argument("--lora_alpha", type=int, default=128, help="LoRA alpha scaling")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8,
                        help="Gradient accumulation steps")
    parser.add_argument("--device", type=normalize_device, default="auto",
                        help="Device: auto, cpu, cuda, cuda:N, mps, rocm, or hip")
    parser.add_argument("--language", default="english",
                        help="Language for codec prefix token (english, chinese, korean, japanese, etc.)")
    parser.add_argument("--max_audio_seconds", type=float, default=30.0,
                        help="Maximum audio duration in seconds (longer clips are skipped)")
    parser.add_argument("--target_loss", type=float, default=None,
                        help="Early-stop when epoch avg_loss first drops at or below this value. "
                             "Best checkpoint with loss >= 4.1 is always preserved. "
                             "Recommended: 4.15 for auto sweet-spot detection.")
    parser.add_argument("--seed", type=int, default=None,
                        help="Random seed for LoRA initialization and reproducible shuffling")
    parser.add_argument("--candidate_checkpoints", type=int, default=0, choices=range(0, 3),
                        help="Keep the first N safe improvement checkpoints for evaluation (0-2)")
    return parser.parse_args()


# ── Data preparation ────────────────────────────────────────────────────

def load_dataset(data_dir, hf_model, processor, device, dtype, max_audio_seconds):
    """Load metadata.jsonl and prepare training samples.

    For each entry, encodes audio to codec IDs and tokenizes text.
    Speaker embedding is extracted from a consistent ref_audio (same for all
    samples) per the official Qwen3-TTS fine-tuning approach.

    Returns sample dicts with pre-computed CPU tensors. Input construction
    transfers only the current sample to the training device.
    """

    # HONOUR THE TRAIN SPLIT WHEN THE DATASET HAS ONE.
    #
    # This loaded the ROOT metadata.jsonl unconditionally. The dataset builder
    # writes 180 train / 20 val with zero overlap AND a root file containing all
    # 200, so every adapter trained on its own validation set: 67 of 75 in the
    # library record num_samples=200. The split existed and was ignored, which
    # meant no adapter could be honestly evaluated - any test line had already
    # been seen.
    #
    # Paths inside all three files are relative to the DATASET ROOT
    # ("train/sample_1200.wav", "val/sample_1210.wav"), so data_dir stays the
    # root and only the choice of metadata file changes. Datasets without a
    # train/ subdirectory fall back to the old behaviour untouched.
    entries, used_split = get_training_metadata(data_dir)

    import librosa
    import numpy as np
    import torch
    from qwen_tts.core.models.modeling_qwen3_tts import mel_spectrogram

    if used_split:
        held = os.path.join(data_dir, "val", "metadata.jsonl")
        n_held = 0
        if os.path.exists(held):
            with open(held, encoding="utf-8") as f:
                n_held = sum(1 for line in f if line.strip())
        print(f"[DATA] Found {len(entries)} entries in train/metadata.jsonl "
              f"({n_held} held out in val/ and NOT trained on)", flush=True)
    else:
        print(f"[DATA] Found {len(entries)} entries in metadata.jsonl "
              f"(no train/ split in this dataset; nothing is held out)",
              flush=True)

    # ── Extract speaker embedding from ref_audio (consistent across all samples) ──
    # Check for ref_audio field in entries, or fall back to ref.wav in dataset dir,
    # or use the first training sample as reference.
    def _resolve_in_data_dir(rel_path):
        """Resolve `rel_path` under data_dir, or return None if it escapes."""
        resolved = os.path.realpath(os.path.join(data_dir, rel_path))
        if is_path_inside(resolved, data_dir):
            return resolved
        return None

    try:
        ref_audio_path = get_training_reference_path(data_dir, entries)
    except ValueError as error:
        print(f"[ERROR] {error}", flush=True)
        sys.exit(1)

    print(f"[DATA] Using reference audio: {os.path.basename(ref_audio_path)}", flush=True)

    ref_audio, _ = librosa.load(ref_audio_path, sr=24000, mono=True)
    ref_audio = ref_audio.astype(np.float32)

    with torch.no_grad():
        ref_mels = mel_spectrogram(
            torch.from_numpy(ref_audio).unsqueeze(0),
            n_fft=1024, num_mels=128, sampling_rate=24000,
            hop_size=256, win_size=1024, fmin=0, fmax=12000,
        ).transpose(1, 2).to(device).to(dtype)
        spk_embedding = hf_model.speaker_encoder(ref_mels).detach().cpu()

    print(f"[DATA] Speaker embedding extracted from reference audio", flush=True)

    samples = []
    skipped_missing = 0
    skipped_too_long = 0

    for i, entry in enumerate(entries):
        audio_rel = entry.get("audio_filepath") or entry.get("audio", "")
        audio_path = os.path.realpath(os.path.join(data_dir, audio_rel))
        if not is_path_inside(audio_path, data_dir):
            print(f"[DATA] SKIP {i+1}/{len(entries)}: {audio_rel} (escapes dataset directory)", flush=True)
            skipped_missing += 1
            continue
        text = entry["text"]

        if not os.path.exists(audio_path):
            print(f"[DATA] SKIP {i+1}/{len(entries)}: {audio_rel} (file not found)", flush=True)
            skipped_missing += 1
            continue

        print(f"[DATA] Tokenizing {i+1}/{len(entries)}: {os.path.basename(audio_path)}", flush=True)

        # Load audio
        audio, sr = librosa.load(audio_path, sr=None, mono=True)
        duration = len(audio) / sr
        if duration > max_audio_seconds:
            print(f"[DATA] SKIP {i+1}/{len(entries)}: {audio_rel} ({duration:.1f}s > {max_audio_seconds}s)", flush=True)
            skipped_too_long += 1
            continue

        # Encode audio to codec IDs via speech tokenizer
        with torch.no_grad():
            enc = hf_model.speech_tokenizer.encode(audio, sr=sr)
            # 12Hz tokenizer returns list of [T, num_code_groups] per sample
            codec_ids = enc.audio_codes[0]  # [T, num_code_groups]

        # Tokenize text with chat template: <|im_start|>assistant\n{text}<|im_end|>\n<|im_start|>assistant\n
        assistant_text = f"<|im_start|>assistant\n{text}<|im_end|>\n<|im_start|>assistant\n"
        text_inputs = processor(text=assistant_text, return_tensors="pt", padding=True)
        text_ids = text_inputs["input_ids"].detach().cpu()
        if text_ids.dim() == 1:
            text_ids = text_ids.unsqueeze(0)

        samples.append({
            "codec_ids": codec_ids.detach().cpu(),      # cached on host [T, num_code_groups]
            "spk_embedding": spk_embedding,             # shared ref embedding [1, enc_dim]
            "text_ids": text_ids,                        # [1, text_len]
            "audio_path": audio_path,
            "text": text,
            "duration": duration,
        })

    skipped = skipped_missing + skipped_too_long
    print(f"[DATA] Prepared {len(samples)} samples ({skipped} skipped: "
          f"{skipped_missing} missing, {skipped_too_long} too long)", flush=True)
    if not samples:
        print("[ERROR] No valid training samples", flush=True)
        sys.exit(1)

    # Duration stats
    durations = [s["duration"] for s in samples]
    total_dur = sum(durations)
    print(f"[DATA] Duration stats: min={min(durations):.1f}s  max={max(durations):.1f}s  "
          f"mean={total_dur/len(durations):.1f}s  total={total_dur/60:.1f}min", flush=True)

    return samples, ref_audio_path


# ── Input construction ──────────────────────────────────────────────────

def build_teacher_forcing_input(sample, hf_model, device, dtype, language="english"):
    """Build the full teacher-forcing input sequence for one training sample.

    Replicates the generate() method's input construction but includes
    ground-truth codec embeddings at every audio timestep.

    Returns:
        inputs_embeds: [1, prefill_len + T, D] full input sequence
        labels: [1, prefill_len + T] with -100 for prefill, first codec group for audio
        all_codec_ids: [T, num_code_groups] ground truth for code predictor
        prefill_len: int, number of prefill positions
    """
    import torch

    talker = hf_model.talker
    config = hf_model.config
    tc = config.talker_config  # talker config
    language = str(language or "").strip().lower()
    language_id = (tc.codec_language_id or {}).get(language)
    if language != "auto" and language_id is None:
        raise ValueError(f"Unsupported training language {language!r}; "
                         "choose a model-supported language or 'auto'.")
    if language == "auto":
        language_id = None

    codec_ids_2d = sample["codec_ids"].to(device)  # only the current sample [T, num_code_groups]
    spk_embedding = sample["spk_embedding"].to(device=device, dtype=dtype)  # [1, enc_dim]
    text_ids = sample["text_ids"].to(device)  # [1, text_len]

    T = codec_ids_2d.shape[0]  # number of audio frames
    num_code_groups = tc.num_code_groups

    # ── Special token embeddings ──
    special_ids = torch.tensor(
        [[config.tts_bos_token_id, config.tts_eos_token_id, config.tts_pad_token_id]],
        device=device, dtype=text_ids.dtype,
    )
    tts_bos_embed, tts_eos_embed, tts_pad_embed = talker.text_projection(
        talker.get_text_embeddings()(special_ids)
    ).chunk(3, dim=1)  # each [1, 1, D]

    # ── Build prefill sequence (mirrors generate method) ──
    parts = []

    # Role tokens: first 3 tokens of text_ids = <|im_start|>assistant\n
    role_embed = talker.text_projection(
        talker.get_text_embeddings()(text_ids[:, :3])
    )  # [1, 3, D]

    # Codec prefix: [think_id, think_bos_id, language_id, think_eos_id]
    if language_id is not None:
        codec_prefill_list = [[tc.codec_think_id, tc.codec_think_bos_id,
                               language_id, tc.codec_think_eos_id]]
    else:
        codec_prefill_list = [[tc.codec_nothink_id, tc.codec_think_bos_id,
                               tc.codec_think_eos_id]]

    codec_prefix_embed = talker.get_input_embeddings()(
        torch.tensor(codec_prefill_list, device=device, dtype=text_ids.dtype)
    )  # [1, 3-4, D]

    # Speaker embed + codec_pad + codec_bos
    codec_suffix_embed = talker.get_input_embeddings()(
        torch.tensor([[tc.codec_pad_id, tc.codec_bos_id]], device=device, dtype=text_ids.dtype)
    )  # [1, 2, D]

    codec_embed = torch.cat([
        codec_prefix_embed,
        spk_embedding.view(1, 1, -1),
        codec_suffix_embed,
    ], dim=1)  # [1, prefix_codec_len, D]  (e.g. 7 for english: think,bos,lang,eos,spk,pad,bos)

    prefix_codec_len = codec_embed.shape[1]

    # Build the text-layer + codec-layer combined prefix
    # tts_pad for (prefix_codec_len - 2) positions + tts_bos, added to codec_embed[:-1]
    tts_prefix = torch.cat([
        tts_pad_embed.expand(-1, prefix_codec_len - 2, -1),
        tts_bos_embed,
    ], dim=1)  # [1, prefix_codec_len - 1, D]

    prefix_embed = tts_prefix + codec_embed[:, :-1]  # [1, prefix_codec_len - 1, D]

    # Combine role + prefix
    role_prefix = torch.cat([role_embed, prefix_embed], dim=1)  # [1, 3 + prefix_codec_len - 1, D]
    parts.append(role_prefix)

    # Text content (non-streaming mode): text_content + eos, with codec_pad overlay
    # text_ids[:, 3:-5] is the actual text content (strip role prefix and chat suffix)
    text_content_ids = text_ids[:, 3:-5]
    text_content_len = text_content_ids.shape[1]

    text_content_embed = talker.text_projection(
        talker.get_text_embeddings()(text_content_ids)
    )  # [1, text_content_len, D]
    text_with_eos = torch.cat([text_content_embed, tts_eos_embed], dim=1)  # [1, text_content_len + 1, D]

    # Codec pad overlay for text portion
    text_pad_ids = torch.full(
        (1, text_content_len + 1), tc.codec_pad_id,
        device=device, dtype=text_ids.dtype,
    )
    text_codec_pad_embed = talker.get_input_embeddings()(text_pad_ids)
    text_portion = text_with_eos + text_codec_pad_embed  # [1, text_content_len + 1, D]
    parts.append(text_portion)

    # End of prefill: tts_pad + codec_bos
    codec_bos_embed = talker.get_input_embeddings()(
        torch.tensor([[tc.codec_bos_id]], device=device, dtype=text_ids.dtype)
    )
    end_embed = tts_pad_embed + codec_bos_embed  # [1, 1, D]
    parts.append(end_embed)

    # Concatenate full prefill
    prefill_embeds = torch.cat(parts, dim=1)  # [1, prefill_len, D]
    prefill_len = prefill_embeds.shape[1]

    # ── Build audio steps (teacher forcing with ground-truth codes) ──
    # For each audio timestep t, the input is: sum of all codec group embeddings + tts_pad
    # codec_ids_2d: [T, num_code_groups]
    codec_ids_per_step = codec_ids_2d  # [T, num_code_groups]

    # Embed each codec group and sum
    # Group 0 uses the main embedding
    group_0_embed = talker.get_input_embeddings()(
        codec_ids_per_step[:, :1]
    )  # [T, 1, D]

    # Groups 1..N-1 use code_predictor embeddings
    group_embeds = [group_0_embed]
    for g in range(1, num_code_groups):
        g_embed = talker.code_predictor.get_input_embeddings()[g - 1](
            codec_ids_per_step[:, g:g + 1]
        )  # [T, 1, D]
        group_embeds.append(g_embed)

    # Sum all groups: [T, 1, D] -> squeeze to [T, D] -> unsqueeze batch
    all_groups = torch.cat(group_embeds, dim=1)  # [T, num_code_groups, D]
    codec_sum = all_groups.sum(dim=1)  # [T, D]

    # Add tts_pad_embed (trailing_text_hidden for non-streaming x-vector mode)
    audio_embeds = codec_sum + tts_pad_embed.squeeze(0)  # [T, D] broadcast
    audio_embeds = audio_embeds.unsqueeze(0)  # [1, T, D]

    # ── Full input sequence ──
    full_input = torch.cat([prefill_embeds, audio_embeds], dim=1)  # [1, prefill_len + T, D]

    # ── Labels ──
    # First codec group at each audio timestep. -100 for prefill (ignored by loss).
    first_codec = codec_ids_2d[:, 0]  # [T] - first code group across all timesteps
    labels = torch.full((1, prefill_len + T), -100, device=device, dtype=torch.long)
    labels[0, prefill_len:] = first_codec

    return full_input, labels, codec_ids_per_step, prefill_len


# ── Training loop ───────────────────────────────────────────────────────

def run_training_sample(sample, hf_model, base_talker, transformer, parameters,
                        device, dtype, language="english", gradient_accumulation_steps=1):
    """Run the single-sample form of the shared minibatch transaction."""
    return run_training_batch([sample], hf_model, base_talker, transformer, parameters,
                              device, dtype, language, gradient_accumulation_steps)


def run_training_batch(samples, hf_model, base_talker, transformer, parameters,
                       device, dtype, language="english", gradient_accumulation_steps=1):
    """Commit a complete minibatch or restore the exact prior gradient references.

    One additional trainable-gradient set may coexist with the accumulated
    window. Merge into the attempted gradients, leaving prior gradients intact
    until the whole minibatch and all scalar loss reads have succeeded. OOM results
    contain only text; failed tensors/exception frames leave before reclamation.
    """
    import torch
    import torch.nn.functional as F

    if not samples:
        raise ValueError("Training minibatch must not be empty")
    previous_grads = [parameter.grad for parameter in parameters]
    committed = False
    try:
        for parameter in parameters:
            parameter.grad = None
        # Build teacher-forcing input
        prepared = [build_teacher_forcing_input(sample, hf_model, device, dtype, language=language)
                    for sample in samples]
        full_input, labels, all_codec_ids, _ = prepared[0]
        forward_args = {"inputs_embeds": full_input, "use_cache": False}
        if len(prepared) > 1:
            max_length = max(item[0].shape[1] for item in prepared)
            full_input = torch.cat([F.pad(item[0], (0, 0, 0, max_length - item[0].shape[1]))
                                    for item in prepared], dim=0)
            labels = torch.cat([F.pad(item[1], (0, max_length - item[1].shape[1]), value=-100)
                                for item in prepared], dim=0)
            all_codec_ids = torch.cat([item[2] for item in prepared], dim=0)
            lengths = torch.tensor([item[0].shape[1] for item in prepared], device=device)
            forward_args = {"inputs_embeds": full_input, "use_cache": False,
                            "attention_mask": (torch.arange(max_length, device=device)[None, :]
                                               < lengths[:, None]).long()}

        # ── Forward pass through talker transformer ──
        # Position IDs are auto-created by the model (3D multi-rope)
        output = transformer(**forward_args)
        hidden_states = output.last_hidden_state  # [batch, seq_len, hidden_size]

        # ── Talker main loss: predict first codec group ──
        # codec_head predictions at audio positions
        # With standard causal LM shift: logit at position i predicts label at position i+1
        # Position prefill_len-1 predicts first audio code (labels[prefill_len])
        logits = base_talker.codec_head(hidden_states)  # [batch, seq_len, vocab_size]

        # Shift: logits[:-1] predict labels[1:]
        shift_logits = logits[:, :-1, :].contiguous()
        shift_labels = labels[:, 1:].contiguous()

        talker_loss = F.cross_entropy(
            shift_logits.view(-1, shift_logits.size(-1)),
            shift_labels.view(-1),
            ignore_index=-100,
        )

        # ── Code predictor loss: predict remaining codec groups ──
        # Extract hidden states at audio-predicting positions
        # Position prefill_len-1 predicts audio step 0,
        # position prefill_len predicts audio step 1, etc.
        audio_parts = [
            hidden_states[index, prefill - 1:prefill + codes.shape[0] - 1, :]
            for index, (_inputs, _labels, codes, prefill) in enumerate(prepared)]
        audio_hidden = audio_parts[0] if len(audio_parts) == 1 else torch.cat(audio_parts, dim=0)

        # all_codec_ids: [T, num_code_groups]
        _, sub_loss = base_talker.forward_sub_talker_finetune(
            all_codec_ids, audio_hidden
        )

        # Combined loss (0.3 weight on sub-talker per official Qwen3-TTS training)
        total_loss = talker_loss + 0.3 * sub_loss

        if not bool(torch.isfinite(total_loss).all()):
            raise RuntimeError("Training produced a non-finite loss; refusing optimizer update")

        # Scale for gradient accumulation
        scaled_loss = total_loss / gradient_accumulation_steps
        scaled_loss.backward()

        # Capture loss values before freeing tensors
        step_loss = total_loss.item()
        step_talker_loss = talker_loss.item()
        step_sub_loss = sub_loss.item()

        merged_grads = []
        with torch.no_grad():
            for parameter, previous in zip(parameters, previous_grads):
                attempted = parameter.grad
                if attempted is not None and previous is not None:
                    attempted.add_(previous)
                gradient = attempted if attempted is not None else previous
                if gradient is not None and not bool(torch.isfinite(gradient).all()):
                    raise RuntimeError("Training produced non-finite gradients; refusing optimizer update")
                merged_grads.append(gradient)
        for parameter, gradient in zip(parameters, merged_grads):
            parameter.grad = gradient
        committed = True
        return {"loss": step_loss, "talker_loss": step_talker_loss, "sub_loss": step_sub_loss}
    except RuntimeError as error:
        if is_oom_failure(error):
            return {"oom_error": str(error)}
        raise
    finally:
        if not committed:
            for parameter, previous in zip(parameters, previous_grads):
                parameter.grad = previous


def apply_training_accumulation_step(parameters, optimizer, configured_steps, successful_steps):
    """Normalize a successful accumulation window before clipping and stepping."""
    import torch
    if not 0 < successful_steps <= configured_steps:
        raise ValueError("Invalid successful gradient accumulation window")
    if successful_steps < configured_steps:
        with torch.no_grad():
            for parameter in parameters:
                if parameter.grad is not None:
                    parameter.grad.mul_(configured_steps / successful_steps)
    torch.nn.utils.clip_grad_norm_(parameters, max_norm=1.0)
    optimizer.step()
    optimizer.zero_grad()


def train(args):
    validate_adapter_training_output(args.output_dir)
    import torch

    device = resolve_device(args.device)
    dtype = torch.bfloat16 if "cuda" in device else torch.float32

    enable_rocm_optimizations()

    print(f"[TRAIN] Device: {device}, dtype: {dtype}", flush=True)

    # Fail fast on non-positive hyperparameters before loading the model /
    # tokenizing (same rationale as the --epochs guard below): lora_r <= 0
    # ZeroDivisions at the alpha/r ratio, gradient_accumulation_steps <= 0
    # ZeroDivisions deep in the training loop, and lora_alpha <= 0 is degenerate.
    for _name, _val in (("--lora_r", args.lora_r),
                        ("--lora_alpha", args.lora_alpha),
                        ("--gradient_accumulation_steps", args.gradient_accumulation_steps),
                        ("--batch_size", args.batch_size),
                        ("--epochs", args.epochs)):
        if _val < 1:
            print(f"[ERROR] {_name} must be >= 1 (got {_val}).", flush=True)
            sys.exit(1)
    if not math.isfinite(args.lr) or args.lr <= 0:
        print(f"[ERROR] --lr must be a finite positive number (got {args.lr}).", flush=True)
        sys.exit(1)

    print(f"[TRAIN] Config: epochs={args.epochs}, lr={args.lr}, lora_r={args.lora_r}, "
          f"lora_alpha={args.lora_alpha}, grad_accum={args.gradient_accumulation_steps}", flush=True)

    # Refuse malformed standalone datasets before loading the Base model.
    get_training_metadata(args.data_dir)

    # ── Load model ──
    print("[TRAIN] Loading Base model...", flush=True)
    from qwen_tts import Qwen3TTSModel

    model = Qwen3TTSModel.from_pretrained(
        args.model_name,
        device_map=device if device != "cpu" else None,
        dtype=dtype,
        attn_implementation="eager",
    )
    processor = model.processor
    hf_model = model.model  # Qwen3TTSForConditionalGeneration

    print("[TRAIN] Base model loaded", flush=True)

    # ── Load data ──
    samples, ref_audio_path = load_dataset(args.data_dir, hf_model, processor, device, dtype, args.max_audio_seconds)

    # ── Pre-training settings summary ──
    sample_presentations = len(samples) * args.epochs
    forward_batches = ((len(samples) + args.batch_size - 1) // args.batch_size) * args.epochs
    effective_batch = args.batch_size * args.gradient_accumulation_steps
    alpha_r_ratio = args.lora_alpha / args.lora_r
    if sample_presentations < 150:
        passes_verdict = "LOW — likely undertrained"
    elif sample_presentations <= 400:
        passes_verdict = "good range"
    elif sample_presentations <= 600:
        passes_verdict = "high — watch for overfit"
    else:
        passes_verdict = "VERY HIGH — strong overfit risk"
    print(f"[TRAIN] === Pre-training settings ===", flush=True)
    print(f"[TRAIN]   samples         : {len(samples)}", flush=True)
    print(f"[TRAIN]   epochs          : {args.epochs}  →  sample presentations: {sample_presentations} ({passes_verdict})", flush=True)
    print(f"[TRAIN]   forward batches : {forward_batches}", flush=True)
    print(f"[TRAIN]   learning rate   : {args.lr:.2e}", flush=True)
    print(f"[TRAIN]   lora_r          : {args.lora_r}", flush=True)
    print(f"[TRAIN]   lora_alpha      : {args.lora_alpha}  (effective scale: {alpha_r_ratio:.1f}×)", flush=True)
    print(f"[TRAIN]   grad_accum      : {args.gradient_accumulation_steps}  (effective batch: {effective_batch})", flush=True)
    print(f"[TRAIN]   batch_size      : {args.batch_size} samples per forward", flush=True)
    print(f"[TRAIN]   max_audio_secs  : {args.max_audio_seconds}", flush=True)
    print(f"[TRAIN]   language        : {args.language}", flush=True)
    if args.target_loss is not None:
        print(f"[TRAIN]   early stop at  : {args.target_loss}  (saves best checkpoint >= 4.1)", flush=True)
    else:
        print(f"[TRAIN]   loss target     : 4.1–4.2  (stop ~4.15; below 4.1 = garble risk)", flush=True)
    print(f"[TRAIN] ============================", flush=True)

    # Set the seed before LoRA initialization and epoch shuffling.
    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(args.seed)

    # ── Apply LoRA ──
    print("[TRAIN] Applying LoRA to talker...", flush=True)
    try:
        from peft import LoraConfig, get_peft_model
    except ImportError:
        print("[ERROR] peft package not installed. Run: pip install peft", flush=True)
        sys.exit(1)

    lora_config = LoraConfig(
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
        lora_dropout=0.05,
        bias="none",
    )

    talker = hf_model.talker  # Qwen3TTSTalkerForConditionalGeneration
    peft_talker = get_peft_model(talker, lora_config)
    hf_model.talker = peft_talker

    # Enable gradient checkpointing for memory efficiency
    peft_talker.enable_input_require_grads()
    peft_talker.base_model.model.model.gradient_checkpointing_enable()

    trainable_parameters = tuple(p for p in peft_talker.parameters() if p.requires_grad)
    trainable_params = sum(p.numel() for p in trainable_parameters)
    total_params = sum(p.numel() for p in peft_talker.parameters())
    print(f"[TRAIN] LoRA applied: {trainable_params:,} trainable / {total_params:,} total "
          f"({100 * trainable_params / total_params:.2f}%)", flush=True)

    # ── Optimizer ──
    optimizer = torch.optim.AdamW(
        trainable_parameters,
        lr=args.lr,
        weight_decay=0.01,
    )

    # ── Training ──
    ref_sample_text = get_training_reference_text(args.data_dir, ref_audio_path, samples)
    os.makedirs(args.output_dir, exist_ok=True)
    peft_talker.train()

    total_steps_per_epoch = len(samples)
    best_loss = float("inf")
    # Safe checkpoint: best loss that's still >= GARBLE_FLOOR
    # This protects against overshooting when early stopping is enabled.
    GARBLE_FLOOR = 4.1  # Empirical threshold: below this, audio quality degrades significantly
    safe_best_loss = float("inf")
    candidate_records = []
    training_start = time.time()
    total_oom_skips = 0

    def publish_checkpoint(model, epoch, keep_candidate=False, finalize=False):
        if model is not None and any(not bool(torch.isfinite(parameter).all())
                                     for parameter in trainable_parameters):
            raise RuntimeError("Training produced non-finite adapter weights; refusing publication")
        metadata = get_training_checkpoint_metadata(args, samples, ref_audio_path, ref_sample_text,
            epochs_completed, avg_loss, best_loss, time.time() - training_start, total_oom_skips)
        return save_training_checkpoint(model, args.output_dir, ref_audio_path, ref_sample_text,
            metadata, candidate_records, args.candidate_checkpoints, epoch, avg_loss,
            keep_candidate=keep_candidate, finalize=finalize)

    consecutive_oom_skips = 0
    # If a real, non-recoverable error (corrupted CUDA context, hardware
    # fault) gets misclassified as a retry-able OOM, every subsequent step
    # would fail the exact same way - silently skipping the rest of training
    # with no cap, ending in a "successful" run on a barely-trained adapter
    # with no error to explain why. A streak this long is no longer
    # consistent with "occasional large batch hit a VRAM limit" - bail out
    # loudly instead of finishing quietly on almost no real training.
    MAX_CONSECUTIVE_OOM_SKIPS = 10
    
    # Access underlying model structure (stable references)
    base_talker = peft_talker.base_model.model  # original talker with LoRA layers
    transformer = base_talker.model  # Qwen3TTSTalkerModel

    epochs_completed = 0
    for epoch in range(1, args.epochs + 1):
        epoch_loss = 0.0
        epoch_steps = 0
        epoch_oom_skips = 0
        gradients_since_step = 0
        optimizer.zero_grad()

        # Shuffle samples each epoch
        epoch_samples = samples.copy()
        random.shuffle(epoch_samples)

        for offset in range(0, len(epoch_samples), args.batch_size):
            batch = epoch_samples[offset:offset + args.batch_size]
            step_idx = offset + len(batch)
            sample_result = run_training_batch(
                batch, hf_model, base_talker, transformer, trainable_parameters,
                device, dtype, language=args.language,
                gradient_accumulation_steps=args.gradient_accumulation_steps)
            if "oom_error" in sample_result:
                epoch_oom_skips += len(batch)
                total_oom_skips += len(batch)
                consecutive_oom_skips += len(batch)
                if consecutive_oom_skips > MAX_CONSECUTIVE_OOM_SKIPS:
                    raise RuntimeError(
                        f"Aborting: {consecutive_oom_skips} consecutive OOM-like "
                        f"sample failures at epoch={epoch} step={step_idx} "
                        f"(requested={total_steps_per_epoch} succeeded={epoch_steps} skipped={epoch_oom_skips}; "
                        f"last error: {sample_result['oom_error']}). "
                        f"This many in a row means something is actually broken, not "
                        f"just an occasional large batch hitting a VRAM limit - "
                        f"continuing would likely train on little to no real data."
                    )
                print(f"[TRAIN] OOM at epoch={epoch} step={step_idx}, skipping {len(batch)} sample(s)", flush=True)
                if "cuda" in device:
                    torch.cuda.empty_cache()
                gc.collect()
                # The helper restored accumulated gradients and released the
                # failed graph. Keep the successful window for its next step.
                continue

            gradients_since_step += 1
            step_loss = sample_result["loss"]
            step_talker_loss = sample_result["talker_loss"]
            step_sub_loss = sample_result["sub_loss"]
            epoch_loss += step_loss * len(batch)
            epoch_steps += len(batch)
            consecutive_oom_skips = 0

            # Gradient accumulation step
            if gradients_since_step >= args.gradient_accumulation_steps or step_idx == total_steps_per_epoch:
                apply_training_accumulation_step(
                    trainable_parameters, optimizer, args.gradient_accumulation_steps,
                    gradients_since_step)
                gradients_since_step = 0

                if "cuda" in device:
                    torch.cuda.empty_cache()

            print(f"[TRAIN] epoch={epoch}/{args.epochs} step={step_idx}/{total_steps_per_epoch} "
                  f"loss={step_loss:.4f} talker_loss={step_talker_loss:.4f} "
                  f"sub_loss={step_sub_loss:.4f} lr={args.lr:.2e}", flush=True)

        # Epoch summary. A zero-success epoch produced no training signal and
        # must never publish an adapter as if it had reached zero loss.
        if epoch_steps == 0:
            raise RuntimeError(
                f"Epoch {epoch} completed with zero successful samples "
                f"({epoch_oom_skips} OOM-like skip(s); requested={total_steps_per_epoch} "
                f"succeeded={epoch_steps} skipped={epoch_oom_skips}); refusing to save an untrained adapter."
            )

        # Approved minimum coverage policy; this is not a quality measurement.
        if epoch_steps * 2 < total_steps_per_epoch:
            raise RuntimeError(
                f"Epoch {epoch}: fewer than half of requested samples succeeded "
                f"(requested={total_steps_per_epoch} succeeded={epoch_steps} skipped={epoch_oom_skips}); "
                f"refusing to publish this epoch."
            )

        # Flush gradients from a partial final accumulation window. An OOM on
        # the final dataset item bypasses the in-loop boundary check above.
        if gradients_since_step:
            apply_training_accumulation_step(
                trainable_parameters, optimizer, args.gradient_accumulation_steps,
                gradients_since_step)

        epochs_completed = epoch
        avg_loss = epoch_loss / epoch_steps

        # Safe checkpoint: save whenever loss improves and is still above garble floor.
        # This ensures we always have the best non-garbling checkpoint on disk,
        # even if later epochs overshoot.
        if args.target_loss is not None:
            if avg_loss >= GARBLE_FLOOR and avg_loss < safe_best_loss:
                safe_best_loss = avg_loss
                best_loss = avg_loss
                candidate_records, saved_candidate, _skipped = publish_checkpoint(
                    peft_talker, epoch, keep_candidate=True)
                if saved_candidate:
                    print(f"[TRAIN] Evaluation candidate saved ({saved_candidate['id']}, "
                          f"loss={avg_loss:.4f})", flush=True)
                print(f"[TRAIN] Safe checkpoint saved (loss={avg_loss:.4f})", flush=True)
        else:
            # Original behaviour: save unconditionally when loss improves
            if avg_loss < best_loss:
                best_loss = avg_loss
                candidate_records, saved_candidate, _skipped = publish_checkpoint(
                    peft_talker, epoch, keep_candidate=True)
                if saved_candidate:
                    print(f"[TRAIN] Evaluation candidate saved ({saved_candidate['id']}, "
                          f"loss={avg_loss:.4f})", flush=True)
                print(f"[TRAIN] Best adapter saved (loss={best_loss:.4f})", flush=True)

        zone = ""
        if avg_loss < GARBLE_FLOOR:
            zone = " [BELOW FLOOR — garble risk]"
        elif args.target_loss is not None and avg_loss <= args.target_loss:
            zone = " [TARGET REACHED]"
        oom_note = f" oom_skips={epoch_oom_skips}" if epoch_oom_skips else ""
        print(f"[EPOCH] {epoch}/{args.epochs} avg_loss={avg_loss:.4f}{zone}{oom_note} "
              f"requested={total_steps_per_epoch} succeeded={epoch_steps} skipped={epoch_oom_skips}", flush=True)

        # Early stopping: first epoch where loss crosses at or below the target
        if args.target_loss is not None and avg_loss <= args.target_loss:
            if avg_loss < GARBLE_FLOOR:
                print(f"[TRAIN] Early stop: loss {avg_loss:.4f} overshot floor ({GARBLE_FLOOR}). "
                      f"Best safe checkpoint: {safe_best_loss:.4f}", flush=True)
            else:
                print(f"[TRAIN] Early stop: loss {avg_loss:.4f} reached target {args.target_loss}", flush=True)
            break

    # ── Final save ──
    training_time = time.time() - training_start

    # Only save here if the last epoch run actually IS the best/safe
    # checkpoint (i.e. the per-epoch logic above already saved it, or would
    # have). Saving unconditionally would overwrite a better/safer earlier
    # checkpoint with the last epoch's weights whenever the last epoch
    # regressed - either by overshooting GARBLE_FLOOR (target_loss mode) or
    # by simply having higher loss than an earlier epoch (either mode). If no
    # checkpoint was ever saved (e.g. the very first epoch already overshot
    # the floor), save anyway so the output directory isn't left empty.
    if args.target_loss is not None:
        have_checkpoint = safe_best_loss < float("inf")
        last_epoch_is_best = avg_loss >= GARBLE_FLOOR and avg_loss <= safe_best_loss
        kept_loss = safe_best_loss
    else:
        have_checkpoint = best_loss < float("inf")
        last_epoch_is_best = avg_loss <= best_loss
        kept_loss = best_loss

    if have_checkpoint and not last_epoch_is_best:
        print(f"[TRAIN] Final epoch (loss={avg_loss:.4f}) is not the best checkpoint - "
              f"keeping the previously saved checkpoint (loss={kept_loss:.4f})", flush=True)
        final_model = None
    else:
        final_model = peft_talker
    candidate_records, _saved, duplicate_candidates = publish_checkpoint(
        final_model, epochs_completed, finalize=True)
    for skipped_candidate in duplicate_candidates:
        print(f"[TRAIN] Evaluation candidate {skipped_candidate['id']} skipped: "
              f"duplicate of {skipped_candidate['duplicate_of']}", flush=True)

    print(f"[DONE] Adapter saved to {args.output_dir} "
          f"(best_loss={best_loss:.4f}, time={training_time:.0f}s)", flush=True)


if __name__ == "__main__":
    args = parse_args()
    try:
        train(args)
    except Exception as e:
        print(f"[ERROR] {e}", flush=True)
        traceback.print_exc()
        sys.exit(1)
