fn main() {
    // An attached Python thread is not a GIL-held interval on this build.
    assert!(
        !pyo3_build_config::get().is_free_threaded(),
        "aa2_ascii_native requires a GIL-enabled CPython build; free-threaded builds are unsupported"
    );
}
