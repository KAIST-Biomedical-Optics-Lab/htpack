@echo off
"%~dp0runtime\python.exe" -B -I -X utf8 "%~dp0validation\validate_windows.py" %*
exit /b %errorlevel%

