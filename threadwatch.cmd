@echo off
setlocal
py -3 "%~dp0scripts\threadwatch.py" %*
exit /b %errorlevel%
