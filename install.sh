#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON:-python}"

"${PYTHON_BIN}" -m venv .venv

if [[ "${OS:-}" == "Windows_NT" ]]; then
    source .venv/Scripts/activate
else
    source .venv/bin/activate
fi

pip install --upgrade pip
pip install -r requirements.txt
playwright install chromium
