@echo off
:: ============================================================
:: run.bat — Start the AI Meeting Intelligence System
:: ============================================================

echo.
echo ============================================================
echo   AI Meeting Intelligence System — Starting
echo ============================================================
echo.

:: Navigate to project root (parent of scripts/)
cd /d "%~dp0.."

:: Load environment variables from config\.env
if exist "config\.env" (
    echo Loading environment from config\.env ...
    for /f "usebackq tokens=1,2 delims==" %%a in ("config\.env") do (
        if not "%%a"=="" if not "%%b"=="" (
            :: Skip comment lines
            set "first=%%a"
            if not "!first:~0,1!"=="#" (
                set %%a=%%b
            )
        )
    )
    echo [OK] Environment loaded.
) else (
    echo [WARNING] config\.env not found. Using system environment variables.
)

:: Check if Ollama is running
curl -s http://localhost:11434/api/tags > nul 2>&1
if errorlevel 1 (
    echo Ollama not running. Starting ollama serve in background...
    start /min "" ollama serve
    :: Wait for Ollama to initialize
    timeout /t 8 /nobreak > nul
    echo [OK] Ollama started.
) else (
    echo [OK] Ollama already running.
)

echo.
echo Starting file watcher...
echo Press Ctrl+C to stop.
echo.

python src\main.py --config config\settings.yaml

pause
