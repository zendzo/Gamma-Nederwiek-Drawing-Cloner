@echo off
rem ---------------------------------------------------------------
rem One-click repack.  Forwards every argument to the build script:
rem   repack.bat                      release build (VERSION file)
rem   repack.bat -Version 0.4C.18     release build with a label
rem   repack.bat -Fast                quick unsigned smoke build
rem ---------------------------------------------------------------
setlocal EnableExtensions
cd /d "%~dp0"

echo ============================================================
echo GammaNederwiek - portable repack
echo ============================================================
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0packaging\build_portable.ps1" %*
set "BUILD_EXIT=%ERRORLEVEL%"

echo.
if not "%BUILD_EXIT%"=="0" (
    echo [ERROR] Repack failed with exit code %BUILD_EXIT%.
) else (
    echo [OK] Repack finished.
)
echo.
pause
exit /b %BUILD_EXIT%
