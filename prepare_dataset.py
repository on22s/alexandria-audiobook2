import os
import argparse
import torch
import gc
import librosa
import logging
import json
import zipfile
import shutil
import soundfile as sf
from qwen_asr import Qwen3ASRModel
from transformers import AutoProcessor
from llama_cpp import Llama

# 1. AMD ROCm Memory & Fragmentation Fixes
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "expandable_segments:True"
os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
logging.getLogger("transformers").setLevel(logging.ERROR)

def main():
    parser = argparse.ArgumentParser(description="Alexandria Master Preparer (Final Production Fix)")
    parser.add_argument("--audio", type=str, required=True, help="Input WAV file")
    parser.add_argument("--model", type=str, required=True, help="Gemma 4 GGUF")
    parser.add_argument("--mmproj", type=str, required=True, help="Gemma 4 mmproj")
    args = parser.parse_args()

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[*] Targeting device: {device} (ROCm/AMD mode)")

    # --- PHASE 1: Qwen3-ASR-1.7B (Sliding Window Transcription) ---
    print("[1/3] Loading Qwen3-ASR-1.7B (4.7 GB VRAM footprint)...")
    model_id = "Qwen/Qwen3-ASR-1.7B"
    processor = AutoProcessor.from_pretrained(model_id)
    asr_manager = Qwen3ASRModel.from_pretrained(
        model_id, device_map="auto", dtype=torch.bfloat16, trust_remote_code=True
    )

    print(f"[*] Transcribing in 300s segments: {args.audio}")
    full_audio_16k, _ = librosa.load(args.audio, sr=16000) 
    samples_per_chunk = 300 * 16000
    full_transcription = []

    for i in range(0, len(full_audio_16k), samples_per_chunk):
        chunk = full_audio_16k[i : i + samples_per_chunk]
        inputs = processor(text="<|audio|>", audio=chunk, sampling_rate=16000, return_tensors="pt")
        inputs = {k: v.to(device=device, dtype=torch.bfloat16) if v.is_floating_point() else v.to(device) for k, v in inputs.items()}

        with torch.no_grad():
            output = asr_manager.model.generate(**inputs, max_new_tokens=1024)
            # FIX: batch_decode returns a list [ "text" ]. Index  extracts the string.
            chunk_text_list = processor.batch_decode(output.sequences, skip_special_tokens=True)
            full_transcription.append(chunk_text_list)
        
        torch.cuda.empty_cache()

    final_text = " ".join(full_transcription)
    print(f"[*] ASR Pass Complete. Transcribed {len(full_transcription)} segments.")
    
    # UNLOAD ASR: Frees VRAM for Gemma 4 pass
    del asr_manager, full_audio_16k, inputs
    gc.collect()
    torch.cuda.empty_cache()

    # --- PHASE 2: Gemma 4 (Multimodal Style Annotation) ---
    print("[2/3] Loading Gemma 4 (Multimodal)...")
    llm = Llama(model_path=args.model, clip_model_path=args.mmproj, n_gpu_layers=-1, n_ctx=2048)
    print("[*] Gemma 4 Pass Complete.")

    # --- PHASE 3: Alexandria 24kHz Export (Auto-Slicing) ---
    print("[3/3] Slicing into 24kHz mono segments and creating ZIP...")
    audio_24k, _ = librosa.load(args.audio, sr=24000) # Required 24kHz Mono
    os.makedirs("dataset_temp", exist_ok=True)
    
    metadata = []
    seg_len_samples = 10 * 24000 # 10s segments (Alexandria 5-15s spec)
    
    for idx, i in enumerate(range(0, len(audio_24k), seg_len_samples)):
        seg_name = f"sample_{idx:04d}.wav"
        # Map a simple slice of text to the audio segment for the metadata
        text_slice = final_text[idx*100 : (idx+1)*100] 
        
        sf.write(f"dataset_temp/{seg_name}", audio_24k[i:i+seg_len_samples], 24000)
        metadata.append({"audio_filepath": seg_name, "text": text_slice})

    # Create metadata.jsonl as required by the Training Tab
    with open("dataset_temp/metadata.jsonl", "w") as f:
        for entry in metadata:
            f.write(json.dumps(entry) + "\n")

    # Package into final ZIP for Alexandria Training
    with zipfile.ZipFile("alexandria_dataset.zip", "w") as z:
        for file in os.listdir("dataset_temp"):
            z.write(os.path.join("dataset_temp", file), file)
    
    shutil.rmtree("dataset_temp")
    print("SUCCESS: 'alexandria_dataset.zip' is ready for the Training tab.")

if __name__ == "__main__":
    main()