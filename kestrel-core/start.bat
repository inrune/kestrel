@echo off
REM Kestrel - one-command launcher (node + dashboard + explorer + wallet).
cd /d "%~dp0"
python -m pip install -r requirements.txt
python -m kestrel.cli start %*
