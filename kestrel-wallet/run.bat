@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Kestrel Wallet

rem ===================================================================
rem  Kestrel Wallet launcher.
rem
rem  Finds Python, offers to install it if it is missing, installs the
rem  one dependency, and starts the app WITHOUT a console window, so
rem  nothing the app prints ever lands on top of whatever you had open.
rem  Anything worth reading goes to kestrel-log.txt next to this file.
rem ===================================================================

set "APP=Kestrel Wallet"
set "PY="
set "PYW="

rem ---- 1. find a Python that is 3.10 or newer -----------------------
rem  The py launcher is the reliable way to ask for a version; plain
rem  "python" on Windows is often the Microsoft Store stub, which opens
rem  the Store instead of running anything.
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PY=%%~C"
  )
)

if not defined PY goto NOPYTHON

rem ---- 2. the one dependency ----------------------------------------
%PY% -c "import ecdsa" >nul 2>&1
if !errorlevel! neq 0 (
  echo Installing the one dependency ^(ecdsa^)...
  %PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
  if !errorlevel! neq 0 (
    echo.
    echo Could not install dependencies. Check your internet connection.
    echo.
    pause
    exit /b 1
  )
)

rem ---- 3. tkinter, which some slim Python builds leave out ----------
%PY% -c "import tkinter" >nul 2>&1
if !errorlevel! neq 0 (
  echo.
  echo This Python has no tkinter, which %APP% needs for its window.
  echo Reinstall Python from python.org and leave "tcl/tk and IDLE"
  echo ticked on the Optional Features page.
  echo.
  pause
  exit /b 1
)

rem ---- 4. start it, windowed -----------------------------------------
rem  pythonw has no console, so nothing can print over your desktop.
for /f "delims=" %%P in ('%PY% -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%P"
if exist "!PYW!" (
  start "" "!PYW!" app.py
) else (
  rem no pythonw: run with a console, but keep it out of the way
  start /min "" %PY% app.py
)
exit /b 0


rem ===================================================================
:NOPYTHON
echo.
echo   Kestrel needs Python 3.10 or newer, and it is not installed.
echo.
echo   This will download the official installer from python.org
echo   and run it for you. It takes about two minutes.
echo.
set /p ANSWER=  Install Python now? [Y/n]
if /i "!ANSWER!"=="n" goto MANUAL

rem  Several versions are tried in turn: python.org keeps old releases
rem  indefinitely, but a single hard-coded URL is still one moved file away
rem  from leaving someone stuck, and any of these runs Kestrel fine.
set "ARCH=amd64"
if /i "%PROCESSOR_ARCHITECTURE%"=="ARM64" set "ARCH=arm64"

set "PYTMP="
for %%V in (3.12.7 3.12.4 3.11.9 3.10.11) do (
  if not defined PYTMP (
    set "PYFILE=python-%%V-%ARCH%.exe"
    set "PYURL=https://www.python.org/ftp/python/%%V/!PYFILE!"
    set "TRY=%TEMP%\!PYFILE!"
    echo   Downloading !PYFILE! ...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$ProgressPreference='SilentlyContinue';" ^
      "[Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12;" ^
      "try{Invoke-WebRequest -Uri '!PYURL!' -OutFile '!TRY!' -UseBasicParsing; exit 0}catch{exit 1}"
    if !errorlevel! equ 0 if exist "!TRY!" set "PYTMP=!TRY!"
  )
)

if not defined PYTMP goto DLFAIL

echo   Installing Python. Approve the prompt if Windows asks.
rem  Just for this user, no admin needed, and on PATH so we find it after.
"!PYTMP!" /passive InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1 Include_launcher=1
set "RC=!errorlevel!"
del "!PYTMP!" >nul 2>&1
if !RC! neq 0 (
  echo.
  echo   The installer exited with code !RC!.
  goto MANUAL
)

echo.
echo   Python installed. Starting %APP% ...
echo   (If it does not open, close this window and run this file again —
echo    Windows sometimes needs a moment to notice the new PATH.^)
timeout /t 3 >nul
start "" "%~f0"
exit /b 0

rem ===================================================================
:DLFAIL
echo.
echo   Could not download the installer. Check your internet connection,
echo   or install Python yourself from:
echo.
echo       https://www.python.org/downloads/
echo.
goto BYE

:MANUAL
echo.
echo   Install Python 3.10 or newer from:
echo.
echo       https://www.python.org/downloads/
echo.
echo   On the first screen of the installer, tick
echo   "Add python.exe to PATH" before pressing Install.
echo   Then run this file again.
echo.

:BYE
pause
exit /b 1
