#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
exec pwsh -NoProfile -File ./run_all.ps1 "${1:-resume}"
