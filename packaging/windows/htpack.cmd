@echo off
"%~dp0runtime\python.exe" -B -I -X utf8 -m htpack %*
exit /b %errorlevel%

