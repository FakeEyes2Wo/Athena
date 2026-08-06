@echo off
setlocal enabledelayedexpansion

set ROOT=%~dp0..
pushd %ROOT%

echo === Athena GUI Dev Mode ===
echo Root: %CD%

echo.
echo [1/2] Checking Python backend...
uv run python -c "from gui_gateway.__main__ import start_server; print('Python: OK')" 2>nul
if %ERRORLEVEL% neq 0 (
    echo Python: FAILED
    echo Run: uv run pytest tests/test_gui_gateway_*.py -v
    popd
    exit /b 1
)
echo Python: OK

echo [2/2] Starting Tauri dev server...
echo   - Python backend auto-spawns on first request
echo   - Frontend at http://localhost:1421
echo   - Ctrl+C to stop
echo.

cd athena-gui
call npm run tauri dev

popd
