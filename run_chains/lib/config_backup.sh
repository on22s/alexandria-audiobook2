# Shared campaign config recovery. A leftover backup is authoritative until
# restoration succeeds; never overwrite it with the temporary campaign config.
restore_config_backup() {
    local backup="$1" config="$2" staging
    [ -f "$backup" ] || return 0
    staging=$(mktemp "${config}.restore.XXXXXX") || {
        echo "REFUSING: could not stage $config; recovery retained at $backup" >&2
        return 1
    }
    if command cp -f "$backup" "$staging" && command mv -f "$staging" "$config"; then
        command rm -f "$backup"
        return $?
    fi
    command rm -f "$staging"
    echo "REFUSING: could not restore $config; recovery retained at $backup" >&2
    return 1
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
