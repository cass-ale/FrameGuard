@echo off
REM ============================================================================
REM FrameGuard Desktop -- Diagnostic Launcher
REM 
REM If FrameGuard.exe doesn't open normally, run this script from the same
REM folder as FrameGuard.exe. It will show any Python errors in a console.
REM ============================================================================

cd /d "%~dp0"
echo.
echo  FrameGuard Desktop - Diagnostic Mode
echo  =====================================
echo.
echo  Install dir: %CD%
echo  Exe exists:  
if exist "FrameGuard.exe" (echo   YES) else (echo   NO - PROBLEM!)
echo  _internal:   
if exist "_internal" (echo   YES) else (echo   NO - PROBLEM!)
echo  python312:   
if exist "_internal\python312.dll" (echo   YES) else (echo   NO - PROBLEM!)
echo  PySide6:     
if exist "_internal\PySide6" (echo   YES) else (echo   NO - PROBLEM!)
echo  Qt plugins:  
if exist "_internal\PySide6\plugins\platforms\qwindows.dll" (echo   YES) else (
    if exist "_internal\plugins\platforms\qwindows.dll" (echo   YES alt path) else (echo   NO - PROBLEM!)
)
echo  assets:      
if exist "assets\frameguard.ico" (echo   YES) else (echo   NO - minor)
echo.
echo  Attempting launch with console output...
echo  (Any Python errors will appear below)
echo  ----------------------------------------
echo.

REM Set Qt plugin path explicitly (common fix for "Could not find platform plugin")
set QT_PLUGIN_PATH=%CD%\_internal\PySide6\plugins
set QT_QPA_PLATFORM_PLUGIN_PATH=%CD%\_internal\PySide6\plugins\platforms

REM Launch with console visible (shows Python tracebacks)
"FrameGuard.exe"

echo.
echo  ----------------------------------------
echo  Exit code: %ERRORLEVEL%
echo.
if %ERRORLEVEL% NEQ 0 (
    echo  ERROR: App crashed with exit code %ERRORLEVEL%
    echo.
    echo  Common fixes:
    echo    1. Install Visual C++ Redistributable 2015-2022 (x64)
    echo       https://aka.ms/vs/17/release/vc_redist.x64.exe
    echo    2. Rebuild with: build_installer.bat (UPX now disabled)
    echo    3. Check if antivirus is blocking the exe
)
echo.
pause
