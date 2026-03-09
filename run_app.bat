@echo off
setlocal
cd /d %~dp0
if not exist ".venv\Scripts\python.exe" (
  echo ERROR: Shared venv python not found at .venv\Scripts\python.exe
  exit /b 1
)
.venv\Scripts\python.exe app.py
endlocal
