#!/bin/sh
set -eu
task_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$task_root/.venv/bin/python" -m yandex_music_mcp.server
