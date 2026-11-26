# Atheris Fuzzing for EPUB and TXT Importers

Linux-only, coverage-guided fuzz harnesses using [Atheris](https://github.com/google/atheris) (`atheris==2.3.0`, prebuilt `cp310` `manylinux` wheel pinned in `app/requirements-fuzz.txt`).

These harnesses live outside `app/tests/` so `unittest discover` never collects or runs them.

## Harnesses

1. **`fuzz_epub_raw.py`** — Raw-byte fuzzing of `routers.script.extract_epub_text(..., archive_bytes=data, include_report=True, include_navigation_only=True)` plus report vs non-report text parity.
2. **`fuzz_epub_structured.py`** — Structure-aware EPUB fuzzing via `atheris.FuzzedDataProvider`, building a valid ZIP (`mimetype`, `META-INF/container.xml`, OPF manifest/spine, nav/NCX, XHTML/HTML chapters) and mutating attributes, properties, encodings, XML prologs, markup, anchors, and text.
3. **`fuzz_txt_import.py`** — Raw-byte fuzzing of the TXT decoding, newline/invisible normalization, and preflight pipeline (`source_encoding.get_decoded_source_text`, `get_normalized_source_newlines`, `routers.script.normalize_extracted_text`, `three_pass_generate.prepare_source_text`).
4. **`fuzz_epub_alignment_parity.py`** — Structure-aware parity fuzzing comparing `alexandria_alignment.load_epub` against `routers.script.extract_epub_text` on navigation-document skipping decisions (PR #2348 parity).

## Running locally (Linux, Python 3.10)

Install the pinned fuzz dependency into `app/env` (never into `app/requirements.txt`):

```bash
app/env/bin/python -m pip install -r app/requirements-fuzz.txt
```

Run any harness for a bounded time using a scratch corpus copy and artifact directory so the checkout stays clean:

```bash
SCRATCH=$(mktemp -d /var/tmp/aa2-fuzz.XXXXXX)
mkdir -p "$SCRATCH/corpus" "$SCRATCH/artifacts"
cp tools/fuzz/corpus/epub_structured/* "$SCRATCH/corpus/"
PYTHONPATH=app:. app/env/bin/python tools/fuzz/fuzz_epub_structured.py \
  -max_total_time=600 -print_final_stats=1 \
  -artifact_prefix="$SCRATCH/artifacts/" "$SCRATCH/corpus"
```

To replay a single crash artifact:

```bash
PYTHONPATH=app:. app/env/bin/python tools/fuzz/fuzz_epub_raw.py /path/to/crash-artifact
```

## Seed corpus

`tools/fuzz/corpus/` contains small **synthetic** seed inputs only (including minimized synthetic regression seeds for bugs discovered by Atheris). Never place user data or copyrighted book text here.
