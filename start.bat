@echo off
REM ===========================================================================
REM  interview-agent - one-click launcher (Windows)
REM
REM  WHY THIS FILE IS ASCII-ONLY
REM  Chinese text in .bat breaks cmd parsing when the code page differs
REM  (we hit "'ARGET' is not recognized" from a REM line before).
REM  All Chinese docs live in README.md.
REM ===========================================================================

setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ============================================================
echo   interview-agent
echo ============================================================
echo.

REM ---- 1) host services: Ollama + model gateway port forward --------
echo [1/4] Host services (Ollama + model gateway)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\host_services.ps1"
if errorlevel 1 (
    echo   WARN: host services reported problems, continuing anyway
)
echo.

REM ---- 2) database + backend inside WSL -----------------------------
echo [2/4] Database + backend (WSL)
if not exist "%SystemRoot%\System32\wsl.exe" (
    echo   ERROR: WSL not found. Install WSL or run the backend manually.
    goto :frontend
)

REM Convert the Windows path to a WSL path so the command works regardless
REM of where the project is checked out.
for /f "usebackq delims=" %%i in (`wsl -d Ubuntu -e wslpath -a "%~dp0"`) do set "WSLPATH=%%i"

wsl -d Ubuntu -e bash -lc "cd '%WSLPATH%' && bash start.sh api"
echo.

:frontend
REM ---- 3) frontend in its own window ---------------------------------
echo [3/4] Frontend (new window)
if not exist "frontend\node_modules" (
    echo   Installing frontend dependencies, this takes a minute...
    pushd frontend
    call npm install
    popd
)
start "interview-agent web" cmd /k "cd /d "%~dp0frontend" && npm run dev"
echo.

REM ---- 4) open browser -----------------------------------------------
echo [4/4] Opening browser...
timeout /t 6 /nobreak >nul
start "" "http://localhost:5173"
echo.

echo ============================================================
echo   Frontend  http://localhost:5173
echo   API docs  http://localhost:8080/docs
echo ============================================================
echo.
echo   Stop:     wsl -d Ubuntu -e bash -lc "cd '%WSLPATH%' && bash start.sh stop"
echo   Status:   wsl -d Ubuntu -e bash -lc "cd '%WSLPATH%' && bash start.sh status"
echo   Health:   wsl -d Ubuntu -e bash -lc "cd '%WSLPATH%' && bash start.sh check"
echo.
echo Press any key to close this window (services keep running).
pause >nul
endlocal
