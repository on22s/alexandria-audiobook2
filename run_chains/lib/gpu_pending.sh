# Process identity for queue markers, shared by writer and status reader.
get_pending_process_token() {
    local pid="$1" stat boot fields
    [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
    IFS= read -r stat < "/proc/$pid/stat" || return 1
    IFS= read -r boot < /proc/sys/kernel/random/boot_id || return 1
    fields="${stat##*) }"
    local -a values
    read -r -a values <<< "$fields"
    [ "${values[0]:-}" != Z ] && [ "${values[0]:-}" != X ] || return 1
    [[ "${values[19]:-}" =~ ^[0-9]+$ ]] || return 1
    printf '%s:%s\n' "$boot" "${values[19]}"
}

save_pending_marker() {
    local path="$1" name="$2" pid="$3" stamp="$4" token
    token=$(get_pending_process_token "$pid") || return 1
    printf '%s\t%s\t%s\t%s\n' "$name" "$pid" "$stamp" "$token" > "$path"
}

is_pending_marker_live() {
    local name pid stamp token extra current
    IFS=$'\t' read -r name pid stamp token extra < "$1" || return 1
    [ -n "$token" ] && [ -z "$extra" ] || return 1
    current=$(get_pending_process_token "$pid" 2>/dev/null) || return 1
    [ "$token" = "$current" ]
}
