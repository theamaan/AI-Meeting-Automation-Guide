@echo off
:: ============================================================
:: setup.bat — First-Time Setup Script
:: Run this ONCE to install all dependencies
:: ============================================================

echo.
echo ============================================================
echo   AI Meeting Intelligence System — Setup
echo ============================================================
echo.

:: Check Python
python --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install Python 3.10+ from https://python.org
    pause
    exit /b 1
)
echo [OK] Python found.

:: Check pip
pip --version > nul 2>&1
if errorlevel 1 (
    echo [ERROR] pip not found.
    pause
    exit /b 1
)
echo [OK] pip found.

:: Navigate to project root
cd /d "%~dp0.."

:: Create required directories
echo.
echo Creating directories...
if not exist "data"  mkdir data
if not exist "logs"  mkdir logs
echo [OK] data/ and logs/ created.

:: Install Python packages
echo.
echo Installing Python dependencies...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed. Check requirements.txt.
    pause
    exit /b 1
)
echo [OK] Python packages installed.

:: Copy .env.example to .env if .env doesn't exist
if not exist "config\.env" (
    copy "config\.env.example" "config\.env" > nul
    echo [OK] config\.env created from template. EDIT IT NOW with your secrets.
) else (
    echo [SKIP] config\.env already exists.
)

:: Check Ollama
ollama --version > nul 2>&1
if errorlevel 1 (
    echo.
    echo [WARNING] Ollama not found in PATH.
    echo   Download Ollama from: https://ollama.com
    echo   Then run: ollama pull gpt-oss:120b-cloud
) else (
    echo [OK] Ollama found.
    echo.
    echo Pulling model: gpt-oss:120b-cloud
    echo [NOTE] This requires internet access and ~70GB disk space.
    echo        Press Ctrl+C to skip if you will pull it manually.
    echo.
    pause
    ollama pull gpt-oss:120b-cloud
)

echo.
echo ============================================================
echo   Setup complete!
echo.
echo   Next steps:
echo   1. Edit config\settings.yaml  (set watch_path, recipients)
echo   2. Edit config\.env           (set Teams webhook, email password)
echo   3. Run: scripts\run.bat
echo ============================================================
echo.
pause
