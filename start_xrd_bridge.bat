@echo off
setlocal EnableExtensions
rem ------------------------------------------------------------------
rem  XRD SDL Bridge launcher - portable, no Python install needed.
rem  Works from any folder (local, mapped drive or \\server\share):
rem  everything is located relative to this .bat file.
rem ------------------------------------------------------------------
pushd "%~dp0" || (echo [ERROR] Cannot open folder "%~dp0" & pause & exit /b 1)
set "ROOT=%CD%"
title XRD Data Bridge
echo ============================================================
echo   Starting XRD Data Bridge on port 8765 ...
echo   Folder: %ROOT%
echo ============================================================

rem Ignore any system Python settings on this PC.
set "PYTHONHOME="
set "PYTHONPATH="

rem --- find bundled Python ---
set "PYEXE="
if exist "%ROOT%\python\python.exe" set "PYEXE=%ROOT%\python\python.exe"
if not defined PYEXE if exist "%ROOT%\..\python\python.exe" set "PYEXE=%ROOT%\..\python\python.exe"
if not defined PYEXE (
  echo [ERROR] Bundled Python not found. Expected: "%ROOT%\python\python.exe"
  goto missing
)

rem --- find the app entry script ---
set "APP="
if exist "%ROOT%\xrd_sdl_bridge\app.py" set "APP=%ROOT%\xrd_sdl_bridge\app.py"
if not defined APP if exist "%ROOT%\app.py" set "APP=%ROOT%\app.py"
if not defined APP if exist "%ROOT%\..\xrd_sdl_bridge\app.py" set "APP=%ROOT%\..\xrd_sdl_bridge\app.py"
if not defined APP (
  echo [ERROR] App not found. Expected: "%ROOT%\xrd_sdl_bridge\app.py"
  goto missing
)

rem Free port 8765 if an old bridge is still running (own processes only).
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8765 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>&1

"%PYEXE%" -X utf8 "%APP%" --open-browser
echo.
echo Bridge stopped.
popd
pause
exit /b 0

:missing
echo.
echo This folder must contain, side by side:
echo     start_xrd_bridge.bat
echo     python\            (portable Python)
echo     xrd_sdl_bridge\    (the app)
echo Copy the WHOLE folder, not just the .bat file.
echo.
echo Current contents of %ROOT%:
dir /b "%ROOT%"
popd
pause
exit /b 1
