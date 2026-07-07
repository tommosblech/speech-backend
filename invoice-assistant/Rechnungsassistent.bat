@echo off
setlocal
rem Doppelklick-Start des Rechnungsassistenten (Windows)
rem Mit Parameter /leise laeuft er ohne Pausen (fuer den unsichtbaren
rem Start ueber RechnungsassistentLeise.vbs).
if /i "%~1"=="/leise" set "QUIET=1"
cd /d "%~dp0"

if not exist "invoice_assistant\cli.py" goto :noextract

where py >nul 2>nul
if %errorlevel%==0 (set "PY=py") else (set "PY=python")
%PY% --version >nul 2>nul
if errorlevel 1 goto :nopython

if exist .venv goto :install
echo Richte den Assistenten ein - beim ersten Mal dauert das ein paar Minuten ...
%PY% -m venv .venv
if errorlevel 1 goto :venvfail

:install
echo Pruefe und installiere Bausteine ...
.venv\Scripts\python -m pip install --disable-pip-version-check -q -r requirements.txt >install.log 2>&1
if errorlevel 1 goto :pipfail

echo Starte den Assistenten - dieses Fenster bitte offen lassen.
.venv\Scripts\python -m invoice_assistant web
if %errorlevel%==42 (
  echo.
  echo Update installiert - der Assistent startet neu ...
  set "IA_RESTARTED=1"
  goto :install
)
echo.
echo Der Assistent wurde beendet.
if not defined QUIET pause
exit /b 0

:noextract
echo FEHLER: Programmdateien nicht gefunden.
echo.
echo Wahrscheinlich wurde die ZIP-Datei noch nicht entpackt.
echo Bitte: Rechtsklick auf die ZIP-Datei, "Alle extrahieren...",
echo dann die Rechnungsassistent.bat aus dem ENTPACKTEN Ordner starten.
if not defined QUIET pause
exit /b 1

:nopython
echo FEHLER: Python wurde nicht gefunden.
echo.
echo Bitte Python von https://python.org installieren und dabei das
echo Haekchen "Add python.exe to PATH" setzen.
if not defined QUIET pause
exit /b 1

:venvfail
echo FEHLER: Die Python-Umgebung (.venv) konnte nicht angelegt werden.
echo Bitte den Text oberhalb dieser Meldung an Claude schicken.
if not defined QUIET pause
exit /b 1

:pipfail
echo FEHLER bei der Installation der Bausteine. Details:
echo ------------------------------------------------------
type install.log
echo ------------------------------------------------------
echo Bitte diesen Text (Foto genuegt) an Claude schicken.
if not defined QUIET pause
exit /b 1
