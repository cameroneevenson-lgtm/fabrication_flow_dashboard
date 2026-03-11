@echo off
setlocal
cd /d %~dp0
if not exist "C:\Tools\.venv\Scripts\python.exe" (
  echo ERROR: Shared venv python not found at C:\Tools\.venv\Scripts\python.exe
  exit /b 1
)
"C:\Tools\.venv\Scripts\python.exe" watch_and_run.py
endlocal
