check_gpu_llm() {
if [ "${REQUIRE_LLM:-0}" = "1" ]; then
    preflight="${REPO}/app/experiments/llm_preflight.py"
    # NOT $PYTHON. Pinokio exports PYTHON=<miniforge>/python, which does not
    # exist on this box, so `${PYTHON:-default}` takes the broken value - the
    # variable IS set, so the default never fires - and this check silently
    # downgraded to "unchecked" the first time it ran.
    preflight_py="${LLM_PREFLIGHT_PYTHON:-${REPO}/app/env/bin/python}"
    if [ -x "$preflight_py" ] && [ -f "$preflight" ]; then
        if ! "$preflight_py" "$preflight" --quiet; then
            write_queue_log "$(stamp) NO_LLM   $NAME (preflight failed)"
            echo "gpu_job: refusing to run $NAME without a working LLM." >&2
            exit 6
        fi
    else
        # Cannot check is not the same as failed. Say so and continue rather
        # than blocking a run on the absence of the checker.
        write_queue_log "$(stamp) LLM_UNCHECKED $NAME (no preflight available)"
        echo "gpu_job: WARNING - REQUIRE_LLM set but preflight unavailable." >&2
    fi
fi

}
