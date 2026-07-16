import argparse
import hashlib
import os
import sys
import json
import re
import time
from dataclasses import dataclass
from openai import OpenAI
from config_settings import load_app_config
from chunk_quality import validate_chunk_quality
from default_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from lmstudio_settings import (ensure_ideal_settings, get_effective_max_tokens,
                               get_next_retry_max_tokens)
from script_repair import build_deterministic_repair
from source_normalization import normalize_known_source_corruptions
from speaker_identity import stabilize_speaker_identities
from script_preflight import audit_script
from utils import (atomic_json_write, extract_balanced, get_runtime_data_dir,
                   get_app_config_path, is_generic_speaker, safe_load_json)


def get_generation_checkpoint_path(output_path):
    return output_path + ".generation_checkpoint.json"


def get_generation_quality_path(output_path):
    return output_path + ".generation_quality.json"


def build_generation_quality_manifest(status, fingerprint, accepted_chunks,
                                      source_normalizations, **details):
    return {
        "status": status,
        "fingerprint": fingerprint,
        "source_normalizations": source_normalizations,
        "accepted_chunk_count": len(accepted_chunks),
        "chunks": [{
            "chunk_number": item["chunk_number"],
            "source_sha256": item["source_sha256"],
            "entry_count": len(item["entries"]),
            "quality": item["quality"],
        } for item in accepted_chunks],
        **details,
    }


def save_generation_quality_manifest(output_path, manifest):
    atomic_json_write(manifest, get_generation_quality_path(output_path))


def passes_final_generation_gate(whole_quality, preflight):
    return bool(whole_quality.get("passed") and not preflight.get("counts", {}).get("blocking"))


def get_generation_fingerprint(source_text, chunks, model_name, base_url, params, chunk_size):
    settings = {
        "model_name": model_name, "base_url": base_url, "chunk_size": chunk_size,
        "system_prompt": params.system_prompt, "user_prompt_template": params.user_prompt_template,
        "max_tokens": params.max_tokens, "temperature": params.temperature,
        "top_p": params.top_p, "top_k": params.top_k, "min_p": params.min_p,
        "presence_penalty": params.presence_penalty, "banned_tokens": params.banned_tokens,
        "context_length": params.context_length, "hard_max_tokens": params.hard_max_tokens,
    }
    encoded = json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {
        "source_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        "settings_sha256": hashlib.sha256(encoded).hexdigest(),
        "chunk_sha256": [hashlib.sha256(chunk.encode("utf-8")).hexdigest() for chunk in chunks],
    }


def load_generation_checkpoint(output_path, fingerprint):
    checkpoint = safe_load_json(get_generation_checkpoint_path(output_path), None)
    if not isinstance(checkpoint, dict) or checkpoint.get("fingerprint") != fingerprint:
        return []
    accepted = checkpoint.get("accepted_chunks")
    if not isinstance(accepted, list) or len(accepted) > len(fingerprint["chunk_sha256"]):
        return []
    for index, item in enumerate(accepted):
        if (not isinstance(item, dict) or not isinstance(item.get("entries"), list)
                or not item.get("quality", {}).get("passed")
                or item.get("source_sha256") != fingerprint["chunk_sha256"][index]):
            return []
    return accepted


def save_generation_checkpoint(output_path, fingerprint, accepted_chunks):
    atomic_json_write({"fingerprint": fingerprint, "accepted_chunks": accepted_chunks},
                      get_generation_checkpoint_path(output_path))


def clear_generation_checkpoint(output_path):
    path = get_generation_checkpoint_path(output_path)
    if os.path.exists(path):
        os.remove(path)

def clean_json_string(text):
    """Clean and combine adjacent complete JSON arrays from an LLM response."""
    # Remove thinking tags (various formats used by different models)
    # GLM, DeepSeek, Qwen, etc. use different thinking tag formats
    text = re.sub(r'<think>[\s\S]*?</think>', '', text)
    text = re.sub(r'<thinking>[\s\S]*?</thinking>', '', text)
    text = re.sub(r'<reflection>[\s\S]*?</reflection>', '', text)
    text = re.sub(r'<reasoning>[\s\S]*?</reasoning>', '', text)
    # Handle unclosed thinking tags (model started thinking but didn't close)
    text = re.sub(r'<think>[\s\S]*$', '', text)
    text = re.sub(r'<thinking>[\s\S]*$', '', text)

    # Remove markdown code blocks
    if "```" in text:
        # Find content between ```json and ``` or just ``` and ```
        match = re.search(r'```(?:json)?\s*([\s\S]*?)```', text)
        if match:
            text = match.group(1).strip()

    # Find the JSON array - match from first [ to its closing ]
    start = text.find('[')
    if start == -1:
        return None

    span = extract_balanced(text, '[', ']')
    if span is None:
        # No closing bracket found, try to salvage
        last_complete = text.rfind('},')
        if last_complete > start:
            return text[start:last_complete+1] + ']'
        return None

    spans = [span]
    cursor = start + len(span)
    while True:
        next_start = text.find('[', cursor)
        if next_start == -1:
            break
        if text[cursor:next_start].strip():
            # Text between arrays is ambiguous; make parsing fail rather than
            # silently discarding either the text or a later array.
            return None
        next_span = extract_balanced(text, '[', ']', next_start)
        if next_span is None:
            return text[start:]
        spans.append(next_span)
        cursor = next_start + len(next_span)

    json_text = spans[0] if len(spans) == 1 else "[" + ",".join(
        item.strip()[1:-1] for item in spans) + "]"

    # Clean control characters inside strings (common LLM issue)
    # Replace literal newlines/tabs inside JSON strings with escaped versions
    def fix_control_chars(match):
        s = match.group(0)
        # Replace unescaped control characters
        s = s.replace('\n', '\\n')
        s = s.replace('\r', '\\r')
        s = s.replace('\t', '\\t')
        return s

    # Fix control characters inside string values
    json_text = re.sub(r'"[^"\\]*(?:\\.[^"\\]*)*"', fix_control_chars, json_text)

    return json_text


def repair_json_array(json_text):
    """Attempt to repair common JSON array issues from LLM output."""
    if not json_text:
        return None

    def _filter_entries(lst):
        """Keep only dict entries; LLMs sometimes emit bare strings in the array."""
        filtered = [e for e in lst if isinstance(e, dict)]
        if len(filtered) < len(lst):
            print(f"  Warning: Dropped {len(lst) - len(filtered)} non-object entries from LLM JSON array")
        return filtered if filtered else None

    # Try parsing as-is first
    try:
        result = json.loads(json_text)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 1: Add missing commas between objects (}\s*{" -> },\n{")
    fixed = re.sub(r'\}\s*\{', '},\n{', json_text)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 2: Remove trailing commas before ]
    fixed = re.sub(r',\s*\]', ']', fixed)
    try:
        result = json.loads(fixed)
        if isinstance(result, list):
            return _filter_entries(result)
    except json.JSONDecodeError:
        pass

    # Fix 3: Try to extract individual entries and rebuild
    entries = []
    # Match individual JSON objects
    pattern = r'\{\s*"speaker"\s*:\s*"[^"]*"\s*,\s*"text"\s*:\s*"(?:[^"\\]|\\.)*"\s*,\s*"instruct"\s*:\s*"(?:[^"\\]|\\.)*"\s*\}'
    matches = re.findall(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = json.loads(match)
            entries.append(entry)
        except json.JSONDecodeError:
            continue

    if entries:
        return entries

    # Fix 4: Last resort - find last complete entry and truncate
    last_complete = json_text.rfind('},')
    if last_complete > 0:
        try:
            truncated = json_text[:last_complete+1] + ']'
            # Ensure it starts with [
            if not truncated.strip().startswith('['):
                truncated = '[' + truncated
            result = json.loads(truncated)
            if isinstance(result, list):
                return _filter_entries(result)
        except json.JSONDecodeError:
            pass

    return None

def salvage_json_entries(json_text):
    """Last resort: extract individual valid entries with regex."""
    entries = []
    # Match individual JSON objects with speaker, text, instruct fields
    pattern = r'\{\s*"speaker"\s*:\s*"([^"]*)"\s*,\s*"text"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"instruct"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
    matches = re.finditer(pattern, json_text, re.DOTALL)

    for match in matches:
        try:
            entry = {
                "speaker": match.group(1),
                "text": match.group(2).replace('\\"', '"').replace('\\n', '\n'),
                "instruct": match.group(3).replace('\\"', '"').replace('\\n', '\n')
            }
            entries.append(entry)
        except Exception as e:
            print(f"  [salvage] discarding malformed candidate: {e}")
            continue

    return entries if entries else None


def fix_mojibake(text):
    """Fix common mojibake characters resulting from CP1252-as-UTF8."""
    replacements = [
        ('â€™', '\u2019'),  # Right single quote
        ('â€˜', '\u2018'),  # Left single quote
        ('â€œ', '\u201c'),  # Left double quote
        ('â€\x9d', '\u201d'),  # Right double quote
        ('â€?', '\u201d'),  # Sometimes ? if undefined
        ('â€“', '\u2013'),  # En dash (UTF-8 E2 80 93 read as CP1252)
        ('â€”', '\u2014'),  # Em dash (UTF-8 E2 80 94 read as CP1252)
        ('â€¦', '\u2026'),  # Ellipsis
    ]

    for bad, good in replacements:
        text = text.replace(bad, good)

    return text

def split_into_chunks(text, max_size=3000):
    """Split text into chunks at paragraph/sentence boundaries."""
    paragraphs = re.split(r'\n\s*\n', text)

    chunks = []
    current_chunk = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current_chunk) + len(para) + 2 > max_size:
            if current_chunk:
                chunks.append(current_chunk.strip())
                current_chunk = ""

            if len(para) > max_size:
                sentences = re.split(r'(?<=[.!?])\s+', para)
                for sentence in sentences:
                    if len(current_chunk) + len(sentence) + 1 > max_size:
                        if current_chunk:
                            chunks.append(current_chunk.strip())
                        current_chunk = sentence
                    else:
                        current_chunk += " " + sentence if current_chunk else sentence
            else:
                current_chunk = para
        else:
            current_chunk += "\n\n" + para if current_chunk else para

    if current_chunk:
        chunks.append(current_chunk.strip())

    return chunks

@dataclass
class LLMGenParams:
    """LLM prompt + sampling settings shared by script generation and review.

    Groups the knobs previously threaded as ~9 separate arguments through
    process_chunk()/review_batch() and their callers. `system_prompt` and
    `user_prompt_template` are optional overrides; each caller falls back to its
    own module default when they are None.

    NOTE: the sampling defaults below are a script-generation baseline only. The
    review pass uses different tuned values (higher max_tokens, lower temperature,
    top_k=20) and always constructs this explicitly, so don't rely on these
    defaults for review — pass review's values in.
    """
    system_prompt: str = None
    user_prompt_template: str = None
    max_tokens: int = 4096
    temperature: float = 0.6
    top_p: float = 0.8
    top_k: int = None
    min_p: float = None
    presence_penalty: float = 0.0
    banned_tokens: list = None
    context_length: int = None
    hard_max_tokens: int = 16384


def _rotate_log_if_large(log_path, max_bytes=10 * 1024 * 1024):
    """Rotate <log_path> to <log_path>.bak once it exceeds max_bytes (best-effort)."""
    if os.path.exists(log_path) and os.path.getsize(log_path) > max_bytes:
        backup_path = log_path + ".bak"
        try:
            if os.path.exists(backup_path):
                os.remove(backup_path)
            os.rename(log_path, backup_path)
        except OSError:
            pass  # If rotation fails, just append to existing log


def call_llm_for_entries(client, model_name, sys_prompt, user_prompt, params,
                         log_name, label, max_retries=2, validate_entries=None,
                         transform_entries=None):
    """Call the LLM and parse a JSON array of entries, with retries.

    Shared by process_chunk() (script generation) and review_batch() (review):
    the two only differed in their log file/label and failure sentinel. Returns a
    list of entries, or [] if every attempt failed to produce parseable JSON.
    `log_name` is the raw-response log basename; `label` tags each block
    (e.g. "CHUNK 3/40" or "BATCH 2/10").
    """
    retry_feedback = None
    requested_max = params.max_tokens
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            attempt_prompt = user_prompt
            if retry_feedback:
                attempt_prompt += ("\n\nYour previous response was rejected by deterministic "
                                   "quality checks. Return the complete source text exactly once. "
                                   f"Failures: {retry_feedback}")
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": attempt_prompt}
            ]
            effective_max = get_effective_max_tokens(
                requested_max, params.context_length, messages,
                params.hard_max_tokens, scale_to_context=False)
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                temperature=params.temperature,
                top_p=params.top_p,
                presence_penalty=params.presence_penalty,
                max_tokens=effective_max,
                extra_body={
                    k: v for k, v in {
                        "top_k": params.top_k,
                        "min_p": params.min_p,
                        "banned_tokens": params.banned_tokens if params.banned_tokens else None,
                    }.items() if v is not None
                }
            )

            choice = response.choices[0]
            text = choice.message.content.strip()
            finish_reason = choice.finish_reason
            usage = getattr(response, 'usage', None)

            # Log raw response for debugging (rotating to cap unbounded growth)
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, log_name)
            _rotate_log_if_large(log_path)
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n{'='*80}\n")
                lf.write(f"{label} | attempt {attempt + 1} | finish_reason={finish_reason}\n")
                if usage:
                    lf.write(f"tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}\n")
                lf.write(f"{'─'*80}\n")
                lf.write(text)
                lf.write(f"\n{'='*80}\n")

            print(f"  finish_reason={finish_reason}", end="")
            if usage:
                print(f" | tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}", end="")
            print(f" | took {time.time() - t0:.1f}s")

            if finish_reason == "length":
                print(f"  WARNING: Response was truncated (hit effective max_tokens={effective_max}). Consider optimizing LM Studio context.")
                if attempt < max_retries:
                    next_max = get_next_retry_max_tokens(
                        requested_max, "token_truncated", params.hard_max_tokens)
                    print(f"  Token budget: requested={requested_max}, effective={effective_max}, next={next_max}")
                    requested_max = next_max

        except Exception as e:
            print(f"Error calling LLM API (attempt {attempt + 1}) after {time.time() - t0:.1f}s: {e}")
            if attempt < max_retries:
                continue
            return []

        # Clean and extract JSON from response
        json_text = clean_json_string(text)

        if not json_text:
            print(f"Warning: Could not find JSON array in {label} response (attempt {attempt + 1})")
            if attempt < max_retries:
                print("Retrying...")
                continue
            print(f"Response preview: {text[:300]}...")
            return []

        # Try to parse, with repair attempts
        entries = repair_json_array(json_text)

        if entries and len(entries) > 0:
            if transform_entries:
                transformed = transform_entries(entries)
                if transformed.get("unresolved"):
                    retry_feedback = "unresolved_safe_repair"
                    print(f"Warning: {label} has unresolved deterministic repairs "
                          f"(attempt {attempt + 1}): {transformed['unresolved']}")
                    if attempt < max_retries:
                        print("Retrying...")
                        continue
                    return []
                entries = transformed["entries"]
                if transformed.get("changes"):
                    print(f"  Applied {len(transformed['changes'])} deterministic chunk repair(s)")
                if transformed.get("review"):
                    print(f"  Speaker identity review: {transformed['review']}")
            if validate_entries:
                quality = validate_entries(entries)
                if not quality["passed"]:
                    retry_feedback = ", ".join(
                        finding["code"] for finding in quality["findings"])
                    metrics = quality.get("metrics", {})
                    codes = {finding["code"] for finding in quality["findings"]}
                    if (attempt < max_retries and finish_reason != "length" and
                            ({"low_source_token_recall", "low_ordered_trigram_recall",
                              "output_source_ratio"} & codes
                             and metrics.get("output_source_ratio", 1.0) < 0.9)):
                        next_max = get_next_retry_max_tokens(
                            requested_max, "incomplete_output", params.hard_max_tokens)
                        if next_max != requested_max:
                            print(f"  Increasing token budget after incomplete output: "
                                  f"{requested_max} -> {next_max}")
                            requested_max = next_max
                    print(f"Warning: {label} failed quality validation "
                          f"(attempt {attempt + 1}): {retry_feedback}; metrics={metrics}")
                    if attempt < max_retries:
                        print("Retrying...")
                        continue
                    return []
            if finish_reason == "length":
                retry_feedback = "token_truncated"
                if attempt < max_retries:
                    print("Retrying truncated response with a larger token budget...")
                    continue
                return []
            if attempt > 0:
                print(f"  Succeeded on retry {attempt + 1}")
            return entries

        print(f"Warning: Could not parse {label} response as JSON (attempt {attempt + 1})")
        print(f"JSON preview: {json_text[:300]}...")

        if attempt < max_retries:
            print("Retrying...")
            continue

        # Last resort after every full-response retry is exhausted.
        salvaged_entries = salvage_json_entries(json_text)
        if salvaged_entries:
            if transform_entries:
                transformed = transform_entries(salvaged_entries)
                if transformed.get("unresolved"):
                    print("Regex-salvaged response has unresolved deterministic repairs")
                    return []
                salvaged_entries = transformed["entries"]
            if validate_entries:
                quality = validate_entries(salvaged_entries)
                if not quality["passed"]:
                    codes = ", ".join(finding["code"] for finding in quality["findings"])
                    print(f"Regex-salvaged response failed quality validation: {codes}")
                    return []
            print(f"Regex-salvaged {len(salvaged_entries)} entries from malformed response")
            return salvaged_entries

    return []


def _build_chunk_context(chunk_num, total_chunks, previous_entries):
    """Build the positional + character-roster context block prepended to a chunk."""
    context_parts = []

    if chunk_num == 1:
        context_parts.append("(Beginning of text)")
    elif chunk_num == total_chunks:
        context_parts.append("(End of text)")
    else:
        context_parts.append(f"(Part {chunk_num} of {total_chunks})")

    if previous_entries and len(previous_entries) > 0:
        # Build character roster for name consistency across chunks
        characters_seen = sorted(set(
            entry.get("speaker", "") for entry in previous_entries
            if entry.get("speaker", "") and entry.get("speaker", "") != "NARRATOR"
        ))
        if characters_seen:
            context_parts.append(f"Characters in this book: {', '.join(characters_seen)}")

        # Include last few entries so the model can maintain style and tone continuity
        tail = previous_entries[-3:]
        context_parts.append("\nPrevious section ended with:")
        for entry in tail:
            context_parts.append(json.dumps(entry, ensure_ascii=False))

    return "\n".join(context_parts)


def process_chunk(client, model_name, chunk, chunk_num, total_chunks, params,
                  previous_entries=None, max_retries=2):
    """Process a text chunk and return JSON script entries."""
    sys_prompt = params.system_prompt or DEFAULT_SYSTEM_PROMPT
    usr_template = params.user_prompt_template or DEFAULT_USER_PROMPT

    context = _build_chunk_context(chunk_num, total_chunks, previous_entries)
    user_prompt = usr_template.format(context=context, chunk=chunk)
    established_speakers = [entry.get("speaker") for entry in (previous_entries or [])
                            if isinstance(entry, dict) and entry.get("speaker")]

    def prepare_entries(entries):
        structural = build_deterministic_repair(entries, chunk)
        if structural["unresolved"]:
            return structural
        identities = stabilize_speaker_identities(structural["entries"], established_speakers)
        return {"entries": identities["entries"],
                "changes": structural["changes"] + identities["changes"],
                "unresolved": [], "review": identities["review"]}

    return call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log",
        label=f"CHUNK {chunk_num}/{total_chunks}",
        max_retries=max_retries,
        validate_entries=lambda entries: validate_chunk_quality(chunk, entries),
        transform_entries=prepare_entries,
    )

def main():
    parser = argparse.ArgumentParser(description="Generate annotated script from a book file.")
    parser.add_argument("input_file", help="Path to the input text/epub file")
    parser.add_argument("--output", default=None, help="Output JSON path (default: ../annotated_script.json)")
    args = parser.parse_args()

    input_file_path = args.input_file
    print(f"Processing book from: {input_file_path}")

    if not os.path.exists(input_file_path):
        print(f"Error: Input file not found: {input_file_path}")
        sys.exit(1)

    with open(input_file_path, 'r', encoding='utf-8') as f:
        book_content = f.read()

    # Fix encoding artifacts
    book_content = fix_mojibake(book_content)
    book_content, source_normalizations = normalize_known_source_corruptions(book_content)
    if source_normalizations:
        print(f"Normalized {len(source_normalizations)} known source corruption(s) in memory; "
              "the upload was not modified.")

    print(f"Read {len(book_content)} characters")

    # Load LLM config
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    config_path = get_app_config_path(data_dir, root, app_dir)
    if not os.path.exists(config_path):
        print("Warning: config.json not found. Using defaults.")
    config = load_app_config(config_path)

    llm_config = config.get("llm", {})
    base_url = llm_config.get("base_url", "http://localhost:11434/v1")
    api_key = llm_config.get("api_key", "local")
    model_name = llm_config.get("model_name", "richardyoung/qwen3-14b-abliterated:Q8_0")
    llm_mode = config.get("llm_mode", "local")

    # Load custom prompts or use defaults
    prompts_config = config.get("prompts") or {}
    system_prompt = prompts_config.get("system_prompt") or DEFAULT_SYSTEM_PROMPT
    user_prompt_template = prompts_config.get("user_prompt") or DEFAULT_USER_PROMPT

    # Load generation settings
    generation_config = config.get("generation") or {}
    chunk_size = generation_config.get("chunk_size", 3000)
    max_tokens = generation_config.get("max_tokens", 4096)
    temperature = generation_config.get("temperature", 0.6)
    top_p = generation_config.get("top_p", 0.8)
    # Default to None (not 0) so an unconfigured sampler is omitted from the
    # request, while an explicit 0 is preserved and sent through.
    top_k = generation_config.get("top_k")
    min_p = generation_config.get("min_p")
    presence_penalty = generation_config.get("presence_penalty", 0.0)
    banned_tokens = generation_config.get("banned_tokens", [])

    print(f"Connecting to: {base_url}")
    print(f"Using model: {model_name}")
    print(f"Chunk size: {chunk_size} chars, Max tokens: {max_tokens}")
    if banned_tokens:
        print(f"Banned tokens: {banned_tokens}")

    # Self-heal a stale/misconfigured local or remote LM Studio before making
    # any calls, mirroring review_script.py/find_nicknames.py. This file has
    # no VRAM watchdog or concurrency wave processing of its own (chunks are
    # processed strictly sequentially), so only the self-heal call applies
    # here - is_remote/lm_status aren't needed for anything else in this file.
    _, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"))
    print(heal_msg)

    # Create OpenAI client with custom base URL
    client = OpenAI(
        base_url=base_url,
        api_key=api_key
    )

    # Split into chunks at natural boundaries
    chunks = split_into_chunks(book_content, max_size=chunk_size)
    total_chunks = len(chunks)

    print(f"Split into {total_chunks} chunks at paragraph/sentence boundaries")

    output_path = args.output or os.path.join(data_dir, "annotated_script.json")

    all_entries = []
    chunk_times = []
    start_time = time.monotonic()

    # Sampling/prompt settings are constant across chunks - build once.
    gen_params = LLMGenParams(
        system_prompt=system_prompt,
        user_prompt_template=user_prompt_template,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
        presence_penalty=presence_penalty,
        banned_tokens=banned_tokens,
        context_length=lm_status.get("context_length"),
    )

    fingerprint = get_generation_fingerprint(
        book_content, chunks, model_name, base_url, gen_params, chunk_size)
    accepted_chunks = load_generation_checkpoint(output_path, fingerprint)
    if accepted_chunks:
        print(f"Resuming from generation checkpoint: {len(accepted_chunks)}/{total_chunks} validated chunks.")
        all_entries = [entry for item in accepted_chunks for entry in item["entries"]]

    for i, chunk in enumerate(chunks, 1):
        if i <= len(accepted_chunks):
            continue
        print(f"Processing chunk {i}/{total_chunks} ({len(chunk)} chars)...")

        chunk_start = time.monotonic()
        previous = all_entries if len(all_entries) > 0 else None
        entries = process_chunk(
            client, model_name, chunk, i, total_chunks, gen_params,
            previous_entries=previous,
        )
        chunk_elapsed = time.monotonic() - chunk_start
        chunk_times.append(chunk_elapsed)

        if not entries:
            save_generation_quality_manifest(output_path, build_generation_quality_manifest(
                "failed", fingerprint, accepted_chunks, source_normalizations,
                total_chunks=total_chunks, failed_chunk=i,
                failure="chunk_failed_after_retries"))
            print(f"Error: chunk {i}/{total_chunks} failed validation after retries; "
                  "preserving existing output and validated checkpoint")
            sys.exit(1)

        quality = validate_chunk_quality(chunk, entries)
        if not quality["passed"]:
            save_generation_quality_manifest(output_path, build_generation_quality_manifest(
                "failed", fingerprint, accepted_chunks, source_normalizations,
                total_chunks=total_chunks, failed_chunk=i,
                failure="post_return_validation_failed", failed_quality=quality))
            print(f"Error: chunk {i}/{total_chunks} failed post-return validation; "
                  "preserving existing output and validated checkpoint")
            sys.exit(1)
        all_entries.extend(entries)
        accepted_chunks.append({
            "chunk_number": i,
            "source_sha256": fingerprint["chunk_sha256"][i - 1],
            "entries": entries,
            "quality": quality,
        })
        save_generation_checkpoint(output_path, fingerprint, accepted_chunks)
        print(f"  Got {len(entries)} entries (chunk took {chunk_elapsed:.0f}s)")

        remaining = total_chunks - i
        if remaining > 0:
            avg = sum(chunk_times) / len(chunk_times)
            eta_sec = avg * remaining
            if eta_sec < 60:
                eta_str = f"{eta_sec:.0f}s"
            elif eta_sec < 3600:
                eta_str = f"{int(eta_sec // 60)}m {int(eta_sec % 60)}s"
            else:
                eta_str = f"{int(eta_sec // 3600)}h {int((eta_sec % 3600) // 60)}m"
            elapsed_total = time.monotonic() - start_time
            print(f"  ETA: ~{eta_str} remaining ({remaining} chunk(s) left, avg {avg:.0f}s/chunk, {elapsed_total:.0f}s elapsed)")

    if not all_entries:
        print("Error: No script entries generated")
        sys.exit(1)

    whole_quality = validate_chunk_quality(book_content, all_entries)
    preflight = audit_script(all_entries, book_content, is_generic_speaker)
    identity_review = stabilize_speaker_identities(all_entries)["review"]
    final_manifest = build_generation_quality_manifest(
        "verified" if passes_final_generation_gate(whole_quality, preflight) else "failed",
        fingerprint, accepted_chunks, source_normalizations,
        total_chunks=total_chunks,
        model_name=model_name,
        whole_book_quality=whole_quality,
        preflight=preflight,
        speaker_identity_review=identity_review,
    )
    save_generation_quality_manifest(output_path, final_manifest)
    if final_manifest["status"] != "verified":
        print("Error: final whole-book quality gate failed; preserving existing output and checkpoint")
        sys.exit(1)

    atomic_json_write(all_entries, output_path)
    save_generation_quality_manifest(output_path, {**final_manifest, "status": "complete"})
    clear_generation_checkpoint(output_path)

    # Only clear chunks when writing to the default annotated_script.json location
    if args.output is None:
        chunks_path = os.path.join(data_dir, "chunks.json")
        if os.path.exists(chunks_path):
            os.remove(chunks_path)
            print("Cleared old chunks.json")

    # Summary (check both "speaker" and "type" fields)
    speakers = set(entry.get("speaker") or entry.get("type") or "UNKNOWN" for entry in all_entries)
    print(f"\nGenerated {len(all_entries)} script entries")
    print(f"Speakers found: {', '.join(sorted(speakers))}")
    print(f"Output saved to: {output_path}")


if __name__ == '__main__':
    main()
