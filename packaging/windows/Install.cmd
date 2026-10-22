@echo off
"%~dp0runtime\python.exe" -B -I -X utf8 "%~dp0support\install_user.py" --source "%~dp0." %*
exit /b %errorlevel%

