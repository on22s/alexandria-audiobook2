vram_free_mib() {
    local rocm_output agents="" hip_selector selected=0 result status
    hip_selector="${HIP_VISIBLE_DEVICES:-${CUDA_VISIBLE_DEVICES:-}}"
    if [ -n "${ROCR_VISIBLE_DEVICES:-}" ] || [ -n "$hip_selector" ]; then
        selected=1
    fi
    if rocm_output=$(rocm-smi --showmeminfo vram --showuniqueid 2>/dev/null); then
        # ROCr already applies its own visibility/reordering to rocminfo's GPU
        # agents. HIP ordinals index that filtered list, not SMI card numbers.
        if [ "$selected" = 1 ] && [[ "$rocm_output" == *"VRAM Total"* ]]; then
            agents=$(timeout 10 rocminfo 2>/dev/null) || return 2
        fi
        result=$(printf '%s\n__ROCM_AGENTS__\n%s\n' "$rocm_output" "$agents" |
            awk -v selected="$selected" -v selector="$hip_selector" '
            function uid(value) {
                value=tolower(value); sub(/^gpu-/, "", value); sub(/^0x/, "", value)
                sub(/^0+/, "", value); return "u:" value
            }
            function measure(card, free) {
                if (!(card in total) || !(card in used) || total[card] <= 0 || used[card] < 0 || used[card] > total[card]) return 0
                free=int((total[card]-used[card])/1048576)
                if (!seen || free < minimum) minimum=free
                seen=1; return 1
            }
            /^__ROCM_AGENTS__$/ { in_agents=1; next }
            !in_agents && match($0, /GPU\[[0-9]+\]/) {
                card=substr($0, RSTART+4, RLENGTH-5)
                if ($0 ~ /Unique ID:/ && $NF ~ /^0x[0-9a-fA-F]+$/) {
                    key=uid($NF)
                    if (key in cards && cards[key] != card) ambiguous[key]=1
                    cards[key]=card
                }
                if ($0 ~ /VRAM Total Memory/ && $NF ~ /^[0-9]+$/) total[card]=$NF+0
                if ($0 ~ /VRAM Total Used Memory/ && $NF ~ /^[0-9]+$/) used[card]=$NF+0
                if ($0 ~ /VRAM Total/) { has_memory=1; memory_cards[card]=1 }
                next
            }
            in_agents && /^Agent [0-9]+/ { agent_uid=""; next }
            in_agents && /^[[:space:]]*Uuid:/ { agent_uid=$NF; next }
            in_agents && /Device Type:/ && $NF == "GPU" { gpu_agents[count++]=uid(agent_uid) }
            END {
                if (!has_memory) exit 1
                if (!selected) {
                    for (card in memory_cards) if (!measure(card)) exit 2
                } else {
                    if (!count) exit 2
                    if (selector == "") {
                        for (i=0; i<count; i++) {
                            if (!(gpu_agents[i] in cards) || (gpu_agents[i] in ambiguous) || !measure(cards[gpu_agents[i]])) exit 2
                        }
                    } else {
                        n=split(selector, indices, ",")
                        for (i=1; i<=n; i++) {
                            token=indices[i]
                            if (token ~ /^GPU-/) {
                                key=uid(token); found=0
                                for (j=0; j<count; j++) if (gpu_agents[j] == key) found=1
                                if (!found) exit 2
                            } else {
                                if (token !~ /^(0|[1-9][0-9]*)$/ || token+0 >= count) exit 2
                                key=gpu_agents[token+0]
                            }
                            if (!(key in cards) || (key in ambiguous) || !measure(cards[key])) exit 2
                        }
                    }
                }
                if (!seen) exit 2
                print minimum
            }')
        status=$?
        if [ "$status" = 0 ]; then
            printf '%s\n' "$result"
            return 0
        elif [ "$status" = 2 ]; then
            return 2
        fi
    fi
    # Thunder cards are NVIDIA-only. Use the least-free visible card so a
    # multi-GPU host cannot pass because an unrelated card is empty.
    local selector="${CUDA_VISIBLE_DEVICES:-${NVIDIA_VISIBLE_DEVICES:-}}"
    local -a nvidia_args
    nvidia_args=(--query-gpu=memory.free --format=csv,noheader,nounits)
    if [ -n "$selector" ] && [ "$selector" != "all" ]; then
        nvidia_args=(-i "$selector" "${nvidia_args[@]}")
    fi
    nvidia-smi "${nvidia_args[@]}" \
        2>/dev/null | awk '
            /^[[:space:]]*[0-9]+/ {
                value=$1+0; if (!seen || value < minimum) minimum=value; seen=1
            }
            END {if (seen) print minimum; else exit 1}'
}

check_gpu_vram() {
REQUIRE_VRAM_MIB=$(( ${REQUIRE_VRAM_GB:-4} * 1024 ))
if free_mib=$(vram_free_mib); then
    if [ "$free_mib" -lt "$REQUIRE_VRAM_MIB" ]; then
        write_queue_log "$(stamp) NO_VRAM  $NAME (${free_mib}MiB free, needs ${REQUIRE_VRAM_MIB}MiB)"
        echo "gpu_job: refusing to run $NAME - only ${free_mib} MiB of VRAM free," >&2
        echo "gpu_job: and it needs ${REQUIRE_VRAM_MIB} MiB. Holding the card:" >&2
        { rocm-smi --showpids 2>/dev/null \
          || nvidia-smi --query-compute-apps=pid,used_memory \
               --format=csv,noheader 2>/dev/null; } | head -4 >&2
        echo "gpu_job: a persistent llama-server is the usual cause; it is" >&2
        echo "gpu_job: started outside the lock and never stops on its own." >&2
        echo "gpu_job: stop it with: pkill -x llama-server" >&2
        echo "gpu_job: or override with REQUIRE_VRAM_GB=0 if this job is small." >&2
        exit 7
    fi
else
    status=$?
    if [ "$status" = 2 ] && [ "$REQUIRE_VRAM_MIB" -gt 0 ]; then
        write_queue_log "$(stamp) NO_VRAM  $NAME (cannot verify selected ROCm card memory)"
        echo "gpu_job: refusing to run $NAME - cannot verify selected ROCm card memory." >&2
        exit 7
    fi
    write_queue_log "$(stamp) VRAM_UNKNOWN $NAME"
    echo "gpu_job: WARNING - cannot read VRAM; running $NAME unchecked." >&2
fi

}
