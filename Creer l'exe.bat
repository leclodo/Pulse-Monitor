@echo off
title Pulse Monitor - creation de l'exe portable et de l'installeur
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py") || (set "PY=python")
for /f "usebackq delims=" %%v in (`%PY% -c "import re;print(re.search(r'APP_VERSION = .([0-9.]+).', open('pulse_monitor.py', encoding='utf-8').read()).group(1))"`) do set "VER=%%v"
if "%VER%"=="" goto :err
echo Pulse Monitor v%VER%
echo.

echo [1/4] Outils necessaires (une seule fois)...
%PY% -m pip install --user --upgrade --quiet pyinstaller psutil pywebview pystray pillow
if errorlevel 1 goto :err

echo [2/4] Creation de Pulse Monitor.exe (1 a 2 minutes)...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
%PY% -m PyInstaller --noconfirm --clean --log-level WARN --windowed --name "Pulse Monitor" --icon "assets\icon.ico" ^
  --add-data "web;web" --add-data "assets;assets" ^
  --collect-all webview --hidden-import pystray._win32 ^
  pulse_monitor.py
if errorlevel 1 goto :err

echo [3/4] Version portable (zip)...
if not exist release mkdir release
echo Version portable : les reglages et l'historique restent dans ce dossier.> "dist\Pulse Monitor\portable.txt"
if exist "release\Pulse-Monitor-v%VER%-portable.zip" del "release\Pulse-Monitor-v%VER%-portable.zip"
powershell -NoProfile -Command "Compress-Archive -Path 'dist\Pulse Monitor' -DestinationPath 'release\Pulse-Monitor-v%VER%-portable.zip'"
if errorlevel 1 goto :err
del "dist\Pulse Monitor\portable.txt"

echo [4/4] Installeur (Setup.exe)...
set "ISCC="
for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set "ISCC=%%~p"
if "%ISCC%"=="" (
    echo Inno Setup n'est pas installe : installation...
    winget install --id JRSoftware.InnoSetup -e --silent --accept-package-agreements --accept-source-agreements
    for %%p in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if exist %%p set "ISCC=%%~p"
)
if "%ISCC%"=="" (
    echo.
    echo Impossible de trouver Inno Setup. Installe-le depuis https://jrsoftware.org/isdl.php
    echo puis relance ce fichier. La version portable est deja prete dans "release".
    goto :clean
)
"%ISCC%" /Q /DMyAppVersion=%VER% installer.iss
if errorlevel 1 goto :err

:clean
rem on ne garde que le resultat final (dossier release)
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist "Pulse Monitor.spec" del "Pulse Monitor.spec"
if exist __pycache__ rmdir /s /q __pycache__

echo.
echo ============================================================
echo  Termine ! Dans le dossier "release" :
echo   - Pulse-Monitor-v%VER%-Setup.exe     (installeur)
echo   - Pulse-Monitor-v%VER%-portable.zip  (sans installation)
echo ============================================================
explorer release
pause
exit /b 0

:err
echo.
echo Une erreur est survenue. Envoie-moi le texte ci-dessus.
pause
exit /b 1
