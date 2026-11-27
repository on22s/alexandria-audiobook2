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

Run any harness directly with libFuzzer `-flag=value` options (space-separated forms like `-max_total_time 60` are rejected with an error). When no positional corpus directory is passed, the harness automatically writes newly discovered inputs into a fresh temporary scratch directory while reading the checked-in seeds under `tools/fuzz/corpus/` read-only. `atheris.Fuzz()` exits inside libFuzzer, so `atexit` handlers and `finally` blocks never run: the harness prints the scratch directory path on startup and leaves it behind for you to remove (CI passes an explicit corpus directory instead):

```bash
app/env/bin/python tools/fuzz/fuzz_epub_structured.py \
  -max_total_time=600 -print_final_stats=1
```

Or pass an explicit scratch corpus and artifact directory (as `.github/workflows/fuzz.yml` does) to retain grown corpus units or crash artifacts after the run:

```bash
SCRATCH=$(mktemp -d /var/tmp/aa2-fuzz.XXXXXX)
mkdir -p "$SCRATCH/corpus" "$SCRATCH/artifacts"
cp tools/fuzz/corpus/epub_structured/* "$SCRATCH/corpus/"
app/env/bin/python tools/fuzz/fuzz_epub_structured.py \
  -max_total_time=600 -print_final_stats=1 \
  -artifact_prefix="$SCRATCH/artifacts/" "$SCRATCH/corpus"
```

`tools/fuzz/.gitignore` also ignores stray `crash-*`, `timeout-*`, `oom-*`, `slow-unit-*`, `fuzz*.log`, and locally grown 40-hex libFuzzer corpus files under `tools/fuzz/corpus/*/`.

To replay a single crash artifact:

```bash
app/env/bin/python tools/fuzz/fuzz_epub_raw.py /path/to/crash-artifact
```

## Exception filters

The harnesses ignore only exceptions that the fuzzed production code documents
as malformed-input refusals; everything else crashes the run:

- **Known library bugs** (`#2367`, `#2368`, `#2370`, `#2371`, `#2375`) are
  matched by the throwing function's source file and function name plus the
  exception type. Each filtered function has one reachable failure site (or,
  for `get_epub_xhtml_text`, the two decode statements documented in #2368),
  so no bytecode/`tb_lasti` inspection is used. Opcode names and call layouts
  differ across CPython versions (`LOAD_METHOD` is gone in 3.12, `CALL_METHOD`
  in 3.13) and Atheris rewrites the code it instruments, so a bytecode window
  would be a CPython-3.10-only assumption. The encoding-declaration filters
  (`#2368`/`#2370`/`#2375`) cover every CPython 3.10 failure mode of those
  bugs: `LookupError` for an unknown codec plus `ValueError` for
  `multi-byte encodings are not supported` (`#2370`/`#2375`) and
  `embedded null character` (`#2368`).
- **`ValueError`** is never caught wholesale. The EPUB harnesses accept only
  the documented message prefixes from `validate_epub_archive`,
  `get_epub_package`, and `extract_epub_text`; the parity harness does the same
  for `load_epub`, whose documented refusals are the archive-admission
  messages plus `EPUB has no readable spine documents` and
  `EPUB spine references missing document: ...`. The extractor's
  `Malformed EPUB resource at spine index ...` wrapper is accepted only when
  its `__cause__` is one of the four documented inner parser messages
  (`invalid XML: ...`, `invalid XML while ...`, `invalid Unicode: ...`,
  `unclosed <...`), because an unexpected `ValueError` from the extraction
  block is wrapped in the same prefix. The stdlib `zipfile` corrupt-offset
  `ValueError` (`negative seek value ...` / `seek out of range`) is accepted
  only from the `zipfile` frame, and #2377's `Invalid IPv6 URL` only from
  `_resolve_epub_href` calling `urllib.parse.urlsplit`. A `ValueError` with
  any other message fails the harness.
- **ZIP/XML refusal exceptions** (`BadZipFile`, `ET.ParseError`, stdlib
  ZIP `RuntimeError`/`KeyError`/`OSError`/`UnicodeError` paths) are scoped to
  the exact stdlib `zipfile`/`ElementTree` frame; `UnicodeError` is always
  caught before `ValueError`.

## Seed corpus

`tools/fuzz/corpus/` contains small **synthetic** seed inputs only (including minimized synthetic regression seeds for bugs discovered by Atheris). Never place user data or copyrighted book text here.

`tools/fuzz/corpus/epub_raw/` includes both #2375 arms
(`regression_2375_opf_unknown_xml_encoding.epub`,
`regression_2375_container_unknown_xml_encoding.epub`), the multi-byte variants
`regression_2375_container_multibyte_xml_encoding.epub` (#2375) and
`regression_2370_nav_multibyte_xml_encoding.epub` (#2370), plus
`regression_2368_embedded_null_encoding.epub` (NUL inside the declared codec
name), `regression_2377_ipv6_manifest_href.epub` (a manifest `href` of
`http://[`, which makes `urlsplit` raise `ValueError: Invalid IPv6 URL`), and
`seed_corrupt_zip_central_directory.epub` (a ZIP member offset that makes
stdlib `BytesIO.seek` refuse with `ValueError`).

`tools/fuzz/corpus/epub_structured/` includes `seed_05_chapter_decode.bin`, a
synthetic input whose generated EPUB has a spine chapter that reaches
`get_epub_xhtml_text` and chapter decoding (the original `seed_01`/`seed_04`
only exercise container/OPF handling because their generated chapters are
absent or non-HTML).
