@echo off
setlocal
set "REPO=D:\DSH\repos\ai-employment-monitor"
set "PY=C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe"
cd /d "%REPO%"
powershell -NoProfile -WindowStyle Hidden -Command "if (-not (Get-NetTCPConnection -LocalPort 8792 -State Listen -ErrorAction SilentlyContinue)) { Start-Process '%PY%' -ArgumentList '-B','tools\ai_server.py' -WorkingDirectory '%REPO%' -WindowStyle Hidden }"
timeout /t 2 >nul
start "" "http://127.0.0.1:8792/"
endlocal
