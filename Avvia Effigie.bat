@echo off
rem Avvio con doppio clic. Se l'ambiente e' pronto apre subito Effigie; altrimenti
rem installa da solo quello che manca (Python, librerie, COLMAP, OpenMVS, ffmpeg).
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" if exist ".venv\.installato" (
    start "" ".venv\Scripts\pythonw.exe" "src\app.py"
    exit /b
)

rem --- Python: se manca lo installa con winget (incluso in Windows 10/11)
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo Python non trovato: lo installo ^(Python 3.12^)...
    winget install --id Python.Python.3.12 -e --silent --scope user --accept-package-agreements --accept-source-agreements
    if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
        set "PY="%LOCALAPPDATA%\Programs\Python\Python312\python.exe""
    ) else (
        echo.
        echo Non sono riuscito a installare Python. Installalo da https://www.python.org
        echo ^(spunta "Add python.exe to PATH"^) e riavvia questo file.
        pause
        exit /b 1
    )
)

echo Preparo Effigie ^(solo la prima volta: alcuni minuti^)...
%PY% "scripts\bootstrap.py"
if errorlevel 1 (
    echo.
    echo Installazione non riuscita: leggi i messaggi qui sopra.
    pause
)
