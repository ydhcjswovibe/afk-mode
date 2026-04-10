#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_DIR="/home/ydhcjswo/plugins/afk-mode"

mkdir -p "${TARGET_DIR}"

rsync -a --delete \
  --exclude .git \
  "${REPO_ROOT}/" \
  "${TARGET_DIR}/"

echo "Published AFK Mode to ${TARGET_DIR}"
