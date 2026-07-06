#!/usr/bin/env bash
# Doppelklick-/Terminal-Start des Rechnungsassistenten (macOS/Linux)
set -e
cd "$(dirname "$0")"
[ -d .venv ] || python3 -m venv .venv
while true; do
  .venv/bin/python -m pip install -q -r requirements.txt
  set +e
  .venv/bin/python -m invoice_assistant web
  code=$?
  set -e
  if [ "$code" != "42" ]; then
    break
  fi
  echo "Update installiert - der Assistent startet neu ..."
  export IA_RESTARTED=1
done
