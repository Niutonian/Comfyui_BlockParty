@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if %ERRORLEVEL%==0 (
  py -3 install.py %*
  exit /b %ERRORLEVEL%
)
python install.py %*
exit /b %ERRORLEVEL%
