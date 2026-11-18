@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title Snapgent Bridge
cd /d "%~dp0"

if not exist "%~dp0logs" mkdir "%~dp0logs" >nul 2>nul
set "LOGFILE=%~dp0logs\start.log"

set "PY=C:\Users\Brokenlink\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PY%" (
    echo.
    echo   ERROR: python.exe not found. Install Python 3.10+ from https://www.python.org/
    echo   and make sure it is on PATH.
    echo.
    pause
    exit /b 1
)

echo.
echo   Configuring VS Code MCP and detecting Roblox Studio...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup-config.ps1"
if errorlevel 1 (
    echo.
    echo   ERROR: Could not configure the VS Code MCP connection.
    echo   Check the message above, then run start.bat again.
    echo.
    pause
    exit /b 1
)

if exist "%~dp0start-effectcraft.ps1" (
    echo.
    echo   Checking EffectCraft control channel...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-effectcraft.ps1"
)

REM Register the snapgent MCP servers in Zed: workspace tools plus the
REM agent-to-agent bridge. Missing Zed or a missing settings file is not
REM fatal - the bridge still serves VS Code and the browser extension.
echo.
echo   Registering Zed MCP servers ^(workspace tools + A2A agent^)...
"%PY%" "%~dp0scripts\register_zed_mcp.py"
if errorlevel 1 (
    echo   NOTE: Zed registration skipped. Run scripts\register_zed_mcp.py after
    echo         installing Zed to enable the Zed agent.
)

set "EXTID="
for /f "usebackq delims=" %%i in (`"%PY%" "%~dp0scripts\check_bridge_health.py" --extension-id`) do set "EXTID=%%i"

REM Free the bridge port if a previous instance is still listening.
set "OLDPID="
for /f "tokens=5" %%a in ('netstat -aon ^| findstr :17613 ^| findstr LISTENING 2^>nul') do (
    set "OLDPID=%%a"
)
if defined OLDPID (
    echo   Replacing previous bridge ^(pid !OLDPID!^) on port 17613...
    taskkill /F /T /PID !OLDPID! >nul 2>nul
    timeout /t 1 /nobreak >nul
)

echo.
echo   +--------------------------------------------------+
echo   ^| Snapgent is now running.                         ^|
echo   ^| Keep this window open while you use the agent.   ^|
echo   ^| Minimise it freely - closing it stops the bridge.^|
echo   +--------------------------------------------------+
echo.
if defined EXTID (
    echo   Extension id pinned: !EXTID!
    echo   If the panel shows "bridge offline", reload the extension once:
    echo   chrome://extensions -> ArteX-TNT -> Reload
    echo.
)

%PY% "%~dp0bridge.py"
set "BRIDGE_EXIT=%errorlevel%"

echo.
if not "%BRIDGE_EXIT%"=="0" (
    echo   Bridge stopped with ERROR code %BRIDGE_EXIT%.
) else (
    echo   Bridge stopped normally.
)
echo   Press any key to close.
pause >nul
exit /b 0
