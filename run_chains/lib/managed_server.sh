# Model flags stay explicit in callers; one owner spans launch, requests and stop.
source "$(dirname "${BASH_SOURCE[0]}")/server_cleanup.sh" || return 1
source "$(dirname "${BASH_SOURCE[0]}")/llm_campaign.sh" || return 1
MANAGED_SERVER_PID=""

ensure_managed_server_lease() {
    LLM_CAMPAIGN_STOP_FUNCTION=stop_managed_server
    # Preserve the queue's standard capacity floor for these serving campaigns.
    LLM_CAMPAIGN_REQUIRE_VRAM_GB="${REQUIRE_VRAM_GB:-4}"
    ensure_llm_campaign_lease "$@"
}

stop_managed_server() {
    [ -n "${MANAGED_SERVER_PID:-}" ] || return 0
    stop_owned_server "$MANAGED_SERVER_PID" "${MANAGED_SERVER_STOP_GRACE:-10}" || return 1
    MANAGED_SERVER_PID=""
    if [ "${MANAGED_SERVER_WAIT_VRAM:-0}" = 1 ]; then
        wait_for_server_vram_release || return 1
    fi
}

start_managed_server() {
    local port="$1" log="$2" attempts="$3" delay="$4" probe_python attempt
    shift 4
    bash "$REPO/gpu_job.sh" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-0}" "${ALEXANDRIA_GPU_LOCK_FD:-9}" || return 4
    if [ -n "${MANAGED_SERVER_PID:-}" ]; then
        echo "REFUSING: managed server already has a captured child" >&2
        return 1
    fi
    probe_python="${MANAGED_SERVER_PYTHON:-${python:-python3}}"
    "$@" > "$log" 2>&1 &
    MANAGED_SERVER_PID=$!
    for ((attempt=0; attempt<attempts; attempt++)); do
        kill -0 "$MANAGED_SERVER_PID" 2>/dev/null || break
        if "$probe_python" -B "$REPO/app/llama_server_process.py" --check-listener-owner "$port" "$MANAGED_SERVER_PID" \
                && curl -fsS --max-time 5 "http://127.0.0.1:$port/health" >/dev/null 2>&1 \
                && "$probe_python" -B "$REPO/app/llama_server_process.py" --check-listener-owner "$port" "$MANAGED_SERVER_PID"; then
            LLM_CAMPAIGN_SERVER_READY=1
            return 0
        fi
        sleep "$delay"
    done
    tail -20 "$log" >&2
    echo "REFUSING: owned server did not become healthy" >&2
    return 1
}
