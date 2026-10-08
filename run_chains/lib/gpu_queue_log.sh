# Shared queue record retention; the separate lock inode never rotates.
get_queue_active_start() {
    awk '
        function pid(record, fields, count) {
            count=split(record, fields, / owner_pid=/)
            return count > 1 && fields[count] ~ /^[0-9]+$/ ? fields[count] : ""
        }
        function body(record) {
            sub(/^[^[:space:]]+[[:space:]]+[^[:space:]]+[[:space:]]+/, "", record)
            sub(/ owner_pid=[0-9]+$/, "", record)
            return record
        }
        $2 == "START" {line=$0; owner=pid($0); name=body($0)}
        $2 ~ /^(OK|FAILED|REFUSED|NO_VRAM|NO_LLM|KILLED|LOCK_FAILED|PENDING_FAILED|INTERRUPTED|STOPPED)$/ {
            event_owner=pid($0)
            if (owner != "" && event_owner != "") {
                if (owner == event_owner) line=""
            } else {
                event_name=body($0)
                if (event_name == name || (substr(event_name,1,length(name)) == name &&
                    substr(event_name,length(name)+1) ~ /^ (rc=[0-9]+( .*)?|\(.*\))$/)) line=""
            }
        }
        END {if (line != "") print line}' "$QLOG"
}

append_queue_log() (
    local limit=8388608 size record active='' bytes index
    record="$*"
    # LC_ALL=C makes Bash string length a byte count, including UTF-8 names.
    export LC_ALL=C
    trap 'code=$?; if [ "$code" -ne 0 ]; then echo "gpu_job: queue log update failed: $QLOG" >&2; fi' EXIT
    bytes=$((${#record} + 1))
    if ! {
        exec 8>> "$QLOG.lock" && flock -x 8 &&
        { [ ! -e "$QLOG" ] || [ -f "$QLOG" ]; } &&
        [ "$bytes" -le "$limit" ];
    }; then
        echo "gpu_job: cannot write queue log $QLOG: $record" >&2
        exit 8
    fi
    size=0
    if [ -e "$QLOG" ]; then
        size=$(stat -Lc %s -- "$QLOG") || exit 8
    fi
    if [ "$((size + bytes))" -gt "$limit" ]; then
        active=$(get_queue_active_start) || exit 8
        # Keep status evidence even if a running job outlives all archives.
        [ "$(( ${#active} + 1 + bytes ))" -le "$limit" ] || exit 8
        for ((index=1; index<=3; index++)); do
            { [ ! -e "$QLOG.$index" ] || [ -f "$QLOG.$index" ]; } || exit 8
        done
        for ((index=3; index>1; index--)); do
            if [ -e "$QLOG.$((index - 1))" ]; then
                mv -fT -- "$QLOG.$((index - 1))" "$QLOG.$index" || exit 8
            fi
        done
        mv -fT -- "$QLOG" "$QLOG.1" || exit 8
        if [ -n "$active" ]; then
            printf '%s\n' "$active" > "$QLOG" || exit 8
        fi
    fi
    if ! printf '%s\n' "$record" >> "$QLOG"; then
        echo "gpu_job: cannot write queue log $QLOG: $record" >&2
        exit 8
    fi
)

get_logged_queue_job() (
    exec 8>> "$QLOG.lock" && flock -s 8 || return 1
    [ -e "$QLOG" ] || return 0
    get_queue_active_start | sed -e 's/^[^ ]* START    //' -e 's/ owner_pid=[0-9]*$//'
)
