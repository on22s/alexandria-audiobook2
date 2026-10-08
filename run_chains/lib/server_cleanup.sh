# The launcher and queued reclamation target the same configured executable.
get_llama_server_binary() {
    printf '%s\n' "${LLAMA_BIN:-$(command -v llama-server 2>/dev/null || echo "$HOME/llama.cpp/build/bin/llama-server")}"
}

# Terminate only the caller's captured child, then reap it. Healthy servers
# keep their established lifetime; callers use this on failed owned launches.
is_owned_server_child() {
    local parent
    parent=$(ps -o ppid= -p "$1" 2>/dev/null) || return 1
    parent=${parent//[[:space:]]/}
    [ "$parent" = "$BASHPID" ]
}

stop_owned_server() {
    local pid="${1:-}" grace="${2:-10}" deadline
    if [[ ! "$pid" =~ ^[0-9]+$ ]] || [ "$pid" -le 0 ] || [[ ! "$grace" =~ ^[0-9]+$ ]]; then
        echo "REFUSING server cleanup: expected a positive child PID and bounded grace" >&2
        return 1
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
        wait "$pid" 2>/dev/null || true
        return 0
    fi
    if ! is_owned_server_child "$pid"; then
        if ! kill -0 "$pid" 2>/dev/null; then
            wait "$pid" 2>/dev/null || true
            return 0
        fi
        echo "REFUSING server cleanup: PID $pid is not this shell's child" >&2
        return 1
    fi
    kill -TERM "$pid" 2>/dev/null || true
    deadline=$((SECONDS + grace))
    while kill -0 "$pid" 2>/dev/null && [ "$SECONDS" -lt "$deadline" ]; do
        sleep 0.1
    done
    if kill -0 "$pid" 2>/dev/null; then
        if ! is_owned_server_child "$pid"; then
            if ! kill -0 "$pid" 2>/dev/null; then
                wait "$pid" 2>/dev/null || true
                return 0
            fi
            echo "REFUSING server cleanup: PID $pid no longer belongs to this shell" >&2
            return 1
        fi
        echo "Server $pid ignored TERM; escalating after ${grace}s" >&2
        kill -KILL "$pid" 2>/dev/null || true
    fi
    wait "$pid" 2>/dev/null || true
}


# Preserve the shared stage runner's ROCm used-memory policy and polling cap.
# Missing telemetry stays unknown; gpu_job.sh still owns the capacity gate.
wait_for_server_vram_release() {
    local waited=0 used
    while [ "$waited" -lt "${STAGE_VRAM_WAIT:-90}" ]; do
        used=$(rocm-smi --showmeminfo vram 2>/dev/null \
               | grep -i 'total used memory' | awk '
                   {
                       sub(/^.*:[[:space:]]*/, "")
                       if ($0 !~ /^[0-9]+[[:space:]]*$/) { unknown = 1; next }
                       value = $0 + 0
                       if (!seen || value > maximum) { maximum = value }
                       seen = 1
                   }
                   END { if (seen && !unknown) { printf "%.0f\n", maximum } }')
        if [ -z "$used" ]; then
            echo "VRAM release unknown: used-memory telemetry unavailable; job capacity gate remains required"
            return 0
        fi
        if [ "$used" -lt 2147483648 ]; then
            echo "VRAM reclaimed after ${waited}s (used ${used} bytes)"
            return 0
        fi
        sleep 3
        waited=$((waited + 3))
    done
    echo "VRAM still occupied after ${waited}s; job capacity gate remains required" >&2
    return 1
}
