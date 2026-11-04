@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Set up the .venv first. See README.md.
  exit /b 1
)
".venv\Scripts\python.exe" visual_demo.py %*
