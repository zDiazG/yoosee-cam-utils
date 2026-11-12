#!/bin/sh
set -eu

hint_install() {
    id=""; like=""
    if [ -r /etc/os-release ]; then
        id=$(. /etc/os-release; printf '%s' "${ID:-}")
        like=$(. /etc/os-release; printf '%s' "${ID_LIKE:-}")
    fi
    case " $id $like " in
        *" debian "*|*" ubuntu "*) printf '%s\n' 'sudo apt install python3 ffmpeg openssl' ;;
        *" arch "*) printf '%s\n' 'sudo pacman -S python ffmpeg openssl' ;;
        *) return 1 ;;
    esac
}

check() {
    missing=""
    py_ok=1
    if ! command -v python3 >/dev/null 2>&1; then
        missing="$missing python3"; py_ok=0
    elif ! python3 -c 'import sys,tomllib; assert sys.version_info >= (3,11)' >/dev/null 2>&1; then
        py_ok=0
        printf '%s\n' 'Python 3.11 or newer is required (found '"$(python3 -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo unknown)"').' >&2
        printf '%s\n' 'Ubuntu 22.04 ships Python 3.10; use Ubuntu 24.04+, Debian 12+ or Arch.' >&2
    fi
    for tool in ffmpeg ffprobe openssl; do
        command -v "$tool" >/dev/null 2>&1 || missing="$missing $tool"
    done
    if [ -n "$missing" ] || [ "$py_ok" -eq 0 ]; then
        [ -z "$missing" ] || printf '%s\n' "Missing:$missing" >&2
        if cmd=$(hint_install); then
            printf '%s\n' "Install with: $cmd" >&2
        else
            printf '%s\n' 'Unknown distribution: install Python 3.11+, ffmpeg (with ffprobe) and openssl.' >&2
        fi
        exit 1
    fi
    printf '%s\n' 'Requirements available.'
}

case "${1:---dry-run}" in
    --dry-run) printf '%s\n' 'Requires Python >=3.11, ffmpeg, ffprobe and openssl. No system packages or services are changed.' ;;
    --check) check ;;
    *) printf '%s\n' 'Usage: install.sh [--dry-run|--check]' >&2; exit 2 ;;
esac
