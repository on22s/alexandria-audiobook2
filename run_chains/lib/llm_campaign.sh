# One lease spans startup, every dependent request, and configured-server stop.
stop_llm_campaign_server() {
    [ "${LLM_CAMPAIGN_SERVER_READY:-0}" = 1 ] || return 0
    bash "$REPO/run_chains/lib/reclaim_vram.sh" "$REPO/gpu_job.sh" "$LLM_CAMPAIGN_NAME.cleanup" true
}

finish_llm_campaign() {
    local campaign_rc="$1"
    if declare -F restore_llm_campaign_state >/dev/null; then
        if ! restore_llm_campaign_state; then
            [ "$campaign_rc" -ne 0 ] || campaign_rc=1
        fi
    fi
    if ! "${LLM_CAMPAIGN_STOP_FUNCTION:-stop_llm_campaign_server}"; then
        [ "$campaign_rc" -ne 0 ] || campaign_rc=4
    fi
    trap - EXIT
    exit "$campaign_rc"
}

ensure_llm_campaign_lease() {
    local name="$1"; shift
    if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
        exec env REQUIRE_LLM=0 REQUIRE_VRAM_GB="${LLM_CAMPAIGN_REQUIRE_VRAM_GB:-0}" "$REPO/gpu_job.sh" "$name" "$@"
    fi
    bash "$REPO/gpu_job.sh" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-0}" "${ALEXANDRIA_GPU_LOCK_FD:-9}" || return 4
    LLM_CAMPAIGN_NAME="$name"
    LLM_CAMPAIGN_SERVER_READY=0
    trap 'finish_llm_campaign $?' EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    trap 'exit 129' HUP
}
