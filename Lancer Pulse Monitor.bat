@echo off
title Pulse Monitor
cd /d "%~dp0"

where py >nul 2>nul && (set "PY=py" & set "PYW=pyw") || (set "PY=python" & set "PYW=pythonw")

%PY% -c "import psutil" >nul 2>nul || (
    echo Installation de psutil...
    %PY% -m pip install --user psutil
)
%PY% -c "import webview" >nul 2>nul || (
    echo Installation de pywebview ^(fenetre de l'application^)...
    %PY% -m pip install --user pywebview
)
%PY% -c "import pystray, PIL" >nul 2>nul || (
    echo Installation de pystray et Pillow ^(icone pres de l'horloge^)...
    %PY% -m pip install --user pystray pillow
)

start "" %PYW% "%~dp0pulse_monitor.py"
