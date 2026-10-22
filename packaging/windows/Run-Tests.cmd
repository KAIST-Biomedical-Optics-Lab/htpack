@echo off
"%~dp0runtime\python.exe" -B -I -X utf8 -m pytest "%~dp0validation\tests" -q --junitxml="%~dp0validation\pytest-windows.xml" %*
exit /b %errorlevel%

