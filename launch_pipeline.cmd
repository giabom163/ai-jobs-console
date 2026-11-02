@echo off
setlocal
chcp 65001 >nul
set "REPO=D:\DSH\repos\ai-employment-monitor"
set "PY=C:\Users\Lenovo\.workbuddy\binaries\python\versions\3.13.12\python.exe"
cd /d "%REPO%"

echo ========================================================
echo [AI 岗位核查台] 启动全自动数据采集与管道处理系统
echo ========================================================
echo.

:: 1. 确保后台 8792 服务正在运行
echo [*] 检查本地 8792 控制台服务...
powershell -NoProfile -WindowStyle Hidden -Command "if (-not (Get-NetTCPConnection -LocalPort 8792 -State Listen -ErrorAction SilentlyContinue)) { Start-Process '%PY%' -ArgumentList '-B','tools\ai_server.py' -WorkingDirectory '%REPO%' -WindowStyle Hidden }"

:: 2. 启动 Boss 直聘平滑分页持续采集引擎
echo.
echo [*] 正在启动 Boss 直聘多页平滑采集引擎 (每页严格间隔 22 秒防风控)...
call ego-browser nodejs -e "await import('file:///D:/DSH/repos/ai-employment-monitor/tools/boss_continuous_collector.mjs')"

:: 3. 运行多源全自动入库与数据管道调度
echo.
echo [*] 正在运行多源数据入库与自动化数据管道...
"%PY%" -B tools\pipeline_daemon.py --once

:: 4. 打开控制台展示大盘最新岗位
echo.
echo [*] 管道执行完毕，正在打开控制台视图...
start "" "http://127.0.0.1:8792/"

echo.
echo [OK] 全流程已顺利完成。按任意键退出控制台窗口。
pause >nul
endlocal
