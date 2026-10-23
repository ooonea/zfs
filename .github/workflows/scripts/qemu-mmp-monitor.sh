#!/usr/bin/env bash
set -u
exec > /var/tmp/mmp-diagnostic.txt 2>&1
for ((tick=0; tick<150; tick++)); do
    test -e /var/tmp/mmp-diagnostic.done && exit 0
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
                age=$(ps -o etimes= -p "$pid")
                age=${age//[[:space:]]/}
                if [[ "$program" = zhack && "$age" =~ ^[0-9]+$ ]] &&
                    ((age >= 300)) &&
                    test ! -e "/var/tmp/mmp-diagnostic-$pid.seen"; then
                    touch "/var/tmp/mmp-diagnostic-$pid.seen"
                    printf 'PTRACE stalled zhack PID %s age %s\n' "$pid" "$age"
                    gdb -batch -ex 'set pagination off' \
                        -ex 'set print frame-arguments none' \
                        -ex 'thread apply all bt' -p "$pid"
                fi
            done
        done
    fi
    sleep 10
done
