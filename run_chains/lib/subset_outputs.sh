# Shared receipt formatting for the subset runner and its watchdog.
print_subset_outputs() {
    local directory="$1" path bytes
    for path in "$directory"/*.zip; do
        [ -f "$path" ] || continue
        bytes=$(stat -c %s -- "$path") || return 1
        printf '  %s (%s bytes)\n' "$(basename -- "$path")" "$bytes"
    done
}
