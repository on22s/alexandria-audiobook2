# Shared campaign config recovery. A leftover backup is authoritative until
# restoration succeeds; never overwrite it with the temporary campaign config.
restore_config_backup() {
    local backup="$1" config="$2"
    [ -f "$backup" ] || return 0
    if ! command cp -f "$backup" "$config"; then
        echo "REFUSING: could not restore $config; recovery retained at $backup" >&2
        return 1
    fi
    command rm -f "$backup"
}

save_config_backup() {
    local config="$1" backup="$2" staging
    staging=$(mktemp "${backup}.XXXXXX") || return 1
    if command cp -f "$config" "$staging" && command mv -f "$staging" "$backup"; then
        return 0
    fi
    command rm -f "$staging"
    echo "REFUSING: could not capture configuration backup $backup" >&2
    return 1
}
