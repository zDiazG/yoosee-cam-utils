#!/bin/sh
set -eu
case "${1:---dry-run}" in
    --dry-run) printf '%s\n' 'Requires Python >=3.11, ffmpeg and ffprobe. No system packages or services are changed.' ;;
    --check)
        python3 -c 'import sys,tomllib; assert sys.version_info >= (3,11)'
        command -v ffmpeg >/dev/null
        command -v ffprobe >/dev/null
        printf '%s\n' 'Requirements available.' ;;
    *) printf '%s\n' 'Usage: install.sh [--dry-run|--check]' >&2; exit 2 ;;
esac
