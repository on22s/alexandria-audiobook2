import argparse
import os
import sys
import json
import re
import time
from dataclasses import dataclass
from openai import OpenAI
from config_settings import load_app_config
from default_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from lmstudio_settings import ensure_ideal_settings, get_effective_max_tokens
from utils import atomic_json_write, extract_balanced, get_runtime_data_dir, get_app_config_path

def clean_json_string(text):
    """Clean and extract valid JSON array from LLM response."""
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

    json_text = span

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
                         log_name, label, max_retries=2):
    """Call the LLM and parse a JSON array of entries, with retries.

    Shared by process_chunk() (script generation) and review_batch() (review):
    the two only differed in their log file/label and failure sentinel. Returns a
    list of entries, or [] if every attempt failed to produce parseable JSON.
    `log_name` is the raw-response log basename; `label` tags each block
    (e.g. "CHUNK 3/40" or "BATCH 2/10").
    """
    for attempt in range(max_retries + 1):
        t0 = time.time()
        try:
            messages = [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt}
            ]
            effective_max = get_effective_max_tokens(
                params.max_tokens, params.context_length, messages,
                params.hard_max_tokens)
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

    return call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="llm_responses.log",
        label=f"CHUNK {chunk_num}/{total_chunks}",
        max_retries=max_retries,
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

    for i, chunk in enumerate(chunks, 1):
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
            print(f"Error: chunk {i}/{total_chunks} produced no entries; preserving existing output")
            sys.exit(1)

        all_entries.extend(entries)
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

    atomic_json_write(all_entries, output_path)

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
