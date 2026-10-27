@echo off
setlocal EnableExtensions
cd /d "%~dp0"

title GammaNederwiek - uv Launcher

echo ============================================================
echo GammaNederwiek - uv Environment Launcher
echo ============================================================
echo.

rem ------------------------------------------------------------
rem 1. Find uv. Install it with the official Windows installer
rem    when uv is not available on PATH.
rem ------------------------------------------------------------
set "UV_EXE="
for /f "delims=" %%I in ('where uv.exe 2^>nul') do (
    if not defined UV_EXE set "UV_EXE=%%I"
)

if not defined UV_EXE (
    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
    )
)

if not defined UV_EXE (
    echo [INFO] uv was not found. Installing uv...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$env:UV_NO_MODIFY_PATH='1'; irm https://astral.sh/uv/install.ps1 | iex"

    if errorlevel 1 (
        echo [ERROR] uv installation failed.
        pause
        exit /b 1
    )

    if exist "%USERPROFILE%\.local\bin\uv.exe" (
        set "UV_EXE=%USERPROFILE%\.local\bin\uv.exe"
    )
)

if not defined UV_EXE (
    for /f "delims=" %%I in ('where uv.exe 2^>nul') do (
        if not defined UV_EXE set "UV_EXE=%%I"
    )
)

if not defined UV_EXE (
    echo [ERROR] uv.exe could not be found after installation.
    echo [INFO] Expected location: %USERPROFILE%\.local\bin\uv.exe
    pause
    exit /b 1
)

echo [OK] uv: %UV_EXE%
"%UV_EXE%" --version
if errorlevel 1 (
    echo [ERROR] uv could not be executed.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem 2. Create the local virtual environment with uv.
rem ------------------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [INFO] Creating .venv with uv...
    "%UV_EXE%" venv ".venv"

    if errorlevel 1 (
        echo [ERROR] Failed to create .venv.
        pause
        exit /b 1
    )
)

rem ------------------------------------------------------------
rem 3. Activate the environment in this launcher session.
rem ------------------------------------------------------------
if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] Virtual environment activation script was not found.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
if errorlevel 1 (
    echo [ERROR] Failed to activate .venv.
    pause
    exit /b 1
)

echo [OK] Active environment: %VIRTUAL_ENV%
python --version
if errorlevel 1 (
    echo [ERROR] Python is not available inside .venv.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem 4. Install or update dependencies using uv.
rem ------------------------------------------------------------
if not exist "requirements.txt" (
    echo [ERROR] requirements.txt was not found in:
    echo        %CD%
    pause
    exit /b 1
)

echo.
echo [INFO] Synchronizing requirements with uv...
"%UV_EXE%" pip install --python "%VIRTUAL_ENV%\Scripts\python.exe" -r "requirements.txt"
if errorlevel 1 (
    echo [ERROR] Dependency installation failed.
    pause
    exit /b 1
)

rem ------------------------------------------------------------
rem 5. Start the application using the activated environment.
rem    app_modern.py runs the same Main controller as app.py but adds the
rem    sidebar shell; it falls back to app.py if the file is missing.
rem ------------------------------------------------------------
set "APP_ENTRY=app.py"
if exist "app_modern.py" set "APP_ENTRY=app_modern.py"
if not exist "%APP_ENTRY%" (
    echo [ERROR] %APP_ENTRY% was not found in:
    echo        %CD%
    pause
    exit /b 1
)

echo.
echo [INFO] Starting GammaNederwiek (%APP_ENTRY%)...
python "%APP_ENTRY%"
set "APP_EXIT=%ERRORLEVEL%"

if not "%APP_EXIT%"=="0" (
    echo.
    echo [ERROR] Application exited with code %APP_EXIT%.
    pause
)

exit /b %APP_EXIT%