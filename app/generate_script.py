import argparse
import os
import sys
import json
import re
import time
import hashlib
from dataclasses import dataclass
from openai import OpenAI
from default_prompts import DEFAULT_SYSTEM_PROMPT, DEFAULT_USER_PROMPT
from utils import atomic_json_write, safe_load_json, file_lock


def _script_checkpoint_path(output_path):
    """The tiny meta sidecar: {completed_chunks,total_chunks,chunk_size,input_hash}."""
    return output_path + ".script_checkpoint.json"


def _script_checkpoint_entries_path(output_path):
    """The append-only sidecar: one JSON line per completed chunk, each line the
    list of entries that chunk produced. Kept separate from the meta so each
    chunk is an O(1) append instead of re-serializing the whole growing script."""
    return output_path + ".script_checkpoint.jsonl"


def compute_input_hash(book_content):
    """Stable hash of the (mojibake-fixed) source text. A changed source means
    the chunk split would differ, so a checkpoint with a different hash must not
    be resumed."""
    return hashlib.sha256(book_content.encode("utf-8")).hexdigest()


def load_book_content(input_file_path):
    """Read + mojibake-fix a source file exactly the way main() does, so the hash
    and split are identical whether computed here or in the generation run."""
    with open(input_file_path, "r", encoding="utf-8") as f:
        return fix_mojibake(f.read())


def compute_split_signature(input_file_path, chunk_size):
    """(total_chunks, input_hash) for a source at the given chunk_size, computed
    the same way main() does. Lets the detect endpoint predict exactly what a
    resume would be validated against, so it never offers an unresumable run."""
    content = load_book_content(input_file_path)
    total_chunks = len(split_into_chunks(content, max_size=chunk_size))
    return total_chunks, compute_input_hash(content)


def script_checkpoint_matches(meta, total_chunks, chunk_size, input_hash):
    """Acceptance predicate for a script checkpoint's meta, shared by
    load_script_checkpoint and app.py's detect endpoint so the UI never offers a
    resume the worker would reject (Rule 15). A checkpoint is resumable only when
    the source/split is identical AND completed_chunks is a valid in-range int."""
    if not isinstance(meta, dict):
        return False
    if (meta.get("total_chunks") != total_chunks or
            meta.get("chunk_size") != chunk_size or
            meta.get("input_hash") != input_hash):
        return False
    cc = meta.get("completed_chunks")
    # bool is an int subclass — reject it explicitly so True/False can't pass.
    if isinstance(cc, bool) or not isinstance(cc, int):
        return False
    return 0 <= cc <= total_chunks


def save_script_checkpoint(output_path, completed_chunks, total_chunks,
                           chunk_size, input_hash, new_entries):
    """Persist progress after one chunk: append THIS chunk's entries as a single
    JSON line, then rewrite the tiny meta. `new_entries` is only the current
    chunk's entries, not the full accumulator — the whole point of the JSONL
    sidecar is to avoid re-serializing every prior chunk on every save."""
    meta = {
        "completed_chunks": completed_chunks,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "input_hash": input_hash,
    }
    entries_path = _script_checkpoint_entries_path(output_path)
    try:
        # file_lock so a concurrent clear/read can't observe a torn append.
        with file_lock(entries_path):
            with open(entries_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(new_entries, ensure_ascii=False) + "\n")
        # Meta is written AFTER the append and is the source of truth for how
        # many lines count — a crash between the two just leaves one stale extra
        # line, which load/resume ignores and truncates.
        atomic_json_write(meta, _script_checkpoint_path(output_path))
    except (OSError, TimeoutError) as e:
        # Mirror review_script.py: never crash generation over a checkpoint
        # write failure (disk full, permissions) — just warn.
        print(f"WARNING: Failed to save script checkpoint: {e}. "
              f"Generation will continue but resume may not work.")


def _read_checkpoint_entries(output_path):
    """Return the parsed per-chunk entry lists from the JSONL sidecar, or None if
    any line is unreadable/malformed (treated as corrupt -> start fresh)."""
    entries_path = _script_checkpoint_entries_path(output_path)
    if not os.path.exists(entries_path):
        return None
    lines = []
    try:
        with open(entries_path, "r", encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                chunk_entries = json.loads(raw)
                if not isinstance(chunk_entries, list):
                    return None
                lines.append(chunk_entries)
    except (json.JSONDecodeError, ValueError, OSError):
        return None
    return lines


def load_script_checkpoint(output_path, total_chunks, chunk_size, input_hash):
    """Return {completed_chunks, all_entries} only if the checkpoint matches this
    run's split exactly and its entries sidecar is consistent; otherwise None
    (caller starts fresh)."""
    meta = safe_load_json(_script_checkpoint_path(output_path))
    if meta is None:
        return None
    if not script_checkpoint_matches(meta, total_chunks, chunk_size, input_hash):
        print("Found a script checkpoint but the source/split changed or it was "
              "malformed - starting fresh.")
        return None
    completed = meta["completed_chunks"]
    per_chunk = _read_checkpoint_entries(output_path)
    if per_chunk is None or len(per_chunk) < completed:
        # Entries sidecar missing/corrupt or has fewer chunks than the meta
        # claims -> can't trust the resume point.
        print("Script checkpoint entries are missing or inconsistent - starting fresh.")
        return None
    # Meta's completed_chunks is authoritative: replay exactly that many lines,
    # dropping any stale extra line from a crashed final append.
    all_entries = []
    for chunk_entries in per_chunk[:completed]:
        all_entries.extend(chunk_entries)
    return {"completed_chunks": completed, "all_entries": all_entries}


def truncate_checkpoint_entries(output_path, completed_chunks):
    """Rewrite the JSONL sidecar to exactly `completed_chunks` lines, dropping any
    stale trailing line left by a crash between append and meta write. Called
    once at resume so subsequent appends stay aligned with the meta count."""
    per_chunk = _read_checkpoint_entries(output_path)
    if per_chunk is None or len(per_chunk) <= completed_chunks:
        return
    entries_path = _script_checkpoint_entries_path(output_path)
    try:
        with file_lock(entries_path):
            with open(entries_path, "w", encoding="utf-8") as f:
                for chunk_entries in per_chunk[:completed_chunks]:
                    f.write(json.dumps(chunk_entries, ensure_ascii=False) + "\n")
    except (OSError, TimeoutError) as e:
        print(f"WARNING: Failed to trim script checkpoint entries: {e}.")


def clear_script_checkpoint(output_path):
    """Remove both checkpoint sidecars, coordinated by file_lock to avoid racing
    a concurrent read/write (matches review_script.clear_checkpoint)."""
    for path in (_script_checkpoint_path(output_path),
                 _script_checkpoint_entries_path(output_path)):
        if not os.path.exists(path):
            continue
        try:
            with file_lock(path):
                os.remove(path)
        except (TimeoutError, OSError) as e:
            if os.path.exists(path):
                print(f"WARNING: Failed to clear script checkpoint {path}: {e}.")


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
    # Use a bracket counter to find the correct closing bracket
    start = text.find('[')
    if start == -1:
        return None

    bracket_count = 0
    end = -1
    in_string = False
    escape_next = False

    for i, char in enumerate(text[start:], start):
        if escape_next:
            escape_next = False
            continue
        if char == '\\':
            escape_next = True
            continue
        if char == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == '[':
            bracket_count += 1
        elif char == ']':
            bracket_count -= 1
            if bracket_count == 0:
                end = i + 1
                break

    if end == -1:
        # No closing bracket found, try to salvage
        last_complete = text.rfind('},')
        if last_complete > start:
            return text[start:last_complete+1] + ']'
        return None

    json_text = text[start:end]

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
        """Keep only dict entries, and ensure each has a string 'text'. LLMs
        sometimes emit bare strings (dropped here) or a dict with a non-string
        'text' (e.g. "text": null), which would later crash normalize/text-loss
        checks that call .lower()/.strip(). Coerce a non-string text rather than
        dropping the entry so no content is lost."""
        filtered = []
        for e in lst:
            if not isinstance(e, dict):
                continue
            if not isinstance(e.get("text"), str):
                e = {**e, "text": "" if e.get("text") is None else str(e.get("text"))}
            filtered.append(e)
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
        except Exception:
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
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=params.temperature,
                top_p=params.top_p,
                presence_penalty=params.presence_penalty,
                max_tokens=params.max_tokens,
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
                print(f"  WARNING: Response was truncated (hit max_tokens={params.max_tokens}). Consider increasing max_tokens.")

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
            continue  # honor the retry instead of preempting it with a partial salvage

        # Final attempt only: last-resort regex salvage of individual entries, so
        # both parse-failure paths follow one policy (Rule 10) — full retries first,
        # partial salvage only when no attempts remain.
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
    parser.add_argument("--resume", action="store_true",
                        help="Resume from a saved checkpoint if one matches this source.")
    args = parser.parse_args()

    input_file_path = args.input_file
    print(f"Processing book from: {input_file_path}")

    if not os.path.exists(input_file_path):
        print(f"Error: Input file not found: {input_file_path}")
        sys.exit(1)

    # Read + fix encoding artifacts (shared with the detect endpoint's signature)
    book_content = load_book_content(input_file_path)

    print(f"Read {len(book_content)} characters")

    # Load LLM config
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")
    else:
        print("Warning: config.json not found. Using defaults.")

    llm_config = config.get("llm", {})
    base_url = llm_config.get("base_url", "http://localhost:11434/v1")
    api_key = llm_config.get("api_key", "local")
    model_name = llm_config.get("model_name", "richardyoung/qwen3-14b-abliterated:Q8_0")

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

    # Create OpenAI client with custom base URL
    client = OpenAI(
        base_url=base_url,
        api_key=api_key
    )

    # Split into chunks at natural boundaries
    chunks = split_into_chunks(book_content, max_size=chunk_size)
    total_chunks = len(chunks)

    print(f"Split into {total_chunks} chunks at paragraph/sentence boundaries")

    output_path = args.output or os.path.join(os.path.dirname(__file__), "..", "annotated_script.json")

    input_hash = compute_input_hash(book_content)
    all_entries = []
    completed_chunks = 0
    if args.resume:
        ckpt = load_script_checkpoint(output_path, total_chunks, chunk_size, input_hash)
        if ckpt:
            all_entries = ckpt["all_entries"]
            completed_chunks = ckpt["completed_chunks"]
            # Drop any stale trailing line so subsequent appends stay aligned.
            truncate_checkpoint_entries(output_path, completed_chunks)
            print(f"Resuming from checkpoint: {completed_chunks}/{total_chunks} chunks already done.")
        else:
            # Clear any partial/stale sidecars so the fresh run doesn't append
            # onto a checkpoint it just rejected.
            clear_script_checkpoint(output_path)
            print("No usable checkpoint - starting fresh.")
    else:
        clear_script_checkpoint(output_path)

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
    )

    for i, chunk in enumerate(chunks, 1):
        if i <= completed_chunks:
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
            # Every LLM attempt for this chunk failed. Do NOT checkpoint it as
            # completed (which would make --resume skip it, dropping a chunk-sized
            # hole in the book) and do not process later chunks past the gap. Stop
            # with a non-zero exit so a --resume retries exactly this chunk (the
            # checkpoint still points at the last fully-completed chunk, i-1).
            print(f"  ERROR: chunk {i}/{total_chunks} produced no entries after all "
                  f"retries — stopping so it can be retried on --resume.")
            sys.exit(1)

        all_entries.extend(entries)
        # Append only THIS chunk's entries (O(1)); the meta records progress.
        save_script_checkpoint(output_path, i, total_chunks, chunk_size,
                               input_hash, entries)
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

    # Save as JSON (atomic write so a crash/kill mid-write can't corrupt the book)
    atomic_json_write(all_entries, output_path)
    clear_script_checkpoint(output_path)

    # Only clear chunks when writing to the default annotated_script.json location
    if args.output is None:
        chunks_path = os.path.join(os.path.dirname(__file__), "..", "chunks.json")
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
