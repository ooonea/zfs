#!/usr/bin/env bash
set -u
exec > /var/tmp/mmp-diagnostic.txt 2>&1
for ((tick=0; ; tick++)); do
    test -e /var/tmp/mmp-diagnostic.done && exit 0
    for pid in $(pgrep -x mmp_concurrent_); do
        date -u
        printf 'TEST SHELL %s\n' "$pid"
        cat "/proc/$pid/stat" "/proc/$pid/wchan" "/proc/$pid/syscall" \
            "/proc/$pid/task/$pid/children"
        children=()
        read -r -a children < "/proc/$pid/task/$pid/children"
        for child in "${children[@]}"; do
            printf 'TEST CHILD %s\n' "$child"
            cat "/proc/$child/stat" "/proc/$child/wchan"
        done
    done
    if ((tick >= 6 && tick % 6 == 0)); then
        date -u
        ps -eo pid,ppid,stat,wchan:40,comm
        for logfile in /var/tmp/mmp/zhack.log.*; do
            if test -f "$logfile"; then
                printf '%s\n' "$logfile"
                cat "$logfile"
            fi
        done
        for program in zhack zpool; do
            for pid in $(pgrep -x "$program"); do
                for task in /proc/"$pid"/task/*; do
                    printf '%s\n' "$task"
                    cat "$task/comm" "$task/wchan" "$task/stack" "$task/syscall"
                done
            done
        done
    fi
    sleep 10
done
