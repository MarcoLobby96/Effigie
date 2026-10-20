@echo off
rem Ricrea dist\Effigie (cartella portatile) e dist\Effigie-Setup-<versione>.exe
rem con il codice attuale. Gli esempi vengono da esempi\ (solo quelli con FONTE.txt).
cd /d "%~dp0.."
if not exist ".venv\Scripts\python.exe" (
    echo Prima avvia "Avvia Effigie.bat" una volta: serve l'ambiente .venv.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" "scripts\build_dist.py" %*
if errorlevel 1 (
    echo.
    echo Creazione dell'installer NON riuscita: leggi i messaggi qui sopra.
) else (
    echo.
    echo Installer pronto nella cartella dist\
    explorer "dist"
)
pause
