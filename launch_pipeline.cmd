@echo off
setlocal
set "REPO=D:\DSH\repos\ai-employment-monitor"
set "PY=C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe"
cd /d "%REPO%"
"%PY%" -B tools\run_pipeline_gui.py
endlocal
