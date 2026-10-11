# Experimental opt-in ASCII speech backend

Python is the default. App requirements, installers and startup do not build or require this extension. The opt-in is read once at worker import; changing it requires a worker restart. Set `AA2_EXPERIMENTAL_ASCII_NATIVE=1` only in an isolated worker with an explicitly prepared compatible extension. A missing/unloadable extension or incompatible API uses Python with a warning. Unexpected native errors and interrupts propagate. `ascii_speech_native.get_ascii_native_status()` reports the worker state without loading anything. This is not a supported release or a claim of whole-audiobook acceleration.

The reviewed prototype is narrowed to plain ASCII strings of 64–4096 characters. All other inputs use the unchanged Python loop, including Unicode, surrogates, subclasses and unusual input/error cases. The upper bound is a cancellation/latency safety bound, not a tuned performance threshold. A local paired smoke of the integrated speech-preparation caller measured the public Pride sentence proxy at468.5→399.0ms (14.8%lower), ASCII512 at33.7%lower, and short Unicode fallback at1.75%slower. These are bounded stage observations, not TTS/audio/whole-book claims or a full performance qualification.

The table comes from the existing Python policy and canonical functions. Rust checks current complete symbol/category/replacement content and function identities under the GIL, then translates under that same GIL. Definition-time function/code witnesses detect alias and in-place code replacement. Exact builtin types and scalar strings avoid equality callbacks or surrogate conversion errors; custom policy/functions invalidate to Python until worker restart. There is no detached work, unbounded cached collection, alternate policy file or automatic rebuild/refresh. Policy snapshots are limited to256symbol entries,16categories and256UTF-8bytes per policy string; larger/custom policies use Python. The native output buffer uses fallible reservation. Other tiny Rust/PyO3 allocations have not been qualified under actual allocator exhaustion. Native calls check Python signals before and after bounded work. A policy update cannot run on another Python thread during validation/translation; this does not add an atomicity guarantee to the legacy Python fallback. Direct native/foreign-thread mutation of Python objects without the GIL is unsupported by Python itself.

## Local developer build, with already available tools only

The crate retains the previously reviewed Cargo.lock and pinned PyO30.27.2. The extension has no abi3 feature and is specific to the selected CPython ABI/platform. From this directory:

```sh
PYO3_PYTHON=/absolute/path/to/app/python cargo build --release --locked --offline
```

This requires a pre-existing complete Cargo cache/toolchain. Do not add it to app installation, fetch dependencies automatically, or silently compile on import. For isolated Linux testing, copy `target/release/libaa2_ascii_native.so` to an external test directory using that interpreter's `sysconfig.get_config_var('EXT_SUFFIX')`, under the name `aa2_ascii_native<suffix>`, and put only that directory on the test worker's PYTHONPATH. Do not commit or copy a binary into the live application.

No wheel build system or mandatory packaging dependency is added. Prebuilt wheel distribution remains a release gate: exact ABI/architecture/libc tags, provenance, native runtime dependencies, clean installation with/without optional wheel, and actual Windows/macOS/native platform tests. Existing Linux CPython3.10 build is the only locally assessed backend; Windows/macOS and free-threaded Python are unqualified. Unsupported environments should leave the option unset and use Python. Do not install packages to satisfy missing wheel tooling during this review.

## Review gates

External checks must establish default-off import/startup without extension, explicit opt-in missing/import-OSError/incompatible-API fallback, exact retained Unicode/ASCII/error/transformations and caller-stage parity, persistent-worker invalidation and restart, policy edits/category/replacement/function changes, concurrent policy transitions, native exceptions/MemoryError/KeyboardInterrupt propagation, bounded native cancellation and existing speech tests. Native result/evidence shape and counts fail loudly if malformed. Installer/requirements files remain unchanged. Broad platform/wheel qualification, the full integrated caller performance matrix and per-arm native memory remain pending; no GPU/model/audio job is part of this feature.
