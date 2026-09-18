@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"
title Kestrel Core

rem  Kestrel Core — the node, on the command line. Unlike the desktop
rem  apps this one WANTS a console: its output is the point. It still
rem  checks Python properly and offers to install it.

set "PY="
for %%C in ("py -3" "python" "python3") do (
  if not defined PY (
    %%~C -c "import sys; raise SystemExit(0 if sys.version_info>=(3,10) else 1)" >nul 2>&1
    if !errorlevel! equ 0 set "PY=%%~C"
  )
)
if not defined PY goto NOPYTHON

%PY% -c "import ecdsa" >nul 2>&1
if !errorlevel! neq 0 (
  echo Installing the one dependency ^(ecdsa^)...
  %PY% -m pip install --quiet --disable-pip-version-check -r requirements.txt
  if !errorlevel! neq 0 (
    echo Could not install dependencies. Check your internet connection.
    pause
    exit /b 1
  )
)

%PY% -m kestrel.cli start %*
exit /b %errorlevel%

:NOPYTHON
echo.
echo   Kestrel Core needs Python 3.10 or newer, and it is not installed.
echo.
set /p ANSWER=  Download and install it from python.org now? [Y/n] 
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

if not defined PYTMP goto MANUAL
"!PYTMP!" /passive InstallAllUsers=0 PrependPath=1 Include_tcltk=1 Include_pip=1 Include_launcher=1
del "!PYTMP!" >nul 2>&1
echo   Done. Run this file again.
pause
exit /b 0

:MANUAL
echo.
echo   Install Python 3.10+ from https://www.python.org/downloads/
echo   and tick "Add python.exe to PATH" on the first screen.
echo.
pause
exit /b 1
