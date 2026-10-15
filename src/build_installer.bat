@echo off
REM ============================================================================
REM FrameGuard Desktop -- Windows Build Script
REM
REM Produces:  Output\FrameGuardInstaller.exe
REM
REM WHY A CLEAN VENV:
REM   Building from a dirty global Python bundles mismatched python3xx.dll
REM   versions and the app crashes with 0xC0000005 (Access Violation).
REM   A clean venv guarantees the bundled DLLs are compatible.
REM
REM Prerequisites on the BUILD machine:
REM   1. Python 3.12 (CI pins 3.12.10 in .python-version)
REM   2. Inno Setup 6.2+ (default install path or on PATH)
REM
REM Usage:
REM   build_installer.bat                full clean build
REM   build_installer.bat --fast         reuse the existing .buildenv
REM   build_installer.bat --skip-tests   skip the pytest gate (not recommended)
REM   build_installer.bat --skip-delta   skip optional external-channel payload
REM ============================================================================

setlocal enabledelayedexpansion
REM Force UTF-8 for Python so paths under non-ASCII folders (the team's Drive
REM folder has a star in its name) don't crash build scripts on print.
set PYTHONUTF8=1
cd /d "%~dp0"

set "FAST=0"
set "SKIP_TESTS=0"
set "SKIP_DELTA=0"
set "CONSTRAINTS=constraints-3.12.txt"
for %%A in (%*) do (
    if /i "%%~A"=="--fast" set "FAST=1"
    if /i "%%~A"=="--skip-tests" set "SKIP_TESTS=1"
    if /i "%%~A"=="--skip-delta" set "SKIP_DELTA=1"
)

echo.
echo  ====================================================
echo   FrameGuard Desktop - Windows Build
echo  ====================================================

REM --- Step 1: Verify Python ---
echo.
echo [1/9] Checking the build environment...
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found on PATH.
    echo        Install Python 3.12 from https://www.python.org/downloads/
    if not defined CI pause
    exit /b 1
)
python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.12 is required for reproducible release builds.
    if not defined CI pause
    exit /b 1
)
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
echo       Python !PYVER!

REM --- Step 2: Clean virtual environment ---
echo.
echo [2/9] Preparing the build environment...
if "!FAST!"=="1" (
    if exist ".buildenv" (
        echo       Reusing existing .buildenv ^(--fast^)
        goto :venv_ready
    )
)
if exist ".buildenv" rmdir /s /q ".buildenv"
python -m venv .buildenv
if errorlevel 1 (
    echo ERROR: Failed to create the virtual environment.
    if not defined CI pause
    exit /b 1
)
echo       Created .buildenv
:venv_ready
call .buildenv\Scripts\activate.bat
set "PY=.buildenv\Scripts\python.exe"

REM Some Python distributions (notably Meta's fb-python) produce a venv whose
REM bundled pip is incomplete - pip.exe is absent and `python -m pip` fails
REM with a vendored-module import error. ensurepip reinstalls a clean copy.
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo       Repairing the virtual environment's pip...
    "%PY%" -m ensurepip --upgrade >nul 2>&1
    "%PY%" -m pip --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Could not bootstrap pip inside .buildenv.
        echo        Try deleting .buildenv and re-running.
        if not defined CI pause
        exit /b 1
    )
)

REM --- Step 3: Dependencies ---
REM Runtime deps come from requirements.txt so the build can never drift from
REM what developers actually run.
echo.
echo [3/9] Installing build dependencies...
if not exist "%CONSTRAINTS%" (
    echo ERROR: Missing %CONSTRAINTS%.
    exit /b 1
)
"%PY%" -m pip install --quiet --upgrade -c "%CONSTRAINTS%" pip setuptools wheel
"%PY%" -m pip install --quiet -c "%CONSTRAINTS%" -r requirements.txt pyinstaller Pillow
if errorlevel 1 (
    echo ERROR: Failed to install dependencies.
    if not defined CI pause
    exit /b 1
)
if "!SKIP_TESTS!"=="0" "%PY%" -m pip install --quiet -c "%CONSTRAINTS%" pytest pytest-timeout
"%PY%" -m pip check
if errorlevel 1 (
    echo ERROR: The build environment has incompatible packages.
    echo        Re-run without --fast to rebuild the virtual environment.
    exit /b 1
)
REM Print the PySide6 version with a direct call, not FOR /F: the nested
REM double quotes
REM double quotes of a -c string inside a FOR command string make cmd
REM execute '"...python.exe" -c "import' as the command and error out.
"%PY%" -c "import PySide6; print('      PySide6    ', PySide6.__version__)"
for /f "tokens=*" %%v in ('"%PY%" -m PyInstaller --version') do echo       PyInstaller %%v

REM --- Step 4: Version validation + build stamp ---
echo.
echo [4/9] Validating version consistency...
"%PY%" validate_version.py
if errorlevel 1 (
    echo.
    echo ERROR: Version numbers disagree across files.
    echo        Fix __init__.py, frameguard.spec, installer.iss and version.json.
    if not defined CI pause
    exit /b 1
)

"%PY%" write_build_stamp.py
if errorlevel 1 (
    echo ERROR: Could not write the build stamp.
    if not defined CI pause
    exit /b 1
)

REM --- Step 5: Tests ---
echo.
echo [5/9] Running the test suite...
if "!SKIP_TESTS!"=="1" (
    echo       SKIPPED ^(--skip-tests^)
) else (
    set "QT_QPA_PLATFORM=windows"
    set "FRAMEGUARD_TEST_MODE=1"
    "%PY%" -c "from PySide6.QtWidgets import QApplication; from PySide6.QtGui import QFontMetrics; from frameguard import design as D; app=QApplication([]); font=D.ui_font(); print('Qt platform=' + app.platformName() + ' font=' + font.family()); assert QFontMetrics(font).inFont('A')"
    if errorlevel 1 (
        echo ERROR: Native Windows Qt cannot rasterize the UI font.
        exit /b 1
    )
    "%PY%" -m pytest
    if errorlevel 1 (
        echo.
        echo ERROR: Tests failed. Fix them before shipping a build.
        if not defined CI pause
        exit /b 1
    )
    "%PY%" tools_dev\startup_smoke.py --executable "%PY%" --entrypoint main.py --artifacts-dir "Output\build-logs\startup-source" --label "source-startup" --qpa windows --timeout 30
    if errorlevel 1 (
        echo ERROR: Source failed the real startup preflight.
        echo        Evidence: Output\build-logs\startup-source
        exit /b 1
    )
    set "QT_QPA_PLATFORM="
    set "FRAMEGUARD_TEST_MODE="
)

REM --- Step 6: Build with PyInstaller ---
echo.
echo [6/9] Building FrameGuard.exe...
if exist "dist" rmdir /s /q "dist"
if exist "build" rmdir /s /q "build"
if not exist "Output\build-logs" mkdir "Output\build-logs"

powershell -NoProfile -ExecutionPolicy Bypass -Command "& '%PY%' -m PyInstaller frameguard.spec --noconfirm --clean 2>&1 | Tee-Object -FilePath 'Output\build-logs\pyinstaller-windows.log'; exit $LASTEXITCODE"
if errorlevel 1 (
    echo.
    echo ERROR: PyInstaller build failed. See Output\build-logs\pyinstaller-windows.log.
    if not defined CI pause
    exit /b 1
)
if exist "build\frameguard\warn-frameguard.txt" (
    copy /y "build\frameguard\warn-frameguard.txt" "Output\build-logs\warn-frameguard-windows.txt" >nul
    "%PY%" tools_dev\check_pyinstaller_warnings.py "build\frameguard\warn-frameguard.txt" --platform win32
    if errorlevel 1 (
        echo ERROR: PyInstaller omitted a required runtime dependency.
        exit /b 1
    )
    findstr /i /c:"missing module named 'frameguard." "build\frameguard\warn-frameguard.txt" >nul
    if not errorlevel 1 (
        echo ERROR: PyInstaller reported a missing FrameGuard module.
        type "build\frameguard\warn-frameguard.txt"
        exit /b 1
    )
)

if not exist "dist\FrameGuard\FrameGuard.exe" (
    echo ERROR: dist\FrameGuard\FrameGuard.exe was not produced.
    if not defined CI pause
    exit /b 1
)
if not exist "dist\FrameGuard\_internal" (
    echo ERROR: dist\FrameGuard\_internal was not produced.
    if not defined CI pause
    exit /b 1
)
echo       Built dist\FrameGuard\FrameGuard.exe

REM --- Step 7: Stage tools ---
echo.
echo [7/9] Staging analysis tools...
"%PY%" download_tools.py --platform windows --dest "dist\FrameGuard\tools"
if errorlevel 1 (
    echo.
    echo ERROR: Required analysis tools could not be staged. FFmpeg ships
    echo        inside the app; a build without it cannot scan video, and
    echo        the smoke test would only fail later with a vaguer message.
    if not defined CI pause
    exit /b 1
)

REM --- Step 8: Smoke test ---
REM Keep the direct-construction component diagnostic, but do not treat it as
REM proof that the production startup preamble or event loop works.
echo.
echo [8/9] Smoke testing the build...
"dist\FrameGuard\FrameGuard.exe" --selftest
if errorlevel 1 (
    echo.
    echo ERROR: The built exe failed its component self-test. Do not ship this build.
    echo        Reproduce with: dist\FrameGuard\FrameGuard.exe --selftest
    if not defined CI pause
    exit /b 1
)
echo       Component self-test passed.

REM Exercise frameguard.gui.main() with internal and external watchdogs and
REM require ordered atomic JSON evidence from the frozen windowed executable.
"%PY%" tools_dev\startup_smoke.py --executable "dist\FrameGuard\FrameGuard.exe" --artifacts-dir "Output\build-logs\startup-exe" --label "frozen-exe-startup" --expect-frozen --qpa windows --timeout 30
if errorlevel 1 (
    echo.
    echo ERROR: The built exe failed the real startup preflight. Do not ship this build.
    echo        Evidence: Output\build-logs\startup-exe
    if not defined CI pause
    exit /b 1
)
echo       Real startup preflight passed.

REM --- Step 9: Inno Setup ---
echo.
echo [9/9] Building the installer...

set "ISCC="
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not defined ISCC if exist "C:\Program Files\Inno Setup 6\ISCC.exe" set "ISCC=C:\Program Files\Inno Setup 6\ISCC.exe"
if not defined ISCC (
    where iscc >nul 2>&1 && set "ISCC=iscc"
)
if not defined ISCC (
    echo ERROR: Inno Setup 6 not found.
    echo        Download: https://jrsoftware.org/isdl.php
    echo.
    echo        The application itself built successfully. You can run it directly:
    echo          dist\FrameGuard\FrameGuard.exe
    if not defined CI pause
    exit /b 1
)

if not exist "Output" mkdir "Output"
if exist "Output\FrameGuardInstaller.exe" del /q "Output\FrameGuardInstaller.exe"
"%ISCC%" installer.iss
if errorlevel 1 (
    echo ERROR: Inno Setup compilation failed. See the output above.
    if not defined CI pause
    exit /b 1
)

if not exist "Output\FrameGuardInstaller.exe" (
    echo ERROR: Inno Setup reported success but Output\FrameGuardInstaller.exe
    echo        does not exist. Check OutputDir/OutputBaseFilename in installer.iss.
    if not defined CI pause
    exit /b 1
)

REM The installer is the shipped artifact, so install it silently into a fresh
REM temp root, verify its layout/association/uninstaller, run the installed
REM executable's real startup preflight, and uninstall before declaring success.
powershell -NoProfile -ExecutionPolicy Bypass -File "tools_dev\windows_installer_smoke.ps1" -Installer "Output\FrameGuardInstaller.exe" -ArtifactsDir "Output\build-logs\installed-smoke" -Python "%PY%"
if errorlevel 1 (
    echo ERROR: The final installer failed installed-artifact smoke. Do not ship this build.
    echo        Evidence: Output\build-logs\installed-smoke
    if not defined CI pause
    exit /b 1
)
echo       Final installed-artifact smoke passed.

REM --- Publish a differential-update release ---
REM Lets existing users update by downloading only the files that changed,
REM instead of re-running the full installer for a small fix.
REM Windows releases go to Output\release-windows, macOS to
REM Output\release-macos: the payloads overlap by filename, so sharing one
REM folder lets one platform's publish silently overwrite the other's.
if "!SKIP_DELTA!"=="0" (
    echo.
    echo       Preparing optional differential-update payload...
    "%PY%" publish_release.py --platform windows --compare-to "Output\release-windows"
    if errorlevel 1 (
        echo       WARNING: could not prepare the optional delta payload.
        echo                The installer is still valid.
    )
) else (
    echo       Optional differential payload skipped ^(--skip-delta^).
)

echo.
echo  ====================================================
echo   BUILD SUCCESSFUL
echo  ====================================================
echo.
echo   Installer: %CD%\Output\FrameGuardInstaller.exe
for %%F in ("Output\FrameGuardInstaller.exe") do echo   Size:      %%~zF bytes
echo.
echo   Before distributing:
echo     1. Install from Output\FrameGuardInstaller.exe
echo     2. Launch from the Start Menu shortcut
echo     3. Scan one known-good and one known-bad file
echo     4. Export a MULTI-FILE PDF report
echo     5. Drag a .fg preset onto the window
echo.
echo  ====================================================
echo.

call deactivate
if not defined CI pause
