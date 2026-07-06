@echo off
rem Doppelklick-Start des Rechnungsassistenten (Windows)
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py) || (set PY=python)
if not exist .venv %PY% -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt
.venv\Scripts\python -m invoice_assistant web
pause
