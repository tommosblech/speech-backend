#!/usr/bin/env bash
# Doppelklick-/Terminal-Start des Rechnungsassistenten (macOS/Linux)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install -q -r requirements.txt
.venv/bin/python -m invoice_assistant web
