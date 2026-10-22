@echo off
setlocal
rem Repository root = two levels above this file (packaging\windows\).
for %%I in ("%~dp0..\..") do set "ROOT=%%~fI"
set "PY=%ROOT%\.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo [htpack] .venv not found. Run "uv sync" in the repository root first. 1>&2
  exit /b 1
)
"%PY%" -B -I -X utf8 -m htpack %*
exit /b %errorlevel%
