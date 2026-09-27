#!/bin/bash
# `crontab` for a container without cron: the agent, healthcheck.py and
# stop_strategy.py keep using crontab -l / crontab - as on a normal Linux box,
# while the entries live in one file that supercronic (-inotify) runs.
# Writes are validated with `supercronic -test` and done in place (same inode)
# so the inotify watch keeps firing.
set -euo pipefail
F="${BLAVE_CRONTAB:-/opt/blave-agent/data/crontab}"

install_from() {
    tmp="$(mktemp)"
    trap 'rm -f "$tmp"' EXIT
    cat "$1" > "$tmp"
    if ! supercronic -test "$tmp" >/dev/null 2>&1; then
        echo "crontab: invalid crontab, not installed:" >&2
        supercronic -test "$tmp" >&2 || true
        exit 1
    fi
    cat "$tmp" > "$F"
}

# drop `-u <user>`: single-user container
if [ "${1:-}" = "-u" ]; then shift 2; fi

case "${1:--}" in
    -l)
        if [ -s "$F" ]; then cat "$F"; else echo "no crontab for $(id -un)" >&2; exit 1; fi ;;
    -r)
        : > "$F" ;;
    -e)
        echo "crontab: -e is not supported here; use: crontab -l > f; edit f; crontab f" >&2; exit 1 ;;
    -)
        install_from /dev/stdin ;;
    -*)
        echo "crontab: unsupported option $1" >&2; exit 1 ;;
    *)
        install_from "$1" ;;
esac
