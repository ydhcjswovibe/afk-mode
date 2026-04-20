#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_DIR="${AFK_MODE_PLUGIN_DIR:-${HOME}/plugins/afk-mode}"

mkdir -p "${TARGET_DIR}"

rsync -a --delete \
  --exclude .git \
  --exclude __pycache__ \
  --exclude '*.pyc' \
  "${REPO_ROOT}/" \
  "${TARGET_DIR}/"

find "${TARGET_DIR}" -type d -name __pycache__ -prune -exec rm -rf {} +
find "${TARGET_DIR}" -type f -name '*.pyc' -delete

chmod +x "${TARGET_DIR}/afk-mode"

echo "Published AFK Mode to ${TARGET_DIR}"
