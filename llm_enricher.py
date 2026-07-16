#!/usr/bin/env python3

import json
import re
import logging
import traceback
from typing import Dict, Any

from llama_cpp import Llama, llama_supports_gpu_offload
from gpu_stats import system_has_gpu

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] [%(name)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

class LLMEnricher:
    FIELD_LABELS = {
        "speaker_attribution": "Speaker Attribution (e.g., main character, narrator, secondary character)",
        "narration_style": "Narration Style (e.g., calm, energetic, sad, questioning)",
        "emotional_tone": "Emotional Tone (e.g., happy, anxious, neutral, excited)",
    }

    def __init__(self, model_path: str, fields=None):
        self.model_path = model_path
        self.fields = fields or list(self.FIELD_LABELS)
        self.llm = None
        try:
            # Build-level check, independent of any specific model load: does
            # this llama-cpp-python install even have GPU support compiled
            # in? n_gpu_layers=-1 below silently falls back to CPU-only
            # decoding if not - much slower, with no clear error to explain
            # why, unless something checks for the mismatch up front.
            if not llama_supports_gpu_offload():
                has_gpu, vendor = system_has_gpu()
                if has_gpu:
                    logger.warning(
                        f"{vendor} GPU detected on this system, but this "
                        f"llama-cpp-python build has no GPU support compiled in "
                        f"(llama_supports_gpu_offload() is False) - LLM inference "
                        f"will run on CPU and be dramatically slower. Rebuild "
                        f"llama-cpp-python with the correct GPU backend flags."
                    )
            logger.info(f"Loading LLM model from: {self.model_path}")
            self.llm = Llama(
                model_path=self.model_path,
                n_ctx=4096,
                n_gpu_layers=-1,
                verbose=False
            )
            logger.info("LLM model loaded successfully.")
        except Exception as e:
            logger.error(f"Failed to load LLM model from {self.model_path}: {e}")
            logger.debug(traceback.format_exc())
            raise

    def enrich_transcript_chunk(self, chunk: Dict[str, Any]) -> Dict[str, Any]:
        """Enriches a transcript chunk with metadata using the LLM.

        Returns a new dict (chunk is not mutated). On any failure, the
        returned dict carries `_enrichment_failed: True` so callers can
        distinguish a failed enrichment from a genuine successful one."""
        if not self.llm:
            logger.error("LLM model not loaded. Cannot enrich transcript.")
            return {**chunk, "_enrichment_failed": True}

        prompt = self._create_prompt(chunk)

        try:
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
        # Try to find JSON in markdown code blocks first
        json_match = re.search(r'```json\s*\n({.*?})\n\s*```', output_text, re.DOTALL)
        if not json_match:
            # Fallback to finding standalone JSON
            json_match = re.search(r'(\{.*?\})', output_text, re.DOTALL)
        
        if json_match:
            try:
                metadata = json.loads(json_match.group(1))
                return metadata
            except json.JSONDecodeError:
                logger.warning(f"Failed to parse LLM output as JSON: {json_match.group(1)[:100]}...")

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

    args = parser.parse_args()

    try:
        selected = [key for key, enabled in (
            ("speaker_attribution", args.speaker_attribution),
            ("narration_style", args.narration_style),
            ("emotional_tone", args.emotional_tone),
        ) if enabled]
        enricher = LLMEnricher(args.model_path, selected or None)
    except Exception as e:
        logger.error(f"Exiting: Could not initialize LLMEnricher: {e}")
        exit(1)

    try:
        with open(args.input_file, 'r') as f:
            transcript_data = json.load(f)
    except FileNotFoundError:
        logger.error(f"Input file not found: {args.input_file}")
        exit(1)
    except json.JSONDecodeError:
        logger.error(f"Failed to decode JSON from input file: {args.input_file}")
        exit(1)

    enriched_data = []
    fail_count = 0
    for i, chunk in enumerate(transcript_data):
        try:
            enriched_chunk = enricher.enrich_transcript_chunk(chunk)
            if enriched_chunk.get("_enrichment_failed"):
                fail_count += 1
            enriched_data.append(enriched_chunk)
        except Exception as e:
            logger.error(f"Error processing chunk {i}: {e}")
            logger.debug(traceback.format_exc())
            fail_count += 1
            enriched_data.append({**chunk, "_enrichment_failed": True})

    if transcript_data and fail_count == len(transcript_data):
        logger.error(f"All {fail_count} chunk(s) failed enrichment - exiting with an error so the caller can detect total failure.")
        exit(1)
    elif fail_count:
        logger.warning(f"{fail_count}/{len(transcript_data)} chunk(s) failed enrichment; continuing with the rest.")

    try:
        with open(args.output_file, 'w') as f:
            json.dump(enriched_data, f, indent=2)
        logger.info(f"Enriched transcript saved to: {args.output_file}")
    except IOError as e:
        logger.error(f"Failed to write output file {args.output_file}: {e}")
        exit(1)

if __name__ == "__main__":
    main()
