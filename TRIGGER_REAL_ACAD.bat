@echo off
rem ===========================================================================
rem  TRIGGER_REAL_ACAD.bat - real AutoCAD verification (WINDOWS ONLY)
rem
rem  Everything in this repository was verified on Linux with unit tests and
rem  headless Qt probes. Three things can only be proven on a machine with
rem  AutoCAD Core Console installed:
rem
rem    1. FULL RUN      - scan -> destination -> clone -> discovery -> plan ->
rem                       writer -> validator -> dry run -> publish -> PDF
rem    2. CANCEL/RESUME - cancel in the middle, resume, confirm nothing is
rem                       redone and no half-written file is accepted
rem    3. PDF SIGNATURE - change the plotter and confirm the PDF stage replots
rem
rem  Usage:
rem    TRIGGER_REAL_ACAD.bat                 (interactive checklist)
rem    TRIGGER_REAL_ACAD.bat check           (prerequisites + tests only)
rem    TRIGGER_REAL_ACAD.bat full            (preflight, then open the app)
rem
rem  This script never touches your source drawings. The app stages copies
rem  under %%TEMP%%\GNI and writes DWGs with QSAVE only.
rem ===========================================================================

setlocal EnableExtensions
cd /d "%~dp0"
set "MODE=%~1"
if "%MODE%"=="" set "MODE=interactive"

title GammaNederwiek - Real AutoCAD Verification

set "LOG_DIR=%CD%\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "STAMP=%DATE:~-4%%DATE:~3,2%%DATE:~0,2%_%TIME:~0,2%%TIME:~3,2%%TIME:~6,2%"
set "STAMP=%STAMP: =0%"
set "LOG=%LOG_DIR%\real_acad_%STAMP%.txt"

echo ============================================================
echo  GammaNederwiek - real AutoCAD verification
echo  Log: %LOG%
echo ============================================================
echo.
echo  GammaNederwiek - verifikasi nyata dengan AutoCAD
echo  Semua langkah dicatat ke: %LOG%
echo.

call :preflight
if errorlevel 1 goto :failed

if /i "%MODE%"=="check" goto :done

call :run_tests
if errorlevel 1 goto :failed

echo.
echo ============================================================
echo  PREFLIGHT OK - prerequisites and test suite are green
echo ============================================================
echo.
echo  The three scenarios below need a human watching the GUI and
echo  AutoCAD. Each one is scripted as a checklist; results are
echo  recorded in the log file.
echo.
echo  [1] FULL RUN
echo      - Setup tab: pick source, destination, template settings
echo      - Workers: 1 (deterministic baseline)
echo      - Click the primary button and let the cascade run
echo      - Expect: every pipeline row becomes DONE, then
echo        "Publish N validated pair(s)" appears
echo      - Publish, then Publish PDFs
echo      - PASS = no stage shows "stalled", publish report has all
echo        pairs PUBLISHED, and every PDF opens in a viewer
echo.
echo  [2] CANCEL AND RESUME
echo      - Start the writer (or PDF) stage on a batch of 20+ drawings
echo      - Click Cancel halfway, close the app
echo      - Reopen, run the same stage again
echo      - Expect: it resumes; already-done drawings are skipped and
echo        a cancelled report reads as "incomplete", never DONE
echo      - PASS = no drawing is written twice and no report claims
echo        "complete" for a cancelled sweep
echo.
echo  [3] PDF SIGNATURE
echo      - Finish a PDF run
echo      - Edit revision\pdf_settings.json: change plotter or
echo        preferred_media_names, or plot_rotation
echo      - Run Publish PDFs again
echo      - PASS = every pair is replotted (not PDF_UNCHANGED)
echo      - Change it back, run again
echo      - PASS = every pair is now PDF_UNCHANGED
echo.
echo  Record the outcome per scenario (PASS / FAIL + note) in the log.
echo.

if /i "%MODE%"=="full" goto :launch

echo  Launching the application for the manual checklist...
goto :launch

:launch
set "APP_ENTRY=app_modern.py"
if not exist "%APP_ENTRY%" set "APP_ENTRY=app.py"
echo.
echo [INFO] Starting %APP_ENTRY% (Ctrl+C here only stops the launcher).
echo.
python "%APP_ENTRY%"
set "APP_EXIT=%ERRORLEVEL%"
echo.
echo [INFO] Application exited with code %APP_EXIT%.
>>"%LOG%" echo [%DATE% %TIME%] app exit code = %APP_EXIT%
if not "%APP_EXIT%"=="0" goto :failed

:done
echo.
echo [DONE] Log written to: %LOG%
echo [DONE] Remember to copy the log and the bundle's _GNI_REPORTS folder
echo        back into the project so the results can be reviewed.
echo.
pause
exit /b 0

:failed
echo.
echo [FAILED] See %LOG%
echo.
pause
exit /b 1

rem ---------------------------------------------------------------------------
rem  Preflight: every item here caused a confusing failure in the past.
rem ---------------------------------------------------------------------------
:preflight
>>"%LOG%" echo ============================================================
>>"%LOG%" echo GammaNederwiek real AutoCAD verification - %DATE% %TIME%
>>"%LOG%" echo ============================================================

echo [1/4] Windows and Python...
>>"%LOG%" echo [1/4] Windows and Python
if not exist ".venv\Scripts\python.exe" (
    echo        [ERROR] .venv is missing. Run run_app.bat once first.
    >>"%LOG%" echo        ERROR: .venv missing
    exit /b 1
)
".venv\Scripts\python.exe" --version
>>"%LOG%" ".venv\Scripts\python.exe" --version 2>&1

echo [2/4] accoreconsole.exe...
>>"%LOG%" echo [2/4] accoreconsole.exe
set "ACCORE="
for /f "delims=" %%I in ('where accoreconsole.exe 2^>nul') do (
    if not defined ACCORE set "ACCORE=%%I"
)
if not defined ACCORE if exist "C:\Program Files\Autodesk\AutoCAD 2024\accoreconsole.exe" set "ACCORE=C:\Program Files\Autodesk\AutoCAD 2024\accoreconsole.exe"
if not defined ACCORE if exist "C:\Program Files\Autodesk\AutoCAD 2023\accoreconsole.exe" set "ACCORE=C:\Program Files\Autodesk\AutoCAD 2023\accoreconsole.exe"
if not defined ACCORE if exist "C:\Program Files\Autodesk\AutoCAD 2022\accoreconsole.exe" set "ACCORE=C:\Program Files\Autodesk\AutoCAD 2022\accoreconsole.exe"
if not defined ACCORE if exist "C:\Program Files\Autodesk\AutoCAD 2021\accoreconsole.exe" set "ACCORE=C:\Program Files\Autodesk\AutoCAD 2021\accoreconsole.exe"
if not defined ACCORE if exist "C:\Program Files\Autodesk\AutoCAD 2020\accoreconsole.exe" set "ACCORE=C:\Program Files\Autodesk\AutoCAD 2020\accoreconsole.exe"
if defined ACCORE goto :accore_ok
echo        [WARN] accoreconsole.exe not found on PATH or in the
echo               default Autodesk folders.
echo               The app can still start, but every drawing stage
echo               will fail. Set the path in config\settings.json.
>>"%LOG%" echo        WARN: accoreconsole.exe not found (will fail at runtime)
goto :accore_done
:accore_ok
echo        [OK] %ACCORE%
>>"%LOG%" echo        OK: %ACCORE%
:accore_done

echo [3/4] Reference images...
>>"%LOG%" echo [3/4] Reference images
set "HAVE_PNG="
for %%F in ("resources\reference_images\*.png" "resources\reference_images\*.PNG") do (
    if exist "%%~F" set "HAVE_PNG=1"
)
if defined HAVE_PNG goto :png_ok
echo        [ERROR] No PNG found in resources\reference_images.
echo               Both reference images must be present or every
echo               CAD folder copy will fail.
>>"%LOG%" echo        ERROR: reference images missing
exit /b 1
:png_ok
echo        [OK] reference images present
>>"%LOG%" echo        OK: reference images present

echo [4/4] LISP files...
>>"%LOG%" echo [4/4] LISP files
for %%L in (title_block_discovery stable_external_writer reopen_validator pdf_modelspace_plotter) do (
    if not exist "lisp\%%L.lsp" (
        echo        [ERROR] lisp\%%L.lsp is missing.
        >>"%LOG%" echo        ERROR: lisp\%%L.lsp missing
        exit /b 1
    )
)
echo        [OK] all live LISP files present
>>"%LOG%" echo        OK: LISP files present

echo.
echo Preflight finished.
>>"%LOG%" echo.
exit /b 0

rem ---------------------------------------------------------------------------
rem  Unit tests - no AutoCAD needed, but they guard the pipeline contracts.
rem ---------------------------------------------------------------------------
:run_tests
echo.
echo [TESTS] Running the offline suite (no AutoCAD required)...
>>"%LOG%" echo [TESTS] unittest discover
".venv\Scripts\python.exe" -m unittest discover -t . -s tests -v >>"%LOG%" 2>&1
if errorlevel 1 (
    echo        [ERROR] Test suite failed. See the log.
    >>"%LOG%" echo        TESTS FAILED
    exit /b 1
)
".venv\Scripts\python.exe" -m unittest discover -t . -s tests 2>&1 | findstr /C:"Ran " /C:"OK"
echo        [OK] offline suite green
>>"%LOG%" echo        TESTS OK
exit /b 0
