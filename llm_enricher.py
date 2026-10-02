#!/usr/bin/env python3

import json
import os
import sys
import logging
import tempfile
import traceback
from contextlib import nullcontext
from typing import Dict, Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
from utils import extract_json_object, file_lock, atomic_json_write
from alexandria_run_manifest import get_file_identity
from experiments.gpu_guard import acquire_gpu_lock, release_gpu_lock

from llama_cpp import Llama, llama_supports_gpu_offload
from gpu_stats import system_has_gpu

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def save_enriched_transcript(data, path):
    directory = os.path.dirname(os.path.abspath(path))
    fd, temporary = tempfile.mkstemp(prefix='.enriched-', suffix='.json', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as output:
            json.dump(data, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

class LLMEnricher:
    FIELD_LABELS = {
        "speaker_attribution": "Speaker Attribution (e.g., main character, narrator, secondary character)",
        "narration_style": "Narration Style (e.g., calm, energetic, sad, questioning)",
        "emotional_tone": "Emotional Tone (e.g., happy, anxious, neutral, excited)",
    }

    def __init__(self, model_path: str, fields=None, *, allow_cpu_fallback=False):
        self.model_path = model_path
        self.fields = list(self.FIELD_LABELS) if fields is None else list(fields)
        self.llm = None
        self._gpu_lease = None
        if not self.fields:
            return
        try:
            self._gpu_lease = acquire_gpu_lock()
            # Build-level check, independent of any specific model load: does
            # this llama-cpp-python install even have GPU support compiled
            # in? n_gpu_layers=-1 below silently falls back to CPU-only
            # decoding if not - much slower, with no clear error to explain
            # why, unless something checks for the mismatch up front.
            if not llama_supports_gpu_offload():
                has_gpu, vendor = system_has_gpu()
                if has_gpu:
                    backend = {"NVIDIA": "CUDA", "AMD/ROCm": "HIP",
                               "Apple Silicon (Metal)": "METAL"}[vendor]
                    if vendor == "AMD/ROCm":
                        rebuild = (
                            "Use ~/Desktop/llama_build/build_llama_rocm.sh for the ROCm source build. "
                            f"Set the script's PY assignment to {sys.executable!r} "
                            "(the running enrichment interpreter) and check AMDGPU_TARGETS "
                            "matches your GPU architecture before running it. "
                            "Exporting PY does not override the script's hardcoded assignment. "
                        )
                    else:
                        rebuild = (
                            "Rebuild in the same Python environment: python -m pip install "
                            "--force-reinstall --no-cache-dir llama-cpp-python "
                            f"-C cmake.args=-DGGML_{backend}=ON. "
                        )
                    message = (
                        f"{vendor} GPU detected, but this llama-cpp-python build has no GPU support. "
                        + rebuild + "Use --allow-cpu-fallback only for intentional CPU enrichment."
                    )
                    if not allow_cpu_fallback:
                        raise RuntimeError(message)
                    logger.warning(message + " Explicit CPU fallback enabled.")
            logger.info(f"Loading LLM model from: {self.model_path}")
            self.llm = Llama(
                model_path=self.model_path,
                n_ctx=4096,
                n_gpu_layers=-1,
                verbose=False
            )
            logger.info("LLM model loaded successfully.")
        except BaseException as e:
            self.close()
            logger.error(f"Failed to load LLM model from {self.model_path}: {e}")
            logger.debug(traceback.format_exc())
            raise

    def close(self):
        """Unload the model before releasing its GPU lease."""
        if self.llm is not None:
            self.llm.close()
            self.llm = None
        release_gpu_lock(self._gpu_lease)
        self._gpu_lease = None

    def enrich_transcript_chunk(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches a transcript chunk with metadata using the LLM.

        Returns a new dict (chunk is not mutated). On any failure, the
        returned dict carries `_enrichment_failed: True` so callers can
        distinguish a failed enrichment from a genuine successful one."""
        if not self.fields:
            return dict(chunk)
        if not self.llm:
            logger.error("LLM model not loaded. Cannot enrich transcript.")
            return {**chunk, "_enrichment_failed": True}

        try:
            prompt = self._create_prompt(chunk)
            logger.info(f"Enriching chunk: {chunk.get('start', 0.0):.2f}s - {chunk.get('end', 0.0):.2f}s")
            output = self.llm(
                prompt,
                max_tokens=150,
                stop=["</s>"],
                temperature=0.7
            )

            parsed = self._parse_llm_output(output['choices'][0]['text'])
            enriched_data = {key: parsed.get(key, "N/A") for key in self.fields}
            if parsed.get("_enrichment_failed"):
                enriched_data["_enrichment_failed"] = True

            return {**chunk, **enriched_data}

        except Exception as e:
            logger.error(f"Error during LLM enrichment for chunk {chunk.get('start', 'N/A')}: {e}")
            logger.debug(traceback.format_exc())
            return {**chunk, "_enrichment_failed": True}

    def _create_prompt(self, chunk: Dict[str, Any]) -> str:
        """Creates a prompt for the LLM to extract metadata."""
        text = chunk.get('text', '')
        speaker = chunk.get('speaker', 'UNKNOWN')
        start = chunk.get('start', 0.0)
        end = chunk.get('end', 0.0)

        requested = "\n".join(f"- {self.FIELD_LABELS[key]}" for key in self.fields)
        schema = ", ".join(f'"{key}"' for key in self.fields)
        example = json.dumps({key: "N/A" for key in self.fields})
        prompt = f"""Analyze the following transcript segment and extract metadata:

Transcript Segment:
"{text}"

Speaker: {speaker}
Start Time: {start:.2f}s
End Time: {end:.2f}s

Extract the following metadata:
{requested}

Provide the output as a JSON object with keys: {schema}. If any information cannot be determined, use 'N/A'.

Example Output Format:
{example}

Output JSON: """
        return prompt

    def _parse_llm_output(self, output_text: str) -> Dict[str, str]:
        """Parses the LLM's output to extract metadata."""
        metadata = extract_json_object(output_text)
        if metadata is not None:
            return metadata

        logger.warning(f"Could not parse LLM output as JSON: {output_text[:200]}")
        return {
            "speaker_attribution": "N/A",
            "narration_style": "N/A",
            "emotional_tone": "N/A",
            "_enrichment_failed": True
        }

def main():
    import argparse
    parser = argparse.ArgumentParser(description="LLM Transcript Enricher")
    parser.add_argument("--model-path", required=True, help="Path to the GGUF LLM model file.")
    parser.add_argument("--input-file", required=True, help="Path to the input JSON file containing transcript segments.")
    parser.add_argument("--output-file", required=True, help="Path to save the enriched transcript JSON file.")
    parser.add_argument("--speaker-attribution", action="store_true")
    parser.add_argument("--narration-style", action="store_true")
    parser.add_argument("--emotional-tone", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Reuse fingerprint-matched accepted enrichment rows after interruption")

    parser.add_argument("--allow-cpu-fallback", action="store_true",
                        help="Allow CPU enrichment when a GPU is detected but this build cannot offload")

    args = parser.parse_args()

    try:
        with open(args.input_file, 'r', encoding='utf-8') as f:
            transcript_data = json.load(f)
    except OSError as e:
        logger.error(f"Could not read input file {args.input_file}: {e}")
        exit(1)
    except (json.JSONDecodeError, UnicodeError):
        logger.error(f"Failed to decode UTF-8 JSON from input file: {args.input_file}")
        exit(1)

    if not isinstance(transcript_data, list) or any(
            not isinstance(chunk, dict) for chunk in transcript_data):
        logger.error("Input must contain a JSON list of transcript objects")
        exit(1)

    selected = [key for key, enabled in (
        ("speaker_attribution", args.speaker_attribution),
        ("narration_style", args.narration_style),
        ("emotional_tone", args.emotional_tone),
    ) if enabled]
    checkpoint_path = args.output_file + ".enrichment_checkpoint.json"
    checkpoint_lock = file_lock(checkpoint_path) if args.resume and selected else nullcontext()
    with checkpoint_lock:
        enricher = None
        try:
            cached = [None] * len(transcript_data)
            identity = None
            if args.resume and selected:
                identity = {
                    "version": 1, "input": get_file_identity(args.input_file),
                    "model": get_file_identity(args.model_path),
                    "implementation": get_file_identity(__file__), "fields": selected,
                    "llama_cpp_version": getattr(sys.modules.get("llama_cpp"), "__version__", None),
                }
                if os.path.exists(checkpoint_path):
                    with open(checkpoint_path, encoding='utf-8') as checkpoint_file:
                        checkpoint = json.load(checkpoint_file)
                    if not isinstance(checkpoint, dict):
                        raise ValueError("Invalid enrichment checkpoint document")
                    if checkpoint.get("identity") != identity:
                        logger.warning("Enrichment checkpoint identity changed; starting fresh.")
                    else:
                        cached = checkpoint.get("rows")
                        if not isinstance(cached, list) or len(cached) != len(transcript_data):
                            raise ValueError("Invalid enrichment checkpoint row count")
                        for original, row in zip(transcript_data, cached):
                            if row is not None and (not isinstance(row, dict)
                                    or row.get("_enrichment_failed")
                                    or set(row) != set(original) | set(selected)
                                    or any(row.get(key) != value for key, value in original.items()
                                           if key not in selected)):
                                raise ValueError("Invalid enrichment checkpoint source row")
                        logger.info("Resuming %s accepted enrichment rows", sum(row is not None for row in cached))
            needs_model = identity is None or any(row is None for row in cached)
            enricher = LLMEnricher(args.model_path, selected, allow_cpu_fallback=args.allow_cpu_fallback) if needs_model else None
            if identity is not None and needs_model:
                if get_file_identity(args.model_path) != identity["model"]:
                    raise ValueError("Enrichment model changed while loading")
        except Exception as e:
            if enricher is not None:
                enricher.close()
            logger.error(f"Exiting: Could not initialize LLMEnricher: {e}")
            exit(1)

        try:
            enriched_data = []
            fail_count = 0
            for i, chunk in enumerate(transcript_data):
                was_cached = cached[i] is not None
                try:
                    enriched_chunk = cached[i] if cached[i] is not None else enricher.enrich_transcript_chunk(chunk)
                    if enriched_chunk.get("_enrichment_failed"):
                        fail_count += 1
                    enriched_data.append(enriched_chunk)
                except Exception as e:
                    logger.error(f"Error processing chunk {i}: {e}")
                    logger.debug(traceback.format_exc())
                    fail_count += 1
                    enriched_data.append({**chunk, "_enrichment_failed": True})
                if identity is not None and not was_cached and not enriched_data[-1].get("_enrichment_failed"):
                    cached[i] = enriched_data[-1]
                    atomic_json_write({"identity": identity, "rows": cached}, checkpoint_path)

            if transcript_data and fail_count == len(transcript_data):
                logger.error(f"All {fail_count} chunk(s) failed enrichment - exiting with an error so the caller can detect total failure.")
                exit(1)
            elif fail_count:
                logger.warning(f"{fail_count}/{len(transcript_data)} chunk(s) failed enrichment; continuing with the rest.")

            try:
                save_enriched_transcript(enriched_data, args.output_file)
                logger.info(f"Enriched transcript saved to: {args.output_file}")
                if identity is not None and os.path.exists(checkpoint_path):
                    os.unlink(checkpoint_path)
            except IOError as e:
                logger.error(f"Failed to write output file {args.output_file}: {e}")
                exit(1)
        finally:
            if enricher is not None:
                enricher.close()

if __name__ == "__main__":
    main()
